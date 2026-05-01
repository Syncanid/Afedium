import threading
import sys
from lib.config import Config
import lib.command as comm_lib

# --- 全局并发锁 ---
plugin_lock = threading.RLock()

# --- 全局状态字典 ---
loaded_plugins: dict = {}
dynamic: dict = {}
static: dict = {}
threads: dict = {}
static_threads: dict = {}
