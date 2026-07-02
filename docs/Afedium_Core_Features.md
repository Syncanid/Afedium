# Afedium Core Features

本文档面向 Afedium 客户端开发者，定义 Core Feature 的发现方式、可选性、控制码、帧格式和通信行为。

## 1. 客户端兼容原则

客户端必须先完成 WebSocket 连接和认证，然后读取 feature 列表，再决定启用哪些能力。

重要规则：

- `server_banner` 是唯一必选 Feature。
- 除 `server_banner` 外，所有 Core Feature 和 Extended Feature 都是可选能力。
- 客户端不得仅凭协议版本假设某个 Feature 存在。
- Feature 缺失时，客户端应隐藏或禁用对应 UI，不应继续探测对应控制码。
- 客户端必须忽略 manifest 中未知字段。
- 文本 payload 使用 UTF-8；除特别说明外，路径和参数不支持空格。

`0x01` 是 feature 发现控制码，不作为业务 Feature 计入可选能力。客户端应在认证完成后立即调用它读取服务端实际可用能力。

## 2. 通用帧格式

每条业务消息的第一个字节是控制码。

大多数请求使用：

```text
chr(control_code) + UID + payload
```

其中：

- `control_code`：1 字节控制码。
- `UID`：4 字节/4 字符客户端请求 ID，用于并发匹配。
- `payload`：控制码自定义内容。

所有 Core 控制码响应使用：

```text
chr(control_code) + UID + JSON_ENVELOPE
```

`JSON_ENVELOPE` 统一为：

```json
{
  "ok": true,
  "data": {},
  "message": "",
  "error": null
}
```

失败时 `ok=false`，错误信息放入 `error.code`、`error.message` 和可选 `error.details`。文件传输控制码 `0x10` 到 `0x16` 也必须回显
UID；持续传输事件额外通过 `data.session_id` 关联上传/下载会话。

## 3. Feature 发现：`0x01`

### 请求

```text
chr(0x01) + UID
```

### 响应

```text
chr(0x01) + UID + JSON_ENVELOPE
```

响应 envelope 的 `data` 是 feature id 到 manifest 的映射：

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
    },
    "file_transfer": {
      "title": "文件传输",
      "standard": "core",
      "protocol_codes": [
        "0x10",
        "0x11",
        "0x12",
        "0x13",
        "0x14",
        "0x15",
        "0x16"
      ]
    }
  },
  "message": "",
  "error": null
}
```

客户端处理要求：

- 如果 `server_banner` 缺失，客户端应认为该服务端不兼容。
- 其他 Feature 缺失不代表服务端错误，只代表该能力当前不可用。
- `standard` 为 `core` 的能力按本文档解析。
- `standard` 为 `extended` 的能力按 `Afedium_Extended_Features.md` 解析。

## 4. `server_banner` 必选 Feature

`server_banner` 用于获取服务端首页状态、Banner 和展示元数据。

Manifest：

```json
{
  "title": "服务端 Banner",
  "standard": "core",
  "protocol_codes": ["0x02"]
}
```

### 4.1 `0x02` 服务端 Banner / 快照

#### 请求

```text
chr(0x02) + UID
```

#### 响应

```text
chr(0x02) + UID + JSON_ENVELOPE
```

当前快照位于 envelope 的 `data` 字段中，示例：

```json
{
  "/T/V系统": "Windows",
  "/APython版本: ": "3.11.0",
  "/P在线模式": true,
  "/P/AGit可用": true,
  "/L/A访问项目": "https://github.com/furryaxw/AFEDIUM/",
  "/C/Vmodules": {
    "core": true
  }
}
```

键名前缀是展示提示，客户端可按需渲染：

| 前缀   | 建议展示行为   |
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

多个前缀可以连写，例如 `/P/B/状态名`。真实 `/` 字符使用 `//` 转义。

## 5. `server_variables` 可选 Feature

`server_variables` 用于按白名单读取或写入服务端变量。

Manifest：

```json
{
  "title": "服务端变量访问",
  "standard": "core",
  "protocol_codes": ["0x04"]
}
```

### 5.1 `0x04` 信息读取和写入

#### `get` 请求

```text
chr(0x04) + UID + "get " + path
```

`path` 是空格分隔路径或特殊短指令。

常用请求：

