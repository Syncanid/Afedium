import asyncio
import base64
import hashlib
import json
import os
import shutil
import traceback
import uuid

from core import get_info_handler, set_info_handler
from lib.Event import Event
from lib.command import command
from lib.common import loaded_plugins, static, plugin_lock
from lib.logger import log
from lib.plugin import AfediumPluginBase
from lib.support_lib import CustomJsonEncoder

CHUNK_SIZE = 1000 * 1024  # 1MB

Info = {
    "name": "外部指令处理器",
    "id": "external_handler",
    "dependencies": [],
    "pip_dependencies": [],
    "linux_dependencies": []
}


# --- 辅助函数保持原样 ---
def try_decode_bytes(data: bytes, encodings=('utf-8', 'gbk')) -> tuple[str, str]:
    for enc in encodings:
        try:
            return enc, data.decode(enc)
        except Exception:
            continue
    return 'gbk', data.decode('gbk', errors='ignore')


def calculate_sha256(data: bytes) -> str:
    if not isinstance(data, bytes):
        raise TypeError("calculate_sha256 的输入必须是字节类型。")
    return hashlib.sha256(data).hexdigest()


def calculate_file_sha256(file_path: str) -> str:
    sha256_hash = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()
    except FileNotFoundError:
        return ""


def b64_encode_bytes(b: bytes) -> str:
    return base64.b64encode(b).decode()


def b64_decode(txt, file):
    data = base64.b64decode(txt)
    with open(file, 'wb') as output:
        output.write(data)
    return f"文件 {file} 写入成功\n"


