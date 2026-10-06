# -*- coding: utf-8 -*-
"""
视频号捕获引擎 — 本地 MITM 代理(方案移植自开源项目 res-downloader/putyy)

原理:启动本地代理并设为系统代理 → 只对视频号相关域名做 HTTPS 解密 →
向视频号播放器的 JS 注入钩子 → 播放器播放时把视频地址/decodeKey 上报 →
本程序捕获后供下载(下载后由 isaac_js 对文件头 XOR 解密)。

仅解密(做 MITM)的域名(最小化干预,其余流量盲转发):
  res.wx.qq.com            播放器 JS(注入钩子)
  wxapp.tc.qq.com          钩子上报端点(捕获 objectDesc)
  channels.weixin.qq.com   分享页(缓存击穿)
"""

import base64
import datetime
import json
import os
import re
import socket
import ssl
import threading
import time

import httpx

from downloader_core import APP_DIR

PROXY_HOST = "127.0.0.1"
PROXY_PORT = 8899
CA_DIR = os.path.join(APP_DIR, "ca")
CA_KEY_PATH = os.path.join(CA_DIR, "ca.key")
CA_CRT_PATH = os.path.join(CA_DIR, "ca.crt")
CA_CN = "无水印视频下载器本地根证书"
# 缓存击穿标签:每次捕获会话动态生成,确保微信内核重新拉取被注入的 JS
CACHE_BUST_TAG = "v=wxd1"  # 兼容旧引用;实际使用见 ChannelsCapture._bust_tag

# 只对这些后缀做 MITM
MITM_HOSTS = ("res.wx.qq.com", "wxapp.tc.qq.com", "channels.weixin.qq.com")
HOOK_MARK = "res-downloader/wechat"

# 播放器 JS 注入(与 res-downloader plugin.qq.com.go 相同)
_MEDIA_HOOK = """
            get media(){
                if(this.objectDesc){
                    fetch("https://wxapp.tc.qq.com/res-downloader/wechat?type=1", {
                      method: "POST",
                      mode: "no-cors",
                      body: JSON.stringify(this.objectDesc),
                    });
                };

"""
_MEDIA_RE = re.compile(rb"get\s*media\(\)\{")

_HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate",
                "proxy-authorization", "te", "trailers", "transfer-encoding",
                "upgrade", "proxy-connection"}


class ProxyError(Exception):
    pass


class CaptureLog:
    """捕获链路诊断日志(追加写到程序目录,便于定位捕获失败的原因)"""

    def __init__(self):
        self._path = os.path.join(APP_DIR, "capture_debug.log")
        self._lock = threading.Lock()

    def write(self, msg: str):
        try:
            with self._lock:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(time.strftime("[%H:%M:%S] ") + msg + "\n")
        except OSError:
            pass


_caplog = CaptureLog()


