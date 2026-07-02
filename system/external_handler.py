import asyncio
import base64
import hashlib
import json
import os
import shutil
import traceback
import uuid

from core import get_info_handler, set_info_handler
from lib.Event import Event
from lib.command import command
from lib.common import loaded_plugins, static, plugin_lock
from lib.external_control import dispatch_external_message, register_control_code, unregister_owner
from lib.feature import FEATURE_RPC_CONTROL_CODE, call_feature
from lib.logger import log
from lib.plugin import AfediumPluginBase
from lib.support_lib import CustomJsonEncoder

CHUNK_SIZE = 1000 * 1024  # 1MB

FILE_UPLOAD_OPEN_CODE = 0x10
FILE_UPLOAD_CHUNK_CODE = 0x11
FILE_DOWNLOAD_REQUEST_CODE = 0x12
FILE_DOWNLOAD_START_CODE = 0x13
FILE_DOWNLOAD_CHUNK_CODE = 0x14
FILE_DOWNLOAD_RETRY_CODE = 0x15
FILE_DOWNLOAD_CANCEL_CODE = 0x16
FILE_MANAGEMENT_DELETE_CODE = 0x18
FILE_MANAGEMENT_LIST_CODE = 0x19
FILE_MANAGEMENT_STAT_CODE = 0x1A
FILE_MANAGEMENT_MKDIR_CODE = 0x1B
FILE_MANAGEMENT_RENAME_CODE = 0x1C

FILE_TRANSFER_PROTOCOL_CODES = [
    FILE_UPLOAD_OPEN_CODE,
    FILE_UPLOAD_CHUNK_CODE,
    FILE_DOWNLOAD_REQUEST_CODE,
    FILE_DOWNLOAD_START_CODE,
    FILE_DOWNLOAD_CHUNK_CODE,
    FILE_DOWNLOAD_RETRY_CODE,
    FILE_DOWNLOAD_CANCEL_CODE,
]

FILE_TRANSFER_INBOUND_CONTROL_CODES = [
    FILE_UPLOAD_OPEN_CODE,
    FILE_UPLOAD_CHUNK_CODE,
    FILE_DOWNLOAD_REQUEST_CODE,
    FILE_DOWNLOAD_START_CODE,
    FILE_DOWNLOAD_RETRY_CODE,
    FILE_DOWNLOAD_CANCEL_CODE,
]

FILE_MANAGEMENT_PROTOCOL_CODES = [
    FILE_MANAGEMENT_DELETE_CODE,
    FILE_MANAGEMENT_LIST_CODE,
    FILE_MANAGEMENT_STAT_CODE,
    FILE_MANAGEMENT_MKDIR_CODE,
    FILE_MANAGEMENT_RENAME_CODE,
]

Info = {
    "name": "外部指令处理器",
    "id": "external_handler",
    "dependencies": [],
    "pip_dependencies": [],
    "linux_dependencies": []
}


# --- 辅助函数保持原样 ---
def try_decode_bytes(data: bytes, encodings=('utf-8', 'gbk')) -> tuple[str, str]:
    for enc in encodings:
        try:
            return enc, data.decode(enc)
        except Exception:
            continue
    return 'gbk', data.decode('gbk', errors='ignore')


def calculate_sha256(data: bytes) -> str:
    if not isinstance(data, bytes):
        raise TypeError("calculate_sha256 的输入必须是字节类型。")
    return hashlib.sha256(data).hexdigest()


def calculate_file_sha256(file_path: str) -> str:
    sha256_hash = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()
    except FileNotFoundError:
        return ""


def b64_encode_bytes(b: bytes) -> str:
    return base64.b64encode(b).decode()


def b64_decode(txt, file):
    data = base64.b64decode(txt)
    with open(file, 'wb') as output:
        output.write(data)
    return f"文件 {file} 写入成功\n"


def parse_json_data(value, *, structured_only=False):
    if not isinstance(value, str):
        return value

    text = value.strip()
    if not text:
        return value

    if structured_only and not (
        (text.startswith("{") and text.endswith("}")) or
        (text.startswith("[") and text.endswith("]"))
    ):
        return value

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


