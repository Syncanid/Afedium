import base64
import inspect
import json
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from lib.Event import Event
from lib.common import static
from lib.logger import log


ExternalControlHandler = Callable[["ExternalMessageContext"], Any]


@dataclass
class ExternalControlRegistration:
    code: int
    owner: str
    handler: ExternalControlHandler
    description: str = ""


class ExternalMessageContext:
    def __init__(self, raw_message: Any, client_id: Any = None):
        self.raw_message = raw_message
        self.client_id = client_id
        self.is_bytes = isinstance(raw_message, (bytes, bytearray))
        self.code = self._read_code(raw_message)
        self.payload = self._read_payload(raw_message)
        self.uid = self._read_uid(raw_message)
        self.body = self._read_body(raw_message)
        self.replied = False

    @staticmethod
    def _read_code(raw_message: Any) -> Optional[int]:
        if raw_message is None or len(raw_message) == 0:
            return None
        first = raw_message[0]
        if isinstance(first, int):
            return first
        return ord(first)

    @staticmethod
    def _read_payload(raw_message: Any) -> Any:
        if raw_message is None or len(raw_message) == 0:
            return b"" if isinstance(raw_message, (bytes, bytearray)) else ""
        return raw_message[1:]

    @staticmethod
    def _read_uid(raw_message: Any) -> str:
        payload = ExternalMessageContext._read_payload(raw_message)
        if isinstance(payload, (bytes, bytearray)):
            return payload[:4].decode("utf-8", errors="ignore")
        return str(payload[:4]) if payload else ""

    @staticmethod
    def _read_body(raw_message: Any) -> Any:
        payload = ExternalMessageContext._read_payload(raw_message)
        if isinstance(payload, (bytes, bytearray)):
            return payload[4:]
        return payload[4:] if len(payload) >= 4 else ""

    def frame(self, code: int, payload: Any) -> str:
        return self.json_frame(code, data=payload)

    def json_frame(
        self,
        code: int,
        *,
        ok: bool = True,
        data: Any = None,
        message: str = "",
        error: Optional[Dict[str, Any]] = None,
    ) -> str:
        return chr(code) + self.uid + json.dumps(
            {
                "ok": ok,
                "data": self._json_safe(data),
                "message": message,
                "error": self._json_safe(error),
            },
            ensure_ascii=False,
            default=str,
        )

    def reply(self, response_data: Any):
        self.replied = True
        static["event_handler"].trigger_event(
            Event("ExternalIO_OUT", response_data=self.response_frame(response_data), client_id=self.client_id)
        )

    def reply_raw(self, response_data: Any):
        self.replied = True
        static["event_handler"].trigger_event(
            Event("ExternalIO_OUT", response_data=response_data, client_id=self.client_id)
        )

    def push(self, code: int, payload: Any):
        self.replied = True
        static["event_handler"].trigger_event(
            Event("ExternalIO_OUT", response_data=self.frame(code, payload), client_id=self.client_id)
        )

    def response_frame(self, response_data: Any) -> Any:
        if isinstance(response_data, (bytes, bytearray)):
            return bytes(response_data)
        if self._looks_like_frame(response_data):
            return response_data
        return self.json_frame(self.code, data=response_data)

    @staticmethod
    def _looks_like_frame(response_data: Any) -> bool:
        if not isinstance(response_data, str) or not response_data:
            return False
        code = ord(response_data[0])
        if code < 0 or code > 255:
            return False
        payload = response_data[1:]
        if payload.lstrip().startswith("{"):
            return True
        return len(payload) >= 4 and payload[4:].lstrip().startswith("{")

    @staticmethod
    def _json_safe(value: Any) -> Any:
        if isinstance(value, (bytes, bytearray)):
            return {
                "encoding": "base64",
                "content_base64": base64.b64encode(bytes(value)).decode("ascii"),
            }
        if isinstance(value, dict):
            return {key: ExternalMessageContext._json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [ExternalMessageContext._json_safe(item) for item in value]
        return value


class ExternalControlRegistry:
    def __init__(self):
        self._lock = threading.RLock()
        self._handlers: Dict[int, ExternalControlRegistration] = {}

    def register(
        self,
        code: int,
        owner: str,
        handler: ExternalControlHandler,
        description: str = "",
        allow_override: bool = False,
    ):
        if not isinstance(code, int) or code < 0 or code > 255:
            raise ValueError("外部控制码必须是 0 到 255 的整数")
        if not owner:
            raise ValueError("外部控制码必须声明所属模块")

        with self._lock:
            existing = self._handlers.get(code)
            if existing and existing.owner != owner and not allow_override:
                raise ValueError(f"控制码 0x{code:02X} 已被 {existing.owner} 注册")
            self._handlers[code] = ExternalControlRegistration(
                code=code,
                owner=owner,
                handler=handler,
                description=description,
            )
            log.debug(f"[外部控制] 已注册 0x{code:02X} -> {owner}")

    def unregister(self, code: int, owner: str):
        with self._lock:
            existing = self._handlers.get(code)
            if existing and existing.owner == owner:
                del self._handlers[code]
                log.debug(f"[外部控制] 已注销 0x{code:02X} <- {owner}")

    def unregister_owner(self, owner: str):
        with self._lock:
            for code, existing in list(self._handlers.items()):
                if existing.owner == owner:
                    del self._handlers[code]
                    log.debug(f"[外部控制] 已注销 0x{code:02X} <- {owner}")

    def get(self, code: int) -> Optional[ExternalControlRegistration]:
        with self._lock:
            return self._handlers.get(code)

    def snapshot(self) -> Dict[str, Dict[str, str]]:
        with self._lock:
            return {
                f"0x{code:02X}": {
                    "owner": reg.owner,
                    "description": reg.description,
                }
                for code, reg in sorted(self._handlers.items())
            }


registry = ExternalControlRegistry()


def register_control_code(
    code: int,
    owner: str,
    handler: ExternalControlHandler,
    description: str = "",
    allow_override: bool = False,
):
    registry.register(code, owner, handler, description, allow_override)


def unregister_control_code(code: int, owner: str):
    registry.unregister(code, owner)


def unregister_owner(owner: str):
    registry.unregister_owner(owner)


def control_code_snapshot() -> Dict[str, Dict[str, str]]:
    return registry.snapshot()


async def dispatch_external_message(raw_message: Any, client_id: Any = None):
    ctx = ExternalMessageContext(raw_message, client_id)
    if ctx.code is None:
        return

    registration = registry.get(ctx.code)
    if not registration:
        ctx.reply(
            ctx.json_frame(
                ctx.code,
                ok=False,
                message=f"未知控制码: 0x{ctx.code:02X}",
                error={
                    "code": "unknown_control_code",
                    "message": f"未知控制码: 0x{ctx.code:02X}",
                    "details": None,
                },
            )
        )
        return

    try:
        result = registration.handler(ctx)
        if inspect.isawaitable(result):
            result = await result
        if result is not None and not ctx.replied:
            ctx.reply(result)
    except Exception as exc:
        log.error(f"[外部控制] 0x{ctx.code:02X} 执行失败: {exc}")
        ctx.reply(
            ctx.json_frame(
                ctx.code,
                ok=False,
                message=f"控制码执行失败: {exc}",
                error={
                    "code": "handler_exception",
                    "message": str(exc),
                    "details": None,
                },
            )
        )
