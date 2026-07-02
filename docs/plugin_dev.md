# AFEDIUM 插件开发指南

## 1. 核心规范与限制

为保障系统稳定性和安全性，插件开发必须遵守以下红线：

1. **死循环**：插件主循环必须监听 `self.stop_event`，禁止使用 `while True: time.sleep(n)`，否则会导致系统无法正常关闭并引发线程泄漏。
2. **标准流**：禁止使用 `sys.stdout` 或 `print()` 打印日志，必须使用 `lib.logger.log`。
3. **指令输出**：指令处理器必须使用 `ctx.reply()` 返回数据。

## 2. 插件的基本结构

所有的 AFEDIUM 插件入口点都必须在 `main.py` 中暴露一个名为 `AFEDIUMPlugin` 的类，该类必须继承自
`lib.plugin.AfediumPluginBase`。

### 最小可用模板 (main.py)

```python
from lib.plugin import AfediumPluginBase
from lib.logger import log
from lib.common import comm_lib

class AFEDIUMPlugin(AfediumPluginBase):
    # 定义插件的默认配置 (启动时会自动合并到 config/你的插件id.json)
    default_config = {
        "hello_message": "你好，世界！",
        "loop_interval": 5
    }

    def setup(self) -> bool:
        """
        初始化阶段：读取配置、注册指令、申请资源。
        必须返回 True 才能进入主循环；返回 False 将中止加载。
        """
        self.message = self.config.conf.get("hello_message")

        # 注册自定义指令
        comm_lib.register("mycmd", self.my_command_handler)
        log.info(f"[{self.id}] 初始化完成，已注册指令: mycmd")
        return True

    def main_loop(self):
        """
        主循环阶段：处理后台常驻任务。
        必须使用 self.stop_event 来感知系统的退出信号。
        """
        log.info(f"[{self.id}] 后台主循环已启动")

        # 使用 stop_event.wait(timeout) 代替 time.sleep()
        # 这样能在收到退出信号时瞬间唤醒并跳出循环
        while not self.stop_event.is_set():
            # 这里写你的周期性后台逻辑
            log.debug(f"[{self.id}] 正在执行后台巡检...")

            # 挂起等待下一次循环
            self.stop_event.wait(timeout=self.config.conf.get("loop_interval", 5))

    def teardown(self):
        """
        清理阶段：系统退出或模块卸载时自动调用。
        必须在此处注销指令、释放端口、关闭文件句柄。
        """
        comm_lib.unregister("mycmd")
        log.info(f"[{self.id}] 资源已安全释放，优雅退出。")

    # --- 自定义指令处理器 ---
    def my_command_handler(self, ctx, args: list):
        """
        指令处理逻辑
        :param ctx: CommandContext 上下文对象，用于隔离多端输出
        :param args: 用户传入的参数列表
        """
        if not args:
            # 基础文本反馈
            ctx.reply(self.message)
            return "执行完毕"

        action = args[0]
        if action == "status":
            # 结构化/复杂流式反馈
            ctx.reply("正在检查状态...")
            ctx.reply(f"当前运行参数: {args[1:]}")
            return "状态正常"
        else:
            return f"未知参数: {action}"
```

## 3. 核心 API 解析

### 3.1 日志系统 (`lib.logger.log`)

标准的日志分级能让控制台保持清爽，并将完整记录自动轮转写入 `logs/afedium.log`：

```python
from lib.logger import log

log.debug("底层调试信息，仅在 debugging=true 时输出")
log.info("常规运行提示")
log.warning("非致命的异常状况")
log.error("核心功能崩溃或严重错误")
```

### 3.2 配置文件 (`self.config`)

基类已自动为您初始化了 `self.config` 对象。

- 读取配置：`self.config.conf.get("key")`
- 写入配置：`self.config.conf["key"] = "new_value"`，然后调用 `self.config.update()` 即可安全写入磁盘（自带线程锁保护）。

### 3.3 指令上下文 (`CommandContext`)

`ctx` 对象用于解决多用户并发请求时的输出串台问题。

