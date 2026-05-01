import uuid

from lib.common import static, loaded_plugins
from lib.logger import log


class Scene:
    """模块专属的 2D/3D 混合场景构建器"""

    def __init__(self, scene_id, plugin_id, target_display="screen_0"):
        self.display = static.get("display")
        self.scene_id = scene_id
        self.plugin_id = plugin_id
        self.target_display = target_display
        self.assets_2d = []
        self.assets_3d = []

        # 解析模块的真实物理路径
        plugin_data = loaded_plugins.get(plugin_id)
        self.mount_path = plugin_data.get('path') if plugin_data else ""

        if not self.mount_path:
            log.warning(f"无法获取插件 {plugin_id} 的物理路径，资源加载可能失败。")

    def add_image(self, internal_path, element_id=None, x=0, y=0, scale=1.0, z_index=0, rotation=0):
        """添加 2D 图像 (填写插件内部的相对路径，如 assets/bg.png)"""
        eid = element_id or f"img_{uuid.uuid4().hex[:6]}"
        self.assets_2d.append({
            "id": eid, "path": internal_path,
            "x": x, "y": y, "scale": scale, "z": z_index, "rotation": rotation
        })
        return self

    def add_model(self, internal_path, element_id=None, x=0, y=0, z=-5.0, scale=1.0, rx=0, ry=0, rz=0):
        """添加 3D 模型 (填写插件内部的相对路径，如 assets/test.obj)"""
        eid = element_id or f"mod_{uuid.uuid4().hex[:6]}"
        self.assets_3d.append({
            "id": eid, "path": internal_path,
            "x": x, "y": y, "z": z, "scale": scale,
            "rx": rx, "ry": ry, "rz": rz
        })
        return self

    def set_transform(self, element_id, **kwargs):
        """
        瞬时改变元素状态。
        支持的 kwargs: x, y, z, scale, rotation(对于2D), rx, ry, rz(对于3D)
        """
        if not self.display: return
        self.display.send_cmd({
            "cmd": "update_scene_element",
            "target_display": self.target_display,
            "scene_id": self.scene_id,
            "element_id": element_id,
            "transform": kwargs
        })

    def animate_to(self, element_id, duration=1.0, **kwargs):
        """
        发起平滑过渡动画，由 GPU 自行计算补间，不阻塞主进程。
        支持的 kwargs: 同 set_transform
        """
        if not self.display: return
        self.display.send_cmd({
            "cmd": "animate_scene_element",
            "target_display": self.target_display,
            "scene_id": self.scene_id,
            "element_id": element_id,
            "duration": duration,
            "target_transform": kwargs
        })

    def show(self):
        """下发场景渲染指令，底层会自动加载进显存"""
        if not self.display:
            log.warning("显示驱动未挂载，场景无法渲染。")
            return

        self.display.send_cmd({
            "cmd": "show_scene",
            "target_display": self.target_display,
            "data": {
                "scene_id": self.scene_id,
                "mount_path": self.mount_path,
                "2d": self.assets_2d,
                "3d": self.assets_3d
            }
        })

    def close(self):
        if self.display:
            self.display.send_cmd({
                "cmd": "close_scene",
                "target_display": self.target_display,
                "scene_id": self.scene_id
            })
