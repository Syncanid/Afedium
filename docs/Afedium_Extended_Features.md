# Afedium Extended Features

本文档面向 Afedium 客户端开发者，定义 Extended Feature 的发现方式、Feature RPC、当前实现和扩展控制码行为。

## 1. 客户端兼容原则

Extended Feature 是 Core 之外的可选能力。

重要规则：

- 除 `server_banner` 外，所有 Core Feature 和 Extended Feature 都是可选的。
- 客户端必须先通过 `0x01` 读取 feature 列表，再决定是否启用某个 Extended Feature。
- 不要假设 `server_pages` 一定存在。
- 不要直接调用未在 feature 列表中声明的 Feature。
- 如果 feature 列表里出现未知字段，客户端必须忽略。
- `transport` 为 `feature_rpc` 时，默认通过 `0x30` 调用。
- `protocol_codes` 只是声明，不代表客户端可以跳过 feature 列表。

## 2. Extended Feature 的注册模型

服务端通过 `lib.feature.register_feature()` 注册 Extended Feature，并在卸载时通过 `unregister_feature()` 注销。

一个 feature 注册后会同时：

- 出现在 `0x01` feature 列表中
- 写入 `static["features"]`
- 通过 `0x30` Feature RPC 暴露方法

注册 manifest 最少应包含：

```json
{
  "id": "example",
  "title": "示例功能",
  "standard": "extended",
  "transport": "feature_rpc"
}
```

推荐字段：

| 字段               | 说明                  |
|------------------|---------------------|
| `id`             | 全局唯一 feature id     |
| `title`          | 客户端展示名称             |
| `standard`       | 固定为 `extended`      |
| `transport`      | 当前通常为 `feature_rpc` |
| `provider`       | 提供者模块 id            |
| `version`        | Feature 版本          |
| `permissions`    | 权限声明                |
| `platforms`      | 支持的客户端平台            |
| `ui_slots`       | 客户端 UI 挂载位          |
| `render_modes`   | 页面或视图渲染模式           |
| `protocol_codes` | 若使用额外外部控制码则声明       |

## 3. Feature RPC：`0x30`

`0x30` 是共享 Feature RPC 控制码，不属于某一个具体 feature。客户端通过它调用某个 feature 的方法。

### 请求

```text
chr(0x30) + UID + JSON
```

请求 JSON 结构：

```json
{
  "feature_id": "server_pages",
  "method": "manifest",
  "payload": {}
}
```

### 响应

```text
chr(0x30) + UID + JSON_ENVELOPE
```

成功：

```json
{
  "ok": true,
  "data": {},
  "message": "",
  "error": null
}
```

失败：

```json
{
  "ok": false,
  "data": null,
  "message": "Feature 不存在: example",
  "error": {
    "code": "feature_not_found",
    "message": "Feature 不存在: example",
    "details": null
  }
}
```

当前实现里的错误码：

| 错误码                   | 触发条件                             |
|-----------------------|----------------------------------|
| `invalid_json`        | 请求体不是合法 JSON                     |
| `bad_request`         | 缺少 `feature_id` 或 `method` 等请求参数 |
| `feature_not_found`   | 未注册该 feature                     |
| `feature_call_failed` | feature 方法执行异常                   |

客户端建议：

- 每次 RPC 都保留原 UID，便于并发请求匹配。
- `payload` 始终发送 JSON object；没有参数时发送 `{}`。
- 返回值按统一 JSON envelope 解析。
- 对 `ok: false` 的响应要显示 `error.message`。

## 4. 当前 Extended Feature

### `server_pages`

`server_pages` 由系统聚合器提供，属于标准的 Extended Feature。它的协议入口始终是唯一的 `feature_id: "server_pages"`；业务插件不应各自注册同名 Feature，而是通过服务端的页面注册库把页面注册到聚合器中。

manifest：

```json
{
  "id": "server_pages",
  "title": "服务端页面",
  "version": "0.2.0",
  "standard": "extended",
  "provider": "server_pages",
  "permissions": [
    "server_pages.manifest",
    "server_pages.rpc",
    "server_pages.assets",
    "server_pages.web_runtime",
    "server_pages.realtime"
  ],
  "transport": "feature_rpc",
  "protocol_codes": [
    "0x30",
    "0x31"
  ],
  "ui_slots": [
    "server.pages"
  ],
  "platforms": [
    "windows",
    "android"
  ],
  "render_modes": [
    "declarative",
    "web_runtime"
  ],
  "aggregates": true
}
```