- `ctx.reply(text)`：向发送指令的客户端流式追加输出内容。
- `ctx.client_id`：(可选) 获取触发指令的 WebSocket 客户端对象，可用于实现高级的权限拦截或私发消息。

## 4. 插件打包 (PYZ)

编写完成后，将包含 `main.py` 和 `info.json` 的目录打包为标准 `.zip` 文件，然后将后缀改为 `.pyz`，放入 `pyz_modules`
目录下即可被系统加载。

---

## 5. 协议扩展接口

插件可以暴露两类服务端接口：

1. 直接注册外部控制码，适合插件自定义二进制或流式协议。
2. 注册 Feature，供客户端通过 `0x30` Feature RPC 调用。

### 5.1 外部控制码

```python
from lib.external_control import register_control_code, unregister_control_code

CONTROL_CODE = 0x40


class AFEDIUMPlugin(AfediumPluginBase):
    def setup(self):
        register_control_code(CONTROL_CODE, self.id, self.handle_control, "示例插件协议")
        return True

    def teardown(self):
        unregister_control_code(CONTROL_CODE, self.id)

    def handle_control(self, ctx):
        return ctx.json_frame(
            CONTROL_CODE,
            data={"message": "ok", "request": ctx.body},
            message="示例插件协议已处理",
        )
```

规则：

- 不要占用核心控制码：`0x00` 到 `0x0F`、`0x10` 到 `0x1F`、`0x30`、`0xFF`。
- 控制码同一时间只能有一个所有者。
- 插件卸载时必须注销自己注册的控制码。
- 外部控制码响应必须使用 `chr(code) + UID + JSON_ENVELOPE`，推荐用 `ctx.json_frame()` 生成。
- 如果处理器已经调用 `ctx.reply()` 主动回复，可以返回 `None`。

### 5.2 Feature

```python
from lib.feature import register_feature, unregister_feature

FEATURE_ID = "example"

class AFEDIUMPlugin(AfediumPluginBase):
    def setup(self):
        register_feature(FEATURE_ID, self.id, self.handle_rpc, {
            "id": FEATURE_ID,
            "title": "示例功能",
            "version": "0.1.0",
            "standard": "extended",
            "permissions": ["example.invoke"],
            "transport": "feature_rpc"
        })
        return True

    def teardown(self):
        unregister_feature(FEATURE_ID, self.id)

    def handle_rpc(self, method, payload, client_id):
        if method == "ping":
            return {"message": "pong", "payload": payload}
        raise ValueError(f"未知方法: {method}")
```

Feature 处理器只返回 `data` 部分；`0x30` 调用层会自动包装为：

```json
{
  "ok": true,
  "data": {},
  "message": "",
  "error": null
}
```

### 5.3 Server Pages Web runtime

插件如果提供 `server_pages` 页面，应通过系统聚合器注册页面。不要在业务插件里直接调用 `register_feature("server_pages", ...)`，同一个 Feature id 只能有一个 owner；系统聚合器会统一暴露 `feature_id: "server_pages"`。

注册入口：

```python
from lib.server_pages import register_page_provider, unregister_page_provider


class AFEDIUMPlugin(AfediumPluginBase):
    def setup(self):
        register_page_provider(
            self.id,
            pages=[self._web_page()],
            provider_version=self.info.get("version", "0.1.0"),
            asset_loader=self._load_asset,
            invoke_handler=self._invoke,
            client_event_handler=self._handle_client_event,
        )
        return True

    def teardown(self):
        unregister_page_provider(self.id)
```

可以发布两类页面：

- `render_mode: "declarative"`：客户端按结构化布局渲染。
- `render_mode: "web_runtime"`：客户端拉取 HTML 资源，在 WebView 中运行。

`declarative` 页面示例定义：

