# -*- coding: utf-8 -*-
"""
抖音解析桥接 — 使用上游 v5.1.3(Douyin_TikTok_Download_API)的原生签名栈 + wreq 浏览器仿真传输。

2026 年起抖音网页接口要求 x-secsdk-web-signature(UIFID 设备签名)并且校验
TLS 指纹:老版 V4 爬虫会被 403 "Uifid Not Found"/"Validate Error" 拒绝。
本模块用 v5 的 NativeSigner(a_bogus + websign)+ 访客 Cookie
(ttwid / s_v_web_id / UIFID_TEMP)+ wreq(Chrome TLS 仿真)在纯 Python 内
完成全部签名与请求,无需浏览器、无需登录。
"""

import asyncio
import re

import httpx

from dtk.core.types import BrowserFamily, Platform
from dtk.platforms.douyin.adapter import ADAPTER
from dtk.signing.base import (
    RequestSpec as SigningRequestSpec,
    SigningSession,
    StaticFingerprint,
)
from dtk.signing.native.signer import NativeSigner
from dtk.transport import WreqTransport
from dtk.transport.base import Fingerprint, RequestSpec as TransportRequestSpec
from dtk.transport.base import TransportIdentity

DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

_FINGERPRINT = StaticFingerprint(
    user_agent=DESKTOP_UA,
    browser_platform="Win32",
    screen_width=1920,
    screen_height=1080,
)
_TRANSPORT_FP = Fingerprint(
    browser_family=BrowserFamily.CHROME,
    browser_major=130,
    user_agent=DESKTOP_UA,
    platform="Win32",
    screen="1920x1080",
    language="zh-CN",
    timezone="Asia/Shanghai",
    hardware_concurrency=8,
    device_memory=8,
)

_signer = NativeSigner(Platform.DOUYIN)
_transport = WreqTransport()
_cookie_jar = None      # 访客身份 Cookie,进程内复用


class DouyinFetchError(Exception):
    """抖音解析失败(网络/风控/接口变更)"""


def _profile():
    # ProfileSource 协议:user_agent / platform / screen / language / timezone
    class _Src:
        user_agent = DESKTOP_UA
        platform = "Win32"
        screen = "1920x1080"
        language = "zh-CN"
        timezone = "Asia/Shanghai"
    return ADAPTER.profile_for(_Src())


async def _ensure_cookie_jar() -> dict:
    """取访客身份 Cookie(由 douyin_identity 自动铸造并缓存)"""
    global _cookie_jar
    if _cookie_jar is None:
        import douyin_identity
        _cookie_jar = await asyncio.to_thread(douyin_identity.get_jar)
    return _cookie_jar


async def refresh_identity() -> None:
    """被风控时强制刷新访客身份"""
    global _cookie_jar
    import douyin_identity
    _cookie_jar = await asyncio.to_thread(douyin_identity.refresh_jar)


async def fetch_aweme_detail(aweme_id: str, *, proxy: str | None = None,
                             timeout: float = 25.0) -> dict:
    """拉取单个作品详情(抖音 /aweme/v1/web/aweme/detail/),返回 aweme_detail 字典"""
    jar = await _ensure_cookie_jar()
    spec = ADAPTER.build_request(
        "douyin.content_detail", profile=_profile(), aweme_id=str(aweme_id)
    )
    session = SigningSession(cookies=jar)
    request = SigningRequestSpec(method=spec["method"], url=spec["url"],
                                 params=spec.get("params"), headers=spec.get("headers"))
    signed = await _signer.sign(request, _FINGERPRINT, session)

    identity = TransportIdentity(
        id="desktop-guest-douyin",
        platform=Platform.DOUYIN,
        fingerprint=_TRANSPORT_FP,
        proxy_url=proxy or None,
        cookies=jar,
    )
    transport_spec = TransportRequestSpec(
        url=signed.signed_url(spec["url"]),   # 已签名 URL,字节级原样发送
        method="GET",
        params=None,
        headers={**dict(spec.get("headers") or {}), **dict(signed.headers)},
        endpoint="douyin.content_detail",
    )
    resp = await _transport.request(identity, transport_spec, timeout=timeout)
    if not resp.ok:
        raise DouyinFetchError(f"抖音接口 HTTP {resp.status}:{resp.text[:150]}")
    try:
        data = resp.json()
    except Exception:
        raise DouyinFetchError(f"抖音接口返回非 JSON({len(resp.body)} 字节,疑似风控页)")
    if data.get("status_code") not in (0, None):
        raise DouyinFetchError(f"抖音接口状态码 {data.get('status_code')}:"
                               f"{data.get('status_msg', '')}")
    detail = data.get("aweme_detail")
    if not detail:
        raise DouyinFetchError("抖音接口未返回作品数据(可能已被删除或风控)")
    return detail


