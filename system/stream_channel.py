from lib.external_control import register_control_code, unregister_control_code
from lib.feature import register_feature, unregister_feature
from lib.logger import log
from lib.plugin import AfediumPluginBase
from lib.stream_channel import STREAM_CONTROL_CODE, get_registry


FEATURE_ID = "stream_channel"

Info = {
    "name": "流通道",
    "id": "stream_channel",
    "dependencies": [],
    "pip_dependencies": [],
    "linux_dependencies": [],
}


class AFEDIUMPlugin(AfediumPluginBase):
    default_config = {
        "enabled": True,
    }

    def setup(self):
        if not self.config.conf.get("enabled", True):
            return True

        self.registry = get_registry()
        register_feature(
            FEATURE_ID,
            self.id,
            self._handle_feature_rpc,
            self._feature_manifest(),
        )
        register_control_code(
            STREAM_CONTROL_CODE,
            self.id,
            self._handle_stream_control,
            description="binary/text stream session channel",
        )
        log.info(f"[{self.id}] stream channel registered")
        return True

    def main_loop(self):
        from lib.common import static

        static["running"][self.id] = True
        self.stop_event.wait()

    def teardown(self):
        unregister_control_code(STREAM_CONTROL_CODE, self.id)
        unregister_feature(FEATURE_ID, self.id)
        log.info(f"[{self.id}] stream channel released")

    def _handle_feature_rpc(self, method, payload, client_id=None):
        if method != "manifest":
            raise ValueError(f"unknown stream feature method: {method}")
        return {
            "feature_id": FEATURE_ID,
            "control_code": f"0x{STREAM_CONTROL_CODE:02X}",
            "supports": ["open", "message", "close", "binary"],
            "session_id_transport": "0x32 binary prefix",
        }

    def _handle_stream_control(self, ctx):
        import json

        if isinstance(ctx.raw_message, (bytes, bytearray)):
            raw = bytes(ctx.raw_message)
            if len(raw) > 1 and raw[1] != ord("{"):
                data = self.registry.handle_binary_frame(raw, ctx.client_id)
                return None if data is None else ctx.json_frame(STREAM_CONTROL_CODE, data=data)
            payload = raw[1:].decode("utf-8")
        else:
            payload = ctx.body.decode("utf-8") if isinstance(ctx.body, bytes) else str(ctx.body or "{}")

        request = json.loads(payload or "{}")
        data = self.registry.handle_control(request, ctx.client_id)
        return ctx.json_frame(STREAM_CONTROL_CODE, data=data)

    def _feature_manifest(self):
        return {
            "id": FEATURE_ID,
            "title": "流通道",
            "version": self.info.get("version", "0.1.0"),
            "standard": "extended",
            "provider": self.id,
            "transport": "external_control",
            "protocol_codes": [f"0x{STREAM_CONTROL_CODE:02X}"],
            "permissions": [
                "stream_channel.control",
                "stream_channel.binary",
            ],
            "platforms": ["windows", "android"],
        }
