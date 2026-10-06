# -*- coding: utf-8 -*-
"""
元宝登录态获取(供视频号解析使用)。

流程:程序打开一个可见的 Edge 窗口(独立配置,不影响用户平时的
浏览器),用户在窗口里微信扫码登录 yuanbao.tencent.com,然后回到
程序点击确认,程序通过 CDP 从浏览器内核读取登录 Cookie 并保存。

之后解析直接使用保存的 Cookie;若服务端返回登录失效(401),
解析器会删除 Cookie 文件,下次解析重新走一遍本流程。
"""

import json
import os
import re
import subprocess
import time
import urllib.request

from downloader_core import APP_DIR, WxNeedLoginError

COOKIE_PATH = os.path.join(APP_DIR, "wx_cookie.txt")
PROFILE_DIR = os.path.join(APP_DIR, "wx_edge_profile")
LOGIN_URL = "https://yuanbao.tencent.com/"

NeedLoginError = WxNeedLoginError


class WxLoginError(Exception):
    """登录流程失败"""


def _free_port(start: int) -> int:
    import socket
    for port in range(start, start + 20):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            s.close()
    return start


def load_cookie():
    try:
        with open(COOKIE_PATH, "r", encoding="utf-8") as f:
            cookie = f.read().strip()
        return cookie or None
    except OSError:
        return None


def save_cookie(cookie: str):
    with open(COOKIE_PATH, "w", encoding="utf-8") as f:
        f.write(cookie)


def clear_cookie():
    try:
        os.remove(COOKIE_PATH)
    except OSError:
        pass


class LoginSession:
    """一次元宝登录会话:打开窗口 → (用户扫码)→ grab() 读取登录态"""

    def __init__(self, on_status=None):
        self.on_status = on_status
        self._proc = None
        self._ws = None
        self._port = None

    def _status(self, msg):
        if self.on_status is not None:
            try:
                self.on_status(msg)
            except Exception:
                pass

    def start(self):
        """打开可见浏览器窗口(独立 profile),建立 CDP 调试通道"""
        from cdp_browser import _Ws, find_browser_exe
        exe = find_browser_exe()
        os.makedirs(PROFILE_DIR, exist_ok=True)
        self._port = _free_port(9223)
        args = [
            exe,
            f"--user-data-dir={PROFILE_DIR}",
            f"--remote-debugging-port={self._port}",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--window-size=1100,820",
            LOGIN_URL,
        ]
        self._status("正在打开登录窗口,请在窗口中用微信扫码登录元宝…")
        self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)

        # 轮询 DevTools HTTP 接口(必须无代理 opener:系统代理若开启,
        # urllib 会把 127.0.0.1 的请求也绕经代理导致连不上)
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}))
        ws_url = None
        stderr_tail = ""
        deadline = time.time() + 30
        while time.time() < deadline:
            if self._proc.poll() is not None:
                break
            try:
                with opener.open(
                        f"http://127.0.0.1:{self._port}/json/version",
                        timeout=2) as resp:
                    info = json.loads(resp.read().decode("utf-8", "replace"))
                ws_url = info.get("webSocketDebuggerUrl") or ""
                if ws_url:
                    break
            except Exception:
                time.sleep(0.5)

        if not ws_url:
            code = self._proc.poll()
            try:
                self._proc.terminate()
            except Exception:
                pass
            hint = "(窗口已打开但调试接口未就绪)" if code is None \
                else f"(浏览器退出,code={code})"
            raise WxLoginError(f"登录窗口启动失败{hint}")

        mm = re.match(r"ws://([^:/]+):(\d+)(/.*)", ws_url)
        host, port2, path = mm.group(1), int(mm.group(2)), mm.group(3)
        self._ws = _Ws(host, port2, path, timeout=15)

    def grab(self) -> str:
        """读取当前全部 Cookie(过滤 tencent.com 域)并保存,返回 Cookie 头"""
        if self._ws is None:
            raise WxLoginError("登录窗口未启动")
        time.sleep(1)
        req_id = 0
        deadline = time.time() + 20
        last_err = ""
        while time.time() < deadline:
            req_id += 1
            try:
                self._ws.send_text(json.dumps(
                    {"id": req_id, "method": "Storage.getCookies"}))
                while True:
                    msg = json.loads(self._ws.recv_text())
                    if msg.get("id") == req_id:
                        break
                all_cookies = ((msg.get("result") or {}).get("cookies")) or []
                jar = {c["name"]: c["value"] for c in all_cookies
                       if "tencent.com" in (c.get("domain") or "")
                       and c.get("value")}
                if jar:
                    cookie = "; ".join(f"{k}={v}" for k, v in jar.items())
                    save_cookie(cookie)
                    return cookie
                last_err = f"已连接但 Cookie 为空(共收到 {len(all_cookies)} 条)"
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
            time.sleep(1.5)
        raise WxLoginError(f"未读取到 Cookie({last_err or '请确认已在窗口中完成登录'})")

    def close(self):
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:
            pass
        try:
            if self._proc is not None:
                self._proc.terminate()
        except Exception:
            pass
        self._ws = None
        self._proc = None
