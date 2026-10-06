# -*- coding: utf-8 -*-
"""
无水印视频下载器 — 核心解析/下载逻辑(与界面解耦,便于测试)

基于开源项目 Douyin_TikTok_Download_API(Evil0ctal,Apache-2.0)的爬虫模块封装。
支持平台:抖音 / TikTok / 哔哩哔哩(视频与图集)。
仅供学习交流使用,请勿用于违法用途。
"""

import asyncio
import os
import re
import sys

import httpx

# ---------------------------------------------------------------------------
# 路径与导入引导:脚本运行时把项目根目录加入 sys.path;打包后由 PyInstaller 处理
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    if APP_DIR not in sys.path:
        sys.path.insert(0, APP_DIR)

from crawlers.hybrid.hybrid_crawler import HybridCrawler
from crawlers.bilibili.web.web_crawler import BilibiliWebCrawler

# 各爬虫模块的运行时配置(用于可选代理 / User-Agent)
import crawlers.douyin.web.web_crawler as _douyin_mod
import crawlers.tiktok.web.web_crawler as _tiktok_web_mod
import crawlers.tiktok.app.app_crawler as _tiktok_app_mod
import crawlers.bilibili.web.web_crawler as _bilibili_mod

_CRAWLER_MODULES = (_douyin_mod, _tiktok_web_mod, _tiktok_app_mod, _bilibili_mod)

FALLBACK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 从分享口令/文本中提取链接(只排除空白、引号、括号与中文标点;
# 注意不能排除 ASCII '?'/'='/'&',小红书等平台链接必须带查询参数)
URL_RE = re.compile(r"https?://[^\s\"'<>()\[\]【】,，。;；!！？]+")
# B站 BV 号
BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")

# B站清晰度:64=720P(免登录最高),80=1080P(可能需要登录,失败自动回退 64)
BILI_QN = "64"


class Cancelled(Exception):
    """用户取消操作"""


class ParseError(Exception):
    """解析失败"""


class WxNeedLoginError(ParseError):
    """视频号需要完成一次元宝扫码授权(GUI 层引导后重试)"""


def apply_proxy(proxy: str) -> None:
    """把代理写入各爬虫模块的运行时配置(留空则恢复为无代理)"""
    proxy = (proxy or "").strip()
    for mod in _CRAWLER_MODULES:
        cfg = getattr(mod, "config", None)
        if not isinstance(cfg, dict):
            continue

        def walk(node):
            if not isinstance(node, dict):
                return
            for key, value in node.items():
                if key in ("proxies", "proxy") and isinstance(value, dict):
                    for pk in ("http", "https", "http://", "https://"):
                        if pk in value:
                            value[pk] = proxy or None
                else:
                    walk(value)

        walk(cfg)


def platform_user_agent(platform: str) -> str:
    """取对应平台配置里的 User-Agent,失败时用通用值"""
    try:
        if platform == "douyin":
            return _douyin_mod.config["TokenManager"]["douyin"]["headers"]["User-Agent"]
        if platform == "tiktok":
            return _tiktok_web_mod.config["TokenManager"]["tiktok"]["headers"]["User-Agent"]
        if platform == "bilibili":
            return _bilibili_mod.config["TokenManager"]["bilibili"]["headers"]["User-Agent"]
        if platform == "kuaishou":
            import kuaishou_parser
            return kuaishou_parser.DESKTOP_UA
        if platform == "xhs":
            import xhs_parser
            return xhs_parser.DESKTOP_UA
    except Exception:
        pass
    return FALLBACK_UA


def extract_urls(text: str) -> list:
    """从文本中提取所有链接(去重、保序)"""
    seen, urls = set(), []
    for url in URL_RE.findall(text or ""):
        url = url.rstrip(".,;)")
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def safe_filename(name: str, max_len: int = 80) -> str:
    """把标题清洗成可安全用作文件名的字符串"""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name or "").strip(" ._")
    return name[:max_len] or "video"


def unique_path(path: str) -> str:
    """目标文件已存在时自动加 (1)(2)... 后缀"""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{root}({i}){ext}"):
        i += 1
    return f"{root}({i}){ext}"