class AFEDIUMPlugin(AfediumPluginBase):
    default_config = {}

    def setup(self):
        self.upload_sessions = {}
        self.download_sessions = {}
        static["event_handler"].register_event("ExternalIO_IN", self.process_input_event)

        # 严格还原能力注册，确保客户端正确识别
        if "features" not in static:
            static["features"] = {}
        static["features"].update({
            "info_access": True,
            "file_transfer": True,
            "file_management": True,
            "terminal": True,
            "system_upgrade": True,
        })
        return True

    def main_loop(self):
        static["running"][self.id] = True
        self.stop_event.wait()

    def teardown(self):
        static["event_handler"].unregister_event("ExternalIO_IN", self.process_input_event)
        self.upload_sessions.clear()
        self.download_sessions.clear()
        log.info(f"[{self.id}] 已清理事件监听和传输会话")

    # 严格还原 0x03 状态返回的数据结构，同时加入线程锁保障安全
    def get_running_safe(self):
        running = {}
        with plugin_lock:
            for module_id in static.get("running", {}).keys():
                try:
                    plugin_data = loaded_plugins.get(module_id)
                    if not plugin_data:
                        running[module_id] = static["running"][module_id]
                        continue

                    module_name = module_id
                    if hasattr(plugin_data, 'Info'):
                        module_name = plugin_data.Info.get("name", module_id)
                    elif isinstance(plugin_data, dict) and 'info' in plugin_data:
                        module_name = plugin_data['info'].get("name", module_id)

                    running[module_name] = static["running"][module_id]

                except KeyError:
                    running[module_id] = static["running"].get(module_id, "unknown")
        return running

    def process_input_event(self, event: Event):
        loop = static.get("asyncio_loop")
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(self.async_worker(event), loop)
        else:
            log.error("[External Handler] 未找到正在运行的 asyncio 事件循环！")

    async def async_worker(self, event: Event):
        command_message = event.data.get("message")
        client_id = event.data.get("client_id")

        if not command_message:
            return

        result = await self._handle_command_and_return(command_message, client_id)

        if result is not None:
            static["event_handler"].trigger_event(
                Event("ExternalIO_OUT", response_data=result, client_id=client_id)
            )

    async def _handle_command_and_return(self, command_message, client_id):
        try:
            # 兼容 Bytes (文件上传流) 和 Str
            if isinstance(command_message, bytes):
                # 尝试将字节流按字符串处理，以兼容原有逻辑（如果客户端传的是bytes格式的指令）
                try:
                    command_message = command_message.decode('utf-8')
                except UnicodeDecodeError:
                    # 如果纯二进制无法decode，说明它是未预期的纯二进制文件块或错乱数据
                    # 在你原有的代码中并没有单独对纯bytes的预处理，它默认 websocket recv 的是 str
                    pass

            if isinstance(command_message, str):
                ctrl = ord(command_message[0])
                message = command_message[1:]

                if ctrl == 0x01:
                    uid = message[0:4]
                    command_to_run = message[4:]
                    cmd_parts = command_to_run.strip().split()

                    if not cmd_parts:
                        return chr(0x01) + uid + "错误: 空指令\n"

                    loop = asyncio.get_running_loop()
                    # 这里调用第五阶段重构后的 command 接口
                    full_output = await loop.run_in_executor(None, command, cmd_parts, client_id)
                    return chr(0x01) + uid + str(full_output)

                elif ctrl == 0x02:
                    uid = message[0:4]
                    message = message[4:]
                    message_parts = message.split(' ', 1)
                    if len(message_parts) < 1:
                        response_content = "参数不足"
                    else:
                        sub_command = message_parts[0]
                        sub_args = message_parts[1].split(' ') if len(message_parts) > 1 else []
                        if sub_command == "get":
                            response_content = get_info_handler(sub_args)
                        elif sub_command == "set":
                            response_content = set_info_handler(sub_args)
                        else:
                            response_content = "unknown"
                    return chr(0x02) + uid + str(response_content)

                elif ctrl == 0x03:
                    uid = message[0:4]
                    data = {
                        "/T/V系统": static.get("SYS_INFO", "Unknown"),
                        "/APython版本: ": static.get("PY_VER", "Unknown"),
                        "/P在线模式": static.get("online", False),
                        "/P/AGit可用": static.get("git_available", False),
                        "/L/A访问项目": "https://github.com/furryaxw/AFEDIUM/",
                        "/C/Vmodules": self.get_running_safe(),  # 严格保持原有的 Dict[str, bool] 格式
                    }
                    return chr(0x03) + uid + json.dumps(data, ensure_ascii=False, cls=CustomJsonEncoder)

                elif ctrl == 0x11:  # 上传握手 / 小文件上传
                    parts = message.split(' ', 3)
                    if len(parts) == 4:
                        file_path, base64_data, expected_checksum, encoding = parts
                        try:
                            data_bytes = base64.b64decode(base64_data)
                            calculated_checksum = calculate_sha256(data_bytes)
                            if calculated_checksum != expected_checksum:
                                return chr(0x11) + f"错误: 校验和不匹配: {file_path} 校验失败"

                            with open(file_path, 'wb') as f:
                                f.write(data_bytes)
                            return chr(0x11) + f"{file_path} 保存成功"
                        except Exception as e:
                            return chr(0x11) + f"保存失败: {e}"
                    elif len(parts) == 3 and parts[1].isdigit():
                        file_path, chunk_count_str, expected_sha256 = parts
                        try:
                            chunk_count = int(chunk_count_str)
                        except ValueError:
                            return chr(0x11) + "参数错误: chunk_count 无效"

                        session_id = str(uuid.uuid4())
                        self.upload_sessions[session_id] = {
                            "file_path": file_path,
                            "chunks": chunk_count,
                            "received_chunk_indices": set(),
                            "received_chunks_data": {},
                            "expected_sha256": expected_sha256,
                            "client_id": client_id
                        }
                        return chr(0x11) + session_id
                    elif len(parts) == 2:
                        file_path = parts[0]
                        base64_data = parts[1]
                        try:
                            data = base64.b64decode(base64_data)
                            with open(file_path, 'wb') as f:
                                f.write(data)
                            return chr(0x11) + f"{file_path} 保存成功"
                        except Exception as e:
                            return chr(0x11) + f"保存失败: {e}"
                    else:
                        return chr(0x11) + "参数错误"

                elif ctrl == 0x18:  # 分块传输 (上传)
                    parts = message.split(' ', 2)
                    if len(parts) == 3:
                        session_id, chunk_index_str, base64_chunk = parts
                        session = self.upload_sessions.get(session_id)
                        if not session:
                            return chr(0x18) + "无效上传会话"

                        try:
                            chunk_index = int(chunk_index_str)
                        except ValueError:
                            return chr(0x18) + "参数错误: chunk_index 无效"

                        session["received_chunk_indices"].add(chunk_index)
                        session["received_chunks_data"][chunk_index] = base64.b64decode(base64_chunk)

                        if len(session["received_chunk_indices"]) == session["chunks"]:
                            full_data_bytes = b''
                            for i in range(session["chunks"]):
                                if i not in session["received_chunks_data"]:
                                    log.warning(f"组装文件时会话 {session_id} 缺少分块 {i}。")
                                    return chr(0x18) + f"文件组装失败: 缺失分块"
                                full_data_bytes += session["received_chunks_data"][i]

                            file_path = session["file_path"]
                            with open(file_path, 'wb') as f:
                                f.write(full_data_bytes)

                            calculated_sha256 = calculate_sha256(full_data_bytes)
                            expected_sha256 = session["expected_sha256"]
                            del self.upload_sessions[session_id]

                            if calculated_sha256 == expected_sha256:
                                response_message = chr(0x18) + f"END {calculated_sha256}"
                            else:
                                response_message = chr(0x18) + f"ERROR_SHA_MISMATCH {calculated_sha256}"

                            static["event_handler"].trigger_event(
                                Event("ExternalIO_OUT", response_data=response_message, client_id=client_id)
                            )
                            return None
                        return None
                    else:
                        return chr(0x18) + "参数错误"

                elif ctrl == 0x19:  # 缺失分块重新协商
                    parts = message.split(' ', 1)
                    if len(parts) == 2:
                        session_id, missing_indices_str = parts
                        try:
                            missing_indices = json.loads(missing_indices_str)
                        except json.JSONDecodeError:
                            return chr(0x19) + "参数错误: missing_indices 无效JSON"

                        download_session = self.download_sessions.get(session_id)
                        if download_session:
                            file_path = download_session["file"]
                            for chunk_index in missing_indices:
                                if session_id not in self.download_sessions:
                                    log.info(f"下载会话 {session_id} 在重发过程中被取消。")
                                    break
                                with open(file_path, 'rb') as f:
                                    f.seek(chunk_index * CHUNK_SIZE)
                                    chunk = f.read(CHUNK_SIZE)
                                    if chunk:
                                        encoded_chunk = base64.b64encode(chunk).decode()
                                        static["event_handler"].trigger_event(
                                            Event("ExternalIO_OUT", response_data=chr(
                                                0x18) + f"{session_id} {chunk_index} {encoded_chunk}",
                                                  client_id=client_id)
                                        )
                            if session_id in self.download_sessions:
                                static["event_handler"].trigger_event(
                                    Event("ExternalIO_OUT", response_data=chr(0x18) + "END", client_id=client_id)
                                )
                            return None
                    return chr(0x19) + "参数错误"

                elif ctrl == 0x1A:  # 取消下载
                    session_id = message.strip()
                    if session_id in self.download_sessions:
                        log.info(f"客户端正在取消下载会话: {session_id}")
                        del self.download_sessions[session_id]
                        static["event_handler"].trigger_event(
                            Event("ExternalIO_OUT", response_data=chr(0x1A) + "CANCELLED", client_id=client_id)
                        )
                    return None

                elif ctrl == 0x1B:  # 确认下载，开始大文件传输
                    session_id = message.strip()
                    download_session = self.download_sessions.get(session_id)
                    if download_session:
                        file_path = download_session["file"]
                        chunk_count = download_session["chunks"]
                        log.info(f"客户端已确认下载会话 {session_id}。开始分块传输。")
                        self._send_file_chunks(session_id, file_path, chunk_count, client_id)
                    else:
                        return chr(0x1B) + "错误: 无效的会话ID"
                    return None

                elif ctrl == 0x13:  # 请求文件下载 / 信息
                    file_path = message.strip()
                    if not os.path.exists(file_path):
                        return chr(0x13) + f"错误: 文件不存在 {file_path}"
                    else:
                        file_size = os.path.getsize(file_path)
                        if file_size > CHUNK_SIZE:
                            file_sha256 = calculate_file_sha256(file_path)
                            chunk_count = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE
                            session_id = str(uuid.uuid4())
                            self.download_sessions[session_id] = {
                                "file": file_path, "chunks": chunk_count, "file_sha256": file_sha256,
                                "client_id": client_id
                            }
                            return chr(0x13) + f"{session_id}:{chunk_count}:{file_sha256}"
                        else:
                            try:
                                with open(file_path, 'rb') as f:
                                    content = f.read()
                                encoding_type = 'binary'
                                if file_path.lower().endswith(('.txt', '.md', '.json', '.csv', '.log', '.xml')):
                                    try:
                                        encoding, decoded_text = try_decode_bytes(content)
                                        encoding_type = encoding
                                        encoded_b64 = base64.b64encode(decoded_text.encode(encoding)).decode()
                                    except Exception:
                                        encoded_b64 = base64.b64encode(content).decode()
                                else:
                                    encoded_b64 = base64.b64encode(content).decode()

                                file_sha256 = calculate_sha256(content)
                                return chr(0x13) + f"{encoding_type}:{encoded_b64}:{file_sha256}"
                            except Exception as e:
                                err_msg = f"读取文件错误: {e}".encode()
                                return chr(0x13) + f"utf-8:{base64.b64encode(err_msg).decode()}:"

                elif ctrl == 0x12:  # 删除
                    target = message.strip()
                    if os.path.isdir(target):
                        shutil.rmtree(target)
                        return chr(0x12) + f"目录已删除: {target}"
                    elif os.path.isfile(target):
                        os.remove(target)
                        return chr(0x12) + f"文件已删除: {target}"
                    else:
                        return chr(0x12) + f"路径不存在: {target}"

                elif ctrl == 0x14:  # 目录列表
                    path_ls = message.strip() or './'
                    if not os.path.isdir(path_ls):
                        return chr(0x14) + json.dumps({"error": f"{path_ls} 不是一个有效目录"})
                    else:
                        files = os.listdir(path_ls)
                        ans = {}
                        for file in files:
                            full_path = os.path.join(path_ls, file)
                            permission = 'd' if os.path.isdir(full_path) else 'f'
                            permission += 'r' if os.access(full_path, os.R_OK) else '-'
                            permission += 'w' if os.access(full_path, os.W_OK) else '-'
                            permission += 'x' if os.access(full_path, os.X_OK) else '-'
                            ans[file] = permission
                        return chr(0x14) + json.dumps(ans)

                elif ctrl == 0x15:  # 文件状态
                    try:
                        stat_result = os.stat(message)
                        stat_dict = {
                            "size": stat_result.st_size, "mtime": stat_result.st_mtime,
                            "ctime": stat_result.st_ctime, "mode": stat_result.st_mode,
                            "uid": stat_result.st_uid, "gid": stat_result.st_gid,
                        }
                        return chr(0x15) + json.dumps(stat_dict)
                    except Exception as e:
                        return chr(0x15) + json.dumps({"error": str(e)})

                elif ctrl == 0x16:  # 创建目录
                    folder_path = message.strip()
                    try:
                        if not os.path.exists(folder_path):
                            os.makedirs(folder_path)
                            return chr(0x16) + f"目录创建成功: {folder_path}"
                        else:
                            return chr(0x16) + f"目录已存在: {folder_path}"
                    except Exception as e:
                        return chr(0x16) + f"目录创建失败: {e}"

                elif ctrl == 0x17:  # 重命名
                    try:
                        old_path, new_path = message.split(' ', 1)
                        if not os.path.exists(old_path):
                            return chr(0x17) + f"源路径不存在: {old_path}"
                        elif os.path.exists(new_path):
                            return chr(0x17) + f"目标路径已存在: {new_path}"
                        else:
                            os.rename(old_path, new_path)
                            return chr(0x17) + f"重命名成功: {old_path} -> {new_path}"
                    except Exception as e:
                        return chr(0x17) + f"重命名失败: {e}"
                else:
                    return f"未知命令: {hex(ctrl)}\n"

        except Exception as e:
            log.error(f"处理外部输入时异常：{e}")
            log.debug(traceback.format_exc())
            try:
                if isinstance(command_message, str):
                    ctrl_char = command_message[0]
                    uid_for_error = command_message[1:5]
                    return f"{ctrl_char}{uid_for_error}指令执行异常：{e}\n"
            except IndexError:
                pass
            return f"指令解析或执行异常：{e}\n"

    def _send_file_chunks(self, session_id: str, file_path: str, chunk_count: int, client_id: str):
        try:
            with open(file_path, 'rb') as f:
                for chunk_index in range(chunk_count):
                    if session_id not in self.download_sessions:
                        log.info(f"下载会话 {session_id} 在发送分块时被取消。")
                        break
                    f.seek(chunk_index * CHUNK_SIZE)
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    encoded_chunk = base64.b64encode(chunk).decode()
                    response_message = chr(0x18) + f"{session_id} {chunk_index} {encoded_chunk}"
                    static["event_handler"].trigger_event(
                        Event("ExternalIO_OUT", response_data=response_message, client_id=client_id)
                    )
            if session_id in self.download_sessions:
                static["event_handler"].trigger_event(
                    Event("ExternalIO_OUT", response_data=chr(0x18) + "END", client_id=client_id)
                )
                del self.download_sessions[session_id]
        except Exception as e:
            error_message = f"下载分块发送异常：{e}"
            log.error(error_message)
            if session_id in self.download_sessions:
                del self.download_sessions[session_id]