客户端 UI 集成建议：

- `server_pages` 不需要单独的“服务端页面”父入口。客户端应把 `manifest.pages` 中的每个页面直接作为 Server Info / 服务端详情页里的条目展示。
- `render_mode` 是客户端选择渲染器的协议字段，不建议作为列表副标题直接显示给用户。
- 客户端可用图标区分渲染器：`declarative` 可显示仪表盘/布局类图标，`web_runtime` 可显示 Web/浏览器类图标。
- `ui_slots: ["server.pages"]` 表示页面挂载到服务端页面区域；它不是固定 UI 文案，也不要求新增独立页面入口。

客户端如果看到 `server_pages`，可按以下方法调用：

#### 4.1 `manifest`

请求：

```json
{
  "feature_id": "server_pages",
  "method": "manifest",
  "payload": {}
}
```

响应结构：

```json
{
  "schema_version": 1,
  "feature_id": "server_pages",
  "provider_id": "server_pages",
  "provider_version": "0.2.0",
  "revision": "...",
  "providers": [
      {
        "provider_id": "server_pages_demo",
        "provider_version": "0.1.0",
        "pages": [
          "server_pages_demo.status",
          "server_pages_demo.web_demo",
          "server_pages_demo.stream_demo",
          "server_pages_demo.camera_demo"
        ]
      }
  ],
  "pages": [
    {
      "page_id": "server_pages_demo.status",
      "provider_id": "server_pages_demo",
      "title": "服务端状态",
      "render_mode": "declarative",
      "revision": "1",
      "permissions": [
        "server_pages.rpc"
      ],
      "entry": {
        "type": "declarative"
      },
      "assets": [],
      "hash": "..."
    }
  ]
}
```

客户端行为：

- 用 `revision` 或页面 `hash` 判断缓存是否需要刷新。
- 遇到未知 `render_mode` 时应降级显示，不要崩溃。
- `providers` 是发布页面的插件清单；客户端可显示或忽略。
- `pages` 是跨插件聚合后的可枚举页面清单，不是固定 UI。
- 列表展示应以 `title` 为主；`page_id` 只作为兜底名称或调试信息。
- `page_id` 全局唯一，推荐格式为 `<provider_id>.<page_name>`。
- `asset_id` 在响应中是全局资源 id，推荐格式为 `<provider_id>/<provider_local_asset_path>`。

#### 4.2 `get_page`

请求：

```json
{
  "feature_id": "server_pages",
  "method": "get_page",
  "payload": {
    "page_id": "server_pages_demo.status"
  }
}
```

响应：返回完整页面定义。

客户端行为：

- 依据 `render_mode` 决定渲染方式。
- `declarative` 页面按布局数据渲染。
- `web_runtime` 页面按 `entry.asset_id` 拉取 HTML 资源，并在 WebView 中注入运行时能力。
- 未知字段必须忽略；未知区块或未知 `render_mode` 应显示降级视图，不应中断整个 Server Info 页面。

当前客户端已实现的 `declarative` 区块：

| 区块 | 说明 |
|------|------|
| `text` | 显示文本，`variant` 可为 `headline`, `title`, `subtitle`, `body`, `caption`, `code` |
| `section` | 带标题和副标题的嵌套区块容器 |
| `alert` | 带 tone 和 icon 的提示条 |
| `metric_grid` | 指标卡网格，适合数值概览 |
| `status_list` | 键值状态行列表 |
| `table` | 小型横向滚动数据表 |
| `list` | 事件或任务列表 |
| `progress` | 线性进度条 |
| `divider` | 分隔线，可带标签 |
| `spacer` | 固定高度留白 |
| `button` | 单个动作按钮，可调用 `action` RPC 或发送 `event` |
| `button_group` | 多个动作按钮 |
| `json_view` | 显示完整 `state` 或绑定值的 JSON |

绑定规则：

- 区块可使用 `binding` 读取页面实例 `state`，支持点路径，例如 `health.running_plugins`。
- `status_list`, `list` 可使用 `items_binding` 读取数组。
- `table` 可使用 `rows_binding` 读取行数组，`columns` 描述列。
- `button` 的 `action` 映射到 `server_pages.invoke`；`event` 映射到 `0x31` 实时事件。
- `tone` 推荐使用 `primary`, `success`, `warning`, `error`, `muted`。

暂未实现的声明式能力包括表单输入、开关、滑块、图片、图表、虚拟 DOM diff 和媒体流控件。需要这些能力时应先扩展 schema，再让客户端按 feature 版本或页面 schema 兼容处理。