def detect_platform(url: str) -> str:
    low = url.lower()
    if "douyin" in low or "iesdouyin" in low:
        return "douyin"
    if "tiktok" in low:
        return "tiktok"
    if "kuaishou" in low or "chenzhongtech" in low or "gifshow" in low:
        return "kuaishou"
    if "xiaohongshu" in low or "xhslink" in low:
        return "xhs"
    if "bilibili" in low or "b23.tv" in low:
        return "bilibili"
    if "weixin.qq.com/sph" in low or "channels.weixin.qq.com" in low:
        return "wxchannels"
    if "weibo" in low or "weibo.cn" in low:
        raise ParseError(
            "微博视频对游客接口全面收紧(需登录态),暂不支持,"
            "可先把视频保存到本地再使用")
    raise ParseError(
        "无法识别链接所属平台(支持抖音 / 快手 / 小红书 / TikTok / 哔哩哔哩;"
        "微信视频号与微博暂不支持)")


class Core:
    """解析与下载核心。所有方法均可独立调用,不依赖任何界面。"""

    def __init__(self, proxy: str = ""):
        apply_proxy(proxy)
        self.proxy = proxy or None
        self._hybrid = None
        self._bilibili = None
        self._status_hook = lambda m: None   # 状态回调(视频号登录提示用)

    # -- 爬虫实例惰性创建(创建时会做网络请求,失败可重试) -------------------
    def _get_hybrid(self):
        if self._hybrid is None:
            self._hybrid = HybridCrawler()
        return self._hybrid

    def _get_bilibili(self):
        if self._bilibili is None:
            self._bilibili = BilibiliWebCrawler()
        return self._bilibili

    # ------------------------------------------------------------------ 解析
    async def parse(self, url: str) -> dict:
        """解析单条链接,返回统一格式的条目字典(失败时条目内含 error 字段)"""
        item = {"url": url, "platform": "", "type": "", "author": "",
                "title": "", "aweme_id": "", "files": [], "error": None}
        try:
            platform = detect_platform(url)
            item["platform"] = {"douyin": "抖音", "tiktok": "TikTok",
                                "bilibili": "哔哩哔哩", "kuaishou": "快手",
                                "xhs": "小红书", "wxchannels": "视频号"}[platform]
            if platform in ("douyin", "tiktok", "kuaishou", "xhs", "wxchannels"):
                if platform == "douyin":
                    # 2026 年起抖音接口需要浏览器安全签名:走 v5 原生签名链路
                    import douyin_v5
                    data = await douyin_v5.parse_share_url(url, proxy=self.proxy)
                elif platform == "kuaishou":
                    import kuaishou_parser
                    data = await kuaishou_parser.parse_share_url(url, proxy=self.proxy)
                elif platform == "xhs":
                    import xhs_parser
                    data = await xhs_parser.parse_share_url(url, proxy=self.proxy)
                elif platform == "wxchannels":
                    import wxchannels_parser
                    data = await wxchannels_parser.parse_share_url(
                        url, proxy=self.proxy,
                        on_status=lambda m: self._status_hook(m))
                else:
                    data = await self._get_hybrid().hybrid_parsing_single_video(
                        url, minimal=True)
                item["aweme_id"] = str(data.get("aweme_id") or "")
                author = data.get("author") or {}
                item["author"] = (author.get("nickname") or author.get("unique_id")
                                  or author.get("short_id") or "")
                item["title"] = (data.get("desc") or "").strip()
                if data.get("type") == "image":
                    image_data = data.get("image_data") or {}
                    imgs = (image_data.get("no_watermark_image_list")
                            or image_data.get("watermark_image_list") or [])
                    if not imgs:
                        raise ParseError("图集内容为空或解析失败")
                    item["type"] = "图集"
                    item["files"] = [("jpg", u) for u in imgs]
                else:
                    vd = data.get("video_data") or {}
                    candidates = (vd.get("candidates")
                                  or [u for u in (vd.get("nwm_video_url_HQ"),
                                                  vd.get("nwm_video_url"),
                                                  vd.get("wm_video_url_HQ"),
                                                  vd.get("wm_video_url")) if u])
                    if not candidates:
                        raise ParseError("未获取到视频直链(该作品可能是直播/不支持的内容)")
                    item["type"] = "视频"
                    item["files"] = [("mp4", candidates[0], tuple(candidates[1:]))]
            else:
                await self._parse_bilibili(item, url)
        except Cancelled:
            raise
        except ParseError:
            raise
        except Exception as exc:
            raise ParseError(f"{type(exc).__name__}: {exc}") from exc
        return item

    async def _resolve_url(self, client: httpx.AsyncClient, url: str) -> str:
        """跟随短链重定向,返回最终地址"""
        resp = await client.get(url, follow_redirects=True)
        return str(resp.url)

    async def _parse_bilibili(self, item: dict, url: str) -> None:
        async with httpx.AsyncClient(timeout=20) as client:
            real_url = await self._resolve_url(client, url if "b23.tv" in url else url)
        match = BV_RE.search(real_url) or BV_RE.search(url)
        if not match:
            raise ParseError("链接中未找到 BV 号")
        bvid = match.group(1)
        item["aweme_id"] = bvid

        resp = await self._get_bilibili().fetch_one_video(bvid)
        if not isinstance(resp, dict) or resp.get("code") != 0:
            raise ParseError(f"B站接口返回异常:{(resp or {}).get('message', resp)}")
        data = resp.get("data") or {}
        if "videoData" in data:  # 兼容不同接口返回结构
            data = data["videoData"] or {}
        item["author"] = (data.get("owner") or {}).get("name") or ""
        item["title"] = (data.get("title") or "").strip()

        pages = data.get("pages") or []
        if not pages:
            pages = [{"cid": data.get("cid"), "part": "", "page": 1}]
        item["type"] = "视频" if len(pages) == 1 else f"视频(共{len(pages)}P)"
        # B站直链在下载时实时获取,这里记录 (bvid, cid, 分P标题, 分P序号)
        for page in pages[:20]:
            item["files"].append(("bili", bvid, page.get("cid"),
                                  page.get("part") or "", page.get("page") or 1))

    # ------------------------------------------------------------------ 下载
    async def resolve_bilibili_url(self, bvid: str, cid) -> list:
        """获取B站视频分段直链列表(渐进式 mp4,免登录、无需 ffmpeg 合并)"""
        headers = {"User-Agent": platform_user_agent("bilibili"),
                   "Referer": "https://www.bilibili.com/"}
        try:
            headers["Cookie"] = _bilibili_mod.config["TokenManager"]["bilibili"]["headers"]["cookie"]
        except Exception:
            pass
        params = {"bvid": bvid, "cid": str(cid), "qn": BILI_QN,
                  "fnval": "0", "platform": "html5", "high_quality": "1"}
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get("https://api.bilibili.com/x/player/wbi/playurl",
                                    params=params, headers=headers)
        data = resp.json()
        if data.get("code") != 0:
            raise ParseError(f"B站播放接口返回异常:{data.get('message', data)}")
        durl = (data.get("data") or {}).get("durl") or []
        links = [seg.get("url") for seg in durl if seg.get("url")]
        if not links:
            raise ParseError("未获取到B站视频直链(可能需要登录账号)")
        return links

    async def download_item(self, item: dict, save_dir: str, index: int,
                            on_file_start=None, on_progress=None,
                            cancel=None) -> list:
        """
        下载一个条目的全部文件,返回保存路径列表。
        on_file_start(filename, seq, total) / on_progress(received, total) 用于进度回调。
        """
        os.makedirs(save_dir, exist_ok=True)
        base = f"{index:02d}_{safe_filename(item['author'])}" if item["author"] else f"{index:02d}"
        title = safe_filename(item["title"]) or item["aweme_id"] or "video"
        name_base = f"{base}_{title}" if title != "video" else base
        if item["platform"] == "哔哩哔哩":
            name_base = f"{base}_{title or item['aweme_id']}"

        saved = []
        total_files = len(item["files"])
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=20, read=120, write=60, pool=60),
                follow_redirects=True) as client:
            for seq, entry in enumerate(item["files"], start=1):
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                kind = entry[0]
                if kind == "bili":
                    _, bvid, cid, part, page_no = entry
                    suffix = ""
                    if total_files > 1:
                        suffix = f"_P{page_no}"
                        if part:
                            suffix += f"_{safe_filename(part, 40)}"
                    filename = f"{name_base}{suffix}.mp4"
                    headers = {"User-Agent": platform_user_agent("bilibili"),
                               "Referer": "https://www.bilibili.com/"}
                    links = await self.resolve_bilibili_url(bvid, cid)
                elif kind == "mp4" and len(entry) > 2:
                    # 主链 + 回退候选,逐个尝试
                    ext, link, fallbacks = entry[0], entry[1], list(entry[2])
                    filename = (f"{name_base}.{ext}" if total_files == 1
                                else f"{name_base}_图{seq}.{ext}")
                    headers = self._platform_headers(item)
                    links = [link] + fallbacks
                else:
                    ext, link = entry[0], entry[1]
                    filename = (f"{name_base}.{ext}" if total_files == 1
                                else f"{name_base}_图{seq}.{ext}")
                    headers = self._platform_headers(item)
                    links = [link]

                # 规范化链接:协议相对 URL(//xxx)补全 https:,去除空白
                links = [("https:" + u if u.startswith("//") else u).strip()
                         for u in links if u]

                if on_file_start is not None:
                    on_file_start(filename, seq, total_files)

                dest = unique_path(os.path.join(save_dir, filename))
                if kind == "bili" and len(links) > 1:
                    await self._download_segments(client, links, headers, dest,
                                                  on_progress, cancel)
                else:
                    last_error = None
                    for attempt, url in enumerate(links):
                        try:
                            await self._download_file(client, url, headers, dest,
                                                      on_progress, cancel)
                            last_error = None
                            break
                        except Cancelled:
                            raise
                        except ParseError as exc:
                            if url is links[-1]:
                                # 最后一个候选仍失败时,再尝试去掉 Referer(部分 CDN 会拒绝陌生 Referer)
                                if headers.get("Referer"):
                                    no_ref = {k: v for k, v in headers.items()
                                              if k.lower() != "referer"}
                                    try:
                                        self._remove_quiet(dest + ".part")
                                        await self._download_file(client, url, no_ref,
                                                                  dest, on_progress, cancel)
                                        last_error = None
                                        break
                                    except (Cancelled, ParseError):
                                        pass
                                raise
                            last_error = exc
                            self._remove_quiet(dest + ".part")
                    if last_error is not None:
                        raise last_error
                saved.append(dest)
        return saved

    @staticmethod
    def _platform_headers(item) -> dict:
        """各平台直链的下载请求头(UA + Referer + 必要的 Cookie)

        item 可以是平台名字符串,也可以是条目 dict(推荐,可拿到原始分享链接作为 Referer)。
        快手 / 小红书等平台的 CDN 会校验 Referer,用原始短链比固定域名更稳。
        """
        if isinstance(item, dict):
            platform = item.get("platform", "")
            share_url = item.get("url", "")
        else:
            platform = item or ""
            share_url = ""
        ua = platform_user_agent(platform or "douyin")
        headers = {"User-Agent": ua, "Accept": "*/*"}
        if platform == "抖音":
            headers["Referer"] = "https://www.douyin.com/"
            try:
                import douyin_identity
                jar = douyin_identity.get_jar()
                if jar:
                    headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in jar.items())
            except Exception:
                pass
        elif platform == "快手":
            # 快手 CDN 对 Referer 校验较严,优先用原始分享链接
            headers["Referer"] = share_url or "https://www.kuaishou.com/"
        elif platform == "小红书":
            headers["Referer"] = share_url or "https://www.xiaohongshu.com/"
        elif platform == "哔哩哔哩":
            headers["Referer"] = "https://www.bilibili.com/"
        elif platform == "TikTok":
            headers["Referer"] = "https://www.tiktok.com/"
        elif platform == "视频号":
            # finder-preview 直链通常允许跨域,带上 channels 域名作为 Referer 更稳
            headers["Referer"] = "https://channels.weixin.qq.com/"
        return headers

    async def _download_file(self, client, url, headers, dest, on_progress, cancel):
        tmp = dest + ".part"
        received = 0
        try:
            async with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code != 200:
                    raise ParseError(f"下载失败,HTTP {resp.status_code}")
                total = int(resp.headers.get("content-length") or 0)
                with open(tmp, "wb") as f:
                    async for chunk in resp.aiter_bytes(64 * 1024):
                        if cancel is not None and cancel.is_set():
                            raise Cancelled()
                        f.write(chunk)
                        received += len(chunk)
                        if on_progress is not None:
                            on_progress(received, total)
            os.replace(tmp, dest)
        except Cancelled:
            self._remove_quiet(tmp)
            raise
        except ParseError:
            self._remove_quiet(tmp)
            raise
        except Exception as exc:
            self._remove_quiet(tmp)
            raise ParseError(f"{type(exc).__name__}: {exc}") from exc

    async def _download_segments(self, client, links, headers, dest, on_progress, cancel):
        """多分段流(B站)依次下载并拼接为一个文件"""
        tmp = dest + ".part"
        try:
            with open(tmp, "wb") as f:
                for url in links:
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    async with client.stream("GET", url, headers=headers) as resp:
                        if resp.status_code != 200:
                            raise ParseError(f"下载失败,HTTP {resp.status_code}")
                        async for chunk in resp.aiter_bytes(64 * 1024):
                            if cancel is not None and cancel.is_set():
                                raise Cancelled()
                            f.write(chunk)
                            if on_progress is not None:
                                on_progress(0, 0)  # 分段总量未知,仅作活跃提示
            os.replace(tmp, dest)
        except Cancelled:
            self._remove_quiet(tmp)
            raise
        except ParseError:
            self._remove_quiet(tmp)
            raise
        except Exception as exc:
            self._remove_quiet(tmp)
            raise ParseError(f"{type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _remove_quiet(path):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def recover_proxy_leak():
    """若上次异常退出导致系统代理残留(标记文件存在),自动还原。

    历史遗留保护:早期版本的【视频号捕获】会临时改写系统代理,
    崩溃时可能残留,把浏览器导向一个已不存在的本地代理端口。
    """
    flag = os.path.join(APP_DIR, "ca", "proxy_active.flag")
    try:
        if not os.path.exists(flag):
            return
        enable, server, override = open(flag).read().split("|")
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            0, winreg.KEY_ALL_ACCESS)
        try:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD,
                              int(enable))
            if server:
                winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, server)
            if override:
                winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ,
                                  override)
        finally:
            winreg.CloseKey(key)
        import ctypes
        wininet = ctypes.windll.wininet
        wininet.InternetSetOptionW(None, 39, None, 0)   # SETTINGS_CHANGED
        wininet.InternetSetOptionW(None, 37, None, 0)   # REFRESH
        os.remove(flag)
    except Exception:
        pass


