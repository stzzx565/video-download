# -*- coding: utf-8 -*-
"""
小红书解析模块。

方案:跟随 xhslink.com 短链(或直接用笔记页链接)到带 xsec_token 的
笔记页,从页面内嵌的 window.__INITIAL_STATE__ 提取笔记数据:
  - 视频笔记:note.video.media.stream.h264[*].masterUrl(无水印直链)
  - 图文笔记:note.imageList[*](无水印图)

注意:SSR 状态里含 JS 字面量 undefined,需清洗成 null 才能解析。
"""

import asyncio
import json
import re

import httpx

DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_NOTE_ID_RE = re.compile(r"/(?:explore|discovery/item|notes)/([0-9a-f]{24})")


class XhsParseError(Exception):
    """小红书解析失败"""


def _extract_state(html: str):
    """括号配对提取 window.__INITIAL_STATE__ 的 JSON(容忍字符串/转义)"""
    i = html.find("window.__INITIAL_STATE__")
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
                    blob = re.sub(r":\s*undefined\b", ":null", html[i:j + 1])
                    try:
                        return json.loads(blob)
                    except ValueError:
                        return None
    return None


def _parse_note(state: dict, note_id: str):
    note_map = (state.get("note") or {}).get("noteDetailMap") or {}
    note = None
    if note_id in note_map:
        note = (note_map[note_id] or {}).get("note")
    if not note:
        for wrap in note_map.values():
            n = (wrap or {}).get("note")
            if n and n.get("noteId") == note_id:
                note = n
                break
    if not note:
        pks = [k for k in note_map if note_map[k]]
        if pks:
            note = (note_map[pks[0]] or {}).get("note")
    if not note:
        return None

    author = ((note.get("user") or {}).get("nickname") or "")
    title = (note.get("title") or note.get("desc") or "").strip()

    if note.get("type") == "video":
        stream = ((note.get("video") or {}).get("media") or {}).get("stream") or {}
        candidates = []
        for pool in ("h264", "h265", "av1"):
            for item in stream.get(pool) or []:
                u = item.get("masterUrl") or ((item.get("backupUrls") or [None])[0])
                if u and u not in candidates:
                    candidates.append(u)
        if not candidates:
            return None
        kind = "video"
        image_urls = []
    else:
        image_urls = []
        for img in note.get("imageList") or []:
            u = (img.get("urlDefault") or img.get("url") or "")
            if not u:
                infos = img.get("infoList") or []
                if infos:
                    u = (infos[-1].get("url") or "")
            if u:
                image_urls.append(u)
        if not image_urls:
            return None
        kind = "image"
        candidates = []

    return {
        "type": kind,
        "platform": "xhs",
        "aweme_id": note.get("noteId") or note_id,
        "desc": title,
        "author": {"nickname": author},
        "video_data": {"candidates": candidates},
        "image_data": {"no_watermark_image_list": image_urls},
    }


async def parse_share_url(url: str, *, proxy: str | None = None) -> dict:
    """分享链接 → 与 douyin_v5.to_minimal 兼容的精简数据字典(附 _files)"""
    proxies = ({"http://": proxy, "https://": proxy} if proxy else None)
    headers = {"User-Agent": DESKTOP_UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    async with httpx.AsyncClient(timeout=25, follow_redirects=True,
                                 proxies=proxies) as client:
        resp = await client.get(url, headers=headers)
        final_url = str(resp.url)
        m = (_NOTE_ID_RE.search(final_url) or _NOTE_ID_RE.search(url))
        if not m:
            raise XhsParseError(f"链接中未找到笔记 ID:{final_url[:80]}")
        note_id = m.group(1)
        state = _extract_state(resp.text)
        data = _parse_note(state, note_id) if state is not None else None
        if data is None:
            # 兜底重试(小红书偶发对同一会话限流,短暂等待后再取一次)
            await asyncio.sleep(2)
            resp2 = await client.get(final_url, headers=headers)
            state = _extract_state(resp2.text) or _extract_state(resp.text)
            data = _parse_note(state, note_id) if state is not None else None
        if data is None:
            raise XhsParseError("页面无笔记数据(可能需要登录或笔记已删除)")
        return data


def parse_share_url_sync(url: str, *, proxy: str | None = None) -> dict:
    """同步包装,供线程内调用"""
    return asyncio.run(parse_share_url(url, proxy=proxy))
