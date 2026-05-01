import threading
import traceback
from lib.logger import log


class Event:
    def __init__(self, name, **kwargs):
        self.name = name
        self.data = kwargs


class EventHandler:
    def __init__(self):
        self.events = {}
        # 引入读写锁，保障多线程环境下的事件注册与触发安全
        self.lock = threading.RLock()

    def register_event(self, event_name, callback):
        with self.lock:
            if event_name not in self.events:
                self.events[event_name] = []
            if callback not in self.events[event_name]:
                self.events[event_name].append(callback)
                log.debug(f"已注册事件监听: {event_name} -> {callback.__name__}")

    def unregister_event(self, event_name, callback):
        with self.lock:
            if event_name in self.events and callback in self.events[event_name]:
                self.events[event_name].remove(callback)
                log.debug(f"已注销事件监听: {event_name} -> {callback.__name__}")

    def trigger_event(self, event):
        with self.lock:
            # 浅拷贝当前事件的回调列表
            # 防止在执行回调时，其他线程动态注销/注册事件导致字典迭代崩溃
            callbacks = self.events.get(event.name, []).copy()

        for callback in callbacks:
            try:
                callback(event)
            except Exception as e:
                log.error(f"执行事件 {event.name} 的回调 {callback.__name__} 时出现异常: {e}")
                log.debug(traceback.format_exc())