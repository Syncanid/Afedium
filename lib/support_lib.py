import ast
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import traceback
import zipfile
import zipimport
from urllib.parse import urlparse

from lib.common import Config, static, loaded_plugins, threads, static_threads
from lib.logger import log
from lib.plugin import AfediumPluginBase

config = Config("core")
anti_hack = re.compile(r"[\.\w-]+ *(?:[~<>=]{2})? *[\.\w]*")


class CustomJsonEncoder(json.JSONEncoder):
    def default(self, obj):
        try:
            return json.JSONEncoder.default(self, obj)
        except TypeError:
            return f"<{obj.__class__.__module__}.{obj.__class__.__name__} object at {hex(id(obj))}>"


def get_info_from_pyz(pyz_path: str):
    try:
        with zipfile.ZipFile(pyz_path, 'r') as zf:
            # 检查根目录下是否存在 info.json
            if 'info.json' in zf.namelist():
                with zf.open('info.json') as info_file:
                    return json.load(info_file)
            else:
                log.error(f"在 '{os.path.basename(pyz_path)}' 的根目录中找不到 info.json 文件。")
    except Exception as e:
        log.error(f"从 '{pyz_path}' 读取元信息时出错: {e}")
    return None


def load_pyz_module(pyz_path, ttl=2):
    if ttl <= 0:
        return None

    pyz_full_path = os.path.abspath(pyz_path)
    log.debug(f"正在加载模块: {pyz_full_path}")

    info = get_info_from_pyz(pyz_full_path)
    if not info:
        log.error("无法读取模块元数据")
        return None

    plugin_id = info.get("id")
    if not plugin_id:
        log.error(f"{os.path.basename(pyz_path)} 的 info.json 中缺少 'id' 字段。")
        return None

    try:
        log.info(f"加载插件 {plugin_id}...")

        importer = zipimport.zipimporter(pyz_full_path)
        plugin_module = importer.load_module('main')

        PluginClass = plugin_module.AFEDIUMPlugin
        if not PluginClass:
            raise AttributeError(f"在模块 '{plugin_id}' 中未找到 'AFEDIUMPlugin' 类")

        default_config = getattr(PluginClass, 'default_config', {})
        plugin_config = Config(plugin_id, default_config)

        plugin_instance = PluginClass(info, plugin_config)

        if not isinstance(plugin_instance, AfediumPluginBase):
            log.warning(f"[{plugin_id}] 这是一个旧版架构的 PYZ 插件，正在动态注入生命周期兼容层...")

            # 强行塞入退出信号灯
            if not hasattr(plugin_instance, 'stop_event'):
                plugin_instance.stop_event = threading.Event()

            # 动态绑定 request_stop 伪装方法
            def request_stop_compat():
                plugin_instance.stop_event.set()
                if hasattr(plugin_instance, 'teardown'):
                    try:
                        plugin_instance.teardown()
                    except Exception as e:
                        log.error(f"[{plugin_id}] 旧版插件 teardown 异常: {e}")

            plugin_instance.request_stop = request_stop_compat

        if not plugin_instance.setup():
            log.error(f"插件 {plugin_id} 设置失败 (setup 返回 False)")
            return None

        static["running"][plugin_id] = False
        module_thread = threading.Thread(
            target=plugin_instance.main_loop,
            name=plugin_id,
            daemon=True
        )
        threads[plugin_id] = module_thread

        loaded_plugins[plugin_id] = {
            'info': info,
            'path': pyz_full_path,
            'instance': plugin_instance
        }

        module_thread.start()
        log.info(f"插件 {plugin_id} 启动成功")

        return module_thread

    except (ModuleNotFoundError, ImportError) as e:
        log.warning(f"插件 {plugin_id} 缺少依赖: {e}")
        try:
            if static.get("online"):
                pip_deps = info.get("pip_dependencies", [])
                linux_deps = info.get("linux_dependencies", [])
                if linux_deps and static.get("SYS_INFO") == "Linux":
                    install_linux(linux_deps)
                if pip_deps:
                    install_pip(pip_deps)
            return load_pyz_module(pyz_path, ttl - 1)
        except Exception as e:
            log.error(f"处理 '{os.path.basename(pyz_path)}' 的依赖时出错: {e}")
            return None
    except Exception as e:
        log.error(f"加载或运行模块 {plugin_id} 时出错: {e}")
        log.debug(traceback.format_exc())
        return None