# ------------------------------------------------------------------ 证书
def ensure_ca() -> str:
    """生成本地根证书(幂等),返回证书文件路径"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    os.makedirs(CA_DIR, exist_ok=True)
    if os.path.exists(CA_KEY_PATH) and os.path.exists(CA_CRT_PATH):
        return CA_CRT_PATH
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_CN)])
    now = datetime.datetime.utcnow()
    crt = (x509.CertificateBuilder()
           .subject_name(name).issuer_name(name)
           .public_key(key.public_key())
           .serial_number(x509.random_serial_number())
           .not_valid_before(now - datetime.timedelta(days=1))
           .not_valid_after(now + datetime.timedelta(days=3650))
           .add_extension(x509.BasicConstraints(ca=True, path_length=None),
                          critical=True)
           .sign(key, hashes.SHA256()))
    with open(CA_KEY_PATH, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))
    with open(CA_CRT_PATH, "wb") as f:
        f.write(crt.public_bytes(serialization.Encoding.PEM))
    return CA_CRT_PATH


def ensure_cert_installed() -> bool:
    """把根证书装入受信任的根存储。

    优先写入本机存储(管理员权限下静默完成);失败(非管理员)则降级到
    当前用户存储 —— 会弹一次 Windows 确认框,需要点"是"。
    返回 True 表示证书已就绪。
    """
    ensure_ca()
    import ctypes
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding

    crt = x509.load_pem_x509_certificate(open(CA_CRT_PATH, "rb").read())
    der = crt.public_bytes(Encoding.DER)

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    X509_ASN_ENCODING = 0x00000001
    PKCS_7_ASN_ENCODING = 0x00010000
    CERT_STORE_ADD_NEW = 1
    CRYPT_E_EXISTS = 0x80092005
    CERT_STORE_PROV_SYSTEM_W = 10
    CERT_SYSTEM_STORE_LOCAL_MACHINE = 0x00020000
    CERT_SYSTEM_STORE_CURRENT_USER = 0x00010000

    def add_once(store_flags):
        """返回 'added' / 'exists' / None(打不开存储,如非管理员)"""
        crypt32.CertOpenStore.restype = ctypes.c_void_p
        crypt32.CertOpenStore.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                          ctypes.c_void_p, ctypes.c_uint32,
                                          ctypes.c_wchar_p]
        h = crypt32.CertOpenStore(CERT_STORE_PROV_SYSTEM_W, 0, None,
                                  store_flags, "Root")
        if not h:
            return None
        try:
            ctx = crypt32.CertCreateCertificateContext(
                X509_ASN_ENCODING | PKCS_7_ASN_ENCODING, der, len(der))
            if not ctx:
                raise ProxyError("CertCreateCertificateContext 失败")
            try:
                if crypt32.CertAddCertificateContextToStore(h, ctx,
                                                            CERT_STORE_ADD_NEW,
                                                            None):
                    return "added"
                err = kernel32.GetLastError() & 0xFFFFFFFF
                if err == CRYPT_E_EXISTS:
                    return "exists"
                return None
            finally:
                crypt32.CertFreeCertificateContext(ctx)
        finally:
            crypt32.CertCloseStore(h, 0)

    r = add_once(CERT_SYSTEM_STORE_LOCAL_MACHINE)
    if r:
        return True
    r = add_once(CERT_SYSTEM_STORE_CURRENT_USER)
    if r:
        return True
    raise ProxyError("证书安装失败:请以管理员身份运行程序,或在弹窗中确认")


def _leaf_ssl_ctx(host: str) -> ssl.SSLContext:
    """为主机签发即时证书并返回服务端 SSLContext"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    ca_key = serialization.load_pem_private_key(
        open(CA_KEY_PATH, "rb").read(), password=None)
    ca_crt = x509.load_pem_x509_certificate(open(CA_CRT_PATH, "rb").read())
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = datetime.datetime.utcnow()
    crt = (x509.CertificateBuilder()
           .subject_name(name).issuer_name(ca_crt.subject)
           .public_key(key.public_key())
           .serial_number(x509.random_serial_number())
           .not_valid_before(now - datetime.timedelta(days=1))
           .not_valid_after(now + datetime.timedelta(days=825))
           .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                          critical=True)
           .add_extension(x509.SubjectAlternativeName(
               [x509.DNSName(host)]), critical=False)
           .sign(ca_key, hashes.SHA256()))
    import tempfile
    pem_key = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption())
    pem_crt = crt.public_bytes(serialization.Encoding.PEM)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
    tmp.write(pem_key + pem_crt)
    tmp.close()
    try:
        ctx.load_cert_chain(tmp.name)
    finally:
        os.unlink(tmp.name)
    return ctx


# ------------------------------------------------------------------ 系统代理
_INTERNET_SETTINGS = (r"Software\Microsoft\Windows"
                      r"\CurrentVersion\Internet Settings")


def _notify_proxy_change():
    import ctypes
    wininet = ctypes.windll.wininet
    wininet.InternetSetOptionW(None, 39, None, 0)   # SETTINGS_CHANGED
    wininet.InternetSetOptionW(None, 37, None, 0)   # REFRESH