| payload                   | 说明                          |
|---------------------------|-----------------------------|
| `get plugins`             | 返回已加载插件 id 列表 JSON          |
| `get command`             | 返回命令树顶层命令 JSON              |
| `get info <pyz_file>`     | 返回指定 PYZ 模块的 `info.json` 信息 |
| `get static features`     | 返回当前 feature 列表 JSON        |
| `get static running`      | 返回运行状态 JSON                 |
| `get static modules`      | 返回本地模块列表 JSON               |
| `get dynamic <module_id>` | 返回模块动态状态 JSON               |
| `get loaded_plugins`      | 返回已加载插件对象信息，可能不适合普通 UI 展示   |

#### `get` 响应

```text
chr(0x04) + UID + JSON_ENVELOPE
```

成功时 `data` 是服务端返回的值，可以是对象、数组、字符串或基础类型；失败时 `ok=false`，错误信息放入 `error`。

客户端建议：

- 只解析 envelope，不再按纯文本或二次 JSON 字符串解析控制码响应。
- 根据 `ok` 判断业务成功或失败。
- 不要读取未列入白名单根节点的路径；允许根节点为 `static`、`dynamic`、`loaded_plugins`。

#### `set` 请求

```text
chr(0x04) + UID + "set " + path + " " + value
```

`value` 会先尝试按 JSON 解析，失败时按原始字符串写入。

示例：

```text
chr(0x04) + UID + "set static online true"
chr(0x04) + UID + "set dynamic demo {\"enabled\":true}"
```

#### `set` 响应

```text
chr(0x04) + UID + JSON_ENVELOPE
```

成功时 `data` 由 `set_info_handler` 决定，通常是字符串或对象；失败时 `ok=false`。

客户端建议：

- JSON 值应使用无空格的紧凑格式。
- 不要尝试覆盖根节点，例如 `set static {...}`。
- 写入前最好先用 `get` 读取现有值，确认目标路径存在且类型符合预期。

## 6. `terminal` 可选 Feature

`terminal` 通过命令树执行服务端命令。它是可选能力，只有 feature 列表声明 `terminal` 时客户端才应显示终端入口。

Manifest：

```json
{
  "title": "终端",
  "standard": "core",
  "protocol_codes": [
    "0x03"
  ]
}
```

### `0x03` 命令执行

#### 请求

```text
chr(0x03) + UID + command_text
```

示例：

```text
chr(0x03) + UID + "help"
chr(0x03) + UID + "core reload"
chr(0x03) + UID + "module install example"
```

#### 响应

```text
chr(0x03) + UID + JSON_ENVELOPE
```

通信行为：

- `command_text` 按空白字符拆分为命令参数。
- 不支持 shell 风格引号、管道、重定向或环境变量展开。
- 服务端执行 Afedium 命令树，不直接执行系统 shell 字符串。
- 输出文本放在 `data` 字段中，可能包含多行。

客户端建议：

- 终端 UI 应读取 envelope 的 `data` 字段并按文本展示。
- 命令不存在时通常返回 `未知的指令。输入 'help' 获取可用指令列表。`。
- 对危险命令加二次确认，例如 `core quit`、`core reload`。

## 7. `system_upgrade` 可选 Feature

`system_upgrade` 表示服务端允许客户端触发核心升级。它复用 `0x03` 命令通道，不分配单独控制码。

Manifest：

```json
{
  "title": "系统升级",
  "standard": "core",
  "protocol_codes": [
    "0x03"
  ]
}
```

### 使用方式

当 feature 列表同时声明 `system_upgrade` 和 `terminal` 时，客户端可发送：

```text
chr(0x03) + UID + "core upgrade"
```

响应仍按 `0x03` 的 JSON envelope 解析，命令输出文本放入 `data`。

客户端建议：

- 如果只有 `terminal` 而没有 `system_upgrade`，不要显示升级按钮。
- 升级可能触发服务端重启或连接断开，客户端应准备重连。
- 升级失败也必须通过 `0x03` 的 JSON envelope 返回，命令输出或错误文本放入 `data` 或 `error.message`。

## 8. `module_mgmt` 可选 Feature

`module_mgmt` 负责模块仓库同步、安装、升级和本地模块状态读取。

Manifest：

```json
{
  "title": "模块管理",
  "standard": "core",
  "protocol_codes": ["0x03", "0x04"]
}
```

通信行为：

