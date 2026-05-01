import importlib.util
import multiprocessing
import os
import sys
import traceback

from lib.common import static, comm_lib
from lib.config import Config
from lib.logger import log
from lib.plugin import AfediumPluginBase

Info = {
    "name": "显示驱动",
    "id": "display_driver",
    "dependencies": [],
    "pip_dependencies": ["pyglet"],
    "linux_dependencies": []
}


# =====================================================================
# 渲染子进程
# =====================================================================
def render_process_main(pipe_conn):
    try:
        import pyglet
        from pyglet.gl import (glClearColor, glEnable, glDisable, glClear,
                               GL_DEPTH_TEST, GL_BLEND, GL_SRC_ALPHA,
                               GL_ONE_MINUS_SRC_ALPHA, GL_DEPTH_BUFFER_BIT,
                               glBlendFunc)
        from pyglet.math import Mat4, Vec3
        pyglet.options['audio'] = ('silent',)

        # ================= 1. 渲染图层管理器 =================
        class Layer:
            def __init__(self, name, z_index):
                self.name = name
                self.z_index = z_index
                self.items = []

            def draw(self, window, default_proj, default_view):
                glClear(GL_DEPTH_BUFFER_BIT)
                for item in list(self.items):
                    window.projection = default_proj
                    window.view = default_view
                    try:
                        if hasattr(item, 'on_draw'):
                            item.on_draw(window)
                        elif hasattr(item, 'draw'):
                            item.draw(window)
                    except Exception as e:
                        print(f"[Render] {self.name} 渲染崩溃: {e}")
                        self.items.remove(item)

            def dispatch_event(self, event_name, *args):
                for item in reversed(self.items):
                    if hasattr(item, event_name):
                        handler = getattr(item, event_name)
                        try:
                            if handler(*args) == pyglet.event.EVENT_HANDLED:
                                return True
                        except Exception as e:
                            print(f"[Render] {self.name} 事件处理崩溃: {e}")
                return False

        # ================= 2. 内置渲染器包装 =================
        class SystemUIRenderer:
            def __init__(self, ctx):
                self.ctx = ctx

            def draw(self, window):
                self.ctx.ui_batch.draw()

            def on_mouse_press(self, x, y, button, modifiers):
                if button == pyglet.window.mouse.LEFT:
                    for pid, popup in list(self.ctx.current_popups.items()):
                        for btn in popup["buttons"]:
                            if btn["x"] <= x <= btn["x"] + btn["w"] and btn["y"] <= y <= btn["y"] + btn["h"]:
                                btn["bg_shape"].color = (40, 90, 140)
                                pipe_conn.send({
                                    "event": "popup_action",
                                    "popup_id": pid,
                                    "action_id": btn["action_id"],
                                    "display_id": self.ctx.display_id,
                                    "form_data": {}
                                })
                                return pyglet.event.EVENT_HANDLED
                return False

        class ManagedSceneRenderer:
            def __init__(self, ctx):
                self.ctx = ctx

            def draw(self, window):
                if self.ctx.current_scenes:
                    window.projection = Mat4.perspective_projection(window.aspect_ratio, z_near=0.1, z_far=1000, fov=45)
                    glEnable(GL_DEPTH_TEST)
                    self.ctx.scene_3d_batch.draw()

                window.projection = Mat4.orthogonal_projection(0, window.width, 0, window.height, -255, 255)
                glDisable(GL_DEPTH_TEST)
                self.ctx.scene_2d_batch.draw()

        # ================= 3. 独立窗口上下文 =================
        class WindowContext:
            def __init__(self, display_id="screen_0", screen_device=None, fullscreen=True):
                self.display_id = display_id

                if screen_device:
                    self.window = pyglet.window.Window(
                        screen=screen_device,
                        fullscreen=fullscreen,
                        caption=f"AFEDIUM Render Engine - {display_id}",
                        resizable=True
                    )
                else:
                    self.window = pyglet.window.Window(width=1024, height=768,
                                                       caption=f"AFEDIUM Render Engine - {display_id}", resizable=True)

                self.layers = {
                    "managed_scene": Layer("managed_scene", 10),
                    "system_ui": Layer("system_ui", 999)
                }

                self.ui_batch = pyglet.graphics.Batch()
                self.current_popups = {}
                self.ui_elements = []

                self.scene_2d_batch = pyglet.graphics.Batch()
                self.scene_3d_batch = pyglet.graphics.Batch()
                self.current_scenes = {}

                # 挂载基础渲染器
                self.layers["system_ui"].items.append(SystemUIRenderer(self))
                self.layers["managed_scene"].items.append(ManagedSceneRenderer(self))

                self._bind_events()

            def route_event(self, event_name, *args):
                sorted_layers = sorted(self.layers.values(), key=lambda l: l.z_index, reverse=True)
                for layer in sorted_layers:
                    if layer.dispatch_event(event_name, *args):
                        return pyglet.event.EVENT_HANDLED
                return False

            def _bind_events(self):
                @self.window.event
                def on_mouse_press(x, y, button, modifiers):
                    if not self.route_event('on_mouse_press', x, y, button, modifiers):
                        pipe_conn.send({"event": "raw_input", "type": "mouse_press", "x": x, "y": y, "button": button,
                                        "display_id": self.display_id})

                @self.window.event
                def on_mouse_motion(x, y, dx, dy):
                    self.route_event('on_mouse_motion', x, y, dx, dy)

                @self.window.event
                def on_key_press(symbol, modifiers):
                    if not self.route_event('on_key_press', symbol, modifiers):
                        key_name = pyglet.window.key.symbol_string(symbol)
                        pipe_conn.send(
                            {"event": "raw_input", "type": "key_press", "key": key_name, "display_id": self.display_id})

                @self.window.event
                def on_key_release(symbol, modifiers):
                    self.route_event('on_key_release', symbol, modifiers)

                @self.window.event
                def on_draw():
                    self.window.clear()
                    glClearColor(0.1, 0.1, 0.12, 1.0)
                    glEnable(GL_BLEND)
                    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)

                    default_proj = self.window.projection
                    default_view = self.window.view

                    sorted_layers = sorted(self.layers.values(), key=lambda l: l.z_index)
                    for layer in sorted_layers:
                        layer.draw(self.window, default_proj, default_view)

                @self.window.event
                def on_close():
                    pipe_conn.send({"event": "window_closed", "display_id": self.display_id})
                    pyglet.app.exit()

            # --- 数据驱动解析逻辑 ---
            def build_ui_from_json(self, data):
                popup_id = data.get("popup_id")
                title = data.get("title", "系统提示")
                if popup_id in self.current_popups: self.current_popups.pop(popup_id)
                for el in self.ui_elements:
                    if hasattr(el, 'delete'): el.delete()
                self.ui_elements.clear()

                start_x, start_y = self.window.width // 2 - 200, self.window.height // 2 + 150
                current_y = start_y

                bg = pyglet.shapes.Rectangle(start_x - 20, start_y - 350, 440, 400, color=(40, 40, 45),
                                             batch=self.ui_batch)
                bg.opacity = 240
                self.ui_elements.append(bg)

                self.ui_elements.append(
                    pyglet.text.Label(title, bold=True, font_size=16, x=start_x, y=current_y, batch=self.ui_batch))
                current_y -= 40

                buttons = []
                for el in data.get("elements", []):
                    if el.get("type") == "label":
                        self.ui_elements.append(
                            pyglet.text.Label(el.get("text", ""), font_size=12, x=start_x, y=current_y,
                                              batch=self.ui_batch))
                        current_y -= 30
                    elif el.get("type") == "button":
                        btn_bg = pyglet.shapes.Rectangle(start_x, current_y - 15, 120, 30, color=(70, 130, 180),
                                                         batch=self.ui_batch)
                        btn_lbl = pyglet.text.Label(el.get("text", "Btn"), font_size=12, x=start_x + 60, y=current_y,
                                                    anchor_x='center', anchor_y='center', batch=self.ui_batch)
                        self.ui_elements.extend([btn_bg, btn_lbl])
                        buttons.append(
                            {"x": start_x, "y": current_y - 15, "w": 120, "h": 30, "action_id": el.get("action_id"),
                             "bg_shape": btn_bg})
                        current_y -= 40
                self.current_popups[popup_id] = {"data": data, "buttons": buttons}

            def apply_transform(self, element, transform):
                """引擎级坐标系变换处理器"""
                if element["type"] == "2d":
                    obj = element["obj"]
                    if "x" in transform: obj.x = transform["x"]
                    if "y" in transform: obj.y = transform["y"]
                    if "scale" in transform: obj.scale = transform["scale"]
                    if "rotation" in transform: obj.rotation = transform["rotation"]

                elif element["type"] == "3d":
                    state = element["state"]
                    # 更新状态记录
                    for k in ["x", "y", "z", "rx", "ry", "rz", "scale"]:
                        if k in transform: state[k] = transform[k]

                    # 重新生成 4x4 变换矩阵 (由于 Pyglet 是列主序 16 元素元组，缩放矩阵需手动构造)
                    trans_mat = Mat4.from_translation(Vec3(state["x"], state["y"], state["z"]))
                    rot_mat_x = Mat4.from_rotation(state["rx"], Vec3(1, 0, 0))
                    rot_mat_y = Mat4.from_rotation(state["ry"], Vec3(0, 1, 0))
                    rot_mat_z = Mat4.from_rotation(state["rz"], Vec3(0, 0, 1))
                    s = state["scale"]
                    scale_mat = Mat4((s, 0, 0, 0, 0, s, 0, 0, 0, 0, s, 0, 0, 0, 0, 1))

                    # 矩阵乘法顺序：缩放 -> 旋转 -> 平移
                    element["obj"].matrix = trans_mat @ rot_mat_z @ rot_mat_y @ rot_mat_x @ scale_mat

            def build_scene_from_json(self, data):
                scene_id = data.get("scene_id")
                mount_path = data.get("mount_path")

                if scene_id in self.current_scenes:
                    for el in self.current_scenes[scene_id]["elements"]:
                        if hasattr(el, 'delete'): el.delete()

                if mount_path and mount_path not in pyglet.resource.path:
                    pyglet.resource.path.append(mount_path)
                    pyglet.resource.reindex()

                scene_elements = []

                for img_data in data.get("2d", []):
                    try:
                        img = pyglet.resource.image(img_data["path"])
                        sprite = pyglet.sprite.Sprite(img, x=img_data["x"], y=img_data["y"], batch=self.scene_2d_batch)
                        sprite.scale = img_data.get("scale", 1.0)
                        sprite.rotation = img_data.get("rotation", 0.0)
                        scene_elements[img_data["id"]] = {"type": "2d", "obj": sprite}
                    except Exception as e:
                        print(f"[Render] VFS 加载2D图像失败 {img_data['path']}: {e}")

                for mod_data in data.get("3d", []):
                    try:
                        model = pyglet.resource.model(mod_data["path"], batch=self.scene_3d_batch)
                        element = {
                            "type": "3d", "obj": model,
                            "state": {
                                "x": mod_data.get("x", 0), "y": mod_data.get("y", 0), "z": mod_data.get("z", -5),
                                "rx": mod_data.get("rx", 0), "ry": mod_data.get("ry", 0), "rz": mod_data.get("rz", 0),
                                "scale": mod_data.get("scale", 1.0)
                            }
                        }
                        self.apply_transform(element, {})  # 触碰一次以生成初始矩阵
                        scene_elements[mod_data["id"]] = element
                    except Exception as e:
                        print(f"[Render] VFS 加载3D模型失败 {mod_data['path']}: {e}")

                self.current_scenes[scene_id] = {"elements": scene_elements}
                if not hasattr(self, 'active_animations'): self.active_animations = []

        # ================= 4. 沙盒脚本注入器 =================
        def load_advanced_view(module_path, class_name, init_kwargs=None):
            try:
                folder = os.path.dirname(module_path)
                if folder not in sys.path: sys.path.insert(0, folder)
                spec = importlib.util.spec_from_file_location("dynamic_view", module_path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)

                # 提取参数并进行实例化
                kwargs = init_kwargs or {}
                return getattr(mod, class_name)(**kwargs)
            except Exception as e:
                print(f"[Render] 加载高级视图失败 {module_path}: {e}")
                return None

        # ================= 5. IPC 指令中枢与多屏幕调度 =================

        active_windows = {}
        physical_screens = {}

        display = pyglet.canvas.get_display()
        screens = display.get_screens()
        default_screen = display.get_default_screen()

        scan_results = []
        for i, screen in enumerate(screens):
            sid = f"screen_{i}"
            physical_screens[sid] = screen
            scan_results.append({
                "id": sid,
                "index": i,
                "width": screen.width,
                "height": screen.height,
                "x": screen.x,
                "y": screen.y,
                "is_default": screen == default_screen
            })

        # 向主进程汇报扫描结果
        pipe_conn.send({"event": "hardware_scan", "screens": scan_results})

        def check_ipc(dt):
            while pipe_conn.poll():
                try:
                    msg = pipe_conn.recv()
                    cmd = msg.get("cmd")

                    # 提取目标显示器，默认为 screen_0 保障向下兼容
                    target_display = msg.get("target_display", "screen_0")
                    ctx = active_windows.get(target_display)

                    if cmd == 'quit':
                        pyglet.app.exit()

                    elif cmd == 'init_displays':
                        config = msg.get("config", {})
                        monitors = config.get("monitors", {})
                        for sid, m_conf in monitors.items():
                            if m_conf.get("enabled", False) and sid in physical_screens:
                                print(f"[Render] 正在挂载屏幕: {sid} (分辨率: {m_conf['width']}x{m_conf['height']})")
                                active_windows[sid] = WindowContext(
                                    display_id=sid,
                                    screen_device=physical_screens[sid],
                                    fullscreen=m_conf.get("fullscreen", True)
                                )

                    elif cmd == 'clear_all':
                        print("[Render] 收到调试指令：正在执行全量清屏...")
                        for win_id, w_ctx in active_windows.items():
                            for l_name, layer in w_ctx.layers.items():
                                for item in layer.items:
                                    if hasattr(item, 'on_destroy'):
                                        try:
                                            item.on_destroy()
                                        except:
                                            pass
                                layer.items.clear()
                            w_ctx.current_popups.clear()
                            for el in w_ctx.ui_elements:
                                if hasattr(el, 'delete'): el.delete()
                            w_ctx.ui_elements.clear()
                            for sid, sdata in w_ctx.current_scenes.items():
                                for el in sdata["elements"]:
                                    if hasattr(el, 'delete'): el.delete()
                            w_ctx.current_scenes.clear()

                    elif ctx:  # 路由到特定窗口的操作
                        if cmd == 'show_popup':
                            ctx.build_ui_from_json(msg.get("data", {}))
                        elif cmd == 'close_popup':
                            pid = msg.get("popup_id")
                            if pid in ctx.current_popups:
                                ctx.current_popups.pop(pid)
                                for el in ctx.ui_elements:
                                    if hasattr(el, 'delete'): el.delete()
                                ctx.ui_elements.clear()
                        elif cmd == 'show_scene':
                            ctx.build_scene_from_json(msg.get("data", {}))
                        elif cmd == 'close_scene':
                            sid = msg.get("scene_id")
                            if sid in ctx.current_scenes:
                                for el in ctx.current_scenes[sid]["elements"]:
                                    if hasattr(el, 'delete'): el.delete()
                                ctx.current_scenes.pop(sid)

                        elif cmd == 'update_scene_element':
                            scene = ctx.current_scenes.get(msg.get("scene_id"))
                            if scene:
                                element = scene["elements"].get(msg.get("element_id"))
                                if element:
                                    ctx.apply_transform(element, msg.get("transform", {}))
                        elif cmd == 'animate_scene_element':
                            scene = ctx.current_scenes.get(msg.get("scene_id"))
                            if scene:
                                element = scene["elements"].get(msg.get("element_id"))
                                if element:
                                    target = msg.get("target_transform", {})
                                    duration = msg.get("duration", 1.0)

                                    # 记录当前的起始状态
                                    start_state = {}
                                    if element["type"] == "2d":
                                        obj = element["obj"]
                                        if "x" in target: start_state["x"] = obj.x
                                        if "y" in target: start_state["y"] = obj.y
                                        if "scale" in target: start_state["scale"] = obj.scale
                                        if "rotation" in target: start_state["rotation"] = obj.rotation
                                    elif element["type"] == "3d":
                                        for k in target.keys():
                                            if k in element["state"]:
                                                start_state[k] = element["state"][k]

                                    ctx.active_animations.append({
                                        "element": element,
                                        "start": start_state,
                                        "target": target,
                                        "duration": duration,
                                        "elapsed": 0.0
                                    })

                        elif cmd == 'create_layer':
                            l_name = msg["layer_name"]
                            ctx.layers[l_name] = Layer(l_name, msg.get("z_index", 100))
                        elif cmd == 'load_advanced':
                            t_layer = msg["layer_name"]
                            if t_layer not in ctx.layers: ctx.layers[t_layer] = Layer(t_layer, msg.get("z_index", 100))

                            # 提取 kwargs 并加载
                            init_kwargs = msg.get("init_kwargs", {})
                            view_inst = load_advanced_view(msg["script_path"], msg["class_name"], init_kwargs)

                            if view_inst:
                                # 绑定向主进程发消息的方法
                                def make_sender(disp_id, lyr):
                                    return lambda data: pipe_conn.send({
                                        "event": "sandbox_msg",
                                        "display_id": disp_id,
                                        "layer": lyr,
                                        "data": data
                                    })

                                view_inst.send_to_main = make_sender(target_display, t_layer)

                                if hasattr(view_inst, 'on_mount'): view_inst.on_mount()
                                ctx.layers[t_layer].items.append(view_inst)

                        elif cmd == 'inject_addon':
                            t_layer = msg["target_layer"]
                            if t_layer in ctx.layers:
                                init_kwargs = msg.get("init_kwargs", {})
                                addon_view = load_advanced_view(msg["script_path"], msg["class_name"], init_kwargs)
                                if addon_view:
                                    def make_sender(disp_id, lyr):
                                        return lambda data: pipe_conn.send({
                                            "event": "sandbox_msg",
                                            "display_id": disp_id,
                                            "layer": lyr,
                                            "data": data
                                        })

                                    addon_view.send_to_main = make_sender(target_display, t_layer)
                                    if hasattr(addon_view, 'on_mount'): addon_view.on_mount()
                                    ctx.layers[t_layer].items.append(addon_view)
                        elif cmd == 'sandbox_emit':
                            t_layer = msg.get("layer_name")
                            if t_layer in ctx.layers:
                                for item in ctx.layers[t_layer].items:
                                    if hasattr(item, 'on_message'):
                                        try:
                                            item.on_message(msg.get("data"))
                                        except Exception as e:
                                            print(f"[Render] 沙盒接收消息崩溃: {e}")
                        elif cmd == 'clear_layer':
                            l_name = msg["layer_name"]
                            if l_name in ctx.layers:
                                for item in ctx.layers[l_name].items:
                                    if hasattr(item, 'on_destroy'):
                                        try:
                                            item.on_destroy()
                                        except:
                                            pass
                                ctx.layers[l_name].items.clear()
                                print(f"[Render] 已清空图层: {l_name} ({target_display})")

                except EOFError:
                    pyglet.app.exit()

        def update_scripts(dt):
            for ctx in active_windows.values():
                # 1. 运行高级沙盒脚本的 update
                for layer in ctx.layers.values():
                    for item in layer.items:
                        if hasattr(item, 'update'):
                            try:
                                item.update(dt)
                            except:
                                pass

                # 2. 运行场景元素补间动画演算
                if hasattr(ctx, 'active_animations'):
                    for anim in ctx.active_animations[:]:
                        anim["elapsed"] += dt
                        progress = min(1.0, anim["elapsed"] / anim["duration"])

                        # 简单的线性插值 (Linear Interpolation)
                        current_transform = {}
                        for k, start_val in anim["start"].items():
                            target_val = anim["target"][k]
                            current_transform[k] = start_val + (target_val - start_val) * progress

                        ctx.apply_transform(anim["element"], current_transform)

                        if progress >= 1.0:
                            ctx.active_animations.remove(anim)

        pyglet.clock.schedule_interval(check_ipc, 1 / 60.0)
        pyglet.clock.schedule_interval(update_scripts, 1 / 60.0)

        pyglet.app.run()
    except Exception as e:
        print(f"[RenderProcess] 引擎严重崩溃: {e}")
        traceback.print_exc()


