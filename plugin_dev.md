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