class AFEDIUMPlugin(AfediumPluginBase):
    default_config = {}

    def setup(self):
        self.upload_sessions = {}
        self.download_sessions = {}
        static["event_handler"].register_event("ExternalIO_IN", self.process_input_event)
        self._register_builtin_control_codes()

        # 注册服务端实际实现的功能声明；客户端通过 0x01 主动读取 features。
        if "features" not in static:
            static["features"] = {}
        static["features"].update({
            "server_banner": {
                "title": "服务端 Banner",
                "standard": "core",
                "protocol_codes": ["0x02"],
            },
            "server_variables": {
                "title": "服务端变量访问",
                "standard": "core",
                "protocol_codes": ["0x04"],
            },
            "file_transfer": {
                "title": "文件传输",
                "standard": "core",
                "protocol_codes": [f"0x{code:02X}" for code in FILE_TRANSFER_PROTOCOL_CODES],
            },
            "file_management": {
                "title": "文件管理",
                "standard": "core",
                "protocol_codes": [f"0x{code:02X}" for code in FILE_MANAGEMENT_PROTOCOL_CODES],
            },
            "terminal": {
                "title": "终端",
                "standard": "core",
                "protocol_codes": ["0x03"],
            },
            "system_upgrade": {
                "title": "系统升级",
                "standard": "core",
                "protocol_codes": ["0x03"],
            },
        })
        return True

    def main_loop(self):
        static["running"][self.id] = True
        self.stop_event.wait()

    def teardown(self):
        static["event_handler"].unregister_event("ExternalIO_IN", self.process_input_event)
        unregister_owner(self.id)
        self.upload_sessions.clear()
        self.download_sessions.clear()
        log.info(f"[{self.id}] 外部输入处理资源已释放")

    def _register_builtin_control_codes(self):
        builtin_codes = [
            0x01, 0x02, 0x03, 0x04,
            *FILE_TRANSFER_INBOUND_CONTROL_CODES,
            *FILE_MANAGEMENT_PROTOCOL_CODES,
        ]
        for code in builtin_codes:
            register_control_code(
                code,
                self.id,
                self._handle_registered_control_code,
                description="Afedium 内置外部控制协议",
            )
        register_control_code(
            FEATURE_RPC_CONTROL_CODE,
            self.id,
            self._handle_feature_rpc_control_code,
            description="Afedium Feature RPC 协议",
        )

    async def _handle_registered_control_code(self, ctx):
        return await self._handle_command_and_return(ctx.raw_message, ctx.client_id)

    async def _handle_feature_rpc_control_code(self, ctx):
        payload = ctx.payload.decode("utf-8") if isinstance(ctx.payload, bytes) else str(ctx.payload)
        uid = payload[:4] if len(payload) >= 4 else ""
        body = payload[4:] if len(payload) >= 4 else payload

        try:
            envelope = json.loads(body or "{}")
            feature_id = envelope.get("feature_id")
            method = envelope.get("method")
            request_payload = envelope.get("payload", {})
            if not feature_id:
                raise ValueError("feature_id 不能为空")
            if not method:
                raise ValueError("method 不能为空")
            response = call_feature(feature_id, method, request_payload, ctx.client_id)
        except json.JSONDecodeError as exc:
            response = {
                "ok": False,
                "data": None,
                "message": str(exc),
                "error": {"code": "invalid_json", "message": str(exc)},
            }
        except Exception as exc:
            log.error(f"[Feature RPC] 请求处理失败: {exc}")
            response = {
                "ok": False,
                "data": None,
                "message": str(exc),
                "error": {"code": "bad_request", "message": str(exc), "details": None},
            }

        return chr(FEATURE_RPC_CONTROL_CODE) + uid + json.dumps(
            response,
            ensure_ascii=False,
            cls=CustomJsonEncoder,
        )

    def get_running_safe(self):
        running = {}
        with plugin_lock:
            for module_id in static.get("running", {}).keys():
                try:
                    plugin_data = loaded_plugins.get(module_id)
                    if not plugin_data:
                        running[module_id] = static["running"][module_id]
                        continue

                    module_name = module_id
                    if hasattr(plugin_data, 'Info'):
                        module_name = plugin_data.Info.get("name", module_id)
                    elif isinstance(plugin_data, dict) and 'info' in plugin_data:
                        module_name = plugin_data['info'].get("name", module_id)

                    running[module_name] = static["running"][module_id]

                except KeyError:
                    running[module_id] = static["running"].get(module_id, "unknown")
        return running

    def process_input_event(self, event: Event):
        loop = static.get("asyncio_loop")
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(self.async_worker(event), loop)
        else:
            log.error("[External Handler] 未找到正在运行的 asyncio 事件循环！")

    async def async_worker(self, event: Event):
        command_message = event.data.get("message")
        client_id = event.data.get("client_id")

        if not command_message:
            return

        await dispatch_external_message(command_message, client_id)

    async def _handle_command_and_return(self, command_message, client_id):
        normalized_message = command_message
        try:
            normalized_message = self._normalize_command_message(command_message)
            if not isinstance(normalized_message, str) or not normalized_message:
                return None

            ctrl = ord(normalized_message[0])
            message = normalized_message[1:]
            uid, _payload = self._split_uid_payload(message)
            handler = self._control_handlers().get(ctrl)
            if not handler:
                return self._control_error(ctrl, "unknown_control_code", f"未知控制码: 0x{ctrl:02X}", uid)

            result = handler(message, client_id)
            if asyncio.iscoroutine(result):
                return await result
            return result

        except Exception as e:
            log.error(f"处理外部输入时异常：{e}")
            log.debug(traceback.format_exc())
            if isinstance(normalized_message, str) and normalized_message:
                ctrl = ord(normalized_message[0])
                uid = normalized_message[1:5]
                return self._control_error(ctrl, "handler_exception", f"指令执行异常: {e}", uid)
            return None

    def _normalize_command_message(self, command_message):
        if isinstance(command_message, bytes):
            try:
                return command_message.decode("utf-8")
            except UnicodeDecodeError:
                return command_message.decode("gbk", errors="ignore")
        return command_message

    def _control_handlers(self):
        return {
            0x01: self._handle_feature_list,
            0x02: self._handle_info_snapshot,
            0x03: self._handle_terminal_command,
            0x04: self._handle_server_variables,
            FILE_UPLOAD_OPEN_CODE: self._handle_file_upload_open,
            FILE_UPLOAD_CHUNK_CODE: self._handle_file_upload_chunk,
            FILE_DOWNLOAD_REQUEST_CODE: self._handle_file_download_request,
            FILE_DOWNLOAD_START_CODE: self._handle_file_download_start,
            FILE_DOWNLOAD_RETRY_CODE: self._handle_file_download_retry,
            FILE_DOWNLOAD_CANCEL_CODE: self._handle_file_download_cancel,
            FILE_MANAGEMENT_DELETE_CODE: self._handle_file_delete,
            FILE_MANAGEMENT_LIST_CODE: self._handle_file_list,
            FILE_MANAGEMENT_STAT_CODE: self._handle_file_stat,
            FILE_MANAGEMENT_MKDIR_CODE: self._handle_file_mkdir,
            FILE_MANAGEMENT_RENAME_CODE: self._handle_file_rename,
        }

    def _split_uid_payload(self, message: str):
        return message[0:4], message[4:]

    def _control_response(self, code: int, data=None, uid: str = "", message: str = ""):
        payload = {
            "ok": True,
            "data": data,
            "message": message,
            "error": None,
        }
        return chr(code) + uid + json.dumps(payload, ensure_ascii=False, cls=CustomJsonEncoder)

    def _control_error(self, code: int, error_code: str, message: str, uid: str = "", details=None):
        payload = {
            "ok": False,
            "data": None,
            "message": message,
            "error": {
                "code": error_code,
                "message": message,
                "details": details,
            },
        }
        return chr(code) + uid + json.dumps(payload, ensure_ascii=False, cls=CustomJsonEncoder)

    async def _handle_terminal_command(self, message, client_id):
        uid, command_to_run = self._split_uid_payload(message)
        cmd_parts = command_to_run.strip().split()
        if not cmd_parts:
            return self._control_error(0x03, "empty_command", "错误: 空指令", uid)

        loop = asyncio.get_running_loop()
        full_output = await loop.run_in_executor(None, command, cmd_parts, client_id)
        return self._control_response(0x03, parse_json_data(full_output, structured_only=True), uid)

    def _handle_server_variables(self, message, _client_id):
        uid, payload = self._split_uid_payload(message)
        message_parts = payload.split(' ', 1)
        if not message_parts or not message_parts[0]:
            return self._control_error(0x04, "bad_request", "参数不足", uid)
        else:
            sub_command = message_parts[0]
            sub_args = message_parts[1].split(' ') if len(message_parts) > 1 else []
            if sub_command == "get":
                response_content = parse_json_data(get_info_handler(sub_args))
            elif sub_command == "set":
                response_content = set_info_handler(sub_args)
            else:
                return self._control_error(0x04, "unknown_info_command", "unknown", uid)
        return self._control_response(0x04, response_content, uid)

    def _handle_info_snapshot(self, message, _client_id):
        uid, _payload = self._split_uid_payload(message)
        data = {
            "/T/V系统": static.get("SYS_INFO", "Unknown"),
            "/APython版本: ": static.get("PY_VER", "Unknown"),
            "/P在线模式": static.get("online", False),
            "/P/AGit可用": static.get("git_available", False),
            "/L/A访问项目": "https://github.com/furryaxw/AFEDIUM/",
            "/C/Vmodules": self.get_running_safe(),
        }
        return self._control_response(0x02, data, uid)

    def _handle_feature_list(self, message, _client_id):
        uid, _payload = self._split_uid_payload(message)
        return self._control_response(0x01, static.get("features", {}), uid)

    def _handle_file_upload_open(self, message, client_id):
        uid, payload = self._split_uid_payload(message)
        parts = payload.split(' ', 3)
        if len(parts) == 4:
            file_path, base64_data, expected_checksum, _encoding = parts
            try:
                data_bytes = base64.b64decode(base64_data)
                calculated_checksum = calculate_sha256(data_bytes)
                if calculated_checksum != expected_checksum:
                    return self._control_error(
                        FILE_UPLOAD_OPEN_CODE,
                        "checksum_mismatch",
                        f"校验和不匹配: {file_path}",
                        uid,
                        {"path": file_path, "expected": expected_checksum, "actual": calculated_checksum},
                    )

                with open(file_path, 'wb') as f:
                    f.write(data_bytes)
                return self._control_response(FILE_UPLOAD_OPEN_CODE, {"path": file_path, "saved": True}, uid, "保存成功")
            except Exception as e:
                return self._control_error(FILE_UPLOAD_OPEN_CODE, "upload_save_failed", f"保存失败: {e}", uid)

        if len(parts) == 3 and parts[1].isdigit():
            file_path, chunk_count_str, expected_sha256 = parts
            try:
                chunk_count = int(chunk_count_str)
            except ValueError:
                return self._control_error(FILE_UPLOAD_OPEN_CODE, "bad_request", "参数错误: chunk_count 无效", uid)

            session_id = str(uuid.uuid4())
            self.upload_sessions[session_id] = {
                "file_path": file_path,
                "chunks": chunk_count,
                "received_chunk_indices": set(),
                "received_chunks_data": {},
                "expected_sha256": expected_sha256,
                "client_id": client_id,
                "uid": uid,
            }
            return self._control_response(
                FILE_UPLOAD_OPEN_CODE,
                {"session_id": session_id, "path": file_path, "chunk_count": chunk_count},
                uid,
                "上传会话已创建",
            )

        if len(parts) == 2:
            file_path = parts[0]
            base64_data = parts[1]
            try:
                data = base64.b64decode(base64_data)
                with open(file_path, 'wb') as f:
                    f.write(data)
                return self._control_response(FILE_UPLOAD_OPEN_CODE, {"path": file_path, "saved": True}, uid, "保存成功")
            except Exception as e:
                return self._control_error(FILE_UPLOAD_OPEN_CODE, "upload_save_failed", f"保存失败: {e}", uid)

        return self._control_error(FILE_UPLOAD_OPEN_CODE, "bad_request", "参数错误", uid)

    def _handle_file_upload_chunk(self, message, client_id):
        uid, payload = self._split_uid_payload(message)
        parts = payload.split(' ', 2)
        if len(parts) != 3:
            return self._control_error(FILE_UPLOAD_CHUNK_CODE, "bad_request", "参数错误", uid)

        session_id, chunk_index_str, base64_chunk = parts
        session = self.upload_sessions.get(session_id)
        if not session:
            return self._control_error(FILE_UPLOAD_CHUNK_CODE, "invalid_session", "无效上传会话", uid)

        try:
            chunk_index = int(chunk_index_str)
        except ValueError:
            return self._control_error(FILE_UPLOAD_CHUNK_CODE, "bad_request", "参数错误: chunk_index 无效", uid)

        session["received_chunk_indices"].add(chunk_index)
        session["received_chunks_data"][chunk_index] = base64.b64decode(base64_chunk)

        if len(session["received_chunk_indices"]) != session["chunks"]:
            return self._control_response(
                FILE_UPLOAD_CHUNK_CODE,
                {
                    "event": "chunk_received",
                    "session_id": session_id,
                    "chunk_index": chunk_index,
                    "received": len(session["received_chunk_indices"]),
                    "total": session["chunks"],
                },
                uid,
                "分块已接收",
            )

        full_data_bytes = b''
        for i in range(session["chunks"]):
            if i not in session["received_chunks_data"]:
                log.warning(f"组装文件时会话 {session_id} 缺少分块 {i}。")
                return self._control_error(
                    FILE_UPLOAD_CHUNK_CODE,
                    "missing_chunk",
                    "文件组装失败: 缺失分块",
                    uid,
                    {"session_id": session_id, "missing_index": i},
                )
            full_data_bytes += session["received_chunks_data"][i]

        file_path = session["file_path"]
        with open(file_path, 'wb') as f:
            f.write(full_data_bytes)

        calculated_sha256 = calculate_sha256(full_data_bytes)
        expected_sha256 = session["expected_sha256"]
        del self.upload_sessions[session_id]

        response = {
            "event": "complete",
            "session_id": session_id,
            "sha256": calculated_sha256,
            "path": file_path,
        }
        if calculated_sha256 == expected_sha256:
            return self._control_response(FILE_UPLOAD_CHUNK_CODE, response, uid, "上传完成")

        response["event"] = "checksum_mismatch"
        response["expected_sha256"] = expected_sha256
        return self._control_error(
            FILE_UPLOAD_CHUNK_CODE,
            "checksum_mismatch",
            "文件校验失败",
            uid,
            response,
        )

    def _handle_file_download_request(self, message, client_id):
        uid, payload = self._split_uid_payload(message)
        file_path = payload.strip()
        if not os.path.exists(file_path):
            return self._control_error(FILE_DOWNLOAD_REQUEST_CODE, "file_not_found", f"文件不存在: {file_path}", uid)

        file_size = os.path.getsize(file_path)
        if file_size > CHUNK_SIZE:
            file_sha256 = calculate_file_sha256(file_path)
            chunk_count = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE
            session_id = str(uuid.uuid4())
            self.download_sessions[session_id] = {
                "file": file_path,
                "chunks": chunk_count,
                "file_sha256": file_sha256,
                "client_id": client_id,
                "uid": uid,
            }
            return self._control_response(
                FILE_DOWNLOAD_REQUEST_CODE,
                {
                    "mode": "chunked",
                    "session_id": session_id,
                    "chunk_count": chunk_count,
                    "sha256": file_sha256,
                    "size": file_size,
                    "path": file_path,
                },
                uid,
                "下载会话已创建",
            )

        try:
            with open(file_path, 'rb') as f:
                content = f.read()
            encoding_type = 'binary'
            if file_path.lower().endswith(('.txt', '.md', '.json', '.csv', '.log', '.xml')):
                try:
                    encoding, decoded_text = try_decode_bytes(content)
                    encoding_type = encoding
                    encoded_b64 = base64.b64encode(decoded_text.encode(encoding)).decode()
                except Exception:
                    encoded_b64 = base64.b64encode(content).decode()
            else:
                encoded_b64 = base64.b64encode(content).decode()

            file_sha256 = calculate_sha256(content)
            return self._control_response(
                FILE_DOWNLOAD_REQUEST_CODE,
                {
                    "mode": "single",
                    "encoding": encoding_type,
                    "content_base64": encoded_b64,
                    "sha256": file_sha256,
                    "size": file_size,
                    "path": file_path,
                },
                uid,
                "文件读取成功",
            )
        except Exception as e:
            return self._control_error(FILE_DOWNLOAD_REQUEST_CODE, "download_read_failed", f"读取文件错误: {e}", uid)

    def _handle_file_download_start(self, message, client_id):
        uid, payload = self._split_uid_payload(message)
        session_id = payload.strip()
        download_session = self.download_sessions.get(session_id)
        if not download_session:
            return self._control_error(FILE_DOWNLOAD_START_CODE, "invalid_session", "无效的会话ID", uid)

        file_path = download_session["file"]
        chunk_count = download_session["chunks"]
        log.info(f"客户端已确认下载会话 {session_id}。开始分块传输。")
        self._emit_external_response(
            FILE_DOWNLOAD_START_CODE,
            {"session_id": session_id, "started": True},
            client_id,
            uid,
            "下载分块发送已开始",
        )
        self._send_file_chunks(session_id, file_path, chunk_count, client_id, uid)
        return None

    def _handle_file_download_retry(self, message, client_id):
        uid, payload = self._split_uid_payload(message)
        parts = payload.split(' ', 1)
        if len(parts) != 2:
            return self._control_error(FILE_DOWNLOAD_RETRY_CODE, "bad_request", "参数错误", uid)

        session_id, missing_indices_str = parts
        try:
            missing_indices = json.loads(missing_indices_str)
        except json.JSONDecodeError:
            return self._control_error(FILE_DOWNLOAD_RETRY_CODE, "bad_request", "参数错误: missing_indices 无效JSON", uid)

        download_session = self.download_sessions.get(session_id)
        if download_session:
            file_path = download_session["file"]
            self._emit_external_response(
                FILE_DOWNLOAD_RETRY_CODE,
                {"session_id": session_id, "requested_indices": missing_indices},
                client_id,
                uid,
                "重传请求已接受",
            )
            for chunk_index in missing_indices:
                if session_id not in self.download_sessions:
                    log.info(f"下载会话 {session_id} 在重发过程中被取消。")
                    break
                with open(file_path, 'rb') as f:
                    f.seek(chunk_index * CHUNK_SIZE)
                    chunk = f.read(CHUNK_SIZE)
                    if chunk:
                        self._emit_download_chunk(session_id, chunk_index, chunk, client_id, uid)
            if session_id in self.download_sessions:
                self._emit_external_response(FILE_DOWNLOAD_CHUNK_CODE, {"event": "complete", "session_id": session_id}, client_id, uid)
            return None

        return self._control_error(FILE_DOWNLOAD_RETRY_CODE, "invalid_session", "参数错误", uid)

    def _handle_file_download_cancel(self, message, client_id):
        uid, payload = self._split_uid_payload(message)
        session_id = payload.strip()
        cancelled = False
        if session_id in self.download_sessions:
            log.info(f"客户端正在取消下载会话: {session_id}")
            del self.download_sessions[session_id]
            cancelled = True
        return self._control_response(
            FILE_DOWNLOAD_CANCEL_CODE,
            {"session_id": session_id, "cancelled": cancelled},
            uid,
            "取消请求已处理",
        )

    def _handle_file_delete(self, message, _client_id):
        uid, payload = self._split_uid_payload(message)
        target = payload.strip()
        if os.path.isdir(target):
            shutil.rmtree(target)
            return self._control_response(FILE_MANAGEMENT_DELETE_CODE, {"path": target, "deleted": True, "kind": "directory"}, uid, "目录已删除")
        if os.path.isfile(target):
            os.remove(target)
            return self._control_response(FILE_MANAGEMENT_DELETE_CODE, {"path": target, "deleted": True, "kind": "file"}, uid, "文件已删除")
        return self._control_error(FILE_MANAGEMENT_DELETE_CODE, "path_not_found", f"路径不存在: {target}", uid)

    def _handle_file_list(self, message, _client_id):
        uid, payload = self._split_uid_payload(message)
        path_ls = payload.strip() or './'
        if not os.path.isdir(path_ls):
            return self._control_error(FILE_MANAGEMENT_LIST_CODE, "not_directory", f"{path_ls} 不是一个有效目录", uid)

        ans = {}
        for file in os.listdir(path_ls):
            full_path = os.path.join(path_ls, file)
            permission = 'd' if os.path.isdir(full_path) else 'f'
            permission += 'r' if os.access(full_path, os.R_OK) else '-'
            permission += 'w' if os.access(full_path, os.W_OK) else '-'
            permission += 'x' if os.access(full_path, os.X_OK) else '-'
            ans[file] = permission
        return self._control_response(FILE_MANAGEMENT_LIST_CODE, ans, uid)

    def _handle_file_stat(self, message, _client_id):
        uid, payload = self._split_uid_payload(message)
        try:
            stat_result = os.stat(payload)
            stat_dict = {
                "size": stat_result.st_size,
                "mtime": stat_result.st_mtime,
                "ctime": stat_result.st_ctime,
                "mode": stat_result.st_mode,
                "uid": stat_result.st_uid,
                "gid": stat_result.st_gid,
            }
            return self._control_response(FILE_MANAGEMENT_STAT_CODE, stat_dict, uid)
        except Exception as e:
            return self._control_error(FILE_MANAGEMENT_STAT_CODE, "stat_failed", str(e), uid)

    def _handle_file_mkdir(self, message, _client_id):
        uid, payload = self._split_uid_payload(message)
        folder_path = payload.strip()
        try:
            if not os.path.exists(folder_path):
                os.makedirs(folder_path)
                return self._control_response(FILE_MANAGEMENT_MKDIR_CODE, {"path": folder_path, "created": True}, uid, "目录创建成功")
            return self._control_response(FILE_MANAGEMENT_MKDIR_CODE, {"path": folder_path, "created": False}, uid, "目录已存在")
        except Exception as e:
            return self._control_error(FILE_MANAGEMENT_MKDIR_CODE, "mkdir_failed", f"目录创建失败: {e}", uid)

    def _handle_file_rename(self, message, _client_id):
        uid, payload = self._split_uid_payload(message)
        try:
            old_path, new_path = payload.split(' ', 1)
            if not os.path.exists(old_path):
                return self._control_error(FILE_MANAGEMENT_RENAME_CODE, "path_not_found", f"源路径不存在: {old_path}", uid)
            if os.path.exists(new_path):
                return self._control_error(FILE_MANAGEMENT_RENAME_CODE, "path_exists", f"目标路径已存在: {new_path}", uid)
            os.rename(old_path, new_path)
            return self._control_response(FILE_MANAGEMENT_RENAME_CODE, {"old_path": old_path, "new_path": new_path, "renamed": True}, uid, "重命名成功")
        except Exception as e:
            return self._control_error(FILE_MANAGEMENT_RENAME_CODE, "rename_failed", f"重命名失败: {e}", uid)

    def _emit_external_response(self, code: int, payload, client_id, uid: str = "", message: str = ""):
        static["event_handler"].trigger_event(
            Event(
                "ExternalIO_OUT",
                response_data=self._control_response(code, payload, uid, message),
                client_id=client_id,
            )
        )

    def _emit_external_error(self, code: int, error_code: str, message: str, client_id, uid: str = "", details=None):
        static["event_handler"].trigger_event(
            Event(
                "ExternalIO_OUT",
                response_data=self._control_error(code, error_code, message, uid, details),
                client_id=client_id,
            )
        )

    def _emit_download_chunk(self, session_id: str, chunk_index: int, chunk: bytes, client_id, uid: str = ""):
        encoded_chunk = base64.b64encode(chunk).decode()
        self._emit_external_response(
            FILE_DOWNLOAD_CHUNK_CODE,
            {"event": "chunk", "session_id": session_id, "chunk_index": chunk_index, "content_base64": encoded_chunk},
            client_id,
            uid,
        )

    def _send_file_chunks(self, session_id: str, file_path: str, chunk_count: int, client_id: str, uid: str = ""):
        try:
            with open(file_path, 'rb') as f:
                for chunk_index in range(chunk_count):
                    if session_id not in self.download_sessions:
                        log.info(f"下载会话 {session_id} 在发送分块时被取消。")
                        break
                    f.seek(chunk_index * CHUNK_SIZE)
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    self._emit_download_chunk(session_id, chunk_index, chunk, client_id, uid)
            if session_id in self.download_sessions:
                self._emit_external_response(FILE_DOWNLOAD_CHUNK_CODE, {"event": "complete", "session_id": session_id}, client_id, uid)
                del self.download_sessions[session_id]
        except Exception as e:
            error_message = f"下载分块发送异常：{e}"
            log.error(error_message)
            if session_id in self.download_sessions:
                del self.download_sessions[session_id]
            self._emit_external_error(
                FILE_DOWNLOAD_CHUNK_CODE,
                "download_chunk_failed",
                error_message,
                client_id,
                uid,
                {"session_id": session_id},
            )
