# AFEDIUM V2 混合图形渲染引擎 - 开发者指南

## 架构概览

AFEDIUM V2 的显示引擎采用了严格的**双进程物理隔离架构**，彻底打破了 Python GIL 的性能瓶颈：

* **主控进程 (Logic)**：运行你的 `main.py` 业务逻辑，处理网络请求和数据存储。它不接触任何图形 API，仅通过极速 IPC 管道向 GPU
  下发 JSON 状态流。
* **渲染进程 (Render)**：独立的 Pyglet 运行环境（OpenGL 3.3 Core Profile）。它在启动时自动完成**多屏硬件扫描与握手**
  ，并接管所有激活的物理显示器，维持稳定的 60FPS 刷新率。
* **独立窗口上下文 (`WindowContext`)**：每个物理屏幕拥有独立的数据空间和 Z-Index 图层管理器。
* **Z-Index 图层管理器**：严格的层级遮挡。系统弹窗永远在最上层 (`Z:999`)，高级模块和场景可以自由定义层级 (`Z:0~899`)。

---

## 轨道一：数据驱动模式 (面向常规开发)

如果你只需渲染 UI、展示 2D 图片或 3D 模型，**你不需要懂任何 OpenGL 知识**。引擎为你提供了开箱即用的构建器库。

### 1. 召唤系统级 UI 弹窗 (`SystemPopup`)

系统 UI 永远置顶，自带 Alpha 透明度混合，适合做表单、确认框和警告。目前默认渲染在主屏幕 (`screen_0`)。

```python
from lib.system_ui import SystemPopup


def my_logic(ctx, args):
    # 1. 实例化构建器
    popup = SystemPopup(title="危险操作验证")

    # 2. 流式添加组件
    popup.add_label("检测到异常网络波动。")
    popup.add_label("是否强行阻断连接？")

    # 3. 绑定回调函数 (运行在主进程，不会阻塞)
    def on_block(data):
        ctx.reply("已阻断连接！")
        popup.close()  # 手动关闭弹窗

    popup.add_button("立即阻断", on_block)
    popup.add_button("忽略", lambda d: popup.close())

    # 4. 一键推入 GPU 渲染队列
    popup.show()
```

### 2. 构建多屏 2D/3D 混合场景 (`Scene`)

引擎内置 VFS（虚拟文件系统），自动从 `.pyz` 压缩包内存中提取资源。

在 V2 架构中，场景构建引入了两个核心概念：

1. **`target_display` (目标显示器)**：决定场景渲染在哪个物理屏幕上。
2. **`element_id` (元素寻址)**：为每个添加的元素赋予唯一标识，用于后续的动态变换。

```python
from lib.scene_builder import Scene


def start_game(ctx, args):
    # 实例化场景，并将其路由到副屏 (screen_1)
    scene = Scene("my_game_scene", self.id, target_display="screen_1")

    # 添加 2D 背景，赋予 element_id="bg1" (Z-index 越小越靠底)
    scene.add_image("assets/bg.png", element_id="bg1", x=0, y=0, scale=1.0)

    # 添加 3D 模型，赋予 element_id="object" (支持 .obj，引擎会自动关联压缩包内的 .mtl 材质)
    # 提示：由于透视矩阵，建议将 Z 轴设置为负数将其推入视野内
    scene.add_model("assets/test.obj", element_id="object", z=-50, ry=3.14)

    # 推入 GPU 显存
    scene.show()
```

### 3. GPU 补间动画与动态变换 (Tweening)

**⚠️ 性能红线**：绝对禁止在主进程使用 `while` 和 `time.sleep()` 高频下发坐标！这会导致 IPC 管道被海量 JSON 塞满并引发严重卡顿。

请使用 Scene 提供的 GPU 动画接口，主进程只下发“意图”，由显卡完成逐帧插值：

```python
# 1. 瞬时状态修改 (传送)
scene.set_transform("test", x=100, z=-20)

# 2. 平滑过渡动画 (动画演算将完全在渲染进程内独立完成)
# 让坦克在 2.5 秒内平滑移动并缩放
scene.animate_to("test", duration=2.5, x=500, z=-10, scale=1.5, ry=0)
```

---

## 轨道二：沙盒注入模式 (面向骨灰级开发)

如果你需要跑自定义的 GLSL 着色器、粒子系统，你需要进行**“沙盒注入”**。

你编写的渲染类将被直接提取到**渲染子进程**的 OpenGL 上下文中执行。你拥有绝对的自由，但也必须承担管理显存的责任。

### 1. 编写原生视图脚本 (`view.py`)

在模块目录下创建独立的 Python 脚本（运行在子进程）：

