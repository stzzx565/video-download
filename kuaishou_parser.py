# -*- coding: utf-8 -*-
"""
快手解析模块。

方案:分享短链(v.kuaishou.com 等)跟随跳转到带分享参数的作品页,
从页面内嵌的 window.__APOLLO_STATE__ 提取
VisionVideoDetailPhoto:{id}.photoUrl(无水印直链)。
注意:干净的 /short-video/{id} 页面(无 cc 等分享参数)对游客不出 SSR
数据,graphql 接口对游客触发验证码,因此必须保留原始跳转参数。

支持链接形态:
  https://v.kuaishou.com/xxxxx            分享短链(推荐)
  https://www.kuaishou.com/f/xxxxx        分享码
  https://www.kuaishou.com/short-video/作品ID?...
  https://kuaishou.cn/short-video/作品ID
  https://*.m.chenzhongtech.com/fw/photo/作品ID?...
  直播间链接会给出明确提示(暂不支持)。
"""

import asyncio
import json
import re

import httpx

DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 "
             "Mobile/15E148 Safari/604.1")

_ID_RE = re.compile(r"photoId=([0-9A-Za-z]+)|/(?:short-video|fw/photo)/([0-9A-Za-z]+)")
_LIVE_RE = re.compile(r"/(?:live|profile)/", re.I)


class KuaishouParseError(Exception):
    """快手解析失败"""


def extract_apollo(html: str):
    """括号配对提取 window.__APOLLO_STATE__ 的 JSON(容忍字符串/转义)"""
    i = html.find("window.__APOLLO_STATE__")
    if i < 0:
        return None
    i = html.find("{", i)
    if i < 0:
        return None
    depth, in_str, esc = 0, False, False
    for j in range(i, len(html)):
        ch = html[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(html[i:j + 1])
                    except ValueError:
                        return None
    return None


def _find_key(node, key):
    if isinstance(node, dict):
        if key in node and node[key]:
            return node[key]
        for v in node.values():
            r = _find_key(v, key)
            if r is not None:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _find_key(v, key)
            if r is not None:
                return r
    return None


def _extract_photo_id(url: str):
    m = _ID_RE.search(url)
    if not m:
        return None
    return m.group(1) or m.group(2)


def _parse_html(url: str, html: str, photo_id):
    apollo = extract_apollo(html)
    if apollo is None:
        return None
    state = apollo.get("defaultClient", apollo)
    photo = state.get(f"VisionVideoDetailPhoto:{photo_id}") \
        or _find_key(apollo, f"VisionVideoDetailPhoto:{photo_id}")
    if not photo:
        pks = [k for k in state if str(k).startswith("VisionVideoDetailPhoto:")]
        photo = state[pks[0]] if pks else None
    if not photo or not photo.get("photoUrl"):
        return None
    author = {}
    akeys = [k for k in state if str(k).startswith("VisionVideoDetailAuthor:")]
    if akeys:
        author = state[akeys[0]] or {}
    ref = photo.get("author")
    if isinstance(ref, dict) and ref.get("__ref"):
        author = state.get(ref["__ref"], author) or {}
    images = photo.get("images") or []
    image_urls = []
    for img in images:
        if isinstance(img, dict):
            u = img.get("url") or ((img.get("url_list") or [None])[0])
            if u:
                image_urls.append(u)
    return {
        "type": "image" if image_urls else "video",
        "platform": "kuaishou",
        "aweme_id": photo_id,
        "desc": (photo.get("caption") or "").strip(),
        "author": {"nickname": author.get("name") or ""},
        "video_data": {"candidates": [photo["photoUrl"]]},
        "image_data": {"no_watermark_image_list": image_urls},
    }


async def parse_share_url(url: str, *, proxy: str | None = None) -> dict:
    """分享链接 → 与 douyin_v5.to_minimal 兼容的精简数据字典"""
    proxies = ({"http://": proxy, "https://": proxy} if proxy else None)
    headers = {"User-Agent": DESKTOP_UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    async with httpx.AsyncClient(timeout=25, follow_redirects=True,
                                 proxies=proxies) as client:
        resp = await client.get(url, headers=headers)
        final_url = str(resp.url)
        if _LIVE_RE.search(final_url) and "short-video" not in final_url \
                and "photo" not in final_url:
            raise KuaishouParseError("这是直播间链接,暂不支持(仅支持作品/图集)")
        photo_id = _extract_photo_id(final_url)
        page_html = resp.text

        # 云端(数据中心 IP)可能拿到无 302 的响应:短链页原样返回 200,
        # 里面带 JS/meta 跳转或风控页。依次尝试:响应体挖 ID → 整链重试。
        if not photo_id:
            m = (re.search(r"photoId[\"']?\s*[:=]\s*[\"']([0-9A-Za-z]+)", page_html)
                 or re.search(r"/(?:short-video|fw/photo)/([0-9A-Za-z]+)", page_html))
            if m:
                photo_id = m.group(1)
                final_url = f"https://www.kuaishou.com/short-video/{photo_id}"
        if not photo_id:
            # 间歇性风控:整链重试一次
            resp = await client.get(url, headers=headers)
            final_url = str(resp.url)
            photo_id = _extract_photo_id(final_url)
            page_html = resp.text
        if not photo_id:
            # 换手机 UA 再试(快手对数据中心 IP + 不同 UA 的风控策略不同)
            mobile_headers = {"User-Agent": MOBILE_UA,
                              "Accept-Language": "zh-CN,zh;q=0.9"}
            resp_m = await client.get(url, headers=mobile_headers)
            final_url = str(resp_m.url)
            photo_id = _extract_photo_id(final_url)
            page_html = resp_m.text
        if not photo_id:
            raise KuaishouParseError(
                f"链接中未找到作品 ID:{final_url[:80]}"
                f"(页面片段:{page_html[:120].strip()[:100]})")

        data = _parse_html(final_url, page_html, photo_id)
        if data is None:
            # 兜底:老式 m.chenzhongtech.com 跳转 / 无 SSR 页面
            # → 用带 cc 参数的标准作品页重试
            retry = f"https://www.kuaishou.com/short-video/{photo_id}?cc=share_copylink"
            resp2 = await client.get(retry, headers=headers)
            data = _parse_html(retry, resp2.text, photo_id)
        if data is None:
            # 再兜底一次:整链重新解析(应对首次进入风控页的情况)
            resp3 = await client.get(url, headers=headers)
            final3 = str(resp3.url)
            pid3 = _extract_photo_id(final3)
            if pid3:
                data = _parse_html(final3, resp3.text, pid3)
                if data is None:
                    retry3 = f"https://www.kuaishou.com/short-video/{pid3}?cc=share_copylink"
                    resp4 = await client.get(retry3, headers=headers)
                    data = _parse_html(retry3, resp4.text, pid3)
        if data is None:
            raise KuaishouParseError(
                "未解析到作品数据(作品可能已被删除,或为不支持的内容)")
        return data


def parse_share_url_sync(url: str, *, proxy: str | None = None) -> dict:
    """同步包装,供线程内调用"""
    return asyncio.run(parse_share_url(url, proxy=proxy))
