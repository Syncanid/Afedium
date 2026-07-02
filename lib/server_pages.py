import base64
import copy
import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from lib.Event import Event
from lib.common import static
from lib.logger import log


FEATURE_ID = "server_pages"
REALTIME_CONTROL_CODE = 0x31
REALTIME_PUSH_UID = "PUSH"

AssetLoader = Callable[[str], Optional[Dict[str, Any]]]
InvokeHandler = Callable[[str, Dict[str, Any], Dict[str, Any]], Any]
ClientEventHandler = Callable[[str, Dict[str, Any], Dict[str, Any]], Any]
OpenHandler = Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]
CloseHandler = Callable[[Dict[str, Any]], None]


@dataclass
class ServerPageProvider:
    provider_id: str
    provider_version: str
    pages: Dict[str, Dict[str, Any]]
    asset_loader: Optional[AssetLoader] = None
    invoke_handler: Optional[InvokeHandler] = None
    client_event_handler: Optional[ClientEventHandler] = None
    open_handler: Optional[OpenHandler] = None
    close_handler: Optional[CloseHandler] = None


class ServerPagesRegistry:
    def __init__(self):
        self._lock = threading.RLock()
        self._providers: Dict[str, ServerPageProvider] = {}
        self._instances: Dict[str, Dict[str, Any]] = {}

    def register_provider(
        self,
        provider_id: str,
        pages: list,
        *,
        provider_version: str = "0.1.0",
        asset_loader: Optional[AssetLoader] = None,
        invoke_handler: Optional[InvokeHandler] = None,
        client_event_handler: Optional[ClientEventHandler] = None,
        open_handler: Optional[OpenHandler] = None,
        close_handler: Optional[CloseHandler] = None,
    ):
        if not provider_id:
            raise ValueError("provider_id 不能为空")
        if not isinstance(pages, list):
            raise ValueError("pages 必须是 list")

        normalized_pages = {}
        for raw_page in pages:
            if not isinstance(raw_page, dict):
                raise ValueError("page 必须是 dict")
            page = copy.deepcopy(raw_page)
            page_id = page.get("page_id")
            if not page_id:
                raise ValueError("page 缺少 page_id")
            page_id = str(page_id)
            if "." not in page_id:
                page_id = f"{provider_id}.{page_id}"
            if not page_id.startswith(f"{provider_id}."):
                raise ValueError(f"page_id 必须以 provider_id 为前缀: {page_id}")
            page["page_id"] = page_id
            page.setdefault("provider_id", provider_id)
            self._normalize_page_assets(provider_id, page)
            normalized_pages[page_id] = page

        with self._lock:
            for page_id in normalized_pages:
                owner = self._find_page_owner_locked(page_id)
                if owner and owner != provider_id:
                    raise ValueError(f"页面 {page_id} 已被 {owner} 注册")
            self._providers[provider_id] = ServerPageProvider(
                provider_id=provider_id,
                provider_version=provider_version,
                pages=normalized_pages,
                asset_loader=asset_loader,
                invoke_handler=invoke_handler,
                client_event_handler=client_event_handler,
                open_handler=open_handler,
                close_handler=close_handler,
            )
        log.info(f"[ServerPages] {provider_id} 已注册 {len(normalized_pages)} 个页面")

    def unregister_provider(self, provider_id: str):
        with self._lock:
            self._providers.pop(provider_id, None)
            closing = [
                instance_id
                for instance_id, instance in self._instances.items()
                if instance.get("provider_id") == provider_id
            ]
            for instance_id in closing:
                self._close_instance_locked(instance_id)
        log.info(f"[ServerPages] {provider_id} 已注销页面提供者")

    def handle_rpc(self, method: str, payload: Dict[str, Any], client_id=None):
        if method == "manifest":
            return self.manifest()
        if method == "get_page":
            return self.get_page(payload.get("page_id"))
        if method == "get_asset":
            return self.get_asset(payload.get("asset_id"), payload.get("page_id"))
        if method == "open_instance":
            return self.open_instance(payload.get("page_id"), client_id)
        if method == "invoke":
            return self.invoke(payload, client_id)
        if method == "close_instance":
            return self.close_instance(payload.get("instance_id"))
        raise ValueError(f"未知服务端页面方法: {method}")

    def manifest(self):
        page_entries = []
        provider_entries = []
        with self._lock:
            providers = list(self._providers.values())
            for provider in providers:
                provider_entries.append(
                    {
                        "provider_id": provider.provider_id,
                        "provider_version": provider.provider_version,
                        "pages": sorted(provider.pages.keys()),
                    }
                )
                for page in provider.pages.values():
                    page_entries.append(self._page_summary(page))

        page_entries.sort(key=lambda item: item.get("title") or item.get("page_id") or "")
        provider_entries.sort(key=lambda item: item["provider_id"])
        return {
            "schema_version": 1,
            "feature_id": FEATURE_ID,
            "provider_id": FEATURE_ID,
            "provider_version": "0.2.0",
            "revision": self._manifest_revision(page_entries),
            "providers": provider_entries,
            "pages": page_entries,
        }

    def get_page(self, page_id):
        provider, page = self._resolve_page(page_id)
        page_copy = copy.deepcopy(page)
        page_copy["provider_id"] = provider.provider_id
        return page_copy

    def get_asset(self, asset_id, page_id=None):
        if not asset_id:
            raise ValueError("asset_id 不能为空")

        provider = None
        provider_asset_id = str(asset_id)
        if page_id:
            provider, _ = self._resolve_page(page_id)
            provider_asset_id = self._strip_asset_provider(provider.provider_id, provider_asset_id)
        else:
            provider_id = provider_asset_id.split("/", 1)[0]
            with self._lock:
                provider = self._providers.get(provider_id)
            if provider and "/" in provider_asset_id:
                provider_asset_id = provider_asset_id.split("/", 1)[1]

        if not provider or not provider.asset_loader:
            raise ValueError(f"资源不存在: {asset_id}")

        asset = provider.asset_loader(provider_asset_id)
        if not asset:
            raise ValueError(f"资源不存在: {asset_id}")

        result = dict(asset)
        result["asset_id"] = self._global_asset_id(
            provider.provider_id,
            str(result.get("asset_id") or provider_asset_id),
        )
        result.setdefault("encoding", "base64")
        result.setdefault("mime", "application/octet-stream")
        if "content" not in result:
            raw = result.pop("bytes", None)
            if raw is None:
                raise ValueError(f"资源内容为空: {asset_id}")
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            result["content"] = base64.b64encode(raw).decode("ascii")
        if "sha256" not in result and result.get("encoding") == "base64":
            result["sha256"] = hashlib.sha256(base64.b64decode(result["content"])).hexdigest()
        return result

    def open_instance(self, page_id, client_id):
        provider, page = self._resolve_page(page_id)
        instance_id = str(uuid.uuid4())
        instance = {
            "instance_id": instance_id,
            "provider_id": provider.provider_id,
            "page_id": page["page_id"],
            "client_id": client_id,
            "opened_at": time.time(),
            "sequence": 0,
            "realtime_subscribed": False,
            "state": {},
        }
        if provider.open_handler:
            initial_state = provider.open_handler(dict(instance))
            if isinstance(initial_state, dict):
                instance["state"] = initial_state

        with self._lock:
            self._instances[instance_id] = instance

        return {
            "instance_id": instance_id,
            "state": {
                "opened_at": instance["opened_at"],
                "page_id": instance["page_id"],
                **instance["state"],
            },
            "realtime": {
                "control_code": f"0x{REALTIME_CONTROL_CODE:02X}",
                "push_uid": REALTIME_PUSH_UID,
            },
        }

    def invoke(self, payload, client_id):
        instance_id = payload.get("instance_id")
        action = payload.get("action")
        action_payload = payload.get("payload", {})
        if not instance_id:
            raise ValueError("instance_id 不能为空")
        if not action:
            raise ValueError("action 不能为空")

        with self._lock:
            instance = self._instances.get(instance_id)
            if not instance:
                raise ValueError(f"页面实例不存在: {instance_id}")
            if instance.get("client_id") is not client_id:
                raise PermissionError("页面实例不属于当前客户端")
            provider = self._providers.get(instance["provider_id"])

        if not provider or not provider.invoke_handler:
            raise ValueError(f"页面不支持动作: {action}")

        context = self._instance_context(self._instance_snapshot(instance))
        result = provider.invoke_handler(str(action), action_payload or {}, context)
        if isinstance(result, dict) and isinstance(result.get("state_patch"), dict):
            self._merge_instance_state(instance_id, result["state_patch"])
        return result

    def handle_realtime(self, payload, client_id):
        if not isinstance(payload, dict):
            raise ValueError("实时通道 payload 必须是 JSON object")

        op = str(payload.get("op") or "event")
        instance_id = payload.get("instance_id")
        if not instance_id:
            raise ValueError("instance_id 不能为空")

        if op == "subscribe":
            instance = self._set_realtime_subscription(str(instance_id), client_id, True)
            return {
                "event": "subscribed",
                "instance_id": instance["instance_id"],
                "page_id": instance["page_id"],
                "provider_id": instance["provider_id"],
                "sequence": instance.get("sequence", 0),
                "state": copy.deepcopy(instance.get("state", {})),
            }

        if op == "unsubscribe":
            instance = self._set_realtime_subscription(str(instance_id), client_id, False)
            return {
                "event": "unsubscribed",
                "instance_id": instance["instance_id"],
            }

        if op == "state_patch":
            patch = payload.get("state_patch")
            if not isinstance(patch, dict):
                raise ValueError("state_patch 必须是 JSON object")
            self._merge_instance_state(str(instance_id), patch, client_id=client_id)
            return {
                "event": "state_patch",
                "instance_id": str(instance_id),
                "state_patch": patch,
            }

        if op == "event":
            event = str(payload.get("event") or "")
            if not event:
                raise ValueError("event 不能为空")
            event_payload = payload.get("payload", {})
            if event_payload is None:
                event_payload = {}
            if not isinstance(event_payload, dict):
                raise ValueError("payload 必须是 JSON object")
            return self.handle_client_event(str(instance_id), event, event_payload, client_id)

        raise ValueError(f"未知实时通道操作: {op}")

    def handle_client_event(self, instance_id: str, event: str, payload: Dict[str, Any], client_id):
        instance = self._require_client_instance(instance_id, client_id)
        provider = self._providers.get(instance["provider_id"])
        if not provider or not provider.client_event_handler:
            return {
                "event": event,
                "instance_id": instance_id,
                "accepted": False,
                "message": "页面未注册客户端事件处理器",
            }

        context = self._instance_context(instance)
        result = provider.client_event_handler(event, payload or {}, context)
        if isinstance(result, dict) and isinstance(result.get("state_patch"), dict):
            self._merge_instance_state(instance_id, result["state_patch"], client_id=client_id)
        if result is None:
            result = {"accepted": True}
        if isinstance(result, dict):
            result.setdefault("event", event)
            result.setdefault("instance_id", instance_id)
        return result

    def emit_instance_event(
        self,
        instance_id: str,
        event: str,
        *,
        payload: Optional[Dict[str, Any]] = None,
        state_patch: Optional[Dict[str, Any]] = None,
        uid: str = REALTIME_PUSH_UID,
    ):
        if not instance_id:
            raise ValueError("instance_id 不能为空")
        if not event:
            raise ValueError("event 不能为空")

        with self._lock:
            instance = self._instances.get(str(instance_id))
            if not instance:
                raise ValueError(f"页面实例不存在: {instance_id}")
            if not instance.get("realtime_subscribed"):
                return None
            client_id = instance.get("client_id")
            if isinstance(state_patch, dict):
                instance["state"] = {
                    **instance.get("state", {}),
                    **state_patch,
                }
            sequence = int(instance.get("sequence", 0)) + 1
            instance["sequence"] = sequence
            frame_payload = {
                "instance_id": instance["instance_id"],
                "page_id": instance["page_id"],
                "provider_id": instance["provider_id"],
                "event": event,
                "payload": payload or {},
                "state_patch": state_patch or {},
                "sequence": sequence,
                "timestamp": time.time(),
            }

        self._emit_realtime_frame(client_id, frame_payload, uid=uid)
        return frame_payload

    def close_instance(self, instance_id):
        if not instance_id:
            raise ValueError("instance_id 不能为空")
        with self._lock:
            return {"closed": self._close_instance_locked(str(instance_id))}

    def _require_client_instance(self, instance_id: str, client_id):
        with self._lock:
            instance = self._instances.get(instance_id)
            if not instance:
                raise ValueError(f"页面实例不存在: {instance_id}")
            if instance.get("client_id") is not client_id:
                raise PermissionError("页面实例不属于当前客户端")
            return self._instance_snapshot(instance)

    def _set_realtime_subscription(self, instance_id: str, client_id, subscribed: bool):
        with self._lock:
            instance = self._instances.get(instance_id)
            if not instance:
                raise ValueError(f"页面实例不存在: {instance_id}")
            if instance.get("client_id") is not client_id:
                raise PermissionError("页面实例不属于当前客户端")
            instance["realtime_subscribed"] = subscribed
            return self._instance_snapshot(instance)

    def _merge_instance_state(
        self,
        instance_id: str,
        state_patch: Dict[str, Any],
        *,
        client_id=None,
    ):
        with self._lock:
            current = self._instances.get(instance_id)
            if not current:
                raise ValueError(f"页面实例不存在: {instance_id}")
            if client_id is not None and current.get("client_id") is not client_id:
                raise PermissionError("页面实例不属于当前客户端")
            current["state"] = {
                **current.get("state", {}),
                **state_patch,
            }
            return copy.deepcopy(current["state"])

    def _instance_snapshot(self, instance):
        return {
            "instance_id": instance["instance_id"],
            "page_id": instance["page_id"],
            "provider_id": instance["provider_id"],
            "client_id": instance["client_id"],
            "opened_at": instance.get("opened_at"),
            "sequence": instance.get("sequence", 0),
            "realtime_subscribed": bool(instance.get("realtime_subscribed", False)),
            "state": copy.deepcopy(instance.get("state", {})),
        }

    def _instance_context(self, instance):
        return {
            "instance_id": instance["instance_id"],
            "page_id": instance["page_id"],
            "provider_id": instance["provider_id"],
            "client_id": instance["client_id"],
            "state": copy.deepcopy(instance.get("state", {})),
            "emit": lambda event, payload=None, state_patch=None: self.emit_instance_event(
                instance["instance_id"],
                event,
                payload=payload,
                state_patch=state_patch,
            ),
        }

    def _emit_realtime_frame(self, client_id, data: Dict[str, Any], uid: str = REALTIME_PUSH_UID):
        payload = {
            "ok": True,
            "data": data,
            "message": "",
            "error": None,
        }
        frame = chr(REALTIME_CONTROL_CODE) + str(uid)[:4].ljust(4, "0") + json.dumps(
            payload,
            ensure_ascii=False,
            default=str,
        )
        static["event_handler"].trigger_event(
            Event("ExternalIO_OUT", response_data=frame, client_id=client_id)
        )

    def _close_instance_locked(self, instance_id: str):
        instance = self._instances.pop(instance_id, None)
        if not instance:
            return False
        provider = self._providers.get(instance.get("provider_id"))
        if provider and provider.close_handler:
            provider.close_handler(dict(instance))
        return True

    def _resolve_page(self, page_id):
        if not page_id:
            raise ValueError("page_id 不能为空")
        with self._lock:
            for provider in self._providers.values():
                page = provider.pages.get(str(page_id))
                if page:
                    return provider, page
        raise ValueError(f"页面不存在: {page_id}")

    def _find_page_owner_locked(self, page_id):
        for provider_id, provider in self._providers.items():
            if page_id in provider.pages:
                return provider_id
        return None

    def _normalize_page_assets(self, provider_id, page):
        entry = page.get("entry")
        if isinstance(entry, dict) and entry.get("asset_id"):
            entry["asset_id"] = self._global_asset_id(provider_id, str(entry["asset_id"]))

        assets = page.get("assets")
        if isinstance(assets, list):
            for asset in assets:
                if isinstance(asset, dict) and asset.get("asset_id"):
                    asset["asset_id"] = self._global_asset_id(
                        provider_id,
                        str(asset["asset_id"]),
                    )

    def _global_asset_id(self, provider_id, asset_id):
        if asset_id.startswith(f"{provider_id}/"):
            return asset_id
        return f"{provider_id}/{asset_id}"

    def _strip_asset_provider(self, provider_id, asset_id):
        prefix = f"{provider_id}/"
        if asset_id.startswith(prefix):
            return asset_id[len(prefix):]
        return asset_id

    def _page_summary(self, page):
        return {
            "page_id": page["page_id"],
            "provider_id": page.get("provider_id"),
            "title": page.get("title"),
            "render_mode": page.get("render_mode"),
            "revision": page.get("revision", "1"),
            "permissions": page.get("permissions", []),
            "entry": page.get("entry"),
            "assets": page.get("assets", []),
            "hash": self._hash_json(page),
        }

    def _manifest_revision(self, pages):
        return self._hash_json(pages)

    def _hash_json(self, value):
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def get_registry() -> ServerPagesRegistry:
    registry = static.get("server_pages")
    if not registry:
        registry = ServerPagesRegistry()
        static["server_pages"] = registry
    return registry


def register_page_provider(
    provider_id: str,
    pages: list,
    *,
    provider_version: str = "0.1.0",
    asset_loader: Optional[AssetLoader] = None,
    invoke_handler: Optional[InvokeHandler] = None,
    client_event_handler: Optional[ClientEventHandler] = None,
    open_handler: Optional[OpenHandler] = None,
    close_handler: Optional[CloseHandler] = None,
):
    return get_registry().register_provider(
        provider_id,
        pages,
        provider_version=provider_version,
        asset_loader=asset_loader,
        invoke_handler=invoke_handler,
        client_event_handler=client_event_handler,
        open_handler=open_handler,
        close_handler=close_handler,
    )


def unregister_page_provider(provider_id: str):
    return get_registry().unregister_provider(provider_id)


def emit_instance_event(
    instance_id: str,
    event: str,
    *,
    payload: Optional[Dict[str, Any]] = None,
    state_patch: Optional[Dict[str, Any]] = None,
):
    return get_registry().emit_instance_event(
        instance_id,
        event,
        payload=payload,
        state_patch=state_patch,
    )