def run_parse_batch(urls, proxy="", on_each=None):
    """批量解析(同步入口,供测试/命令行使用),返回条目列表"""
    core = Core(proxy)

    async def _run():
        items = []
        for url in urls:
            try:
                item = await core.parse(url)
            except ParseError as exc:
                item = {"url": url, "platform": "未知", "type": "", "author": "",
                        "title": "", "aweme_id": "", "files": [], "error": str(exc)}
            items.append(item)
            if on_each is not None:
                on_each(item)
        return items

    return asyncio.run(_run())


def run_download_batch(items, save_dir, proxy="", on_event=None, cancel=None):
    """批量下载(同步入口)。on_event(str) 用于输出日志。返回 (成功数, 失败数)"""
    core = Core(proxy)
    ok = fail = 0

    async def _run():
        nonlocal ok, fail
        for idx, item in enumerate([it for it in items if not it.get("error")], start=1):
            try:
                paths = await core.download_item(
                    item, save_dir, idx,
                    on_file_start=lambda fn, s, t: on_event and on_event(
                        f"[{idx}] 开始下载:{fn}"),
                    on_progress=lambda rec, tot: None,
                    cancel=cancel)
                ok += 1
                if on_event is not None:
                    for p in paths:
                        on_event(f"[{idx}] 已保存:{os.path.basename(p)}")
            except Cancelled:
                if on_event is not None:
                    on_event("已取消")
                break
            except Exception as exc:
                fail += 1
                if on_event is not None:
                    on_event(f"[{idx}] 下载失败:{exc}")

    asyncio.run(_run())
    return ok, fail


if __name__ == "__main__":
    # 命令行自测:python downloader_core.py <链接> [更多链接...]
    sys.stdout.reconfigure(encoding="utf-8")
    args = [a for a in sys.argv[1:] if a.startswith("http")]
    if not args:
        print("用法:python downloader_core.py <视频链接> [更多链接...]")
        sys.exit(1)
    for it in run_parse_batch(extract_urls(" ".join(args))):
        if it.get("error"):
            print(f"[失败] {it['url']}\n       {it['error']}")
        else:
            print(f"[成功] {it['platform']} {it['type']} | {it['author']} | "
                  f"{it['title'][:40]}\n       直链数:{len(it['files'])}")
