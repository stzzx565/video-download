# -*- coding: utf-8 -*-
"""
抖音访客身份管理。

抖音 2026 年起的网页接口要求真实浏览器安全 SDK 生成的设备身份
(UIFID_TEMP 等)。本模块通过 CDP 直连系统已安装的 Edge/Chrome 无头
访问一次抖音首页,收集安全 Cookie 并缓存到磁盘,供 douyin_v5 签名
链路使用(零第三方依赖,见 cdp_browser.py)。

- 优先 Edge(Windows 自带),失败回退 Chrome;
- 身份缓存于程序目录 douyin_identity.json,默认 6 小时内复用;
- 解析失败时可强制刷新。
"""

import json
import os
import threading
import time

from downloader_core import APP_DIR

JAR_PATH = os.path.join(APP_DIR, "douyin_identity.json")
JAR_MAX_AGE_SECONDS = 6 * 3600

_lock = threading.Lock()
_cache = None            # (jar_dict, timestamp)


class IdentityMintError(Exception):
    """无法自动获取抖音访客身份"""


def _collect_browser_cookies() -> dict:
    """无头模式驱动系统浏览器访问抖音首页,收集 Cookie(含 HttpOnly)

    通过 CDP 直连系统 Edge/Chrome(零依赖,见 cdp_browser.py)。
    注意 UA 必须是纯净 Chrome(不带 Edg/ 标识),否则抖音会对无头实例
    弹验证码中间页,身份永远发不下来。
    """
    import cdp_browser
    jar = cdp_browser.mint_cookies(
        "https://www.douyin.com/",
        wait_names=("UIFID_TEMP", "ttwid", "s_v_web_id"),
        timeout_s=45.0)
    if not jar.get("UIFID_TEMP"):
        raise IdentityMintError(
            "浏览器访问抖音未生成设备身份(UIFID_TEMP 缺失,可能被风控)")
    return jar


def _load_disk():
    try:
        with open(JAR_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        jar, ts = data.get("jar") or {}, float(data.get("time") or 0)
        if jar.get("UIFID_TEMP") and (time.time() - ts) < JAR_MAX_AGE_SECONDS:
            return jar
    except Exception:
        pass
    return None


def _save_disk(jar):
    try:
        with open(JAR_PATH, "w", encoding="utf-8") as f:
            json.dump({"jar": jar, "time": time.time()}, f)
    except Exception:
        pass


def get_jar(force_refresh: bool = False) -> dict:
    """取可用的访客身份;需要时自动铸造并缓存(线程安全)"""
    global _cache
    with _lock:
        if not force_refresh:
            if _cache is not None:
                return _cache
            disk = _load_disk()
            if disk:
                _cache = disk
                return _cache
        jar = _collect_browser_cookies()
        if not jar.get("UIFID_TEMP"):
            raise IdentityMintError("浏览器访问抖音未生成设备身份(UIFID_TEMP 缺失)")
        _cache = jar
        _save_disk(jar)
        return jar


def refresh_jar() -> dict:
    """强制刷新身份(解析被风控时调用)"""
    return get_jar(force_refresh=True)