class SystemProxy:
    """系统代理开关(记录原状态,可恢复;带崩溃自恢复标记)"""

    _MARK_PATH = os.path.join(APP_DIR, "ca", "proxy_active.flag")

    def __init__(self):
        import winreg
        self._reg = winreg
        self._saved = None

    @classmethod
    def recover_if_leaked(cls):
        """上次运行若崩溃未还原系统代理,此处恢复"""
        try:
            if os.path.exists(cls._MARK_PATH):
                saved = open(cls._MARK_PATH).read().split("|")
                enable, server, override = int(saved[0]), saved[1], saved[2]
                import winreg
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                     _INTERNET_SETTINGS, 0,
                                     winreg.KEY_ALL_ACCESS)
                try:
                    winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD,
                                      enable)
                    if server:
                        winreg.SetValueEx(key, "ProxyServer", 0,
                                          winreg.REG_SZ, server)
                    if override:
                        winreg.SetValueEx(key, "ProxyOverride", 0,
                                          winreg.REG_SZ, override)
                finally:
                    winreg.CloseKey(key)
                _notify_proxy_change()
                os.remove(cls._MARK_PATH)
        except Exception:
            pass

    def enable(self, proxy_server=f"{PROXY_HOST}:{PROXY_PORT}"):
        key = self._reg.OpenKey(self._reg.HKEY_CURRENT_USER,
                                _INTERNET_SETTINGS, 0, self._reg.KEY_ALL_ACCESS)
        try:
            try:
                old_enable, _ = self._reg.QueryValueEx(key, "ProxyEnable")
            except OSError:
                old_enable = 0
            try:
                old_server, _ = self._reg.QueryValueEx(key, "ProxyServer")
            except OSError:
                old_server = ""
            try:
                old_override, _ = self._reg.QueryValueEx(key, "ProxyOverride")
            except OSError:
                old_override = ""
            self._saved = (old_enable, old_server, old_override)
            self._reg.SetValueEx(key, "ProxyEnable", 0, self._reg.REG_DWORD, 1)
            self._reg.SetValueEx(key, "ProxyServer", 0, self._reg.REG_SZ,
                                 proxy_server)
            self._reg.SetValueEx(key, "ProxyOverride", 0, self._reg.REG_SZ,
                                 "localhost;127.*;10.*;172.16.*;172.17.*;"
                                 "172.18.*;172.19.*;172.20.*;172.21.*;"
                                 "172.22.*;172.23.*;172.24.*;172.25.*;"
                                 "172.26.*;172.27.*;172.28.*;172.29.*;"
                                 "172.30.*;172.31.*;192.168.*;<local>")
        finally:
            self._reg.CloseKey(key)
        _notify_proxy_change()
        # 写泄漏恢复标记(异常退出后下次启动可还原)
        try:
            os.makedirs(os.path.dirname(self._MARK_PATH), exist_ok=True)
            with open(self._MARK_PATH, "w") as f:
                f.write("|".join(str(x) for x in self._saved))
        except OSError:
            pass

    def restore(self):
        if self._saved is None:
            return
        old_enable, old_server, old_override = self._saved
        key = self._reg.OpenKey(self._reg.HKEY_CURRENT_USER,
                                _INTERNET_SETTINGS, 0, self._reg.KEY_ALL_ACCESS)
        try:
            self._reg.SetValueEx(key, "ProxyEnable", 0, self._reg.REG_DWORD,
                                 old_enable)
            if old_server:
                self._reg.SetValueEx(key, "ProxyServer", 0, self._reg.REG_SZ,
                                     old_server)
            if old_override:
                self._reg.SetValueEx(key, "ProxyOverride", 0, self._reg.REG_SZ,
                                     old_override)
        finally:
            self._reg.CloseKey(key)
        self._saved = None
        try:
            if os.path.exists(self._MARK_PATH):
                os.remove(self._MARK_PATH)
        except OSError:
            pass
        _notify_proxy_change()