def get_plugin_resource(plugin_id: str, resource_path_in_plugin: str, mode: str = 'rb'):
    plugin_data = loaded_plugins.get(plugin_id)
    if not plugin_data:
        log.warning(f"资源加载错误: 未找到ID为 '{plugin_id}' 的已加载插件")
        return None

    # 1. 优先检查外部持久化目录 (plugin_data/)
    external_path = os.path.join('plugin_data', plugin_id, resource_path_in_plugin)
    try:
        if os.path.exists(external_path):
            encoding = 'utf-8' if 'b' not in mode else None
            with open(external_path, mode, encoding=encoding) as f:
                return f.read()
    except Exception as e:
        log.error(f"资源加载器: 检查外部资源时出错: {e}")

    content = None
    plugin_path = plugin_data.get('path')
    if not plugin_path:
        return None

    # 2. 如果是通过 dev_tools 挂载的源码目录
    if plugin_data.get('is_dev'):
        internal_full_path = os.path.join(plugin_path, resource_path_in_plugin)
        try:
            if os.path.exists(internal_full_path):
                encoding = 'utf-8' if 'b' not in mode else None
                with open(internal_full_path, mode, encoding=encoding) as f:
                    content = f.read()
        except Exception as e:
            log.error(f"资源加载器: 读取开发源码目录内资源时出错: {e}")

    # 3. 否则，走标准的 PYZ (zip) 解压逻辑
    else:
        try:
            import zipfile
            with zipfile.ZipFile(plugin_path, 'r') as zf:
                internal_full_path = resource_path_in_plugin.replace('\\', '/')
                if internal_full_path in zf.namelist():
                    if 'b' in mode:
                        content = zf.read(internal_full_path)
                    else:
                        content = zf.read(internal_full_path).decode('utf-8')
        except Exception as e:
            log.error(f"资源加载器: 检查PYZ内部资源时出错: {e}")

    # 4. 如果在内部找到了默认资源，自动将其复制释放到外部 plugin_data/ 供用户后续修改
    if content is not None and external_path is not None:
        try:
            os.makedirs(os.path.dirname(external_path), exist_ok=True)
            write_mode = 'wb' if 'b' in mode else 'w'
            encoding = 'utf-8' if 'b' not in mode else None
            with open(external_path, write_mode, encoding=encoding) as f:
                f.write(content)
        except Exception as e:
            log.error(f"资源加载器: 释放默认资源到外部时出错: {e}")

    return content


def is_valid_url(url: str) -> bool:
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except ValueError:
        return False


def install_pip(package, mirror: str = None):
    if not static.get("online"):
        return False

    if not config.conf.get("allow_global_install", False):
        log.warning(f"插件尝试执行全局 pip install: {package}")
        log.warning("已拦截。若确需自动安装依赖，请在 config/core.json 中设置 \"allow_global_install\": true")
        return False

    if mirror is None:
        mirror = config.conf.get('pip_mirror', '')

    if isinstance(package, list):
        package = "\n".join(package)
    pkgs = re.findall(anti_hack, package)
    pkgs = ' '.join(pkgs)
    log.info(f"正在尝试安装 Pip 依赖：{pkgs}")

    if mirror:
        mirror = '-i ' + mirror
    else:
        mirror = ''

    pip_cmd = f'{static.get("PY_EXEC", sys.executable)} -m pip install {mirror} '
    command_to_run = pip_cmd.split() + pkgs.split()

    log.debug(f"执行pip指令: {' '.join(command_to_run)}")

    try:
        creation_flags = 0
        if static.get("SYS_INFO") == "Windows":
            creation_flags = subprocess.CREATE_NEW_CONSOLE

        process = subprocess.Popen(
            command_to_run,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='ignore',
            creationflags=creation_flags
        )

        stdout, stderr = process.communicate(timeout=300)

        if stdout:
            log.debug(f"PIP STDOUT:\n{stdout}")
        if stderr:
            log.warning(f"PIP STDERR:\n{stderr}")

        if process.returncode == 0:
            log.info(f"依赖 {pkgs} 安装成功完成。")
            return True
        else:
            log.error(f"安装失败，Pip 返回错误码: {process.returncode}")
            return False

    except subprocess.TimeoutExpired:
        log.error("Pip 安装超时！进程已被终止。")
        process.kill()
        return False
    except Exception as e:
        log.error(f"执行 pip 时发生未知异常: {e}")
        return False