async def resolve_short_url(url: str, *, proxy: str | None = None) -> str:
    """把 v.douyin.com 短链解析成最终地址(含作品 ID)"""
    proxies = ({"http://": proxy, "https://": proxy} if proxy else None)
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 proxies=proxies) as client:
        resp = await client.get(url, headers={"User-Agent": DESKTOP_UA})
    return str(resp.url)


def extract_aweme_id(url: str) -> str:
    m = (re.search(r"/(?:video|note|slides)/(\d+)", url)
         or re.search(r"[?&]aweme_id=(\d+)", url)
         or re.search(r"modal_id=(\d+)", url))
    if not m:
        raise DouyinFetchError(f"链接中未找到作品 ID:{url}")
    return m.group(1)


def to_minimal(detail: dict) -> dict:
    """把 aweme_detail 转成与老版 HybridCrawler.minimal 兼容的精简结构"""
    aweme_type = detail.get("aweme_type")
    url_type_code_dict = {0: "video", 2: "image", 4: "video", 68: "image",
                          51: "video", 55: "video", 58: "video", 61: "video",
                          150: "image"}
    url_type = url_type_code_dict.get(aweme_type, "video")
    video = detail.get("video") or {}
    result = {
        "type": url_type,
        "platform": "douyin",
        "aweme_id": str(detail.get("aweme_id") or ""),
        "desc": detail.get("desc"),
        "create_time": detail.get("create_time"),
        "author": detail.get("author") or {},
        "music": detail.get("music") or {},
        "statistics": detail.get("statistics") or {},
        "cover_data": {
            "cover": video.get("cover"),
            "origin_cover": video.get("origin_cover"),
            "dynamic_cover": video.get("dynamic_cover"),
        },
        "hashtags": detail.get("text_extra"),
    }
    if url_type == "video":
        uri = (video.get("play_addr") or {}).get("uri", "")
        hq = ((video.get("play_addr") or {}).get("url_list") or [None])[0]
        nwm_hq = (hq or "").replace("playwm", "play")
        snssdk_nwm = (f"https://aweme.snssdk.com/aweme/v1/play/"
                      f"?video_id={uri}&ratio=1080p&line=0") if uri else None
        # 按优先级排列的下载候选(主链可能过期/受限,逐个回退)
        candidates = []
        for u in (nwm_hq, snssdk_nwm, hq):
            if u and u not in candidates:
                candidates.append(u)
        result["video_data"] = {
            "wm_video_url": f"https://aweme.snssdk.com/aweme/v1/playwm/?video_id={uri}&radio=1080p&line=0",
            "wm_video_url_HQ": hq,
            "nwm_video_url": snssdk_nwm,
            "nwm_video_url_HQ": nwm_hq,
            "candidates": candidates,
        }
    else:
        images = detail.get("images") or []
        no_wm, wm = [], []
        for img in images:
            no_wm.append(((img.get("url_list") or [""])[0]))
            wm.append(((img.get("download_url_list") or [""])[0]))
        result["image_data"] = {
            "no_watermark_image_list": no_wm,
            "watermark_image_list": wm,
        }
    return result


async def parse_share_url(url: str, *, proxy: str | None = None) -> dict:
    """分享链接(口令)→ 精简数据字典;身份失效时自动刷新重试一次"""
    final_url = await resolve_short_url(url, proxy=proxy)
    aweme_id = extract_aweme_id(final_url) if "/video/" in final_url or "/note/" in final_url \
        or "/slides/" in final_url or "aweme_id" in final_url else None
    if not aweme_id:
        aweme_id = extract_aweme_id(url)
    try:
        detail = await fetch_aweme_detail(aweme_id, proxy=proxy)
    except DouyinFetchError:
        await refresh_identity()
        detail = await fetch_aweme_detail(aweme_id, proxy=proxy)
    return to_minimal(detail)


def parse_share_url_sync(url: str, *, proxy: str | None = None) -> dict:
    """同步包装,供线程内调用"""
    return asyncio.run(parse_share_url(url, proxy=proxy))
