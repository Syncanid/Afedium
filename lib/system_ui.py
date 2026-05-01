import uuid

from lib.common import static
from lib.logger import log


class SystemPopup:
    """系统级 UI 构建库 (永远置顶，用于通知、确认、表单)"""

    def __init__(self, title="系统提示"):
        self.display = static.get("display")
        if not self.display:
            log.warning("显示驱动未挂载，系统 UI 指令将被丢弃。")

        self.popup_id = f"sys_popup_{uuid.uuid4().hex[:8]}"
        self.title = title
        self.elements = []
        self.callbacks = {}

    def add_label(self, text):
        self.elements.append({"type": "label", "text": text})
        return self

    def add_button(self, text, on_click):
        action_id = f"btn_{uuid.uuid4().hex[:6]}"
        self.elements.append({"type": "button", "text": text, "action_id": action_id})
        self.callbacks[action_id] = on_click
        return self

    def show(self):
        if not self.display: return None
        self.display.active_popups[self.popup_id] = self
        self.display.send_cmd({
            "cmd": "show_popup",
            "data": {
                "popup_id": self.popup_id,
                "title": self.title,
                "elements": self.elements
            }
        })
        return self.popup_id

    def close(self):
        if self.display:
            self.display.close_popup(self.popup_id)