# ------------------------------------------------------------------ 代理本体
class ChannelsCapture:
    """
    视频号捕获器。用法:
        cap = ChannelsCapture(on_item=回调, on_status=回调)
        cap.start()   # 装证书 + 启动代理 + 设系统代理
        cap.stop()    # 停代理 + 还原系统代理
    """

    def __init__(self, on_item=None, on_status=None, port: int = PROXY_PORT):
        self.on_item = on_item or (lambda item: None)
        self.on_status = on_status or (lambda msg: None)
        self.port = port
        self._bust_tag = CACHE_BUST_TAG
        self._running = False
        self._listen_sock = None
        self._thread = None
        self._sysproxy = SystemProxy()
        # trust_env=False:绝不走系统代理,否则捕获时上游请求会绕回自身死锁
        self._http = httpx.Client(http2=False, trust_env=False,
                                  timeout=httpx.Timeout(30, read=60),
                                  follow_redirects=False)
        self._seen = set()
        self._lock = threading.Lock()
        self.items = []            # 捕获的条目(dict)
        self._counter = 0

    # ---- 生命周期
    def start(self):
        if self._running:
            return
        SystemProxy.recover_if_leaked()
        ensure_cert_installed()
        self._running = True
        self._listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listen_sock.bind((PROXY_HOST, self.port))
        self._listen_sock.listen(64)
        self._listen_sock.settimeout(1.0)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        self._sysproxy.enable(f"{PROXY_HOST}:{self.port}")
        self._bust_tag = f"v=wxd{int(time.time())}"   # 每会话新鲜,强制刷新 JS
        _caplog.write(f"===== 捕获会话开始 bust={self._bust_tag} =====")
        self.on_status(f"代理已启动:{PROXY_HOST}:{self.port}(系统代理已开启)")

    def stop(self):
        self._running = False
        try:
            if self._listen_sock:
                self._listen_sock.close()
        except OSError:
            pass
        self._sysproxy.restore()
        try:
            self._http.close()
        except Exception:
            pass
        self.on_status("捕获已停止,系统代理已还原")

    @property
    def running(self):
        return self._running

    # ---- 主循环
    def _serve(self):
        while self._running:
            try:
                conn, _ = self._listen_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._client, args=(conn,),
                             daemon=True).start()
        self.on_status("代理线程退出")

    # ---- 单连接
    def _client(self, conn: socket.socket):
        try:
            conn.settimeout(60)
            head = self._read_head(conn)
            if head is None:
                return
            method, target, headers, rest = head[0], head[1], head[2], head[3]
            if method.upper() == "CONNECT":
                host, _, port = target.partition(":")
                port = int(port or 443)
                _caplog.write(f"CONNECT {target} MITM={self._should_mitm(host)}")
                conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                if self._should_mitm(host):
                    try:
                        ctx = _leaf_ssl_ctx(host)
                        tls = ctx.wrap_socket(conn, server_side=True)
                    except (ssl.SSLError, OSError) as exc:
                        _caplog.write(f"MITM TLS 握手失败 {host}: {exc!r}")
                        conn.close()
                        return
                    self._mitm_session(tls, host, rest)
                else:
                    self._tunnel(conn, host, port)
            else:
                # 明文 HTTP:绝对形式转发
                self._forward_plain(conn, method, target, headers)
        except Exception as exc:
            import sys as _s; print('[proxy] _client 异常:', repr(exc)[:200], file=_s.stderr)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _should_mitm(host: str):
        return any(host == h or host.endswith("." + h) for h in MITM_HOSTS)

    # ---- MITM 会话(解密后的 HTTP/1.1)
    def _mitm_session(self, tls: socket.socket, host: str, pending=b""):
        try:
            while self._running:
                head = self._read_head(tls)
                if head is None:
                    break
                method, target, headers, rest = head
                body = self._read_body(tls, headers, rest)
                _caplog.write(f"请求 {host} {method} {target[:90]}")
                uri = target

                # 1) 捕获端点:钩子上报的 objectDesc
                if HOOK_MARK in uri:
                    self._handle_capture(body)
                    tls.sendall(b"HTTP/1.1 200 OK\r\n"
                                b"Content-Type: text/plain; charset=utf-8\r\n"
                                b"Content-Length: 28\r\n"
                                b"Connection: keep-alive\r\n\r\n"
                                b"The content does not exist\r\n")
                    continue

                # 2) 转发并按需注入
                self._forward_mitm(tls, host, method, uri, headers, body)
        except Exception:
            pass
        finally:
            try:
                tls.close()
            except OSError:
                pass

    def _forward_mitm(self, tls, host, method, uri, headers, body):
        import sys as _sys
        url = f"https://{host}{uri}"
        fwd = {k: v for k, v in headers.items() if k.lower() not in _HOP_HEADERS}
        req_headers = dict(fwd)
        is_js_inject = (host == "res.wx.qq.com"
                        and "virtual_svg-icons-register.publish" in uri)
        bust = (host == "channels.weixin.qq.com" and "/web/pages/" in uri)
        try:
            # stream():响应体只在需要时读取一次;iter_raw 原样透传字节
            with self._http.stream(method.upper(), url, headers=req_headers,
                                   content=body) as r:
                if r.status_code >= 400 and r.status_code < 500:
                    pass  # 4xx 也原样转发(如 404),不影响浏览器
                content_type = r.headers.get("content-type", "")
                need_patch = is_js_inject and "javascript" in content_type.lower()
                if bust:
                    # 页面/清单类响应,替换 .js" 以击穿缓存
                    r.read()
                    patched = r.content.replace(
                        b'.js"', f'.js?{self._bust_tag}"'.encode())
                    _caplog.write(f"缓存击穿: {uri[:70]} 替换"
                                  f"{r.content.count(b'.js\"')}处")
                    self._send_full(tls, r, patched, drop_encoding=True)
                elif need_patch:
                    r.read()
                    matches = len(_MEDIA_RE.findall(r.content))
                    _caplog.write(f"注入: {uri[:80]} 命中{matches}处 "
                                  f"({len(r.content)}B)")
                    patched = _MEDIA_RE.sub(_MEDIA_HOOK.encode(), r.content,
                                            count=1)
                    self._send_full(tls, r, patched, drop_encoding=True)
                else:
                    self._send_stream(tls, r, url)
        except Exception as exc:
            print(f"[proxy] 上游异常 {url[:80]}: {exc!r}", file=_sys.stderr)
            try:
                msg = str(exc).encode()[:200]
                tls.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: "
                            + str(len(msg)).encode() + b"\r\n\r\n" + msg)
            except OSError:
                pass

    # ---- 捕获处理(对应 res-downloader handleMedia)
    def _handle_capture(self, body: bytes):
        _caplog.write(f"捕获端点收到上报 {len(body)}B")
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except Exception:
            _caplog.write("捕获端点: JSON 解析失败")
            return
        media_arr = data.get("media") or []
        if not media_arr:
            _caplog.write("捕获端点: media 为空")
            return
        m0 = media_arr[0] or {}
        raw_url = m0.get("url") or ""
        if not raw_url:
            return
        if isinstance(m0.get("urlToken"), str) and m0["urlToken"]:
            raw_url += m0["urlToken"]
        sign = raw_url.split("?")[0][:120]
        with self._lock:
            if sign in self._seen:
                return
            self._seen.add(sign)
            item = {
                "id": f"c{self._counter + 1}",
                "platform": "视频号",
                "type": "视频" if m0.get("mediaType") != 9 else "图片",
                "author": "视频号",
                "title": (data.get("description") or "").strip() or "视频号视频",
                "aweme_id": "",
                "url": raw_url,
                "decode_key": m0.get("decodeKey") or "",
                "file_size": m0.get("fileSize") or 0,
                "cover_url": m0.get("coverUrl") or "",
                "error": None,
                "files": [("mp4", raw_url)],
            }
            formats = []
            for spec in m0.get("spec") or []:
                if isinstance(spec, dict) and spec.get("fileFormat"):
                    formats.append(spec["fileFormat"])
            if formats:
                item["wx_file_formats"] = "#".join(formats)
            self._counter += 1
            self.items.append(item)
        self.on_item(item)

    # ---- 盲转发
    def _tunnel(self, conn: socket.socket, host: str, port: int):
        try:
            remote = socket.create_connection((host, port), timeout=30)
        except OSError:
            conn.close()
            return
        sockets = [conn, remote]
        conn.settimeout(None)
        remote.settimeout(None)

        def pump(src, dst):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        t1 = threading.Thread(target=pump, args=(conn, remote), daemon=True)
        t2 = threading.Thread(target=pump, args=(remote, conn), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        try:
            remote.close()
        except OSError:
            pass

    # ---- 明文 HTTP 转发
    def _forward_plain(self, conn, method, target, headers):
        try:
            # target 形如 http://host/path
            fwd = {k: v for k, v in headers.items()
                   if k.lower() not in _HOP_HEADERS}
            with self._http.stream(method.upper(), target,
                                   headers=fwd) as r:
                self._send_stream(conn, r, target)
        except Exception:
            pass

    # ---- HTTP 读辅助
    @staticmethod
    def _read_head(sock):
        """读请求头,返回 (method, target, headers, rest) 或 None"""
        buf = b""
        try:
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    return None
                buf += chunk
                if len(buf) > 256 * 1024:
                    return None
        except (socket.timeout, OSError):
            return None
        head, _, rest = buf.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split()
        if len(parts) < 2:
            return None
        method, target = parts[0], parts[1]
        headers = {}
        for line in lines[1:]:
            k, _, v = line.partition(":")
            headers[k.strip()] = v.strip()
        return method, target, headers, rest

    @staticmethod
    def _read_body(sock, headers, initial=b""):
        body = initial or b""
        try:
            length = int(headers.get("Content-Length")
                         or headers.get("content-length") or 0)
        except ValueError:
            length = 0
        while len(body) < length:
            chunk = sock.recv(min(65536, length - len(body)))
            if not chunk:
                break
            body += chunk
        return body

    # ---- 响应回写
    def _send_full(self, sock, resp, content: bytes, drop_encoding=False):
        try:
            lines = [f"HTTP/1.1 {resp.status_code} {resp.reason_phrase}".encode()]
            for k, v in resp.headers.items():
                if k.lower() in _HOP_HEADERS or k.lower() == "content-length":
                    continue
                if drop_encoding and k.lower() == "content-encoding":
                    continue
                lines.append(f"{k}: {v}".encode("latin-1", "replace"))
            lines.append(b"Content-Length: " + str(len(content)).encode())
            lines.append(b"Connection: keep-alive")
            sock.sendall(b"\r\n".join(lines) + b"\r\n\r\n" + content)
        except OSError:
            pass

    def _send_stream(self, sock, resp, url=""):
        try:
            lines = [f"HTTP/1.1 {resp.status_code} {resp.reason_phrase}".encode()]
            for k, v in resp.headers.items():
                if k.lower() in _HOP_HEADERS:
                    continue
                lines.append(f"{k}: {v}".encode("latin-1", "replace"))
            # iter_raw = 未解压的原始字节:Content-Encoding / Content-Length
            # 头原样透传,浏览器收到的字节与直连完全一致(iter_bytes 会解压,
            # 配上原 Content-Encoding 头会让浏览器二次解压失败 → 页面全挂)
            total = resp.headers.get("content-length")
            if total:
                # 已知长度:声明后原样拷贝原始字节
                lines.append(b"Content-Length: " + total.encode())
                sock.sendall(b"\r\n".join(lines) + b"\r\n\r\n")
                sent = 0
                for chunk in resp.iter_raw(65536):
                    sock.sendall(chunk)
                    sent += len(chunk)
                    if total and sent >= int(total):
                        break
            else:
                # 未知长度:分块传输
                lines.append(b"Transfer-Encoding: chunked")
                sock.sendall(b"\r\n".join(lines) + b"\r\n\r\n")
                for chunk in resp.iter_raw(65536):
                    if not chunk:
                        break
                    sock.sendall(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                sock.sendall(b"0\r\n\r\n")
        except Exception as exc:
            # 上游/下游任一侧中断:记录后交由连接关闭处理
            import sys as _sys
            print(f"[proxy] 响应转发中断 {url[:60]}: {exc!r}", file=_sys.stderr)