# =====================================================================
# 主进程逻辑
# =====================================================================

class AFEDIUMPlugin(AfediumPluginBase):
    default_config = {"enabled": True}

    def setup(self):
        if not self.config.conf.get("enabled", True): return True

        # 独立管理显示器的配置文件
        self.display_config = Config("display_driver", {
            "enabled": True,
            "monitors": {}
        })

        self.active_popups = {}
        self.parent_conn, self.child_conn = multiprocessing.Pipe()
        self.render_proc = multiprocessing.Process(
            target=render_process_main, args=(self.child_conn,), daemon=True
        )
        self.render_proc.start()

        static["display"] = self

        cmd = comm_lib.register("display", description="全局显示引擎调试控制")
        cmd.subcommand("clear", self.cmd_clear, "【调试】强制清空所有屏幕显存")
        cmd.subcommand("restart", self.cmd_restart, "【调试】硬重启 GPU 渲染进程")
        cmd.subcommand("status", self.cmd_status, "【调试】查看显示驱动运行状态")

        return True

    def main_loop(self):
        static["running"][self.id] = True
        while not self.stop_event.is_set():
            while self.parent_conn.poll():
                try:
                    self.handle_render_event(self.parent_conn.recv())
                except EOFError:
                    break
            self.stop_event.wait(timeout=0.01)

    def teardown(self):
        if hasattr(self, 'render_proc') and self.render_proc.is_alive():
            try:
                self.parent_conn.send({"cmd": "quit"})
                self.render_proc.join(timeout=3)
            except Exception:
                pass
            if self.render_proc.is_alive(): self.render_proc.terminate()

    def handle_render_event(self, msg):
        event_type = msg.get("event")
        if event_type == "hardware_scan":
            detected_screens = msg.get("screens", [])
            monitors_conf = self.display_config.conf.get("monitors", {})

            log.info(f"[{self.id}] 收到 GPU 硬件报告，发现 {len(detected_screens)} 个物理显示器。")

            for s in detected_screens:
                sid = s["id"]
                if sid not in monitors_conf:
                    # 发现新屏幕，记录到配置中。为防止干扰用户，默认只启用主屏幕
                    monitors_conf[sid] = {
                        "name": f"Display {s['index']}",
                        "enabled": s["is_default"],
                        "width": s["width"],
                        "height": s["height"],
                        "x": s["x"],
                        "y": s["y"],
                        "fullscreen": True
                    }
                else:
                    # 如果屏幕已在配置中，更新它的物理分辨率（以防用户改了系统分辨率）
                    monitors_conf[sid].update({
                        "width": s["width"],
                        "height": s["height"],
                        "x": s["x"],
                        "y": s["y"]
                    })

            self.display_config.conf["monitors"] = monitors_conf
            self.display_config.update()  # 固化到本地 JSON

            # 将最终的“排班表”发回给渲染进程
            self.send_cmd({"cmd": "init_displays", "config": self.display_config.conf})

        elif event_type == "popup_action":
            pid = msg.get("popup_id")
            aid = msg.get("action_id")
            form_data = msg.get("form_data", {})

            popup = self.active_popups.get(pid)
            if popup and aid in popup.callbacks:
                handler = popup.callbacks[aid]
                try:
                    handler(form_data)
                except Exception as e:
                    log.error(f"UI 回调异常: {e}")

        elif event_type == "sandbox_msg":
            display_id = msg.get("display_id")
            layer = msg.get("layer")
            data = msg.get("data")
            log.debug(f"[{self.id}] 收到沙盒 [{display_id} | {layer}] 消息: {data}")
            # 触发系统全局事件，让其他业务插件可以通过 register_event 监听到
            if "event_handler" in static:
                from lib.Event import Event
                static["event_handler"].trigger_event(
                    Event("SandboxMessage", display_id=display_id, layer=layer, data=data)
                )

        elif event_type == "raw_input":
            # 记录来源于哪个屏幕的输入
            display_source = msg.get('display_id', 'unknown')
            if msg.get("type") == "key_press":
                log.debug(f"[{self.id}] 屏幕[{display_source}] 捕获按键: {msg.get('key')}")
            elif msg.get("type") == "mouse_press":
                log.debug(f"[{self.id}] 屏幕[{display_source}] 捕获点击: (X:{msg.get('x')}, Y:{msg.get('y')})")
        elif event_type == "window_closed":
            log.info(f"[{self.id}] 渲染窗口 [{msg.get('display_id')}] 已关闭")

    def send_cmd(self, cmd_dict):
        if hasattr(self, 'parent_conn'): self.parent_conn.send(cmd_dict)

    def cmd_clear(self, ctx, args):
        self.send_cmd({"cmd": "clear_all"})
        self.active_popups.clear()
        return "指令下达：已通知 GPU 管线执行全屏幕图层清理。"

    def cmd_restart(self, ctx, args):
        """暴力的硬重启：如果子进程彻底卡死（比如死循环），直接杀掉重建"""
        ctx.reply("正在终止当前的 GPU 渲染子进程...")
        if hasattr(self, 'render_proc') and self.render_proc.is_alive():
            try:
                self.parent_conn.send({"cmd": "quit"})
                self.render_proc.join(timeout=2)
            except Exception:
                pass

            # 2秒内没死透，直接从操作系统层面发送 SIGTERM 强杀
            if self.render_proc.is_alive():
                ctx.reply("进程未响应，正在执行系统级强杀 (SIGTERM)...")
                self.render_proc.terminate()
                self.render_proc.join()

        ctx.reply("正在拉起全新的渲染管线...")
        self.active_popups.clear()
        # 必须重新创建 Pipe 管道，否则新老进程通信会错乱
        self.parent_conn, self.child_conn = multiprocessing.Pipe()
        self.render_proc = multiprocessing.Process(
            target=render_process_main, args=(self.child_conn,), daemon=True
        )
        self.render_proc.start()

        return f"渲染引擎硬重启完毕！(全新 PID: {self.render_proc.pid})"

    def cmd_status(self, ctx, args):
        """状态查询"""
        alive = self.render_proc.is_alive() if hasattr(self, 'render_proc') else False
        pid = self.render_proc.pid if alive else "N/A"
        popups = len(self.active_popups)

        status_text = (
            f"=== AFEDIUM 显示引擎状态 ===\n"
            f"- 独立渲染进程存活: {'🟢 是' if alive else '🔴 否'}\n"
            f"- 操作系统 PID: {pid}\n"
            f"- 主进程待命 UI 路由数: {popups} 个\n"
            f"(目前正运行在虚拟化 Context 模式中，准备迎接多屏幕硬件握手)"
        )
        return status_text

    def close_popup(self, popup_id):
        self.active_popups.pop(popup_id, None)
        self.send_cmd({"cmd": "close_popup", "popup_id": popup_id})
