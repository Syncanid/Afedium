from lib.common import static
from lib.external_control import register_control_code, unregister_control_code
from lib.feature import register_feature, unregister_feature
from lib.logger import log
from lib.plugin import AfediumPluginBase
from lib.server_pages import FEATURE_ID, REALTIME_CONTROL_CODE, get_registry


Info = {
    "name": "服务端页面",
    "id": "server_pages",
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
        static["server_pages"] = self.registry
        register_feature(
            FEATURE_ID,
            self.id,
            self.registry.handle_rpc,
            self._feature_manifest(),
        )
        register_control_code(
            REALTIME_CONTROL_CODE,
            self.id,
            self._handle_realtime_control,
            description="server_pages realtime state/event channel",
        )
        log.info(f"[{self.id}] 服务端页面聚合已注册")
        return True

    def main_loop(self):
        static["running"][self.id] = True
        self.stop_event.wait()

    def teardown(self):
        unregister_control_code(REALTIME_CONTROL_CODE, self.id)
        unregister_feature(FEATURE_ID, self.id)
        log.info(f"[{self.id}] 服务端页面聚合已释放")

    def _handle_realtime_control(self, ctx):
        import json

        payload = ctx.body.decode("utf-8") if isinstance(ctx.body, bytes) else str(ctx.body or "{}")
        request = json.loads(payload or "{}")
        data = self.registry.handle_realtime(request, ctx.client_id)
        return ctx.json_frame(REALTIME_CONTROL_CODE, data=data)

    def _feature_manifest(self):
        return {
            "id": FEATURE_ID,
            "title": "服务端页面",
            "version": self.info.get("version", "0.2.0"),
            "standard": "extended",
            "provider": self.id,
            "permissions": [
                "server_pages.manifest",
                "server_pages.rpc",
                "server_pages.assets",
                "server_pages.web_runtime",
                "server_pages.realtime",
            ],
            "transport": "feature_rpc",
            "protocol_codes": ["0x30", f"0x{REALTIME_CONTROL_CODE:02X}"],
            "ui_slots": ["server.pages"],
            "platforms": ["windows", "android"],
            "render_modes": ["declarative", "web_runtime"],
            "aggregates": True,
        }