#### 4.3 `get_asset`

请求：

```json
{
  "feature_id": "server_pages",
  "method": "get_asset",
  "payload": {
    "asset_id": "server_pages_demo/web-demo/index.html"
  }
}
```

响应：

```json
{
  "asset_id": "server_pages_demo/web-demo/index.html",
  "encoding": "base64",
  "mime": "text/html; charset=utf-8",
  "sha256": "...",
  "content": "..."
}
```

客户端行为：

- 先按 Base64 解码，再按 `mime` 处理。
- 可用 `sha256` 校验资源完整性。
- 聚合器会把插件本地资源路径转换为全局 `asset_id`，避免多个插件使用同名资源时冲突。
- 客户端应使用 `get_page` / `manifest` 返回的 `entry.asset_id` 或 `assets[].asset_id`，不要自行拼接资源路径。

#### 4.3.1 Web runtime 注入环境

当 `render_mode` 为 `web_runtime` 时，客户端在 HTML 页面执行前注入以下运行时对象。页面作者只应依赖这些对象，不应直接依赖 Android WebView、iOS WebKit 或 Windows WebView2 的原生桥接对象。

##### `window.afediumBridge`

```ts
window.afediumBridge.invoke(action: string, payload?: object): Promise<any>
window.afediumBridge.sendEvent(event: string, payload?: object): Promise<any>
window.afediumBridge.openStream(providerId: string, metadata?: object): Promise<any>
window.afediumBridge.sendStreamMessage(sessionId: string, event: string, payload?: object): Promise<any>
window.afediumBridge.sendStreamBinary(sessionId: string, payload: ArrayLike<number>): Promise<any>
window.afediumBridge.closeStream(sessionId: string): Promise<any>
```

行为：

- `invoke()` 会映射为当前页面实例上的 `server_pages.invoke`。
- 客户端负责附加当前 `instance_id`；Web 页面只传 `action` 和业务 `payload`。
- 只有页面定义中包含 `bridge_permissions: ["invoke"]` 时，客户端才允许桥接调用。
- `payload` 应为 JSON object；非 object 值可由客户端包装为 `{ "value": ... }`。
- Promise resolve 的值是 `server_pages.invoke` 返回的 `data`。
- Promise reject 的值应包含服务端 envelope 或客户端桥接错误信息。
- `sendEvent()` 走 `0x31` 实时通道，适合 UI 事件、轻量状态同步、低延迟交互；它不是高吞吐二进制传输接口。
- 客户端收到服务端实时事件时，会向 Web 页面派发 `afediumserverpageevent`。
- 只有页面定义中包含 `bridge_permissions: ["stream"]` 时，客户端才允许调用 `openStream()`, `sendStreamMessage()`, `sendStreamBinary()`, `closeStream()`。
- `openStream()` / `sendStreamMessage()` / `closeStream()` 映射到 `stream_channel` 的 `0x32` 文本控制帧。
- `sendStreamBinary()` 发送 `0x32` 二进制帧。客户端收到同一会话的服务端文本事件时派发 `afediumstreammessage`，收到二进制帧时派发 `afediumstreambinary`。
- 服务端到客户端的视频流可复用同一套事件：页面先用 `openStream()` 打开会话，再用 `sendStreamMessage(sessionId, "start", config)` 请求服务端开始推流；服务端随后通过 `0x32` 二进制帧发送编码后的帧，客户端 Web runtime 派发 `afediumstreambinary`，页面负责解码并渲染。

Web 页面示例：

```html
<script>
  async function refresh() {
    const result = await window.afediumBridge.invoke("echo", {
      source: "web_runtime"
    });
    console.log(result);
  }
</script>
```

监听服务端实时事件：

```html
<script>
  window.addEventListener("afediumserverpageevent", function (event) {
    console.log(event.detail.event, event.detail.state_patch);
  });
</script>
```

客户端实现要求：

- 注入 `afediumBridge` 的失败不应被主题注入、样式注入或页面脚本错误影响。
- Windows 客户端可在内部使用 WebView2 `chrome.webview.postMessage`；移动端可在内部使用 `AfediumBridge` JavaScript channel。但页面代码必须统一使用 `window.afediumBridge`。

##### 主题颜色注入