```python
{
    "schema_version": 1,
    "page_id": "example.status",
    "title": "状态面板",
    "render_mode": "declarative",
    "revision": "1",
    "permissions": ["server_pages.rpc", "server_pages.realtime"],
    "entry": {"type": "declarative"},
    "layout": {
        "type": "stack",
        "gap": 12,
        "blocks": [
            {"type": "text", "variant": "headline", "text": "状态面板"},
            {
                "type": "metric_grid",
                "metrics": [
                    {
                        "label": "运行插件",
                        "binding": "health.running_plugins",
                        "suffix": " 个",
                        "tone": "success",
                        "icon": "plugin"
                    }
                ]
            },
            {
                "type": "button_group",
                "buttons": [
                    {"label": "刷新", "icon": "refresh", "action": "refresh_status"},
                    {"label": "Ping", "icon": "event", "event": "ping"}
                ]
            },
            {"type": "json_view", "binding": "health"}
        ]
    }
}
```

当前客户端支持的声明式区块包括 `text`, `section`, `alert`, `metric_grid`, `status_list`, `table`, `list`, `progress`, `divider`, `spacer`, `button`, `button_group`, `json_view`。区块可用 `binding` 读取实例 `state` 的点路径；列表类区块可用 `items_binding`，表格可用 `rows_binding`。声明式页面适合状态面板、控制按钮和轻量实时反馈；表单编辑、媒体流和复杂交互仍应使用 `web_runtime` 或等待 schema 扩展。

`web_runtime` 页面示例定义：

```python
{
    "schema_version": 1,
    "page_id": "example.web_demo",
    "title": "Web 示例",
    "render_mode": "web_runtime",
    "revision": "1",
    "permissions": ["server_pages.rpc", "server_pages.web_runtime"],
    "entry": {
        "type": "asset",
        "asset_id": "web-demo/index.html"
    },
    "assets": [
        {
            "asset_id": "web-demo/index.html",
            "mime": "text/html; charset=utf-8"
        }
    ],
    "bridge_permissions": ["invoke"]
}
```

`page_id` 必须全局唯一，推荐使用 `<插件id>.<页面名>`。`asset_id` 在插件内可以写本地路径，例如 `web-demo/index.html`；聚合器对外返回时会自动加上 provider 前缀，例如 `example/web-demo/index.html`，客户端应直接使用返回值，不要自行拼接。

资源加载器示例：

```python
import hashlib
from lib.support_lib import get_plugin_resource

def _load_asset(self, asset_id):
    if asset_id != "web-demo/index.html":
        return None
    content = get_plugin_resource(self.id, "assets/web-demo/index.html", mode="rb")
    return {
        "asset_id": asset_id,
        "encoding": "base64",
        "mime": "text/html; charset=utf-8",
        "bytes": content,
        "sha256": hashlib.sha256(content).hexdigest(),
    }
```

动作处理器示例：

```python
def _invoke(self, action, payload, context):
    if action == "echo":
        return {"state_patch": {"echo": payload}}
    raise ValueError(f"未知页面动作: {action}")

def _handle_client_event(self, event, payload, context):
    if event == "ping":
        return {"state_patch": {"last_ping": payload}}
    return {"accepted": True}
```

`context` 包含 `instance_id`, `page_id`, `provider_id`, `client_id`, `state`，以及 `emit(event, payload=None, state_patch=None)`。如果返回值里包含 `state_patch`，聚合器会合并到该页面实例的服务端状态；如果调用 `context["emit"](...)`，聚合器会立刻向该实例下发 `0x31 PUSH`。

服务端主动推送示例：

```python
def _invoke(self, action, payload, context):
    if action == "start_realtime_push":
        context["emit"](
            "server_push",
            payload={"message": "服务端主动推送"},
            state_patch={"last_server_push": time.time()},
        )
        return {"state_patch": {"message": "已触发一次 PUSH"}}
    raise ValueError(f"未知页面动作: {action}")
```

页面内统一使用客户端注入的 `window.afediumBridge`，不要直接调用平台原生对象：

```html
<script>
  async function echo() {
    const result = await window.afediumBridge.invoke("echo", {
      source: "web_runtime"
    });
    console.log(result);
  }
</script>
```

实时事件使用 `sendEvent()`，服务端响应或主动推送会通过 `afediumserverpageevent` 进入页面：

