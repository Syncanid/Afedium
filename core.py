import json
import os
import platform
import time
import traceback

from lib.Event import EventHandler
from lib.common import *
from lib.logger import log

Info = {
    "name": "AFEDIUM 核心",
    "id": "core",
    "dependencies": [],
    "pip_dependencies": [],
    "linux_dependencies": []
}

ALLOWED_ROOTS = {
    "static": static,
    "dynamic": dynamic,
    "loaded_plugins": loaded_plugins,
}


def safe_traverse(args):
    """安全地在白名单对象中寻址，防止任意代码执行"""
    if not args:
        raise ValueError("参数不能为空")
    root_name = args[0]

    if root_name in ALLOWED_ROOTS:
        current_obj = ALLOWED_ROOTS[root_name]
    else:
        raise PermissionError(f"拒绝访问未授权的系统根节点: {root_name}")

    for seg in args[1:]:
        if isinstance(current_obj, (dict, list)):
            if isinstance(current_obj, list) and str(seg).isdigit():
                seg = int(seg)
            current_obj = current_obj[seg]
        elif hasattr(current_obj, seg):
            current_obj = getattr(current_obj, seg)
        else:
            raise AttributeError(f"对象不支持索引访问或没有属性: {seg}")
    return current_obj


def get_handler(ctx, args):
    from lib.support_lib import get_info_from_pyz, CustomJsonEncoder
    if not args:
        raise ValueError("参数不能为空")
    try:
        # 拦截原有协议中的特殊短指令
        if args[0] == "plugins":
            return json.dumps(static.get("modules", {}))
        elif args[0] == "command":
            return json.dumps(list(comm_lib.command_list.keys()))
        elif args[0] == "info":
            if len(args) < 2:
                raise ValueError("info 命令需要指定 PYZ 文件名")
            return get_info_from_pyz(f"{static.get('pyz_module_path', 'pyz_modules')}/{args[1]}")
        else:
            # 走白名单安全路由
            obj = safe_traverse(args)
            return json.dumps(obj, ensure_ascii=False, cls=CustomJsonEncoder)
    except Exception as e:
        log.warning(f"外部尝试读取数据失败 {args}: {e}")
        return f"执行失败: {e}"


def set_handler(ctx, args):
    if len(args) < 2:
        raise Exception("参数不足")
    data = args.pop()
    try:
        root_obj_name = args[0]
        if root_obj_name not in ALLOWED_ROOTS:
            raise PermissionError(f"拒绝修改未授权的系统根节点: {root_obj_name}")

        if len(args) == 1:
            raise PermissionError("为防止破坏核心结构，禁止直接覆盖根节点")

        parent = safe_traverse(args[:-1])
        final_key = args[-1]

        if isinstance(parent, (dict, list)):
            if isinstance(parent, list) and str(final_key).isdigit():
                final_key = int(final_key)
            parent[final_key] = data
        elif hasattr(parent, final_key):
            setattr(parent, final_key, data)
        else:
            raise AttributeError("无法对目标路径赋值")
    except Exception as e:
        log.warning(f"外部尝试修改数据失败 {args}: {e}")
        return f"执行失败: {e}"


def set_info_handler(args):
    from lib.command import CommandContext

    # 解析外部发来的 JSON 数据流并走安全的设值路由
    if len(args) < 2:
        return "参数不足"
    data_str = args.pop()
    try:
        data = json.loads(data_str)
    except json.JSONDecodeError:
        data = data_str

    args.append(data)

    # 构造一个虚拟的上下文对象
    ctx = CommandContext()
    result = set_handler(ctx, args)

    if result is not None:
        return result
    return ctx.get_result() or "success"


def get_info_handler(args):
    from lib.support_lib import get_info_from_pyz, CustomJsonEncoder
    if not args:
        raise ValueError("参数不能为空")
    try:
        if args[0] == "plugins":
            return json.dumps(list(loaded_plugins.keys()))
        elif args[0] == "command":
            return json.dumps(list(comm_lib.command_list.keys()))
        elif args[0] == "info":
            if len(args) < 2:
                raise ValueError("info 命令需要指定 PYZ 文件名")
            return get_info_from_pyz(f"{static.get('pyz_module_path', 'pyz_modules')}/{args[1]}")
        else:
            obj = safe_traverse(args)
            return json.dumps(obj, ensure_ascii=False, cls=CustomJsonEncoder)
    except Exception as e:
        log.warning(f"获取信息失败 {args}: {e}")
        return f"执行失败: {e}"