- 模块命令通过 `0x03` 执行，例如 `module update`、`module install <id>`、`module upgrade <id>`。
- 本地模块状态通过 `0x04` 读取，例如 `get static modules`。
- 客户端应把在线模块元数据和本地安装状态分开处理。

## 9. `file_transfer` 可选 Feature

`file_transfer` 提供文件上传、下载、分块传输和校验。它是可选能力，只有 feature 列表声明 `file_transfer` 时客户端才应启用文件传输
UI。

Manifest：

```json
{
  "title": "文件传输",
  "standard": "core",
  "protocol_codes": [
    "0x10",
    "0x11",
    "0x12",
    "0x13",
    "0x14",
    "0x15",
    "0x16"
  ]
}
```

### 文件传输通用规则

- 所有 `0x10` 到 `0x16` 的请求和响应都使用 `chr(code) + UID + payload` 格式；响应 payload 必须是 `JSON_ENVELOPE`。
- 路径参数不支持空格。
- 文件内容使用 Base64 文本传输。
- 大文件分块大小为 `1000 * 1024` 字节。
- 客户端应使用 SHA-256 校验完整文件。
- `0x14` 是服务端下行事件控制码，事件也必须回显原下载会话的 UID，并把事件类型放入 `data.event`。

### 7.1 `0x10` 上传握手或小文件上传

#### 小文件上传，不带校验

请求：

```text
chr(0x10) + UID + file_path + " " + base64_data
```

响应：

```text
chr(0x10) + UID + JSON_ENVELOPE
```

成功时 `data`：

```json
{
  "path": "./demo.txt",
  "saved": true
}
```

#### 小文件上传，带校验

请求：

```text
chr(0x10) + UID + file_path + " " + base64_data + " " + expected_sha256 + " " + encoding
```

`encoding` 当前只作为占位字段，服务端不会使用它。

校验失败时 `ok=false`：

```json
{
  "ok": false,
  "data": null,
  "message": "校验和不匹配: ./demo.txt",
  "error": {
    "code": "checksum_mismatch",
    "message": "校验和不匹配: ./demo.txt",
    "details": {
      "path": "./demo.txt",
      "expected": "<expected_sha256>",
      "actual": "<calculated_sha256>"
    }
  }
}
```

#### 大文件上传握手

请求：

```text
chr(0x10) + UID + file_path + " " + chunk_count + " " + expected_sha256
```

响应 `data`：

```json
{
  "session_id": "<uuid>",
  "path": "./large.bin",
  "chunk_count": 8
}
```

客户端收到 `session_id` 后，用 `0x11` 逐块上传。

### 7.2 `0x11` 上传分块

请求：

```text
chr(0x11) + UID + session_id + " " + chunk_index + " " + base64_chunk
```

行为：

- `chunk_index` 从 `0` 开始。
- 每个分块都有 JSON envelope 响应。
- 服务端收到全部分块后组装文件并返回最终结果。

中间分块 `data`：

```json
{
  "event": "chunk_received",
  "session_id": "<uuid>",
  "chunk_index": 0,
  "received": 1,
  "total": 8
}
```

最终成功 `data`：

```json
{
  "event": "complete",
  "session_id": "<uuid>",
  "sha256": "<calculated_sha256>",
  "path": "./large.bin"
}
```

### 7.3 `0x12` 下载请求

请求：

```text
chr(0x12) + UID + file_path
```

小文件响应 `data`：

```json
{
  "mode": "single",
  "encoding": "binary",
  "content_base64": "<base64_data>",
  "sha256": "<sha256>",
  "size": 123,
  "path": "./demo.bin"
}
```

`encoding` 可能是 `binary`、`utf-8` 或 `gbk`。

大文件响应 `data`：

```json
{
  "mode": "chunked",
  "session_id": "<uuid>",
  "chunk_count": 8,
  "sha256": "<file_sha256>",
  "size": 7340032,
  "path": "./large.bin"
}
```

客户端按 `data.mode` 判断 `single` 或 `chunked`，不再解析冒号拼接字符串。

### 7.4 `0x13` 确认开始下载

请求：

```text
chr(0x13) + UID + session_id
```

响应：

```text
chr(0x13) + UID + JSON_ENVELOPE
```

成功时 `data`：

```json
{
  "session_id": "<uuid>",
  "started": true
}
```

服务端随后用 `0x14` 推送分块事件。

