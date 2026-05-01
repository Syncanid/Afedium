import importlib.util
import json
import os
import shutil
import sys
import threading
import time
import traceback

from lib.common import static, loaded_plugins, threads, plugin_lock, comm_lib
from lib.config import Config
from lib.logger import log
from lib.plugin import AfediumPluginBase

Info = {
    "name": "开发者工具",
    "id": "dev_tools",
    "dependencies": [],
    "pip_dependencies": [],
    "linux_dependencies": []
}


class AFEDIUMPlugin(AfediumPluginBase):
    default_config = {
        "enabled": False,  # 全局开关
        "source_dir": "dev_plugins",  # 开发源码目录
        "output_dir": "pyzoutput",  # 打包输出目录
        "hot_reload": True,  # 开启代码热重载
        "reload_interval": 2.0  # 文件扫描间隔(秒)
    }

    def setup(self):
        self.enabled = self.config.conf.get("enabled", False)
        self.source_dir = self.config.conf.get("source_dir", "dev_plugins")
        self.output_dir = self.config.conf.get("output_dir", "pyzoutput")
        self.hot_reload = self.config.conf.get("hot_reload", True)
        self.reload_interval = self.config.conf.get("reload_interval", 2.0)

        # 记录已追踪的源码目录，格式: { mod_path: {'mtime': float, 'plugin_id': str} }
        self.tracked_dirs = {}

        if not self.enabled:
            return True

        os.makedirs(self.source_dir, exist_ok=True)
        os.makedirs(self.output_dir, exist_ok=True)

        # 注册开发者指令
        comm_lib.register("pack", self.command_pack, "将源码文件夹打包为 .pyz")
        cmd_dev = comm_lib.register("dev", description="开发者管理工具")
        cmd_dev.subcommand("list", self.cmd_dev_list, "列出当前挂载的源码模块及其状态")
        cmd_dev.subcommand("reload", self.cmd_dev_reload, "手动强制重载指定的开发者模块")

        log.info(
            f"[{self.id}] 开发者模式启动！监听目录: ./{self.source_dir}/ | 打包输出: ./{self.output_dir}/ (热重载: {'开' if self.hot_reload else '关'})")

        # 启动时进行一次全量扫描和加载
        self.check_for_reloads()
        return True

    def main_loop(self):
        static["running"][self.id] = True

        # 文件监控主循环
        while not self.stop_event.is_set():
            if self.hot_reload:
                self.check_for_reloads()
            # 挂起等待，支持协作式秒退
            self.stop_event.wait(timeout=self.reload_interval)

    def teardown(self):
        if self.enabled:
            comm_lib.unregister("pack")
            comm_lib.unregister("dev")
            # 优雅卸载所有正在挂载的开发模块
            for mod_path, data in list(self.tracked_dirs.items()):
                if data['plugin_id']:
                    self.unload_dev_module(data['plugin_id'])
            log.info(f"[{self.id}] 已清理所有开发挂载模块。")

    # ================= 热重载核心引擎 =================
    def get_dir_mtime(self, dir_path):
        """获取目录下 .py 和 .json 文件的最新修改时间"""
        max_mtime = 0
        for root, _, files in os.walk(dir_path):
            for f in files:
                if f.endswith(('.py', '.json')):
                    try:
                        mtime = os.path.getmtime(os.path.join(root, f))
                        if mtime > max_mtime: max_mtime = mtime
                    except OSError:
                        pass
        return max_mtime

    def check_for_reloads(self):
        """扫描目录并对比修改时间"""
        if not os.path.exists(self.source_dir): return

        current_dirs = [os.path.join(self.source_dir, d) for d in os.listdir(self.source_dir) if
                        os.path.isdir(os.path.join(self.source_dir, d))]

        for mod_path in current_dirs:
            current_mtime = self.get_dir_mtime(mod_path)
            tracked = self.tracked_dirs.get(mod_path)

            # 1. 发现新放入的源码目录
            if not tracked:
                log.info(f"[{self.id}] 检测到新模块源码，正在挂载: {os.path.basename(mod_path)}")
                self.load_dev_module(mod_path)

            # 2. 发现代码被修改 (Ctrl+S 保存)
            elif current_mtime > tracked['mtime']:
                log.info(f"[{self.id}] 检测到代码更改，正在重载: {os.path.basename(mod_path)}")
                # 如果之前加载成功了，先把它安全卸载掉
                if tracked['plugin_id']:
                    self.unload_dev_module(tracked['plugin_id'])
                    time.sleep(0.3)  # 给线程一点释放端口和锁的时间
                # 重新挂载新代码
                self.load_dev_module(mod_path)

    def load_dev_module(self, mod_path):
        """直接从源码文件夹热加载模块到系统中"""
        folder_name = os.path.basename(mod_path)
        info_path = os.path.join(mod_path, "info.json")
        main_path = os.path.join(mod_path, "main.py")

        # 无论成功失败，都先更新文件的最后修改时间戳
        # 这样即使你写了语法错误导致崩溃，系统也不会无限重载，而是安静等待你下一次保存修复
        self.tracked_dirs[mod_path] = {'mtime': self.get_dir_mtime(mod_path), 'plugin_id': None}

        if not os.path.exists(info_path) or not os.path.exists(main_path):
            log.warning(f"[{self.id}] 源码缺少 info.json 或 main.py，跳过: {folder_name}")
            return False

        try:
            with open(info_path, 'r', encoding='utf-8') as f:
                info = json.load(f)
            plugin_id = info.get("id")

            # 关键：将开发目录临时加入系统路径，允许其内部 import 相对文件
            if mod_path not in sys.path:
                sys.path.insert(0, mod_path)

            # 关键：销毁旧的 Python 模块缓存，强制从硬盘读取最新代码
            module_name = f"dev_plugin_{plugin_id}"
            if module_name in sys.modules:
                del sys.modules[module_name]

            # 动态编译与反射导入
            spec = importlib.util.spec_from_file_location(module_name, main_path)
            plugin_module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = plugin_module
            spec.loader.exec_module(plugin_module)

            PluginClass = getattr(plugin_module, 'AFEDIUMPlugin', None)
            if not PluginClass:
                log.error(f"[{self.id}] {folder_name} 未继承或未定义 AFEDIUMPlugin 类")
                return False

            # 初始化并伪装成正规军加载进核心
            default_config = getattr(PluginClass, 'default_config', {})
            plugin_instance = PluginClass(info, Config(plugin_id, default_config))

            if not plugin_instance.setup():
                log.error(f"[{self.id}] 模块 {plugin_id} setup 失败")
                return False

            static["running"][plugin_id] = False
            module_thread = threading.Thread(target=plugin_instance.main_loop, name=plugin_id, daemon=True)

            with plugin_lock:
                threads[plugin_id] = module_thread
                loaded_plugins[plugin_id] = {
                    'info': info, 'path': mod_path,
                    'instance': plugin_instance, 'is_dev': True
                }

            module_thread.start()

            # 更新追踪器状态，标记成功挂载的 ID
            self.tracked_dirs[mod_path]['plugin_id'] = plugin_id
            log.info(f"[{self.id}] 开发挂载成功: {plugin_id}")
            return True

        except Exception as e:
            log.error(f"[{self.id}] 挂载源码 {folder_name} 时发生异常 (请修复代码后保存重试):\n{e}")
            log.debug(traceback.format_exc())
            return False

    def unload_dev_module(self, plugin_id):
        """利用新架构的安全退出机制，拔掉开发模块"""
        with plugin_lock:
            plugin_data = loaded_plugins.get(plugin_id)
            thread_obj = threads.get(plugin_id)
            if not plugin_data or not thread_obj: return

            if plugin_id in static["running"]:
                static["running"][plugin_id] = False

            instance = plugin_data.get('instance')
            if hasattr(instance, 'request_stop'):
                instance.request_stop()

        thread_obj.join(timeout=3)

        with plugin_lock:
            threads.pop(plugin_id, None)
            loaded_plugins.pop(plugin_id, None)
            # 清理系统路径
            mod_path = plugin_data.get('path')
            if mod_path in sys.path:
                sys.path.remove(mod_path)

        log.info(f"[{self.id}] 源码挂载已拔出: {plugin_id}")

    # ================= 开发者工具指令 =================
    def cmd_dev_list(self, ctx, args):
        ctx.reply("当前挂载的源码模块:")
        for p, data in self.tracked_dirs.items():
            status = f"ID: {data['plugin_id']}" if data['plugin_id'] else "挂载失败/未激活"
            ctx.reply(f" - {os.path.basename(p)} -> [{status}]")
        return "列表获取完毕"

    def cmd_dev_reload(self, ctx, args):
        if not args:
            return "用法: dev reload <模块ID>"
        pid = args[0]
        for p, data in self.tracked_dirs.items():
            if data['plugin_id'] == pid:
                ctx.reply(f"手动强制重载: {pid}")
                self.unload_dev_module(pid)
                self.load_dev_module(p)
                return "重载指令已下达"
        return f"未找到活动的挂载模块: {pid}"

    def command_pack(self, ctx, args):
        if not os.path.exists(self.source_dir):
            return f"错误: 目录 '{self.source_dir}' 不存在。"

        target_modules = args if args else [d for d in os.listdir(self.source_dir) if
                                            os.path.isdir(os.path.join(self.source_dir, d))]
        if not target_modules: return "目录中没有找到可打包的模块。"

        os.makedirs(self.output_dir, exist_ok=True)

        success_count = 0
        for mod_name in target_modules:
            mod_source_path = os.path.join(self.source_dir, mod_name)
            if not os.path.isdir(mod_source_path): continue

            target_base_path = os.path.join(self.output_dir, mod_name)
            pyz_file_path = target_base_path + '.pyz'
            zip_file_path = target_base_path + '.zip'

            try:
                ctx.reply(f"正在打包 '{mod_name}' 到 {self.output_dir}/ ...")
                if os.path.exists(pyz_file_path): os.remove(pyz_file_path)

                shutil.make_archive(target_base_path, 'zip', mod_source_path)
                os.rename(zip_file_path, pyz_file_path)

                ctx.reply(f"成功生成: {self.output_dir}/{mod_name}.pyz")
                success_count += 1
            except Exception as e:
                ctx.reply(f"打包 '{mod_name}' 失败: {e}")
                if os.path.exists(zip_file_path): os.remove(zip_file_path)

        return f"打包完毕，共输出 {success_count} 个模块到 ./{self.output_dir}/。"
