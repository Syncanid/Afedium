import logging
import os
import json
from logging.handlers import TimedRotatingFileHandler


def setup_logger():
    os.makedirs('logs', exist_ok=True)

    debugging = False
    try:
        config_path = './config/core.json'
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                conf = json.load(f)
                # 兼容不同大小写或配置习惯
                debugging = conf.get("debugging", False)
    except Exception:
        # 如果这是首次启动，配置文件还不存在，默认关闭 debug
        pass

    logger = logging.getLogger("afedium")
    # 总开关必须设置为 DEBUG，这样底层的 handlers 才能接收到 debug 级别的信号进行分流
    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter('%(asctime)s - %(threadName)s - %(name)s - %(levelname)s - %(message)s')

    # 1. 控制台输出 Handler
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    # 控制台根据 debugging 状态决定输出级别
    ch.setLevel(logging.DEBUG if debugging else logging.INFO)
    logger.addHandler(ch)

    # 2. 基础 Info 日志文件 Handler (始终只接收 INFO 及以上级别的日志)
    fh_info = TimedRotatingFileHandler("logs/afedium.log", when="midnight", backupCount=7, encoding='utf-8')
    fh_info.setFormatter(formatter)
    fh_info.setLevel(logging.INFO)
    logger.addHandler(fh_info)

    # 3. 专属 Debug 日志文件 Handler (仅在开启 debugging 时创建)
    if debugging:
        # Debug 日志通常产生较快，保留最近 3 天即可，避免占用过多磁盘空间
        fh_debug = TimedRotatingFileHandler("logs/afedium_debug.log", when="midnight", backupCount=3, encoding='utf-8')
        fh_debug.setFormatter(formatter)
        fh_debug.setLevel(logging.DEBUG)
        logger.addHandler(fh_debug)

    return logger


# 全局单例 logger
log = setup_logger()