### 7.5 `0x14` 下载分块

分块事件：

```text
chr(0x14) + UID + JSON_ENVELOPE
```

`data` 示例：

```json
{
  "event": "chunk",
  "session_id": "<uuid>",
  "chunk_index": 0,
  "content_base64": "<base64_chunk>"
}
```

结束事件：

```json
{
  "event": "complete",
  "session_id": "<uuid>"
}
```

客户端应按 `data.chunk_index` 组装文件，并用 `0x12` 大文件响应中的 `data.sha256` 校验。

### 7.6 `0x15` 请求重传下载分块

请求：

```text
chr(0x15) + UID + session_id + " " + missing_indices_json
```

示例：

```text
chr(0x15) + UID + session_id + " [0,2,5]"
```

响应 `data`：

```json
{
  "session_id": "<uuid>",
  "requested_indices": [
    0,
    2,
    5
  ]
}
```

服务端随后重新发送缺失分块，并在结束时通过 `0x14` 发送 `data.event="complete"`。

### 7.7 `0x16` 取消下载

请求：

```text
chr(0x16) + UID + session_id
```

响应 `data`：

```json
{
  "session_id": "<uuid>",
  "cancelled": true
}
```

## 10. `file_management` 可选 Feature

`file_management` 提供目录浏览和基础文件操作。它是可选能力，只有 feature 列表声明 `file_management` 时客户端才应启用文件管理
UI。

Manifest：

```json
{
  "title": "文件管理",
  "standard": "core",
  "protocol_codes": [
    "0x18",
    "0x19",
    "0x1A",
    "0x1B",
    "0x1C"
  ]
}
```

本组控制码使用通用 UID 格式。

### 8.1 `0x18` 删除文件或目录

请求：

```text
chr(0x18) + UID + target_path
```

响应：

```text
chr(0x18) + UID + JSON_ENVELOPE
```

成功时 `data`：

```json
{
  "path": "./old",
  "deleted": true,
  "kind": "directory"
}
```

如果目标是目录，服务端会递归删除。

### 8.2 `0x19` 列出目录

请求：

```text
chr(0x19) + UID + directory_path
```

`directory_path` 为空时使用 `./`。

响应：

```text
chr(0x19) + UID + JSON_ENVELOPE
```

成功时 `data`：

```json
{
  "config": "drwx",
  "core.py": "frw-"
}
```

权限字符串格式：

- 第一位：`d` 表示目录，`f` 表示文件。
- 后三位：`r`、`w`、`x` 或 `-`。

### 8.3 `0x1A` 文件状态

请求：

```text
chr(0x1A) + UID + path
```

响应：

```text
chr(0x1A) + UID + JSON_ENVELOPE
```

成功时 `data`：

```json
{
  "size": 123,
  "mtime": 1710000000.0,
  "ctime": 1710000000.0,
  "mode": 33206,
  "uid": 0,
  "gid": 0
}
```

失败时返回 `ok=false`：

```json
{
  "ok": false,
  "data": null,
  "message": "<错误说明>",
  "error": {
    "code": "stat_failed",
    "message": "<错误说明>",
    "details": null
  }
}
```

### 8.4 `0x1B` 创建目录

请求：

```text
chr(0x1B) + UID + folder_path
```

响应：

```text
chr(0x1B) + UID + JSON_ENVELOPE
```

成功时 `data`：

```json
{
  "path": "./new_folder",
  "created": true
}
```

服务端会创建缺失的父目录。

### 8.5 `0x1C` 重命名

请求：

```text
chr(0x1C) + UID + old_path + " " + new_path
```

响应：

```text
chr(0x1C) + UID + JSON_ENVELOPE
```

成功时 `data`：

```json
{
  "old_path": "./old",
  "new_path": "./new",
  "renamed": true
}
```

当前格式不支持带空格路径。

## 11. 客户端降级建议

客户端可以按以下顺序初始化：

1. 完成 WebSocket 连接和认证。
2. 调用 `0x01` 获取 feature 列表。
3. 确认 `server_banner` 存在；不存在则停止兼容流程。
4. 使用 `0x02` 获取状态快照。
5. 对 `server_variables`、`terminal`、`system_upgrade`、`module_mgmt`、`file_transfer`、`file_management` 按 feature 列表逐项启用 UI。

任何可选 Feature 缺失都不应影响基本状态页工作。
