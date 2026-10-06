# -*- coding: utf-8 -*-
"""
ISAAC64 密钥流求值器(视频号视频解密用)。

原理:视频号视频文件头 ~128KB 用 ISAAC64 伪随机流 XOR 加密,密钥流由
接口返回的 decodeKey 播种生成。微信播放器自带的 decrypt.js(取自
res-downloader 项目,内嵌 WASM)负责生成密钥流。

本模块把 decrypt.js 内联进一个本地 HTML,用 CDP 无头 Edge 加载求值
(零移植风险,算法与微信播放器完全一致)。无头 Edge 惰性启动、进程复用。
"""

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

from downloader_core import APP_DIR


def _decrypt_js_candidates():
    paths = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            paths.append(os.path.join(meipass, "assets", "decrypt.mjs"))
            paths.append(os.path.join(meipass, "decrypt.mjs"))
    paths.append(os.path.join(APP_DIR, "assets", "decrypt.mjs"))
    paths.append(os.path.join(APP_DIR, "decrypt.mjs"))
    return paths

_RUNNER_HTML = """<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>
<script type='module'>
__DECRYPT_SRC__
window.__getB64 = async function (seed) {
  for (let i = 0; i < 500; i++) {
    try {
      const arr = getDecryptionArray(seed);
      let bin = "";
      const CH = 8192;
      for (let j = 0; j < arr.length; j += CH) {
        bin += String.fromCharCode.apply(null, arr.subarray(j, j + CH));
      }
      return btoa(bin);
    } catch (e) {
      await new Promise(r => setTimeout(r, 60));
    }
  }
  throw new Error("wasm init timeout");
};
window.__runnerReady = true;
</script></body></html>"""


class IsaacError(Exception):
    pass


class IsaacEvaluator:
    """无头 Edge + decrypt.js 求值器(线程安全)"""

    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self._ws = None
        self._req_id = 0

    # -------------------------------------------------- 生命周期
    def _start(self):
        from cdp_browser import _Ws, find_browser_exe
        src = None
        for path in _decrypt_js_candidates():
            if os.path.exists(path):
                src = open(path, "r", encoding="utf-8").read()
                break
        if src is None:
            raise IsaacError("缺少 decrypt.mjs(视频号解密脚本)")
        src = src.replace("export {getDecryptionArray}", "")
        html = _RUNNER_HTML.replace("__DECRYPT_SRC__", src)
        runner = os.path.join(tempfile.gettempdir(), "wxd_runner.html")
        with open(runner, "w", encoding="utf-8") as f:
            f.write(html)

        exe = find_browser_exe()
        profile = tempfile.mkdtemp(prefix="dlb_isaac_")
        args = [exe, "--headless=new", "--remote-debugging-port=0",
                f"--user-data-dir={profile}", "--no-first-run",
                "--no-default-browser-check", "--no-sandbox",
                "--allow-file-access-from-files", "about:blank"]
        self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE)
        ws_url = None
        deadline = time.time() + 20
        while time.time() < deadline:
            line = self._proc.stderr.readline()
            if not line:
                if self._proc.poll() is not None:
                    raise IsaacError("无头浏览器启动失败")
                continue
            m = re.search(r"DevTools listening on (ws://[^\s]+)",
                          line.decode("utf-8", "replace"))
            if m:
                ws_url = m.group(1)
                break
        if not ws_url:
            raise IsaacError("未获取到浏览器调试端口")
        mm = re.match(r"ws://([^:/]+):(\d+)(/.*)", ws_url)
        host, port, path = mm.group(1), int(mm.group(2)), mm.group(3)
        targets = json.loads(urllib.request.urlopen(
            f"http://{host}:{port}/json/list", timeout=10).read().decode())
        page = next(t for t in targets if t.get("type") == "page")
        self._ws = _Ws(host, port,
                       page["webSocketDebuggerUrl"].split(f"{host}:{port}")[1],
                       timeout=120)
        self._call("Page.enable")
        self._call("Page.navigate",
                   {"url": "file:///" + runner.replace("\\", "/")})
        for _ in range(90):
            r = self._call("Runtime.evaluate",
                           {"expression": "window.__runnerReady === true",
                            "returnByValue": True})
            if (r.get("result") or {}).get("value"):
                return
            time.sleep(1)
        raise IsaacError("decrypt.js 初始化超时")

    def _call(self, method, params=None):
        self._req_id += 1
        self._ws.send_text(json.dumps(
            {"id": self._req_id, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self._ws.recv_text())
            if msg.get("id") == self._req_id:
                return msg.get("result") or {}

    def close(self):
        with self._lock:
            try:
                if self._ws:
                    self._ws.close()
            except Exception:
                pass
            try:
                if self._proc:
                    self._proc.terminate()
            except Exception:
                pass
            self._ws, self._proc = None, None

    # -------------------------------------------------- 对外接口
    def get_keystream_b64(self, decode_key: str) -> bytes:
        """decodeKey → ISAAC64 密钥流字节(128KB,已含倒序)"""
        with self._lock:
            if self._ws is None:
                self._start()
            try:
                r = self._call("Runtime.evaluate", {
                    "expression": f"window.__getB64({json.dumps(str(decode_key))})",
                    "awaitPromise": True, "returnByValue": True})
            except Exception:
                # 连接失效则重启一次
                self.close()
                self._start()
                r = self._call("Runtime.evaluate", {
                    "expression": f"window.__getB64({json.dumps(str(decode_key))})",
                    "awaitPromise": True, "returnByValue": True})
            b64 = (r.get("result") or {}).get("value") or ""
            if not b64:
                desc = (r.get("exceptionDetails") or {}).get("exception", {})
                raise IsaacError(f"密钥流生成失败:{str(desc.get('description', ''))[:120]}")
            return base64.b64decode(b64)

    def xor_file_head(self, file_path: str, decode_key: str):
        """对文件头做 XOR 解密(与 res-downloader decodeWxFile 一致)"""
        ks = self.get_keystream_b64(decode_key)
        with open(file_path, "r+b") as f:
            head = f.read(len(ks))
            n = min(len(ks), len(head))
            f.seek(0)
            f.write(bytes(a ^ b for a, b in zip(head[:n], ks[:n])))


_evaluator = None
_eval_lock = threading.Lock()


def get_evaluator() -> IsaacEvaluator:
    global _evaluator
    with _eval_lock:
        if _evaluator is None:
            _evaluator = IsaacEvaluator()
        return _evaluator


def shutdown():
    global _evaluator
    with _eval_lock:
        if _evaluator is not None:
            _evaluator.close()
            _evaluator = None
