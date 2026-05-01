import threading
from abc import ABC, abstractmethod

from lib.logger import log


class AfediumPluginBase(ABC):
    def __init__(self, info, config):
        self.info = info
        self.config = config
        self.id = info.get("id", "unknown")
        # 协作式退出信号
        self.stop_event = threading.Event()

    @abstractmethod
    def setup(self) -> bool:
        """
        初始化逻辑。
        返回 True 表示成功，False 将导致加载中止。
        """
        pass

    @abstractmethod
    def main_loop(self):
        """
        主循环逻辑。
        必须使用 self.stop_event.wait(timeout) 或定期检查 self.stop_event.is_set()，
        严禁使用死循环或无限期的 time.sleep()。
        """
        pass

    def teardown(self):
        """
        清理逻辑：注销指令、释放端口、关闭文件等。
        """
        pass

    def request_stop(self):
        """接收核心框架的停止信号"""
        log.info(f"[{self.id}] 收到停止信号，正在执行清理流程...")
        self.stop_event.set()
        try:
            self.teardown()
        except Exception as e:
            log.error(f"[{self.id}] 清理流程抛出异常: {e}")