```python
# view.py (运行在独立的 GPU 子进程中！)
import pyglet
from pyglet.gl import *


class MyHardcoreView:
    def __init__(self, theme_color="blue", **kwargs):
        """接收主进程传入的 init_kwargs"""
        self.theme = theme_color

    def on_mount(self):
        """生命周期：图层挂载时触发，用于加载贴图、编译 Shader"""
        self.batch = pyglet.graphics.Batch()
        # 主动向主进程打招呼
        self.send_to_main({"status": "ready", "msg": "沙盒渲染器已上线"})

    def on_message(self, data):
        """生命周期：接收主进程主动下发的指令"""
        if data.get("action") == "explode":
            print("播放爆炸特效！")

    def update(self, dt):
        """生命周期：每秒 60 次触发，用于处理物理演算"""
        pass

    def on_draw(self, window):
        """生命周期：每一帧触发。在这里尽情发挥！"""
        glEnable(GL_DEPTH_TEST)

        # 你的 OpenGL 绘制逻辑...

        # 规范：画完后请务必恢复状态，以免弄脏系统上下文！
        glDisable(GL_DEPTH_TEST)

    def on_destroy(self):
        """生命周期：模块关闭、切换或清屏时触发。
        ⚠️ 极度重要：必须在这里调用 glDelete 等方法释放显存！"""
        pass

    # --- 全局输入路由穿透机制 ---
    def on_mouse_press(self, x, y, button, modifiers):
        """如果上层图层没有拦截，事件会穿透到这里"""
        return pyglet.event.EVENT_HANDLED  # 吞噬事件
```

### 2. 通过 IPC 实施跨屏注入与传参

在主进程 (`main.py`) 中，将上述脚本送入指定屏幕的 GPU 管线，并传入初始化参数 `init_kwargs`：

```python
import os
from lib.common import static


def inject_my_engine(ctx, args):
    display = static.get("display")
    script_path = os.path.join(self.plugin_path, "view.py")

    display.send_cmd({
        "cmd": "load_advanced",
        "target_display": "screen_0",  # 指定注入到主屏幕
        "layer_name": "my_custom_layer",
        "z_index": 50,  # 决定你的遮挡关系
        "script_path": script_path,
        "class_name": "MyHardcoreView",
        "init_kwargs": {"theme_color": "red"}  # 传入初始化参数
    })
```

### 3. Addon 外挂机制 (跨模块视图注入)

引擎支持你将自己的脚本强行注入到其他模块的图层中（实现 HUD 叠加、性能监视器等）。
只需将 `cmd` 改为 `"inject_addon"`，并提供 `target_layer`（目标图层名）即可。你的脚本会在目标图层渲染完毕后，利用 Alpha
通道覆盖在画面上方。



---

## 轨道三：沙盒双向通讯与跨屏互联

沙盒处于隔离的 GPU 进程中，屏幕之间、屏幕与主程序之间的通讯必须遵循 **“V 字型路由原则”**。
**绝对禁止**屏幕 A 的沙盒直接通过内存调用屏幕 B 的沙盒（这会导致 OpenGL 上下文死锁）。

### 1. 主进程呼叫沙盒 (Main -> Sandbox)

主进程通过发送 `sandbox_emit` 指令，精准呼叫特定屏幕、特定图层上的沙盒。这会触发沙盒内部的 `on_message(self, data)` 钩子。

```python
# 在 main.py 中：
display.send_cmd({
    "cmd": "sandbox_emit",
    "target_display": "screen_0",
    "layer_name": "my_custom_layer",
    "data": {"action": "explode"}
})
```

### 2. 沙盒呼叫主进程 (Sandbox -> Main)

沙盒在 `on_mount` 之后，可以通过 `self.send_to_main({"key": "value"})` 将数据发回主进程。
主进程的显示驱动收到数据后，会将其包装成名为 `SandboxMessage` 的全局事件通过 `lib.Event` 广播出去。

```python
# 在 main.py 中监听沙盒发来的消息：
def setup(self):
    static["event_handler"].register_event("SandboxMessage", self.on_sandbox_msg)


def on_sandbox_msg(self, event):
    screen = event.data.get("display_id")
    layer = event.data.get("layer")
    payload = event.data.get("data")
    print(f"收到屏幕 {screen} 的消息: {payload}")
```

### 3. 屏幕间通讯 (Screen A -> Main -> Screen B)

如果副屏需要主屏配合播放动画，副屏沙盒应使用 `send_to_main` 发送请求，主进程的业务逻辑校验后，再使用 `sandbox_emit`
转发给主屏。主进程永远是唯一的中转路由器。

---

## 避坑指南与底层调试

引擎是冰冷且严苛的，如果你不遵循规范，不仅画面会崩溃，还可能拖垮整个操作系统。请牢记以下法则：

1. **废弃图元警告**：AFEDIUM 运行在 OpenGL 3.3 Core Profile 模式。**绝对禁止使用 `GL_QUADS`**（四边形），请使用
   `GL_TRIANGLES` 拼合，否则引擎会报 `0x500 Invalid enum` 错误。
2. **显存泄漏 (Memory Safety)**：沙盒脚本在 `on_destroy()` 中如果未清理 FBO 或纹理，在执行模块热重载（`reload`
   ）时会导致显存无限累加，最终引发系统 OOM。
3. **Pyglet 矩阵限制**：Pyglet 的 `Mat4` 在处理 3D 变换时非常严格。如果直接操作底层矩阵，请遵循 `缩放 -> 旋转 -> 平移`
   的乘法顺序。
4. **IPC 阻塞防范**：永远不要在 `view.py` (渲染进程) 中执行阻塞操作（如 `requests.get` 或高耗时 `while`
   ），这会瞬间导致整个屏幕的画面冻结。网络与耗时计算请交由主进程处理，通过 `display_driver` 的自定义事件通知渲染层。
