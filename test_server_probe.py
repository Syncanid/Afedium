#!/usr/bin/env python3
"""
End-to-end probe for a running Afedium WebSocket server.

The probe uses the public WebSocket protocol only. It reads config/ws_server.json
for defaults, writes temporary files under .afedium_probe/, and removes them at
the end when possible.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import secrets
import socket
import sys
import time
import traceback
from pathlib import Path
from typing import Any


AUTH_CONTROL_CODE = 0xFF
FEATURE_RPC_CONTROL_CODE = 0x30
CHUNK_SIZE = 1000 * 1024


class ProbeSkip(Exception):
    pass


class ProbeFailure(Exception):
    pass


def load_config(root: Path) -> dict[str, Any]:
    path = root / "config" / "ws_server.json"
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def decode_b64(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"))


def type_name(value: Any) -> str:
    return type(value).__name__


class AfediumProbe:
    def __init__(self, args: argparse.Namespace, config: dict[str, Any]):
        self.args = args
        self.config = config
        self.ws = None
        self.counter = 0
        self.results: list[dict[str, Any]] = []
        self.features: dict[str, Any] = {}
        self.unsolicited: list[str] = []

        run_id = f"run_{int(time.time())}_{secrets.token_hex(3)}"
        self.base_dir = ".afedium_probe"
        self.run_dir = f"{self.base_dir}/{run_id}"
        self.small_file = f"{self.run_dir}/small.txt"
        self.renamed_file = f"{self.run_dir}/small-renamed.txt"
        self.chunk_file = f"{self.run_dir}/chunk-upload.bin"
        self.large_file = f"{self.run_dir}/large-transfer.bin"
        self.created_base_dir = False

    def uid(self) -> str:
        self.counter = (self.counter + 1) & 0xFFFF
        return f"{self.counter:04X}"

    def add_result(self, name: str, status: str, detail: str = "", data: Any = None):
        item = {
            "name": name,
            "status": status,
            "detail": detail,
        }
        if data is not None:
            item["data"] = data
        self.results.append(item)
        if not self.args.json:
            suffix = f" - {detail}" if detail else ""
            print(f"[{status}] {name}{suffix}")

    async def step(self, name: str, func):
        try:
            detail = await func()
            self.add_result(name, "PASS", str(detail or ""))
        except ProbeSkip as exc:
            self.add_result(name, "SKIP", str(exc))
        except Exception as exc:
            detail = str(exc) or exc.__class__.__name__
            if self.args.verbose:
                detail += "\n" + traceback.format_exc()
            self.add_result(name, "FAIL", detail)

    async def run(self):
        await self.step("udp_discovery", self.probe_discovery)
        await self.step("websocket_connect", self.connect)
        await self.step("auth", self.authenticate)
        await self.step("feature_list_0x01", self.probe_feature_list)
        await self.step("server_banner_0x02", self.probe_banner)
        await self.step("server_variables_read_0x04", self.probe_server_variables_read)
        if self.args.skip_write:
            self.add_result("server_variables_write_0x04", "SKIP", "--skip-write")
        else:
            await self.step("server_variables_write_0x04", self.probe_server_variables_write)
        await self.step("terminal_help_0x03", self.probe_terminal)
        await self.step("module_mgmt_local", self.probe_module_mgmt)
        if self.args.include_network:
            await self.step("module_mgmt_update_network", self.probe_module_update)
        else:
            self.add_result("module_mgmt_update_network", "SKIP", "use --include-network")

        if self.args.skip_write:
            self.add_result("file_setup", "SKIP", "--skip-write")
            self.add_result("file_transfer_small_0x10_0x12", "SKIP", "--skip-write")
            self.add_result("file_transfer_chunk_upload_0x10_0x11", "SKIP", "--skip-write")
            self.add_result("file_management_0x18_0x1C", "SKIP", "--skip-write")
        else:
            await self.step("file_setup", self.probe_file_setup)
            await self.step("file_transfer_small_0x10_0x12", self.probe_small_transfer)
            await self.step("file_transfer_chunk_upload_0x10_0x11", self.probe_chunk_upload)
            if self.args.skip_large_transfer:
                self.add_result("file_transfer_large_0x12_0x16", "SKIP", "--skip-large-transfer")
            else:
                await self.step("file_transfer_large_0x12_0x16", self.probe_large_download_flow)
            await self.step("file_management_0x18_0x1C", self.probe_file_management)
            await self.step("cleanup_probe_files", self.cleanup_probe_files)

        await self.step("feature_rpc_server_pages_0x30", self.probe_server_pages)
        await self.close()

        if self.args.json:
            print(json.dumps(self.results, ensure_ascii=False, indent=2))

        failures = [item for item in self.results if item["status"] == "FAIL"]
        return 1 if failures else 0

    async def close(self):
        if self.ws is not None:
            await self.ws.close()
            self.ws = None

    def require_ws(self):
        if self.ws is None:
            raise ProbeSkip("websocket is not connected")

    async def probe_discovery(self):
        if self.args.skip_discovery:
            raise ProbeSkip("--skip-discovery")

        expected_uuid = self.config.get("server_uuid")
        broadcast_port = int(self.args.broadcast_port or self.config.get("broadcast_port", 12840))
        deadline = time.monotonic() + self.args.discovery_timeout
        found = None

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", broadcast_port))
            sock.settimeout(0.25)
            while time.monotonic() < deadline:
                try:
                    data, addr = sock.recvfrom(64 * 1024)
                except socket.timeout:
                    continue
                try:
                    payload = json.loads(data.decode("utf-8"))
                except Exception:
                    continue
                if payload.get("service") != "afedium_server":
                    continue
                if expected_uuid and payload.get("UUID") != expected_uuid:
                    continue
                found = (payload, addr)
                break
        finally:
            sock.close()

        if not found:
            raise ProbeFailure(f"no afedium_server broadcast on UDP {broadcast_port}")

        payload, addr = found
        return f"{payload.get('server_name')} {payload.get('ip')}:{payload.get('port')} from {addr[0]}"

    async def connect(self):
        try:
            import websockets
        except ImportError as exc:
            raise ProbeFailure("missing dependency: websockets") from exc

        port = int(self.args.port or self.config.get("server_port", 11840))
        url = f"ws://{self.args.host}:{port}"
        self.ws = await websockets.connect(
            url,
            open_timeout=self.args.timeout,
            close_timeout=self.args.timeout,
            max_size=32 * 1024 * 1024,
        )
        return url

    async def authenticate(self):
        self.require_ws()
        auth_type = str(self.config.get("auth_type", "none")).lower()
        if auth_type == "none":
            return "server auth_type=none"

        challenge = await self.recv_text(self.args.timeout)
        if not challenge or ord(challenge[0]) != AUTH_CONTROL_CODE:
            raise ProbeFailure("first frame is not auth challenge")
        payload = json.loads(challenge[1:])
        if payload.get("stage") != "challenge":
            raise ProbeFailure(f"unexpected auth stage: {payload}")

        answer = self.args.auth_answer
        if answer is None and auth_type == "password":
            answer = self.args.password if self.args.password is not None else self.config.get("password")
        if answer is None:
            raise ProbeFailure(f"auth_type={auth_type} requires --auth-answer")

        await self.ws.send(chr(AUTH_CONTROL_CODE) + compact_json({"answer": str(answer)}))
        response = await self.recv_text(self.args.timeout)
        if not response or ord(response[0]) != AUTH_CONTROL_CODE:
            raise ProbeFailure("auth response is not an auth frame")
        result = json.loads(response[1:])
        if result.get("stage") != "ok":
            raise ProbeFailure(f"auth failed: {result}")
        return f"{auth_type} accepted"

    async def probe_feature_list(self):
        env = await self.control(0x01)
        data = self.expect_ok(env, dict)
        self.features = data
        required = [
            "server_banner",
            "server_variables",
            "file_transfer",
            "file_management",
            "terminal",
            "system_upgrade",
        ]
        missing = [name for name in required if name not in data]
        if missing:
            raise ProbeFailure(f"missing features: {missing}")
        return f"{len(data)} features: {', '.join(sorted(data.keys()))}"

    async def probe_banner(self):
        env = await self.control(0x02)
        data = self.expect_ok(env, dict)
        return f"{len(data)} banner fields"

    async def probe_server_variables_read(self):
        env = await self.control(0x04, "get static modules")
        data = self.expect_ok(env, dict)
        if isinstance(env.get("data"), str):
            raise ProbeFailure("0x04 data is still a JSON string; server was not unified")
        return f"static modules keys={sorted(data.keys())[:8]}"

    async def probe_server_variables_write(self):
        key = f"afedium_probe_{secrets.token_hex(4)}"
        expected = {"ok": True, "key": key}
        write_payload = f"set dynamic {key} {compact_json(expected)}"
        write_env = await self.control(0x04, write_payload)
        self.expect_ok(write_env)

        read_env = await self.control(0x04, f"get dynamic {key}")
        data = self.expect_ok(read_env, dict)
        if data != expected:
            raise ProbeFailure(f"dynamic roundtrip mismatch: {data!r}")
        return key

    async def probe_terminal(self):
        env = await self.control(0x03, "help")
        data = self.expect_ok(env, str)
        if "module" not in data and "core" not in data:
            raise ProbeFailure("help output does not mention expected command groups")
        return f"{len(data)} chars"

    async def probe_module_mgmt(self):
        env = await self.control(0x03, "module")
        data = self.expect_ok(env, str)
        for word in ("install", "update", "upgrade"):
            if word not in data:
                raise ProbeFailure(f"module help missing {word}")

        modules_env = await self.control(0x04, "get static modules")
        modules = self.expect_ok(modules_env, dict)
        return f"module help ok, installed={sorted(modules.keys())[:8]}"

    async def probe_module_update(self):
        env = await self.control(0x03, "module update", timeout=self.args.network_timeout)
        data = self.expect_ok(env)
        if not isinstance(data, dict):
            raise ProbeFailure(f"module update data is {type_name(data)}, expected dict")
        return f"{len(data)} online modules"

    async def probe_file_setup(self):
        base_env = await self.control(0x1B, self.base_dir)
        base_data = self.expect_ok(base_env, dict)
        self.created_base_dir = bool(base_data.get("created"))

        run_env = await self.control(0x1B, self.run_dir)
        run_data = self.expect_ok(run_env, dict)
        if not run_data.get("created") and not run_data.get("path"):
            raise ProbeFailure(f"unexpected mkdir response: {run_data}")

        list_env = await self.control(0x19, self.base_dir)
        listing = self.expect_ok(list_env, dict)
        run_name = self.run_dir.rsplit("/", 1)[-1]
        if run_name not in listing:
            raise ProbeFailure(f"{run_name} not listed under {self.base_dir}")
        return self.run_dir

    async def probe_small_transfer(self):
        content = f"Afedium probe small file {time.time()}\n".encode("utf-8")
        digest = sha256_bytes(content)
        payload = f"{self.small_file} {b64(content)} {digest} utf-8"
        upload_env = await self.control(0x10, payload)
        upload_data = self.expect_ok(upload_env, dict)
        if not upload_data.get("saved"):
            raise ProbeFailure(f"small upload not saved: {upload_data}")

        download_env = await self.control(0x12, self.small_file)
        download_data = self.expect_ok(download_env, dict)
        if download_data.get("mode") != "single":
            raise ProbeFailure(f"small download mode is {download_data.get('mode')}")
        downloaded = decode_b64(download_data["content_base64"])
        if downloaded != content:
            raise ProbeFailure("small download content mismatch")
        if download_data.get("sha256") != digest:
            raise ProbeFailure("small download sha256 mismatch")
        return f"{len(content)} bytes"

    async def probe_chunk_upload(self):
        content = b"chunk-A:" + secrets.token_bytes(128) + b":chunk-B"
        pieces = [content[:64], content[64:128], content[128:]]
        digest = sha256_bytes(content)

        open_env = await self.control(0x10, f"{self.chunk_file} {len(pieces)} {digest}")
        open_data = self.expect_ok(open_env, dict)
        session_id = open_data.get("session_id")
        if not session_id:
            raise ProbeFailure(f"chunk upload missing session_id: {open_data}")

        last_data = None
        for index, piece in enumerate(pieces):
            env = await self.control(0x11, f"{session_id} {index} {b64(piece)}")
            last_data = self.expect_ok(env, dict)

        if last_data.get("event") != "complete":
            raise ProbeFailure(f"chunk upload did not complete: {last_data}")
        if last_data.get("sha256") != digest:
            raise ProbeFailure("chunk upload sha256 mismatch")

        download_env = await self.control(0x12, self.chunk_file)
        download_data = self.expect_ok(download_env, dict)
        if decode_b64(download_data["content_base64"]) != content:
            raise ProbeFailure("chunk uploaded file download mismatch")
        return f"{len(content)} bytes in {len(pieces)} chunks"

    async def probe_large_download_flow(self):
        content = self.make_large_content()
        digest = sha256_bytes(content)
        pieces = [content[i:i + 250_000] for i in range(0, len(content), 250_000)]

        open_env = await self.control(0x10, f"{self.large_file} {len(pieces)} {digest}", timeout=20)
        session_id = self.expect_ok(open_env, dict).get("session_id")
        if not session_id:
            raise ProbeFailure("large upload missing session_id")
        for index, piece in enumerate(pieces):
            env = await self.control(0x11, f"{session_id} {index} {b64(piece)}", timeout=20)
            self.expect_ok(env, dict)

        retry_request = await self.control(0x12, self.large_file, timeout=20)
        retry_data = self.expect_ok(retry_request, dict)
        retry_session = retry_data.get("session_id")
        if retry_data.get("mode") != "chunked" or not retry_session:
            raise ProbeFailure(f"large download did not create chunked session: {retry_data}")

        await self.send_frame(0x15, compact_json([0]), uid="RTRY", prefix_payload=f"{retry_session} ")
        retry_ack = self.expect_ok(await self.recv_envelope(0x15, "RTRY", timeout=20), dict)
        if retry_ack.get("requested_indices") != [0]:
            raise ProbeFailure(f"retry ack mismatch: {retry_ack}")
        retry_chunk = self.expect_ok(await self.recv_envelope(0x14, "RTRY", timeout=20), dict)
        if retry_chunk.get("event") != "chunk" or retry_chunk.get("chunk_index") != 0:
            raise ProbeFailure(f"retry chunk mismatch: {retry_chunk}")
        retry_done = self.expect_ok(await self.recv_envelope(0x14, "RTRY", timeout=20), dict)
        if retry_done.get("event") != "complete":
            raise ProbeFailure(f"retry did not complete: {retry_done}")
        self.expect_ok(await self.control(0x16, retry_session), dict)

        request_env = await self.control(0x12, self.large_file, timeout=20)
        request_data = self.expect_ok(request_env, dict)
        session_id = request_data.get("session_id")
        chunk_count = int(request_data.get("chunk_count", 0))
        if request_data.get("mode") != "chunked" or chunk_count < 2:
            raise ProbeFailure(f"expected chunked download, got {request_data}")

        await self.send_frame(0x13, session_id, uid="DLST")
        start_data = self.expect_ok(await self.recv_envelope(0x13, "DLST", timeout=20), dict)
        if not start_data.get("started"):
            raise ProbeFailure(f"download start ack mismatch: {start_data}")

        chunks: dict[int, bytes] = {}
        while True:
            env = await self.recv_envelope(0x14, "DLST", timeout=30)
            data = self.expect_ok(env, dict)
            event = data.get("event")
            if event == "chunk":
                chunks[int(data["chunk_index"])] = decode_b64(data["content_base64"])
            elif event == "complete":
                break
            else:
                raise ProbeFailure(f"unexpected download event: {data}")

        if len(chunks) != chunk_count:
            raise ProbeFailure(f"received {len(chunks)} chunks, expected {chunk_count}")
        reconstructed = b"".join(chunks[index] for index in range(chunk_count))
        if sha256_bytes(reconstructed) != digest:
            raise ProbeFailure("large reconstructed sha256 mismatch")
        return f"{len(content)} bytes, {chunk_count} chunks"

    async def probe_file_management(self):
        stat_env = await self.control(0x1A, self.small_file)
        stat_data = self.expect_ok(stat_env, dict)
        if int(stat_data.get("size", -1)) <= 0:
            raise ProbeFailure(f"invalid stat response: {stat_data}")

        rename_env = await self.control(0x1C, f"{self.small_file} {self.renamed_file}")
        rename_data = self.expect_ok(rename_env, dict)
        if not rename_data.get("renamed"):
            raise ProbeFailure(f"rename failed: {rename_data}")

        list_env = await self.control(0x19, self.run_dir)
        listing = self.expect_ok(list_env, dict)
        if "small-renamed.txt" not in listing:
            raise ProbeFailure(f"renamed file missing from listing: {listing}")

        delete_env = await self.control(0x18, self.renamed_file)
        delete_data = self.expect_ok(delete_env, dict)
        if not delete_data.get("deleted"):
            raise ProbeFailure(f"delete failed: {delete_data}")
        return "stat, rename, list, delete"

    async def cleanup_probe_files(self):
        deleted = []
        for path in [self.small_file, self.renamed_file, self.chunk_file, self.large_file, self.run_dir]:
            try:
                env = await self.control(0x18, path)
                if env.get("ok"):
                    deleted.append(path)
            except Exception:
                pass
        if self.created_base_dir:
            try:
                env = await self.control(0x18, self.base_dir)
                if env.get("ok"):
                    deleted.append(self.base_dir)
            except Exception:
                pass
        return f"deleted {len(deleted)} paths"

    async def probe_server_pages(self):
        if "server_pages" not in self.features:
            raise ProbeSkip("server_pages feature not advertised")

        manifest_env = await self.feature_rpc("server_pages", "manifest", {})
        manifest = self.expect_ok(manifest_env, dict)
        pages = manifest.get("pages")
        if not isinstance(pages, list) or not pages:
            raise ProbeFailure(f"server_pages manifest has no pages: {manifest}")

        page_id = pages[0].get("page_id")
        page_env = await self.feature_rpc("server_pages", "get_page", {"page_id": page_id})
        page = self.expect_ok(page_env, dict)
        if page.get("page_id") != page_id:
            raise ProbeFailure(f"get_page mismatch: {page}")

        instance_env = await self.feature_rpc("server_pages", "open_instance", {"page_id": page_id})
        instance = self.expect_ok(instance_env, dict)
        instance_id = instance.get("instance_id")
        if not instance_id:
            raise ProbeFailure(f"open_instance missing instance_id: {instance}")

        invoke_env = await self.feature_rpc(
            "server_pages",
            "invoke",
            {"instance_id": instance_id, "action": "refresh_status", "payload": {}},
        )
        invoke_data = self.expect_ok(invoke_env, dict)
        if "state_patch" not in invoke_data:
            raise ProbeFailure(f"invoke missing state_patch: {invoke_data}")

        close_env = await self.feature_rpc("server_pages", "close_instance", {"instance_id": instance_id})
        close_data = self.expect_ok(close_env, dict)
        if close_data.get("closed") is not True:
            raise ProbeFailure(f"close_instance failed: {close_data}")

        asset_pages = [page for page in pages if page.get("assets")]
        if asset_pages:
            asset_id = asset_pages[0]["assets"][0]["asset_id"]
            asset_env = await self.feature_rpc("server_pages", "get_asset", {"asset_id": asset_id})
            asset = self.expect_ok(asset_env, dict)
            if asset.get("encoding") != "base64" or not asset.get("content"):
                raise ProbeFailure(f"asset response invalid: {asset}")

        return f"{len(pages)} pages"

    def make_large_content(self) -> bytes:
        pattern = b"AfediumLargeProbe0123456789"
        repeated = pattern * ((CHUNK_SIZE // len(pattern)) + 2)
        return repeated[:CHUNK_SIZE + 128]

    async def control(self, code: int, payload: str = "", *, timeout: float | None = None) -> dict[str, Any]:
        uid = await self.send_frame(code, payload)
        return await self.recv_envelope(code, uid, timeout=timeout or self.args.timeout)

    async def send_frame(
        self,
        code: int,
        payload: str,
        *,
        uid: str | None = None,
        prefix_payload: str = "",
    ) -> str:
        self.require_ws()
        actual_uid = uid or self.uid()
        await self.ws.send(chr(code) + actual_uid + prefix_payload + payload)
        return actual_uid

    async def feature_rpc(self, feature_id: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = compact_json({
            "feature_id": feature_id,
            "method": method,
            "payload": payload,
        })
        uid = await self.send_frame(FEATURE_RPC_CONTROL_CODE, body)
        return await self.recv_envelope(FEATURE_RPC_CONTROL_CODE, uid, timeout=self.args.timeout)

    async def recv_envelope(self, code: int, uid: str, *, timeout: float | None = None) -> dict[str, Any]:
        expected_prefix = chr(code) + uid
        deadline = time.monotonic() + (timeout or self.args.timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProbeFailure(f"timeout waiting for 0x{code:02X}/{uid}")
            text = await self.recv_text(remaining)
            if not text.startswith(expected_prefix):
                self.unsolicited.append(text)
                continue
            try:
                return json.loads(text[len(expected_prefix):])
            except json.JSONDecodeError as exc:
                raise ProbeFailure(f"invalid JSON envelope for 0x{code:02X}/{uid}: {exc}") from exc

    async def recv_text(self, timeout: float) -> str:
        self.require_ws()
        message = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
        if isinstance(message, bytes):
            return message.decode("utf-8")
        return str(message)

    def expect_ok(self, envelope: dict[str, Any], expected_type: type | tuple[type, ...] | None = None):
        if envelope.get("ok") is not True:
            raise ProbeFailure(f"request failed: {envelope}")
        data = envelope.get("data")
        if expected_type is not None and not isinstance(data, expected_type):
            raise ProbeFailure(f"data type is {type_name(data)}, expected {expected_type}")
        return data


def parse_args(config: dict[str, Any]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe a running Afedium WebSocket server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(config.get("server_port", 11840)))
    parser.add_argument("--broadcast-port", type=int, default=int(config.get("broadcast_port", 12840)))
    parser.add_argument("--password", default=None, help="Password auth answer. Defaults to config/ws_server.json password.")
    parser.add_argument("--auth-answer", default=None, help="Explicit auth answer, useful for captcha mode.")
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--network-timeout", type=float, default=20.0)
    parser.add_argument("--discovery-timeout", type=float, default=2.5)
    parser.add_argument("--include-network", action="store_true", help="Also run module update, which depends on the remote module index.")
    parser.add_argument("--skip-discovery", action="store_true")
    parser.add_argument("--skip-write", action="store_true", help="Skip dynamic writes and temporary file tests.")
    parser.add_argument("--skip-large-transfer", action="store_true", help="Skip >1MB chunked download/retry/cancel tests.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON results only.")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


async def async_main() -> int:
    root = Path(__file__).resolve().parent
    config = load_config(root)
    args = parse_args(config)
    probe = AfediumProbe(args, config)
    return await probe.run()


def main() -> int:
    if os.getcwd() != str(Path(__file__).resolve().parent):
        os.chdir(Path(__file__).resolve().parent)
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