def unload_module(t_name):
    with plugin_lock:
        thread_to_stop = threads.get(t_name) or static_threads.get(t_name)
        if not thread_to_stop:
            return

        log.info(f"正在停止模块: {t_name}")
        if t_name in static["running"]:
            static["running"][t_name] = False

        # 尝试触发插件基类中的协作式退出信号
        plugin_data = loaded_plugins.get(t_name)
        if plugin_data and isinstance(plugin_data, dict):
            instance = plugin_data.get('instance')
            if hasattr(instance, 'request_stop'):
                instance.request_stop()

    # 给予优雅退出时间，杜绝内存泄漏
    thread_to_stop.join(timeout=5)

    if thread_to_stop.is_alive():
        log.warning(f"模块 {t_name} 未能在超时时间内优雅退出，可能发生泄漏。")
    else:
        log.info(f"{t_name} 已成功退出")

    with plugin_lock:
        if t_name in threads: del threads[t_name]
        if t_name in static_threads: del static_threads[t_name]

        plugin_data = loaded_plugins.get(t_name)
        if plugin_data and isinstance(plugin_data, dict):
            path_to_remove = plugin_data.get('path')
            if path_to_remove and path_to_remove in sys.path:
                try:
                    sys.path.remove(path_to_remove)
                    log.debug(f"已从 sys.path 移除模块路径: {path_to_remove}")
                except ValueError:
                    pass
            del loaded_plugins[t_name]


def quit_all():
    try:
        with plugin_lock:
            tmp_threads = {**threads, **static_threads}

        log.info(f"正在关闭 {len(tmp_threads)} 个模块...")
        for thread_name in list(tmp_threads.keys()):
            try:
                unload_module(thread_name)
            except Exception as e:
                log.error(f"关闭线程 {thread_name} 时出现异常:\n{e}")

    except Exception as e:
        sys.__stderr__.write(f"quit_all 函数出现严重错误: {e}\n")
        traceback.print_exc(file=sys.__stderr__)


def thread_watcher():
    while True:
        with plugin_lock:
            all_threads = {**threads}

        for name, thread_obj in list(all_threads.items()):
            if not thread_obj.is_alive():
                with plugin_lock:
                    if name in threads: threads.pop(name)
                    if name in static_threads: static_threads.pop(name)
                    if name in static["running"]: static["running"][name] = False
                log.info(f"线程 {name} 已自动清理")
        time.sleep(1)


def command_handler(ctx, args):
    from lib.support_lib import git_pull, load_pyz_module
    if not args: return "错误: 指令不能为空"

    if args[0] == "quit":
        try:
            target = args[1]
            with plugin_lock:
                exists = target in threads or target in static_threads
            if exists:
                unload_module(target)
                return f"已卸载模块: {target}"
            else:
                return "未知模块"
        except IndexError:
            quit_all()
            log.info("主程序终止")
            os._exit(0)
    elif args[0] == "boot":
        pyz_file = args[1]
        if not pyz_file.endswith(".pyz"): pyz_file += ".pyz"
        pyz_path = os.path.join(static.get("pyz_module_path", "pyz_modules"), pyz_file)
        if os.path.exists(pyz_path):
            return load_pyz_module(pyz_path)
        else:
            return f"错误: 文件不存在 {pyz_path}"
    elif args[0] == "command":
        if len(args) > 2 and args[1] == "unregister":
            return comm_lib.unregister(args[2])
        else:
            return "未知指令(unregister)"
    elif args[0] == "reload":
        quit_all()
        executable = sys.executable
        sys_args = sys.argv[:]
        sys_args.insert(0, executable)
        os.execvp(executable, sys_args)
    elif args[0] == "upgrade":
        if static.get("online"):
            result = git_pull()
            if result is not None: log.info(result)
            if result and "Already up to date." not in result:
                log.info("将在 1 秒后重启...")
                time.sleep(1)
                return command_handler(ctx, ["reload"])
            return None
        else:
            return "离线模式无法更新"
    else:
        return "未知指令(quit/boot/upgrade/command/reload)"


def help_handler(ctx, args):
    return json.dumps(list(comm_lib.command_list.keys()))


config = Config
pip_mirror = ''
config_file = 'config/main.json'
default = {
    "hostname": platform.node(),
    "Disabled": ["module_template"],
    "online": True,
    "allow_global_install": False,
    "pip_mirror": "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple",
    "git_mirror": "https://ghproxy.com/https://github.com",
    "portable_git": "WIP",
    "debugging": False,
}
event_handler = EventHandler()


