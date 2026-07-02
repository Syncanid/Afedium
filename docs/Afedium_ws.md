# Afedium WebSocket 通信协议文档

协议版本：`v2.0`

本文档描述 Afedium 客户端与服务端之间的发现、认证、外部控制码和 Feature RPC 协议。`features` 只声明服务端实际实现并对外提供的功能，并由客户端在
WebSocket 握手和认证完成后主动请求。

---

## 1. UDP 服务发现

服务端会周期性发送 UDP 广播，客户端监听广播后再建立 WebSocket 会话。

| 项目   | 值                 |
|------|-------------------|
| 广播地址 | `255.255.255.255` |
| 默认端口 | `12840`           |
| 编码   | UTF-8 JSON        |

### 1.1 广播字段

| 字段             | 类型      | 必须 | 说明                   |
|----------------|---------|----|----------------------|
| `server_name`  | String  | 是  | 服务端展示名称              |
| `service`      | String  | 是  | 固定为 `afedium_server` |
| `ip`           | String  | 是  | 服务端 IPv4 地址          |
| `port`         | Integer | 是  | WebSocket 端口         |
| `UUID`         | String  | 是  | 服务端唯一标识              |
| `using_auth`   | Boolean | 是  | 是否启用认证               |
| `auth_timeout` | Integer | 是  | 认证超时秒数               |
| `timestamp`    | String  | 是  | ISO 8601 时间          |

### 1.2 广播示例

```json
{
  "server_name": "afedium_ws",
  "service": "afedium_server",
  "ip": "192.168.1.100",
  "port": 11840,
  "UUID": "a1b2c3d4-e5f6-7890-1234-567890abcdef",
  "using_auth": true,
  "auth_timeout": 60,
  "timestamp": "2026-06-25T10:00:00.000000"
}
```

---

## 2. WebSocket 认证

客户端建立 WebSocket 连接后，如果服务端启用了认证，服务端会先进入认证流程。认证流程只使用 `0xFF`，payload 为 JSON。

| 控制码    | 方向 | 说明                     |
|--------|----|------------------------|
| `0xFF` | 双向 | 认证 challenge、认证输入和认证结果 |

服务端 challenge：

```json
{
  "stage": "challenge",
  "auth_type": "password",
  "timeout": 60,
  "message": "请输入密码"
}
```

客户端响应：

```json
{
  "answer": "afedium"
}
```

服务端结果：

```json
{
  "stage": "ok",
  "message": "认证成功"
}
```

或：

```json
{
  "stage": "error",
  "message": "认证失败"
}
```

认证完成后，WebSocket 消息进入外部控制码协议。客户端应首先使用 `0x01` 拉取服务端 feature 列表。

---

## 3. 外部控制码协议

每条业务消息的第一个字节是控制码。后续内容由对应控制码定义。常规请求使用 `控制码 + 4 字节 UID + payload`，服务端响应使用相同控制码和
UID，并统一返回 JSON envelope，便于客户端并发匹配和错误处理。

### 3.1 内置控制码

| 控制码           | Feature                                       | 说明                             |
|---------------|-----------------------------------------------|--------------------------------|
| `0x01`        | `feature_registry`                            | 获取服务端 feature 列表               |
| `0x02`        | `server_banner`                               | 获取服务端 Banner 和状态快照             |
| `0x03`        | `terminal` / `system_upgrade` / `module_mgmt` | 执行终端/命令树指令，系统升级和模块命令也通过命令树触发   |
| `0x04`        | `server_variables` / `module_mgmt`            | 按白名单读取或写入服务端变量，模块管理也用它读取本地模块状态 |
| `0x05`-`0x0F` | 保留                                            | 系统保留                           |
| `0x10`        | `file_transfer`                               | 上传握手或小文件上传                     |
| `0x11`        | `file_transfer`                               | 上传分块                           |
| `0x12`        | `file_transfer`                               | 文件下载请求                         |
| `0x13`        | `file_transfer`                               | 确认开始下载                         |
| `0x14`        | `file_transfer`                               | 下载分块，服务端下行                     |
| `0x15`        | `file_transfer`                               | 请求重传下载分块                       |
| `0x16`        | `file_transfer`                               | 取消下载                           |
| `0x17`        | 保留                                            | 文件传输扩展预留                       |
| `0x18`        | `file_management`                             | 删除文件或目录                        |
| `0x19`        | `file_management`                             | 列出目录                           |
| `0x1A`        | `file_management`                             | 文件状态                           |
| `0x1B`        | `file_management`                             | 创建目录                           |
| `0x1C`        | `file_management`                             | 重命名                            |
| `0x1D`-`0x1F` | 保留                                            | 文件管理扩展预留                       |
| `0x30`        | `feature_rpc`                                 | Feature RPC 共享控制接口             |
| `0x31`        | `server_pages`                                | Server Pages 实时状态/事件通道        |
| `0x32`        | `stream_channel`                              | 高吞吐文本控制 + 二进制流通道           |

插件可以通过 `lib.external_control.register_control_code` 注册额外控制码。控制码同一时间只能有一个所有者，插件卸载时必须注销自己的控制码。
插件控制码 handler 如果直接返回普通 Python 值，框架会自动封装为当前控制码和 UID 对应的 JSON envelope；如果 handler
手动返回完整帧，也必须保持 `chr(code) + UID + JSON_ENVELOPE` 格式。

### 3.2 统一响应 envelope

除认证 `0xFF` 外，所有控制码响应必须使用以下格式：

```text
chr(code) + UID + JSON_ENVELOPE
```

成功响应：

```json
{
  "ok": true,
  "data": {},
  "message": "",
  "error": null
}
```

