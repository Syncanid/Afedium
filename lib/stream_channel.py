import json
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from lib.Event import Event
from lib.common import static
from lib.logger import log


STREAM_CONTROL_CODE = 0x32
STREAM_PUSH_UID = "PUSH"

StreamOpenHandler = Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]
StreamDataHandler = Callable[[bytes, Dict[str, Any]], Optional[Any]]
StreamMessageHandler = Callable[[str, Dict[str, Any], Dict[str, Any]], Optional[Any]]
StreamCloseHandler = Callable[[Dict[str, Any]], None]


@dataclass
class StreamProvider:
    provider_id: str
    provider_version: str
    open_handler: Optional[StreamOpenHandler] = None
    data_handler: Optional[StreamDataHandler] = None
    message_handler: Optional[StreamMessageHandler] = None
    close_handler: Optional[StreamCloseHandler] = None


class StreamChannelRegistry:
    def __init__(self):
        self._lock = threading.RLock()
        self._providers: Dict[str, StreamProvider] = {}
        self._sessions: Dict[str, Dict[str, Any]] = {}

    def register_provider(
        self,
        provider_id: str,
        *,
        provider_version: str = "0.1.0",
        open_handler: Optional[StreamOpenHandler] = None,
        data_handler: Optional[StreamDataHandler] = None,
        message_handler: Optional[StreamMessageHandler] = None,
        close_handler: Optional[StreamCloseHandler] = None,
    ):
        if not provider_id:
            raise ValueError("provider_id is required")
        with self._lock:
            self._providers[provider_id] = StreamProvider(
                provider_id=provider_id,
                provider_version=provider_version,
                open_handler=open_handler,
                data_handler=data_handler,
                message_handler=message_handler,
                close_handler=close_handler,
            )
        log.info(f"[StreamChannel] registered provider {provider_id}")

    def unregister_provider(self, provider_id: str):
        with self._lock:
            self._providers.pop(provider_id, None)
            closing = [
                session_id
                for session_id, session in self._sessions.items()
                if session.get("provider_id") == provider_id
            ]
            for session_id in closing:
                self._close_session_locked(session_id)
        log.info(f"[StreamChannel] unregistered provider {provider_id}")

    def handle_control(self, payload: Dict[str, Any], client_id=None):
        if not isinstance(payload, dict):
            raise ValueError("stream control payload must be a JSON object")
        op = str(payload.get("op") or "")
        if op == "open":
            return self.open_session(payload.get("provider_id"), payload, client_id)
        if op == "message":
            return self.handle_message(
                payload.get("session_id"),
                payload.get("event") or payload.get("message"),
                payload.get("payload", {}),
                client_id,
            )
        if op == "close":
            return self.close_session(payload.get("session_id"), client_id)
        raise ValueError(f"unknown stream op: {op}")

    def open_session(self, provider_id, payload: Dict[str, Any], client_id=None):
        if not provider_id:
            raise ValueError("provider_id is required")
        with self._lock:
            provider = self._providers.get(str(provider_id))
        if not provider:
            raise ValueError(f"stream provider not found: {provider_id}")

        session_id = str(uuid.uuid4())
        session = {
            "session_id": session_id,
            "provider_id": provider.provider_id,
            "client_id": client_id,
            "opened_at": time.time(),
            "metadata": dict(payload.get("metadata") or {}),
            "sequence": 0,
        }
        response = None
        if provider.open_handler:
            response = provider.open_handler(dict(session))
        with self._lock:
            self._sessions[session_id] = session
        return {
            "event": "opened",
            "session_id": session_id,
            "provider_id": provider.provider_id,
            "metadata": response or {},
        }

    def handle_message(self, session_id, event, payload, client_id=None):
        if not event:
            raise ValueError("event is required")
        session, provider = self._require_session_provider(session_id, client_id)
        if not provider.message_handler:
            return {"event": event, "session_id": session["session_id"], "accepted": False}
        if not isinstance(payload, dict):
            payload = {"value": payload}
        result = provider.message_handler(str(event), payload, self._session_context(session))
        return result if result is not None else {"event": event, "session_id": session["session_id"], "accepted": True}

    def handle_binary_frame(self, frame: bytes, client_id=None):
        session_id, payload = self.decode_binary_frame(frame)
        session, provider = self._require_session_provider(session_id, client_id)
        if not provider.data_handler:
            raise ValueError(f"stream provider has no data handler: {provider.provider_id}")
        result = provider.data_handler(payload, self._session_context(session))
        if result is not None:
            return {"event": "data", "session_id": session_id, "result": result}
        return None

    def close_session(self, session_id, client_id=None):
        if not session_id:
            raise ValueError("session_id is required")
        with self._lock:
            session = self._sessions.get(str(session_id))
            if not session:
                return {"event": "closed", "session_id": str(session_id), "closed": False}
            if client_id is not None and session.get("client_id") is not client_id:
                raise PermissionError("stream session does not belong to this client")
            closed = self._close_session_locked(str(session_id))
        return {"event": "closed", "session_id": str(session_id), "closed": closed}

    def send_event(self, session_id: str, event: str, payload: Optional[Dict[str, Any]] = None):
        session = self._require_session(session_id)
        data = {
            "event": event,
            "session_id": session_id,
            "provider_id": session["provider_id"],
            "payload": payload or {},
            "timestamp": time.time(),
        }
        self._emit_json(session.get("client_id"), data)
        return data

    def send_binary(self, session_id: str, payload: bytes):
        session = self._require_session(session_id)
        frame = self.encode_binary_frame(session_id, payload)
        static["event_handler"].trigger_event(
            Event("ExternalIO_OUT", response_data=frame, client_id=session.get("client_id"))
        )
        return True

    def encode_binary_frame(self, session_id: str, payload: bytes):
        session_bytes = session_id.encode("utf-8")
        if len(session_bytes) > 255:
            raise ValueError("session_id is too long")
        return bytes([STREAM_CONTROL_CODE, len(session_bytes)]) + session_bytes + bytes(payload)

    def decode_binary_frame(self, frame: bytes):
        if not isinstance(frame, (bytes, bytearray)) or len(frame) < 2:
            raise ValueError("invalid stream binary frame")
        raw = bytes(frame)
        if raw[0] != STREAM_CONTROL_CODE:
            raise ValueError("invalid stream control code")
        id_size = raw[1]
        if len(raw) < 2 + id_size:
            raise ValueError("invalid stream session id")
        session_id = raw[2:2 + id_size].decode("utf-8")
        return session_id, raw[2 + id_size:]

    def _require_session_provider(self, session_id, client_id=None):
        session = self._require_session(session_id, client_id=client_id)
        with self._lock:
            provider = self._providers.get(session["provider_id"])
        if not provider:
            raise ValueError(f"stream provider not found: {session['provider_id']}")
        return session, provider

    def _require_session(self, session_id, client_id=None):
        if not session_id:
            raise ValueError("session_id is required")
        with self._lock:
            session = self._sessions.get(str(session_id))
            if not session:
                raise ValueError(f"stream session not found: {session_id}")
            if client_id is not None and session.get("client_id") is not client_id:
                raise PermissionError("stream session does not belong to this client")
            return dict(session)

    def _session_context(self, session):
        return {
            **dict(session),
            "send_event": lambda event, payload=None: self.send_event(session["session_id"], event, payload),
            "send_binary": lambda payload: self.send_binary(session["session_id"], payload),
        }

    def _close_session_locked(self, session_id: str):
        session = self._sessions.pop(session_id, None)
        if not session:
            return False
        provider = self._providers.get(session.get("provider_id"))
        if provider and provider.close_handler:
            provider.close_handler(dict(session))
        return True

    def _emit_json(self, client_id, data: Dict[str, Any], uid: str = STREAM_PUSH_UID):
        frame = chr(STREAM_CONTROL_CODE) + str(uid)[:4].ljust(4, "0") + json.dumps(
            {"ok": True, "data": data, "message": "", "error": None},
            ensure_ascii=False,
            default=str,
        )
        static["event_handler"].trigger_event(
            Event("ExternalIO_OUT", response_data=frame, client_id=client_id)
        )


def get_registry() -> StreamChannelRegistry:
    registry = static.get("stream_channel")
    if not registry:
        registry = StreamChannelRegistry()
        static["stream_channel"] = registry
    return registry


def register_stream_provider(provider_id: str, **kwargs):
    return get_registry().register_provider(provider_id, **kwargs)


def unregister_stream_provider(provider_id: str):
    return get_registry().unregister_provider(provider_id)


def send_stream_event(session_id: str, event: str, payload: Optional[Dict[str, Any]] = None):
    return get_registry().send_event(session_id, event, payload)


def send_stream_binary(session_id: str, payload: bytes):
    return get_registry().send_binary(session_id, payload)
