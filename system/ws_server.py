import asyncio
import json
import secrets
import socket
import uuid
from datetime import datetime

import websockets
from lib.Event import Event
from lib.common import static
from lib.config import Config
from lib.logger import log
from lib.plugin import AfediumPluginBase

Info = {
    "name": "WS服务器",
    "id": "ws_server",
    "dependencies": [],
    "pip_dependencies": ["websockets"],
    "linux_dependencies": []
}


class BroadcastProtocol(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport


class AFEDIUMPlugin(AfediumPluginBase):
    default_config = {
        "server_name": "afedium",
        "server_port": 11840,
        "broadcast_port": 12840,
        "broadcast_interval": 1,
        "server_uuid": str(uuid.uuid4()),
        "features": {},
        "auth_type": "none",
        "auth_timeout": 60,
        "password": "afedium",
        "captcha_length": 6,
    }

    def setup(self):
        # 兼容自动获取主机名的默认配置
        if self.config.conf.get("server_name") == "afedium":
            self.config.conf["server_name"] = static.get("hostname", "afedium")
            self.config.update()

        self.verification_codes = {}
        self.connected_clients = set()
        self.loop = None

        # 注册用于发送响应的出口事件
        static["event_handler"].register_event("ExternalIO_OUT", self.handle_external_output)

        if "features" not in static:
            static["features"] = {}
        static["features"].update(self.config.conf.get("features", {}))

        auth_type = self.config.conf.get("auth_type", "none").lower()
        if auth_type not in ["none", "password", "captcha"]:
            log.warning(f"[{self.id}] 无效的认证类型: {auth_type}，有效值为 'none', 'password', 'captcha'")

        return True

    def main_loop(self):
        static["running"][self.id] = True
        try:
            asyncio.run(self.run_server())
        except Exception as e:
            log.error(f"[{self.id}] asyncio 循环异常: {e}")

    def teardown(self):
        static["event_handler"].unregister_event("ExternalIO_OUT", self.handle_external_output)
        log.info(f"[{self.id}] 已清理网络事件绑定")

    async def _websocket_handler(self, websocket):
        self.connected_clients.add(websocket)
        try:
            auth_type = self.config.conf["auth_type"].lower()
            if auth_type == "captcha":
                verification_code = ''.join(
                    secrets.choice('0123456789') for _ in range(self.config.conf["captcha_length"]))
                self.verification_codes[websocket] = verification_code
                log.info(f"新客户端连接，验证码：{verification_code}")

                await websocket.send(chr(0x90) + "请输入验证码")
                client_response = await asyncio.wait_for(websocket.recv(), timeout=self.config.conf["auth_timeout"])

                if isinstance(client_response, str) and client_response[0] == chr(0x90) and client_response[
                    1:] == verification_code:
                    del self.verification_codes[websocket]
                    await websocket.send(chr(0x91))
                    log.debug(f"客户端 {websocket.remote_address} 认证成功")
                else:
                    await websocket.send(chr(0x92))
                    log.warning(f"客户端 {websocket.remote_address} 认证失败")
                    return

            elif auth_type == "password":
                await websocket.send(chr(0x90) + "请输入密码")
                client_response = await asyncio.wait_for(websocket.recv(), timeout=self.config.conf["auth_timeout"])

                if isinstance(client_response, str) and client_response[0] == chr(0x90) and client_response[1:] == \
                        self.config.conf["password"]:
                    await websocket.send(chr(0x91))
                    log.debug(f"客户端 {websocket.remote_address} 认证成功")
                else:
                    await websocket.send(chr(0x92))
                    log.warning(f"客户端 {websocket.remote_address} 认证失败")
                    return

            # 主消息接收循环
            while not self.stop_event.is_set():
                # 附带超时以确保循环能检查 stop_event
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=2.0)
                    log.debug(f"[WsServer] 收到客户端消息 (长度: {len(message)})")
                    static["event_handler"].trigger_event(
                        Event("ExternalIO_IN", message=message, client_id=websocket)
                    )
                except asyncio.TimeoutError:
                    continue

        except websockets.exceptions.ConnectionClosedOK:
            log.debug(f"客户端 {websocket.remote_address} 正常断开连接。")
        except websockets.exceptions.ConnectionClosedError as e:
            log.warning(f"客户端 {websocket.remote_address} 异常断开连接: {e}")
        except Exception as e:
            log.error(f"[WsServer] 连接处理异常: {e}")
        finally:
            self.connected_clients.remove(websocket)
            self.verification_codes.pop(websocket, None)

    def handle_external_output(self, event: Event):
        response_data = event.data.get("response_data")
        client_websocket = event.data.get("client_id")

        if client_websocket and response_data is not None and self.loop and self.loop.is_running():
            log.debug(f"[WsServer] 发送响应给 {client_websocket.remote_address}")
            log.debug(f"[WsServer] {response_data}")
            asyncio.run_coroutine_threadsafe(
                client_websocket.send(response_data), self.loop
            )

    async def get_local_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return socket.gethostbyname(socket.gethostname())

    async def broadcast_server(self):
        transport, protocol = await self.loop.create_datagram_endpoint(
            lambda: BroadcastProtocol(),
            family=socket.AF_INET,
            allow_broadcast=True
        )
        local_ip = await self.get_local_ip()
        try:
            using_auth = self.config.conf["auth_type"].lower() != "none"
            while not self.stop_event.is_set():
                data = json.dumps({
                    "server_name": self.config.conf["server_name"],
                    "service": "afedium_server",
                    "ip": local_ip,
                    "port": self.config.conf["server_port"],
                    "UUID": self.config.conf["server_uuid"],
                    "feature": static.get("features", {}),
                    "using_auth": using_auth,
                    "auth_timeout": self.config.conf["auth_timeout"],
                    "timestamp": datetime.now().isoformat(),
                }).encode('utf-8')
                transport.sendto(data, ('255.255.255.255', self.config.conf["broadcast_port"]))

                # 分片睡眠，以便快速响应 stop_event
                for _ in range(int(self.config.conf["broadcast_interval"] * 10)):
                    if self.stop_event.is_set(): break
                    await asyncio.sleep(0.1)
        finally:
            transport.close()

    async def run_server(self):
        self.loop = asyncio.get_running_loop()
        static["asyncio_loop"] = self.loop

        broadcast_task = asyncio.create_task(self.broadcast_server())

        server = await websockets.serve(
            self._websocket_handler,
            "0.0.0.0",
            self.config.conf["server_port"],
            ping_interval=30,
            ping_timeout=60,
            close_timeout=10
        )
        log.info(f"[{self.id}] WebSocket 服务器已在端口 {self.config.conf['server_port']} 启动")

        # 挂起协程直到收到停止信号
        while not self.stop_event.is_set():
            await asyncio.sleep(0.5)

        # 优雅清理流程
        log.info(f"[{self.id}] 正在关闭 WebSocket 服务器...")
        broadcast_task.cancel()

        if self.connected_clients:
            log.info(f"[{self.id}] 正在强制断开 {len(self.connected_clients)} 个活跃连接...")
            close_tasks = [ws.close() for ws in self.connected_clients]
            # 并发执行所有断开任务，不等客户端回应
            await asyncio.gather(*close_tasks, return_exceptions=True)

        server.close()
        await server.wait_closed()
        log.info(f"[{self.id}] WebSocket 服务器已安全关闭。")