```html
<script>
  async function ping() {
    await window.afediumBridge.sendEvent("ping", {
      source: "web_runtime"
    });
  }

  window.addEventListener("afediumserverpageevent", function (event) {
    console.log(event.detail.state_patch);
  });
</script>
```

桥接规则：

- `bridge_permissions` 必须包含 `"invoke"`，否则客户端应拒绝 Web 页面调用服务端动作。
- `bridge_permissions` 必须包含 `"stream"`，否则客户端应拒绝 Web 页面调用 `stream_channel`。
- `invoke(action, payload)` 会调用当前页面实例的 `server_pages.invoke`。
- `openStream(providerId, metadata)`, `sendStreamMessage(sessionId, event, payload)`, `sendStreamBinary(sessionId, payload)`, `closeStream(sessionId)` 会调用 `0x32 stream_channel`。
- `0x31` 适合服务端主动 UI 消息、实例状态增量同步、按钮触发后的异步进度反馈；这类场景通常只需要 `bridge_permissions: ["invoke"]`，由服务端在后台使用 `context["emit"]()` 推送。
- 客户端会把服务端流文本事件派发为 `afediumstreammessage`，把服务端二进制帧派发为 `afediumstreambinary`。
- 服务端推视频流时，页面应只发送 `start/stop/configure` 这类控制消息；视频帧由服务端通过 `context["send_binary"](frame_bytes)` 推给客户端，Web 页面在 `afediumstreambinary` 中渲染。
- `payload` 应保持为 JSON object。
- 插件侧 `invoke` 处理器仍然只返回 `data`，不需要手工包装 `ok/message/error`。

客户端会向 Web runtime 注入主题运行时，并把客户端配色写到 `document.documentElement.style` 上的 CSS 变量。插件页面应优先使用这些变量同步客户端配色，不要直接读取 WebView 平台桥接对象：

```css
body {
  background: var(--af-background);
  color: var(--af-text);
}

.panel {
  background: var(--af-surface);
  border: 1px solid var(--af-border);
}

button {
  background: var(--af-primary);
  color: var(--af-on-primary);
}
```

当前标准变量包括：

`--af-background`, `--af-surface`, `--af-surface-alt`, `--af-text`, `--af-text-muted`,
`--af-primary`, `--af-on-primary`, `--af-border`, `--af-error`, `--af-on-error`,
`--af-success`, `--af-on-success`, `--af-warning`, `--af-on-warning`,
`--af-primary-soft`, `--af-error-soft`, `--af-success-soft`, `--af-warning-soft`。

客户端还会暴露 `window.afediumTheme`。客户端内部可通过类似 `window.afediumThemeBridge.apply(theme)` 的入口刷新主题；插件页面只需要读取 CSS 变量或监听主题变化事件。主题变化时客户端会派发 `afediumthemechange`：

```js
window.addEventListener("afediumthemechange", function (event) {
  console.log(event.detail.mode);
});
```

### 5.4 Stream Channel provider

高吞吐或低延迟数据不要放进 `server_pages.invoke` 或 `0x30` JSON RPC。插件可以注册 `stream_channel` provider，通过 `0x32` 接收文本控制帧和二进制帧：

```python
from lib.stream_channel import register_stream_provider, unregister_stream_provider


class AFEDIUMPlugin(AfediumPluginBase):
    def setup(self):
        register_stream_provider(
            self.id,
            provider_version=self.info.get("version", "0.1.0"),
            open_handler=self._stream_open,
            message_handler=self._stream_message,
            data_handler=self._stream_data,
            close_handler=self._stream_close,
        )
        return True

    def teardown(self):
        unregister_stream_provider(self.id)

    def _stream_open(self, context):
        return {"accepted": True}

    def _stream_message(self, event, payload, context):
        return {"event": event, "accepted": True}

    def _stream_data(self, data, context):
        return {"bytes": len(data)}

    def _stream_close(self, context):
        pass
```

二进制帧格式为 `byte(0x32) + byte(session_id_length) + session_id_utf8 + payload_bytes`。未来音视频流应在 provider 文档里明确 codec、时间戳、关键帧、背压和丢帧策略。
