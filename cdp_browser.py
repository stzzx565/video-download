# -*- coding: utf-8 -*-
"""
通过 CDP(Chrome DevTools Protocol)直连系统 Edge/Chrome 无头浏览器,
收集目标网站下发的 Cookie —— 零第三方依赖(纯标准库 socket/json)。

用途:抖音接口需要真实浏览器安全 SDK 生成的设备身份 Cookie
(UIFID_TEMP 等),此模块用无头浏览器访问一次页面即可拿到。
取代 playwright(其 node 驱动体积超过 100MB,是打包体积的主要来源)。
"""

import base64
import json
import os
import re
import secrets
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request


class CdpError(Exception):
    """CDP 流程失败"""


# ---------------------------------------------------------------- 浏览器定位
_EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
_CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]


def find_browser_exe():
    """依次寻找 Edge / Chrome 可执行文件"""
    for path in _EDGE_PATHS + _CHROME_PATHS:
        if os.path.exists(path):
            return path
    raise CdpError("未找到 Edge 或 Chrome 浏览器(抖音解析需要其中之一)")


# ---------------------------------------------------------------- 极简 WebSocket
class _Ws:
    """只够 CDP 用的最小 WebSocket 客户端(文本帧)"""

    def __init__(self, host, port, path, timeout=20.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        # 读响应头
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CdpError("WebSocket 握手失败(连接中断)")
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise CdpError(f"WebSocket 握手被拒绝:{head[:80]!r}")
        self._buf = rest
        self._closed = False

    def send_text(self, text: str):
        payload = text.encode("utf-8")
        header = bytearray([0x81])  # FIN + text
        n = len(payload)
        mask = secrets.token_bytes(4)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + mask + masked)

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise CdpError("WebSocket 连接中断")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv_text(self) -> str:
        while True:
            b1, b2 = self._recv_exact(2)
            opcode = b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            payload = self._recv_exact(length)
            if opcode == 0x9:          # ping → pong
                self.sock.sendall(bytes([0x8A, 0x80]) + secrets.token_bytes(4))
                continue
            if opcode == 0x8:          # close
                self._closed = True
                raise CdpError("WebSocket 已被对端关闭")
            if opcode == 0x1:
                return payload.decode("utf-8", "replace")
            # 其它帧(二进制等)忽略

    def close(self):
        try:
            self.sock.sendall(bytes([0x88, 0x80]) + secrets.token_bytes(4))
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------- CDP 会话
def _http_json(url: str, timeout=10.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def mint_cookies(url: str, wait_names, *, timeout_s: float = 45.0,
                 browser_exe: str | None = None) -> dict:
    """
    无头打开 url,轮询等待 wait_names 中的 Cookie 全部出现后返回 jar。
    阻塞式,建议放到线程里调用。
    """
    exe = browser_exe or find_browser_exe()
    wait_names = set(wait_names)
    user_data = tempfile.mkdtemp(prefix="dlb_")
    args = [
        exe,
        "--headless=new",
        "--remote-debugging-port=0",
        f"--user-data-dir={user_data}",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--window-size=1536,864",
        "--lang=zh-CN",
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "--disable-blink-features=AutomationControlled",
        url,
    ]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)
    ws_url = None
    try:
        # 1. 从 stderr 读取 DevTools 调试地址
        deadline = time.time() + 20
        while time.time() < deadline:
            line = proc.stderr.readline()
            if not line:
                if proc.poll() is not None:
                    raise CdpError(f"浏览器启动失败(退出码 {proc.returncode})")
                continue
            m = re.search(r"DevTools listening on (ws://[^\s]+)", line.decode("utf-8", "replace"))
            if m:
                ws_url = m.group(1)
                break
        if not ws_url:
            raise CdpError("未获取到浏览器调试端口(DevTools listening 超时)")

        m = re.match(r"ws://([^:/]+):(\d+)(/.*)", ws_url)
        host, port, path = m.group(1), int(m.group(2)), m.group(3)

        # 2. 浏览器级 WS + Storage.getCookies 轮询
        ws = _Ws(host, port, path)
        jar = {}
        try:
            deadline = time.time() + timeout_s
            req_id = 0
            while time.time() < deadline:
                req_id += 1
                ws.send_text(json.dumps({"id": req_id, "method": "Storage.getCookies"}))
                # 读到对应 id 的响应(跳过事件)
                while True:
                    msg = json.loads(ws.recv_text())
                    if msg.get("id") == req_id:
                        break
                for c in (msg.get("result") or {}).get("cookies") or []:
                    jar[c["name"]] = c["value"]
                if wait_names <= set(jar):
                    break
                time.sleep(1.0)
            return jar
        finally:
            ws.close()
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            import shutil
            shutil.rmtree(user_data, ignore_errors=True)
        except Exception:
            pass
