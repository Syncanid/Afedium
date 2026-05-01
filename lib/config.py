import json
import threading


class Config:
    conf = {}

    def __init__(self, file: str, default: dict = None):
        self.default = default
        self.file = "./config/" + file + ".json"
        self.lock = threading.RLock()
        self.load_or_init(default)

    def load_or_init(self, default):
        with self.lock:
            try:
                with open(self.file, 'r', encoding='utf-8') as f:
                    self.conf = json.load(f)
                if default is not None:
                    # 检查是否有缺失的默认键
                    missing_keys = [k for k in self.default.keys() if k not in self.conf]
                    if missing_keys:
                        self._locked_update()
            except Exception:
                self.conf = default or {}
                self._locked_write(self.conf)

    def write(self, t: dict):
        with self.lock:
            self.conf.update(t)
            self._locked_write(self.conf)

    def wipe(self):
        with self.lock:
            self.conf = self.default.copy() if self.default else {}
            self._locked_write(self.conf)

    def update(self):
        with self.lock:
            self._locked_update()

    def _locked_update(self):
        # 内部无锁方法，必须在持有锁的情况下调用
        if self.default:
            merged = self.default.copy()
            merged.update(self.conf)
            self.conf = merged
            self._locked_write(self.conf)

    def _locked_write(self, data_dict):
        try:
            with open(self.file, 'w', encoding='utf-8') as f:
                json.dump(data_dict, f, ensure_ascii=False, indent=4)
        except Exception as e:
            from lib.logger import log
            log.error(f"写入配置文件 {self.file} 失败: {e}")