def install_linux(package):
    if not static.get("online"):
        return False

    if not config.conf.get("allow_global_install", False):
        log.warning(f"插件尝试执行全局 apt install: {package}")
        log.warning("已拦截。若确需自动安装系统库，请在 config/core.json 中设置 \"allow_global_install\": true")
        return False

    pkgs = re.findall(anti_hack, package)
    pkgs = ' '.join(pkgs)
    log.info(f"正在尝试安装 Linux 系统包：{pkgs}")
    command_to_run = ["apt", "install", "-y"] + pkgs.split()

    try:
        result = subprocess.run(
            command_to_run,
            check=True,
            capture_output=True,
            text=True
        )
        log.debug(f"APT STDOUT:\n{result.stdout}")
        return result
    except subprocess.CalledProcessError as e:
        log.error(f"APT 安装失败，返回码 {e.returncode}\nSTDERR: {e.stderr}")
        return False


def git_pull(path='./'):
    if not (static.get("online") and static.get("git_available")):
        return None
    if not os.path.isdir(path):
        log.error(f"错误: Git 目录 '{path}' 不存在。")
        return None
    log.info(f"正在尝试更新：{path}")
    try:
        return subprocess.run(["git", "pull"],
                              cwd=path,
                              check=True,
                              capture_output=True,
                              text=True
                              ).stdout.strip()
    except subprocess.CalledProcessError as e:
        log.error(f"Git Pull 失败: {e.stderr}")
        return None


def check_git():
    git_path = shutil.which('git')
    if git_path:
        static["git_available"] = True
        return git_path

    local_git_path = os.path.abspath("./Git/cmd/git")
    if static.get("SYS_INFO") == "Windows":
        if os.path.exists(local_git_path):
            os.environ["PATH"] += os.pathsep + os.path.dirname(local_git_path)
            static["git_available"] = True
            return local_git_path

    static["git_available"] = False
    return False


def load_static(static_path, f_name, ttl: int = 1):
    def get_info_from_py(module_file_path):
        try:
            with open(module_file_path, "r", encoding="utf-8") as f:
                code = f.read()
            tree = ast.parse(code)
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and len(node.targets) == 1:
                    target = node.targets[0]
                    if isinstance(target, ast.Name) and target.id == 'Info':
                        try:
                            return ast.literal_eval(node.value)
                        except ValueError:
                            continue
        except (IOError, SyntaxError):
            pass
        return None

    try:
        module = importlib.import_module(f"{os.path.basename(static_path)}.{f_name}")
        info = getattr(module, 'Info', None)
        if not info:
            log.error(f"系统模块 {f_name} 缺少 Info 字典")
            return None

        plugin_id = info["id"]
        PluginClass = getattr(module, 'AFEDIUMPlugin', None)

        if not PluginClass:
            # 兼容老版本系统模块写法
            log.warning(f"系统模块 {f_name} 未继承 AfediumPluginBase，正在使用旧版函数式加载。")
            loaded_plugins[plugin_id] = module
            static_threads[plugin_id] = threading.Thread(target=module.__init__, name=plugin_id, daemon=True)
            static_threads[plugin_id].start()
            return static_threads[plugin_id]

        # 现代化类加载方式
        default_config = getattr(PluginClass, 'default_config', getattr(module, 'default', {}))
        plugin_config = Config(plugin_id, default_config)

        plugin_instance = PluginClass(info, plugin_config)

        if not plugin_instance.setup():
            log.error(f"系统模块 {plugin_id} 设置失败 (setup 返回 False)")
            return None

        static["running"][plugin_id] = False
        module_thread = threading.Thread(
            target=plugin_instance.main_loop,
            name=plugin_id,
            daemon=True
        )

        static_threads[plugin_id] = module_thread

        # 将系统模块包装成和 PYZ 模块一样的字典结构
        loaded_plugins[plugin_id] = {
            'info': info,
            'path': static_path,
            'instance': plugin_instance
        }

        module_thread.start()
        log.info(f"系统模块 {plugin_id} 启动成功")
        return module_thread

    except (ModuleNotFoundError, ImportError) as e:
        # 依赖自动安装逻辑保持不变
        try:
            if not static.get("online"):
                log.warning("离线模式无法自动更新依赖")
                return None
            log.info(f"加载系统模块 {f_name} 时出现依赖问题: {e}")
            if ttl == 0: return None

            info = get_info_from_py(os.path.join(static_path, f"{f_name}.py"))
            if not info: return None

            pkgs = info.get("pip_dependencies", [])
            linux_deps = info.get("linux_dependencies", [])

            if linux_deps and static.get("SYS_INFO") == "Linux":
                install_linux(linux_deps)
            if pkgs:
                install_pip(pkgs)

            return load_static(static_path, f_name, ttl - 1)
        except Exception as ex:
            log.error(f"修复 {f_name} 依赖时出错: {ex}")
            return None
    except Exception as e:
        log.error(f"加载系统模块 {f_name} 时出错: {e}")
        log.debug(traceback.format_exc())
        return None
