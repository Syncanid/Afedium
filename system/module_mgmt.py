import json
import os
import subprocess
import threading
import time
import urllib
from urllib.parse import urlparse

import requests
from lib.common import static, dynamic, comm_lib
from lib.logger import log
from lib.plugin import AfediumPluginBase
from lib.support_lib import is_valid_url, load_pyz_module

ENDPOINT_COMPATIBLE_VERSION = 2

Info = {
    "name": "模块管理器",
    "id": "module_mgmt",
    "dependencies": [],
    "pip_dependencies": ["requests"],
    "linux_dependencies": []
}


class AFEDIUMPlugin(AfediumPluginBase):
    default_config = {
        "modules_endpoint": "http://modules.afedium.furryaxw.top/index.json",
        "modules": {
            "core": {
                "id": "core",
                "type": "core",
                "method": "none",
                "source": "waiting",
            },
        },
        "waiting": [],
    }

    def setup(self):
        self.module_data = {}
        self.site_url = ""
        dynamic[self.id] = {}

        # 注册模块命令
        cmd = comm_lib.register("module", description="模块管理系统")
        cmd.subcommand("install", self.install_modules, "从仓库安装指定的模块")
        cmd.subcommand("update", self.update_modules, "从云端更新模块列表仓库")
        cmd.subcommand("upgrade", self.upgrade_modules, "升级已安装的模块到最新版本")

        if "features" not in static:
            static["features"] = {}
        static["features"].update({"module_mgmt": True})

        # 启动后台 Git 源检查
        threading.Thread(target=self.setup_core_git_source, daemon=True).start()
        return True

    def main_loop(self):
        # 系统模块管理器没有高频轮询需求，只需安静等待退出信号
        static["running"][self.id] = True
        self.stop_event.wait()

    def teardown(self):
        # 优雅清理：注销指令
        comm_lib.unregister("module")
        log.info(f"[{self.id}] 模块管理器已注销核心指令，优雅关闭。")

    def setup_core_git_source(self):
        # 等待 core 完全启动
        while not static.get("running", {}).get("core", False):
            if self.stop_event.is_set(): return
            time.sleep(0.5)

        try:
            is_git_repo = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                capture_output=True, text=True
            ).stdout.strip()

            if is_git_repo == "true":
                source = subprocess.run(["git", "config", "--get", "remote.origin.url"], capture_output=True,
                                        text=True).stdout.strip()
                branch = subprocess.run(["git", "branch", "--show-current"], capture_output=True,
                                        text=True).stdout.strip()
                self.config.conf["modules"]["core"] = {
                    "id": "core", "type": "core", "method": "git",
                    "source": source, "branch": branch,
                }
            else:
                self.config.conf["modules"]["core"]["method"] = "none"
                self.config.conf["modules"]["core"]["source"] = "local"
            self.config.update()

            if self.config.conf.get("waiting"):
                log.info("发现未完成的安装队列，准备继续安装...")
                from lib.command import CommandContext
                self.install_modules(CommandContext(), [])

        except FileNotFoundError:
            log.warning("Git 未安装，跳过 Git 源配置。")
        except Exception as e:
            log.error(f"配置 Git 源时出错: {e}")

    # --- 核心业务逻辑 ---
    def install_modules(self, ctx, modules: list):
        if not static.get("online"):
            ctx.reply("错误: 系统当前处于离线模式，无法安装模块。")
            return False

        # 1. 依赖解析与队列构建
        for module in modules.copy() if modules else []:
            if module not in self.module_data:
                ctx.reply(f"失败: 无法在模块仓库中找到模块 '{module}'")
                continue
            if module in static.get("modules", {}):
                ctx.reply(f"提示: {module} 已安装")
                if module in self.config.conf["waiting"]:
                    self.config.conf["waiting"].remove(module)
                continue

            if module not in self.config.conf["waiting"]:
                self.config.conf["waiting"].append(module)

            if self.module_data[module].get("dependencies"):
                for dependent in self.module_data[module]["dependencies"]:
                    if dependent in list(static.get("modules", {}).keys()) + self.config.conf["waiting"]:
                        ctx.reply(f"依赖已满足或已入队: {dependent}")
                    else:
                        ctx.reply(f"检测到新依赖: {dependent}，将加入安装队列首位")
                        if dependent not in self.config.conf["waiting"]:
                            self.config.conf["waiting"].insert(0, dependent)
        self.config.update()

        # 2. 事务性队列消费
        for module_n in self.config.conf["waiting"].copy():
            if self.stop_event.is_set():
                ctx.reply("安装被系统中止")
                return False
            try:
                module = self.module_data.get(module_n)
                if not module:
                    ctx.reply(f"错误: 仓库中找不到 {module_n} 的元数据，跳过。")
                    self.config.conf["waiting"].remove(module_n)
                    continue

                # 二次依赖校验：防止它的前置依赖在上一步安装失败导致连环崩溃
                deps_met = True
                for dep in module.get("dependencies", []):
                    if dep not in static.get("modules", {}):
                        ctx.reply(f"中止安装 {module_n}: 缺少前置依赖 {dep} (可能该依赖未能成功加载)。")
                        deps_met = False
                        break
                if not deps_met:
                    self.config.conf["waiting"].remove(module_n)
                    self.config.update()
                    continue

                ctx.reply(f"开始下载: {module_n}...")
                module_url = module["url"] if is_valid_url(module["url"]) else urllib.parse.urljoin(self.site_url,
                                                                                                    module["url"])
                path = static.get("pyz_module_path", "pyz_modules")

                # 隔离下载：下载到临时文件名
                tmp_file_name = f"{module_n}_temp.pyz"
                downloaded_file = self.download_file(module_url, path, ctx, force_filename=tmp_file_name)

                if not downloaded_file:
                    ctx.reply(f"下载 {module_n} 失败，已跳过。")
                    continue

                tmp_module_path = os.path.join(path, tmp_file_name)
                final_module_path = os.path.join(path, f"{module_n}.pyz")
                backup_path = final_module_path + ".bak"

                # 备份旧文件
                if os.path.exists(final_module_path):
                    import shutil
                    shutil.move(final_module_path, backup_path)

                # 提交新文件
                os.rename(tmp_module_path, final_module_path)

                # --- 先尝试加载，通过后再登记 ---
                ctx.reply(f"正在尝试验证并加载 {module_n}...")
                load_result = load_pyz_module(final_module_path)

                if load_result is not None:
                    # 加载成功：提交状态
                    self.config.conf["modules"][module_n] = {
                        "id": module["id"], "type": "pyz", "method": "get", "source": module_url
                    }
                    self.config.conf["waiting"].remove(module_n)
                    self.config.update()
                    static["modules"][module_n] = module.get("version")
                    ctx.reply(f"🎉 模块 {module_n} 安装并启动成功！")

                    if os.path.exists(backup_path):
                        os.remove(backup_path)  # 清理备份
                else:
                    # 加载失败：执行回滚
                    ctx.reply(f"⚠️ 模块 {module_n} 加载验证失败，正在回滚...")
                    if os.path.exists(final_module_path):
                        os.remove(final_module_path)
                    if os.path.exists(backup_path):
                        os.rename(backup_path, final_module_path)
                        ctx.reply(f"已恢复 {module_n} 的历史版本文件。")

                    self.config.conf["waiting"].remove(module_n)
                    self.config.update()

            except Exception as e:
                log.error(f"安装流程异常: {e}")
                ctx.reply(f"安装 {module_n} 时发生内部错误: {e}")
                if module_n in self.config.conf["waiting"]:
                    self.config.conf["waiting"].remove(module_n)
                    self.config.update()

        ctx.reply("安装队列处理完毕")
        return True

    def upgrade_modules(self, ctx, modules: list):
        if not static.get("online"): return "离线模式无法升级"

        from lib.command import command
        for module in modules:
            # 升级前的安全释放：尝试优雅地关闭旧版本正在运行的线程
            ctx.reply(f"准备升级: 正在尝试终止旧版本 {module} 的后台进程...")
            command(["quit", module])
            static.get("modules", {}).pop(module, None)

        return self.install_modules(ctx, modules)

    def update_modules(self, ctx):
        if not static.get("online"): return "离线模式无法更新仓库"
        try:
            raw = self.get_json_from_url(self.config.conf["modules_endpoint"])
            if not raw: return "错误: 无法获取模块列表。"

            endpoint_version = raw.get("version")
            if endpoint_version != ENDPOINT_COMPATIBLE_VERSION:
                ctx.reply(f"错误：模块仓库版本不匹配 (仓库: {endpoint_version}, 需要: {ENDPOINT_COMPATIBLE_VERSION})。")
                return "操作中止。"

            self.site_url = raw["base_url"]
            for module in raw["modules"]:
                self.module_data[module["id"]] = module
            dynamic[self.id]["data"] = self.module_data
            return json.dumps(self.module_data, ensure_ascii=False)
        except Exception as e:
            log.error(f"更新模块列表失败: {e}")
            return f"更新模块列表失败: {e}"

    def get_json_from_url(self, url):
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            log.error(f"从 {url} 获取 JSON 时出错: {e}")
        return None

    def download_file(self, url, folder_path='./', ctx=None, force_filename=None):
        try:
            os.makedirs(folder_path, exist_ok=True)
            if force_filename:
                filename = force_filename
            else:
                parsed_url = urlparse(url)
                filename = os.path.basename(parsed_url.path) or "downloaded_file"

            file_path = os.path.join(folder_path, filename)
            log.info(f"开始下载: {url}")
            if ctx: ctx.reply(f"下载中: {filename} ...")

            with requests.get(url, stream=True, timeout=15) as r:
                r.raise_for_status()
                with open(file_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if self.stop_event.is_set(): return False
                        f.write(chunk)

            log.info(f"下载成功: {file_path}")
            return filename
        except Exception as e:
            log.error(f"下载失败: {e}")
            return False