失败响应：

```json
{
  "ok": false,
  "data": null,
  "message": "错误说明",
  "error": {
    "code": "bad_request",
    "message": "错误说明",
    "details": null
  }
}
```

`data` 可以是对象、数组、字符串、数字、布尔值或 `null`。客户端不得再按纯文本或“JSON 字符串再套字符串”的形式解析控制码响应。

### 3.3 Feature 列表

客户端必须在 WebSocket 握手和认证完成后主动请求 feature 列表，UDP 广播不携带 `features`。

请求格式：

```text
chr(0x01) + UID
```

响应格式：

```text
chr(0x01) + UID + JSON_ENVELOPE
```

成功时 `data` 为服务端当前注册的 features 映射：

```json
{
  "ok": true,
  "data": {
    "server_banner": {
      "title": "服务端 Banner",
      "standard": "core",
      "protocol_codes": ["0x02"]
    },
    "server_variables": {
      "title": "服务端变量访问",
      "standard": "core",
      "protocol_codes": ["0x04"]
    },
    "module_mgmt": {
      "title": "模块管理",
      "standard": "core",
      "protocol_codes": ["0x03", "0x04"]
    }
  },
  "message": "",
  "error": null
}
```

失败时：

```json
{
  "ok": false,
  "data": null,
  "message": "Feature 列表读取失败",
  "error": {
    "code": "bad_request",
    "message": "Feature 列表读取失败",
    "details": null
  }
}
```

---

## 4. Feature RPC

Feature RPC 固定使用控制码 `0x30`，不经过 `0x03` 指令通道。`0x30` 是共享控制接口，不属于 `server_pages` 或任何单个
feature；具体目标由请求体中的 `feature_id` 决定。

### 4.1 请求格式

```text
chr(0x30) + UID + JSON
```

JSON 请求体：

```json
{
  "feature_id": "example",
  "method": "manifest",
  "payload": {}
}
```

### 4.2 响应格式

```text
chr(0x30) + UID + JSON_ENVELOPE
```

统一 envelope 形状：

```json
{
  "ok": true,
  "data": {},
  "message": "",
  "error": null
}
```

失败响应：

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

---

## 5. Core 与 Extended Features

Core 是服务端基础功能集合，其中只有 `server_banner` 被视为必选；其他 Core Feature 和所有 Extended Feature 都是可选能力。Extended
是可删除、可替换、可由插件提供的服务端扩展功能集合。详细规范见 [Afedium_Core_Features.md](Afedium_Core_Features.md)
和 [Afedium_Extended_Features.md](Afedium_Extended_Features.md)。

### 5.1 Core

| Feature            | 说明                |
|--------------------|-------------------|
| `server_banner`    | 服务端 Banner 和状态快照  |
| `server_variables` | 服务端变量访问           |
| `terminal`         | 终端命令能力            |
| `module_mgmt`      | 模块仓库同步、安装、升级和状态读取 |
| `file_transfer`    | 文件上传、下载、分块传输      |
| `file_management`  | 删除、列出、状态、创建目录、重命名 |
| `system_upgrade`   | 服务端系统升级能力         |

### 5.2 Extended

| Feature        | 说明                          |
|----------------|-----------------------------|
| `server_pages` | 服务端定义页面聚合器，通过共享 Feature RPC 调用 |
| `stream_channel` | 面向插件的高性能流会话通道，支持二进制帧 |

`server_pages` 是唯一的页面聚合 Feature，多个插件可把页面注册到该聚合器。页面清单、实例生命周期、Web runtime 桥接和主题颜色注入规范见
[Afedium_Extended_Features.md](Afedium_Extended_Features.md)。客户端应把 `server_pages.manifest.pages` 中的页面直接挂到
Server Info / 服务端详情页，不需要新增单独的“服务端页面”父入口。

### 5.3 `0x31` Server Pages 实时通道

`0x31` 使用常规文本帧：

```text
chr(0x31) + UID + JSON
```

客户端打开 `server_pages` 实例后可发送：

```json
{
  "op": "subscribe",
  "instance_id": "..."
}
```

之后服务端可主动下发：

```text
chr(0x31) + "PUSH" + JSON_ENVELOPE
```

`data` 包含 `instance_id`, `event`, `payload`, `state_patch`, `sequence`, `timestamp`。客户端应按 `instance_id` 过滤事件，并把 `state_patch` 合并到该实例状态。

### 5.4 `0x32` 流通道

`0x32` 有两种帧：

- 文本控制帧：`chr(0x32) + UID + JSON`，用于 `open`, `message`, `close`。
- 二进制数据帧：`byte(0x32) + byte(session_id_length) + session_id_utf8 + payload_bytes`。

流通道用于大吞吐、低延迟或未来音视频类数据，不应把这类负载塞进 `0x30` JSON RPC 或 Base64 字段。客户端必须保留 WebSocket 二进制帧，不要把 `List<int>` 强制 UTF-8 解码。

---

## 6. `0x02` 格式化协议

`0x02` 返回的 JSON 键名可以带格式化前缀，客户端可根据前缀决定展示方式。

| 前缀   | 说明       |
|------|----------|
| 无    | 默认标签和值   |
| `/T` | 主标题      |
| `/S` | 副标题      |
| `/B` | 加粗       |
| `/I` | 斜体       |
| `/P` | 状态点      |
| `/K` | 单值状态指示   |
| `/C` | 状态芯片组    |
| `/L` | 链接       |
| `/A` | 追加到前一个项目 |
| `/H` | 隐藏       |
| `/V` | 仅显示值     |

多个前缀可连写，例如 `/P/B/状态名`。真实 `/` 字符使用 `//` 转义。