客户端应向 Web runtime 注入主题运行时，使服务端页面跟随客户端主题。主题运行时必须把客户端传入的颜色写入 `document.documentElement.style` 上的 CSS 变量；页面 CSS 只读取 `var(--af-*)`，不需要也不应该直接调用 WebView2、Android WebView 等平台桥接对象。

| CSS 变量 | 含义 |
|----------|------|
| `--af-background` | 页面背景 |
| `--af-surface` | 主表面 |
| `--af-surface-alt` | 次级表面 |
| `--af-text` | 主文字 |
| `--af-text-muted` | 弱化文字 |
| `--af-primary` | 主色 |
| `--af-on-primary` | 主色上的文字 |
| `--af-border` | 边框 |
| `--af-error` | 错误色 |
| `--af-on-error` | 错误色上的文字 |
| `--af-success` | 成功色 |
| `--af-on-success` | 成功色上的文字 |
| `--af-warning` | 警告色 |
| `--af-on-warning` | 警告色上的文字 |
| `--af-primary-soft` | 主色弱背景 |
| `--af-error-soft` | 错误弱背景 |
| `--af-success-soft` | 成功弱背景 |
| `--af-warning-soft` | 警告弱背景 |

客户端还应注入 `window.afediumTheme`：

```js
window.afediumTheme = {
  mode: "light",
  colors: {
    background: "#F4F6F8",
    surface: "#FFFFFF",
    surfaceAlt: "#EFF2F6",
    text: "#111827",
    textMuted: "#6B7280",
    primary: "#2563EB",
    onPrimary: "#FFFFFF",
    border: "#D5DCE5",
    error: "#B42318",
    onError: "#FFFFFF",
    success: "#027A48",
    onSuccess: "#FFFFFF",
    warning: "#B54708",
    onWarning: "#FFFFFF",
    primarySoft: "#E8F0FF",
    errorSoft: "#FFF0F0",
    successSoft: "#ECFDF3",
    warningSoft: "#FFF6E8"
  }
}
```

`mode` 取值为 `"light"` 或 `"dark"`。客户端可以提供类似 `window.afediumThemeBridge.apply(theme)` 的内部更新入口；该入口负责更新 `window.afediumTheme`、刷新根节点 CSS 变量，并派发主题变化事件。页面作者应把它视为客户端运行时能力，不要替换或绕过它。

当客户端主题变化且 WebView 仍在当前页面内时，客户端应重新传入最新主题，更新 CSS 变量和 `window.afediumTheme`，并派发：

```js
window.dispatchEvent(new CustomEvent("afediumthemechange", {
  detail: window.afediumTheme
}));
```

页面 CSS 应优先使用 `--af-*` 变量：

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

#### 4.4 `open_instance`

请求：

```json
{
  "feature_id": "server_pages",
  "method": "open_instance",
  "payload": {
    "page_id": "server_pages_demo.status"
  }
}
```

响应：

```json
{
  "instance_id": "...",
  "state": {
    "opened_at": 1710000000.0,
    "page_id": "server_pages_demo.status"
  },
  "realtime": {
    "control_code": "0x31",
    "push_uid": "PUSH"
  }
}
```

客户端行为：

- `instance_id` 只对创建它的客户端有效。
- 打开后客户端可在后续 `invoke` 中复用该实例。
- 如果页面需要实时同步，打开实例后客户端应通过 `0x31` 发送 `op: "subscribe"`。

#### 4.5 `invoke`

请求：

```json
{
  "feature_id": "server_pages",
  "method": "invoke",
  "payload": {
    "instance_id": "...",
    "action": "refresh_status",
    "payload": {}
  }
}
```

当前支持动作：

| 动作               | 说明         |
|------------------|------------|
| `refresh_status` | 示例页面动作：刷新服务端状态 |
| `echo`           | 示例页面动作：回传 payload |
| `start_realtime_push` | 示例页面动作：启动 `0x31 PUSH` 主动推送 |
| `stop_realtime_push` | 示例页面动作：停止 `0x31 PUSH` 主动推送 |

响应通常是：

```json
{
  "state_patch": {
    "message": "服务端状态已刷新"
  }
}
```

客户端行为：

- `invoke` 前必须确保实例属于当前客户端。
- `state_patch` 表示局部状态更新，不一定覆盖完整状态。
- 页面 provider 的 `invoke_handler` 可以通过 `context["emit"](event, payload, state_patch)` 主动向当前实例发送 `0x31 PUSH`。
- 不要把 `invoke` 当成通用远程执行接口。

#### 4.6 `close_instance`

请求：

