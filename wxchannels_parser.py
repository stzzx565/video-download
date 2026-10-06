# -*- coding: utf-8 -*-
"""
微信视频号解析模块(方案源自 z-video-downloader / wx_channels_download)。

分享链接(https://weixin.qq.com/sph/xxxx)→ 视频直链,两条路线自动切换:

路线1(在线服务):POST sph.litao.workers.dev(需代理/境外网络),
  与 z-video-downloader 的在线解析完全一致。
路线2(本地复刻):sph.litao.workers.dev 在国内被墙,但其 worker 源码
  (ltaoo/wx_channels_download/internal/workers/sph)的逻辑全部是
  国内可达域名:
    ① 元宝 get_parse_result(分享链接 → exportId + playable_url)
    ② 视频号 finder-preview get_feed_info(→ H.264/H.265 预览直链,免解密)
  ① 需要 yuanbao.tencent.com 的登录 Cookie —— 本模块用 browser_cookie3
  自动从本机 Edge/Chrome 读取(用户只需在浏览器里登录元宝一次)。

视频直链为预览地址,无需解密,直接下载。
"""

import asyncio
import random
import re
import time
from urllib.parse import parse_qs, quote, urlparse

import httpx

DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

# 路线1:z-video-downloader 所用的在线解析服务
ONLINE_API = "https://sph.litao.workers.dev/api/fetch_video_profile"

# 路线2:本地复刻(worker 同款流程,全部国内可达)
YUANBAO_API = "https://yuanbao.tencent.com/api/weixin/get_parse_result"
FEED_INFO_API = ("https://channels.weixin.qq.com/finder-preview/api/feed/"
                 "get_feed_info")

_SHARE_ID_RE = re.compile(r"weixin\.qq\.com/sph/([A-Za-z0-9_-]+)")

# 在线服务可达性记忆:连接类失败后本会话内不再重试(避免每次白等 15 秒)
_online_disabled = False


class WxChannelsParseError(Exception):
    """视频号解析失败"""


def _rid() -> str:
    ts_hex = format(int(time.time()), "x")
    rand_hex = "".join(random.choice("0123456789abcdef") for _ in range(8))
    return f"{ts_hex}-{rand_hex}"


def extract_share_id(url: str):
    m = _SHARE_ID_RE.search(url or "")
    return m.group(1) if m else None


# ------------------------------------------------------------------ 路线1
async def _parse_online(client: httpx.AsyncClient, share_url: str) -> dict:
    """在线解析服务(z-video-downloader 同款,被墙时快速失败)"""
    r = await client.post(ONLINE_API, json={"url": share_url},
                          headers={"Content-Type": "application/json",
                                   "User-Agent": DESKTOP_UA},
                          timeout=12)
    if not (200 <= r.status_code < 300):
        raise WxChannelsParseError(f"在线服务 HTTP {r.status_code}")
    data = r.json()
    if data.get("errCode") not in (0, None):
        raise WxChannelsParseError(f"在线服务错误:{data.get('errMsg', 'unknown')}")
    if "error" in data:
        raise WxChannelsParseError(f"在线服务错误:{data['error'][:120]}")
    d = data.get("data") or {}
    feed = d.get("feedInfo") or {}
    if not feed:
        raise WxChannelsParseError("在线服务未返回作品数据")
    return feed


# ------------------------------------------------------------------ 路线2
def _cookie_from_browser(on_status=None) -> str:
    """获取元宝登录 Cookie:读本地缓存;没有则抛 NeedLoginError,
    由 GUI 层打开登录窗口并引导用户扫码确认。"""
    import wx_login
    cookie = wx_login.load_cookie()
    if cookie:
        return cookie
    raise wx_login.NeedLoginError(
        "视频号首次下载需要一次授权:程序将打开浏览器窗口,"
        "请用微信扫码登录元宝,完成后回到程序点击确认按钮。")


