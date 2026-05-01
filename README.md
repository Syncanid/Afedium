# AFEDIUM

**Advanced Fursuit of Electrical Device and Information Universal Manager**

AFEDIUM 是一个为 Linux 系统设计的模块化远程管理平台，支持快速部署、多功能拓展和高可用性。它允许你通过 WebSocket 远程控制设备、查看状态、管理文件，适用于电子眼显示等场景。

---

## ✨ 核心特性

* **🔌 模块化设计**
  功能模块可动态加载、卸载，互不干扰，独立运行。

* **🌐 WebSocket 远程控制**
  内置 WebSocket 服务，支持安全连接与远程操作。

* **🔒 灵活认证机制**
  支持免密、固定密码、动态验证码等多种认证方式。

* **🖥️ 远程终端访问**
  可执行后端命令，实时返回输出结果。

* **📂 全功能文件管理**
  支持远程浏览、上传、下载、删除、重命名、移动，支持大文件分块与校验。

* **⚙️ 状态监控与控制**
  实时查看或修改系统与模块状态，方便调试。

* **🔄 自动依赖管理**
  模块启动时自动安装缺失的 Python 包或系统依赖。

* **🚀 在线更新机制**
  支持 Git 拉取更新并热重载，无需重启部署。

* **🎨 易于扩展**
  开发者可按需编写新模块，快速添加功能。

---

## 🏗️ 项目结构一览

项目结构清晰，便于理解与扩展：

```
Afedium/
├── config/              # 各类配置文件
├── lib/                 # 通用库和工具函数
├── logs/                # 日志输出目录
├── pyz_modules/         # 模块存放目录
├── system/              # 核心功能模块
├── plugin_data/         # 模块使用的静态资源
├── core.py              # 主程序入口
└── README.md            # 本文件
```

---

## 🚀 快速开始

### 环境依赖

* Python 3.9+
* pip（用于安装依赖）

### 安装运行

1. **克隆项目**

   ```bash
   git clone https://github.com/furryaxw/Afedium.git
   cd Afedium
   ```

2. **运行主程序**

   ```bash
   python3 core.py
   ```

   程序将自动安装所需依赖，并启动本地控制台和 WebSocket 服务。默认配置支持单显示器即插即用，无需额外设置。

---

## ⚙️ 配置

### 配置文件说明

| 配置文件               | 模块    | 功能简介                  |
|--------------------|-------|-----------------------|
| `main.json`        | 核心框架  | 设置调试模式、控制插件启用、设置下载镜像。 |
| `ws_server.json`   | 通信模块  | WebSocket 通讯设置。       |
| `module_mgmt.json` | 模块管理器 | 模块配置。                 |

---

## 📡 远程控制客户端

你可以通过 WebSocket 客户端远程连接 AFEDIUM：

* 推荐使用官方客户端 [Afedium Mobile](https://github.com/furryaxw/afedium_mobile)。
* 或参考文档 [Afedium_ws.md](Afedium_ws.md) 自行开发客户端。

---

## 🧩 自定义模块开发

如果你想开发自定义模块，请参考详细的开发文档和示例：  
👉 [AFEDIUM 模块开发文档](plugin_dev.md)  
👉 [AFEDIUM 模块模板仓库](https://github.com/furryaxw/Afedium_modules)

---

欢迎贡献和反馈！AFEDIUM 正在不断发展中，期待你的参与！

---

## License
本项目采用 [GPL-3.0 License](LICENSE) 开源许可。