```json
{
  "feature_id": "server_pages",
  "method": "close_instance",
  "payload": {
    "instance_id": "..."
  }
}
```

响应：

```json
{
  "closed": true
}
```

客户端行为：

- 关闭实例后不要继续复用旧 `instance_id`。
- 客户端切换页面前最好先关闭旧实例。

#### 4.7 实时双向同步：`0x31`

`server_pages` 的实时通道用于页面实例级状态同步和轻量事件，不经过 `0x30` Feature RPC。

订阅：

```text
chr(0x31) + UID + JSON
```

```json
{
  "op": "subscribe",
  "instance_id": "..."
}
```

客户端事件：

```json
{
  "op": "event",
  "instance_id": "...",
  "event": "ping",
  "payload": {
    "source": "web_runtime"
  }
}
```

状态补丁：

```json
{
  "op": "state_patch",
  "instance_id": "...",
  "state_patch": {
    "value": 1
  }
}
```

服务端主动下发：

```text
chr(0x31) + "PUSH" + JSON_ENVELOPE
```

`data` 示例：

```json
{
  "event": "state_patch",
  "instance_id": "...",
  "page_id": "server_pages_demo.status",
  "provider_id": "server_pages_demo",
  "payload": {},
  "state_patch": {
    "message": "实时事件已收到"
  },
  "sequence": 1,
  "timestamp": 1710000000.0
}
```

客户端行为：

- 按 `instance_id` 过滤事件。
- 用 `sequence` 去重或丢弃过期事件。
- `state` 表示完整状态；`state_patch` 表示局部合并。
- Web runtime 中把事件派发为 `window` 的 `afediumserverpageevent`。
- 典型模式是：页面先通过 `invoke("start_realtime_push")` 或类似动作启动服务端任务，后续由服务端线程或回调通过 `PUSH` 主动下发增量事件。

#### 4.8 高性能流通道：`0x32`

`stream_channel` 是系统 Extended Feature，用于插件注册面向大吞吐或低延迟场景的流 provider。它为未来双向音视频流保留了二进制传输路径。

Feature manifest：

```json
{
  "id": "stream_channel",
  "title": "流通道",
  "standard": "extended",
  "transport": "external_control",
  "protocol_codes": ["0x32"],
  "permissions": [
    "stream_channel.control",
    "stream_channel.binary"
  ]
}
```

文本控制帧：

```text
chr(0x32) + UID + JSON
```

打开会话：

```json
{
  "op": "open",
  "provider_id": "example_stream",
  "metadata": {
    "kind": "audio"
  }
}
```

发送控制消息：

```json
{
  "op": "message",
  "session_id": "...",
  "event": "configure",
  "payload": {}
}
```

关闭会话：

```json
{
  "op": "close",
  "session_id": "..."
}
```

二进制数据帧：

```text
byte(0x32) + byte(session_id_length) + session_id_utf8 + payload_bytes
```

客户端行为：

- WebSocket 层必须暴露二进制流，不要把二进制帧 UTF-8 解码到文本消息流。
- 音视频、传感器高速采样、大块实时数据应使用 `0x32`，不要走 `0x30` JSON/Base64。
- 轻量 UI 同步、按钮触发后的进度回报、服务端主动消息提示应优先使用 `0x31`，不要为这类场景额外打开 `0x32` 会话。
- `0x32` 只定义传输和会话，具体编解码、背压、丢帧策略由 provider 的 feature 文档补充。

## 5. 外部控制码扩展

Extended Feature 也可以直接注册自定义外部控制码，但这只适合 Feature RPC 不方便表达的场景，例如二进制流、低延迟消息或历史协议兼容。

规则：

- 不得占用 `0x00` 到 `0x1F`、`0x30`、`0x31`、`0x32`、`0xFF`
- 必须通过 `lib.external_control.register_control_code()` 注册
- 必须在 `teardown()` 中注销
- 如果使用额外控制码，manifest 应声明 `protocol_codes`

客户端如果看到 `protocol_codes` 中包含额外控制码，必须按该 feature 的专用文档实现，不要猜测 payload 结构。

## 6. 兼容规则

- `feature_id` 发布后不得随意重命名
- `method` 的请求和响应结构应保持向后兼容
- 新增字段必须是可选字段
- 删除方法、改变字段类型或改变权限语义属于破坏性变更
- 插件禁用或卸载后，对应 feature 必须从 `0x01` feature 列表中消失
- 客户端不得假设某个 Extended Feature 一定存在，必须以 `0x01` 返回值为准