async def _parse_local(client: httpx.AsyncClient, share_url: str,
                       on_status=None) -> dict:
    """本地复刻 worker 流程:元宝(登录态)→ finder-preview"""
    import wx_login
    cookie = _cookie_from_browser(on_status=on_status)
    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": "https://yuanbao.tencent.com",
        "referer": "https://yuanbao.tencent.com/",
        "user-agent": DESKTOP_UA,
        "x-language": "zh-CN",
        "x-platform": "windows",
        "x-requested-with": "XMLHttpRequest",
        "x-source": "web",
        "cookie": cookie,
    }
    r = await client.post(YUANBAO_API,
                          json={"type": "video_channel_url",
                                "url": share_url, "scene": 1},
                          headers=headers)
    if r.status_code == 401:
        # 登录态失效:删除缓存,下次解析重新走一次授权
        wx_login.clear_cookie()
        raise wx_login.NeedLoginError(
            "元宝登录态已失效,已为你重置。请重新点击【解析链接】完成一次"
            "扫码授权。")
    if not (200 <= r.status_code < 300):
        raise WxChannelsParseError(f"元宝接口 HTTP {r.status_code}")
    data = r.json()
    inner = data.get("data") or {}
    export_id = inner.get("wx_export_id")
    playable = (inner.get("playable_url") or "").strip()
    if not export_id or not playable:
        raise WxChannelsParseError(
            f"元宝未返回解析结果(分享链接可能无效):{str(data)[:120]}")
    qs = parse_qs(urlparse(playable).query)
    token = (qs.get("token") or [""])[0]
    eid = (qs.get("eid") or [""])[0] or export_id
    if not token:
        raise WxChannelsParseError("元宝返回的播放地址缺少 token")

    page_url = "https://channels.weixin.qq.com/finder-preview/pages/feed"
    api = (f"{FEED_INFO_API}?_rid={_rid()}"
           f"&_pageUrl={quote(page_url, safe='')}")
    r2 = await client.post(api,
                           json={"baseReq": {"generalToken": token},
                                 "exportId": eid},
                           headers={"Accept": "application/json",
                                    "Content-Type": "application/json",
                                    "Origin": "https://channels.weixin.qq.com",
                                    "Referer": page_url,
                                    "User-Agent": DESKTOP_UA})
    if not (200 <= r2.status_code < 300):
        raise WxChannelsParseError(f"视频号预览接口 HTTP {r2.status_code}")
    body = r2.json()
    if body.get("errCode") not in (0, None):
        raise WxChannelsParseError(
            f"预览接口错误:{body.get('errMsg') or body}")
    d = body.get("data") or {}
    err = d.get("errMsg")
    if isinstance(err, dict) and (err.get("title") or err.get("content")):
        raise WxChannelsParseError(
            f"作品不可访问:{err.get('title', '')} {err.get('content', '')}")
    feed = d.get("feedInfo") or {}
    if not feed:
        raise WxChannelsParseError("预览接口未返回作品数据(可能已删除)")
    return feed


# ------------------------------------------------------------------ 共通
def _to_minimal(feed: dict) -> dict:
    """feedInfo → 与其它平台一致的精简字典"""
    author = (feed.get("authorInfo")
              or feed.get("author") or {}).get("nickname") or ""

    pic_info = feed.get("picInfo") or []
    video_info = feed.get("h264VideoInfo") or {}
    video_url = (video_info.get("videoUrl")
                 or (feed.get("videoUrl") or "")).strip()
    h265_url = ((feed.get("h265VideoInfo") or {}).get("videoUrl") or "").strip()
    media_type = feed.get("mediaType", 0)

    if video_url or h265_url:
        candidates = [u for u in (video_url, h265_url) if u]
        files_kind = "video"
        image_urls = []
    elif pic_info:
        image_urls = [(p.get("url") or "").strip()
                      for p in pic_info if (p.get("url") or "").strip()]
        image_urls = [u for u in image_urls if u]
        if not image_urls:
            raise WxChannelsParseError("图集作品未解析到图片地址")
        files_kind = "image"
        candidates = []
    elif media_type == 2:
        raise WxChannelsParseError("图集作品未解析到图片地址")
    else:
        raise WxChannelsParseError(
            "未获取到视频直链(需要有效的元宝登录态,或在程序代理框填入"
            "可用代理后重试)")

    return {
        "type": files_kind,
        "platform": "wxchannels",
        "aweme_id": str(feed.get("objectId") or feed.get("id") or ""),
        "desc": (feed.get("description") or "").strip(),
        "author": {"nickname": author},
        "video_data": {"candidates": candidates},
        "image_data": {"no_watermark_image_list": image_urls},
    }


async def parse_share_url(url: str, *, proxy: str | None = None,
                          on_status=None) -> dict:
    """分享链接 → 精简数据字典(在线服务优先,本地复刻兜底)"""
    import wx_login
    share_id = extract_share_id(url)
    if not share_id:
        raise WxChannelsParseError(
            f"不是有效的视频号分享链接(需 https://weixin.qq.com/sph/xxx):"
            f"{(url or '')[:80]}")
    share_url = f"https://weixin.qq.com/sph/{share_id}"

    global _online_disabled
    proxies = ({"http://": proxy, "https://": proxy} if proxy else None)
    errors = []
    async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                 proxies=proxies) as client:
        # 路线1:在线服务(被墙时约 15 秒内失败;失败过一次本会话内跳过)
        if not _online_disabled:
            try:
                result = await _parse_online(client, share_url)
                _online_disabled = False
                return _to_minimal(result)
            except WxChannelsParseError as exc:
                if "errCode" in str(exc) or "未返回" in str(exc):
                    raise          # 服务可达但业务失败:不必换路线
                errors.append(f"在线服务:{exc}")
                _online_disabled = True   # 网络/服务不可达,本次会话跳过
            except Exception as exc:
                errors.append(f"在线服务:{type(exc).__name__}: {str(exc)[:60]}")
                _online_disabled = True

        # 路线2:本地复刻(元宝登录态 + finder-preview)
        try:
            return _to_minimal(await _parse_local(client, share_url,
                                                  on_status=on_status))
        except wx_login.NeedLoginError:
            raise                 # 必须抛给 GUI 层弹出授权引导
        except WxChannelsParseError:
            raise
        except Exception as exc:
            errors.append(f"本地流程:{type(exc).__name__}: {str(exc)[:80]}")

    raise WxChannelsParseError("；".join(errors))


def parse_share_url_sync(url: str, *, proxy: str | None = None,
                         on_status=None) -> dict:
    """同步包装,供线程内调用"""
    return asyncio.run(parse_share_url(url, proxy=proxy, on_status=on_status))