def main():
    global pip_mirror

    for folder in ['logs', 'config', 'system', 'pyz_modules', 'lib']:
        if not os.path.exists(folder):
            os.mkdir(folder)

    log.info("AFEDIUM 核心正在启动...")

    try:
        config = Config("core", default)

        static["hostname"] = config.conf.get("hostname", platform.node())
        static["SYS_INFO"] = platform.system()
        static["SYS_VER"] = platform.version()
        static["PY_EXEC"] = sys.executable
        static["PY_VER"] = platform.python_version()

        if static["PY_VER"][0] != '3':
            log.error(f"Python 版本 {static['PY_VER']} 不受支持")
            exit(-1)

        static["running"] = {}
        static["modules"] = {"core": None}
        static["online"] = config.conf.get('online', True)
        static["debugging"] = config.conf.get('debugging', False)
        static["running"]["core"] = False
        static["event_handler"] = event_handler
        static["features"] = {}

        log.info(f"系统: {static['SYS_INFO']}")
        log.info(f"Python: {static['PY_VER']}")
        log.info(f"已禁用模块: {config.conf.get('Disabled', [])}")

        from lib.support_lib import get_info_from_pyz, check_git, load_pyz_module, load_static

        # 1. 加载系统模块
        static["system_path"] = "system"
        sys.path.append(static["system_path"])
        if os.path.exists(static["system_path"]):
            system_files = [f.split('.')[0] for f in os.listdir(static["system_path"]) if f.endswith(".py")]
            for name in system_files:
                if name not in config.conf.get('Disabled', []):
                    log.info(f"正在加载系统模块: {name}")
                    load_static(static["system_path"], name)

        # 2. 加载 PYZ 动态模块
        static["pyz_module_path"] = "pyz_modules"
        sys.path.append(static["pyz_module_path"])
        if os.path.exists(static["pyz_module_path"]):
            pyz_files = [f for f in os.listdir(static["pyz_module_path"]) if f.endswith(".pyz")]
            for file_name in pyz_files:
                module_path = os.path.join(static["pyz_module_path"], file_name)
                info = get_info_from_pyz(module_path)
                if info:
                    module_id = info.get("id")
                    static["modules"][module_id] = info.get("version")
                    if module_id not in config.conf.get('Disabled', []):
                        log.info(f"正在加载 PYZ 模块: {file_name}")
                        load_pyz_module(module_path)
                else:
                    log.warning(f"无法从 {file_name} 读取信息，已跳过。")

        static["running"]["core"] = True

        # Git 环境检查
        if not check_git():
            log.warning("未检测到Git环境，某些功能可能不可用")

        with plugin_lock:
            log.info(f"已加载的系统模块: {list(static_threads.keys())}")
            log.info(f"已加载的PYZ模块: {list(threads.keys())}")

        watchdog = threading.Thread(target=thread_watcher, name="watch_dog", daemon=True)
        watchdog.start()
        log.info("线程监视器已启动")

        # 注册核心指令
        cmd_core = comm_lib.register("core", description="核心系统控制")

        cmd_core.subcommand("quit", lambda ctx, args: command_handler(ctx, ["quit"] + args),
                            "卸载指定模块或关闭整个服务器")
        cmd_core.subcommand("boot", lambda ctx, args: command_handler(ctx, ["boot"] + args),
                            "手动启动指定的 PYZ 模块")
        cmd_core.subcommand("reload", lambda ctx, args: command_handler(ctx, ["reload"] + args),
                            "热重启整个 AFEDIUM 核心进程")
        cmd_core.subcommand("upgrade", lambda ctx, args: command_handler(ctx, ["upgrade"] + args),
                            "从 Git 拉取更新并自动重启")

        comm_lib.register("get", get_handler, "获取系统内部运行数据")
        comm_lib.register("set", set_handler, "修改系统内部运行数据")

        # 终端交互主循环
        while True:
            sys.stdout.write("$: ")
            sys.stdout.flush()
            user_input = input()
            if user_input.strip():
                command_result = comm_lib.command(user_input.split())
                if command_result is not None:
                    print(command_result)

    except KeyboardInterrupt:
        quit_all()
        log.info("主程序终止")
        os._exit(0)
    except Exception as e:
        log.error(f"主循环出错: {e}")
        traceback.print_exc()


if __name__ == '__main__':
    main()
