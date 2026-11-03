"""HTTP 基础设施：全局限速 + 重试 + 退避。

为什么必须限速：东方财富会对高频请求直接掐连接（RemoteDisconnected），
我在开发时就因为连发 20 多个请求被临时封了 IP。定时任务里更要克制。
"""
from __future__ import annotations

import json
import logging
import random
import ssl
import time
import urllib.error
import urllib.request

# 东财部分子域（datacenter-web 等）的 SSL 证书缺少 keyUsage 扩展，
# Python 严格模式会拒绝。创建宽松 context 作为降级方案。
_SSL_LENIENT = ssl.create_default_context()
_SSL_LENIENT.check_hostname = False
_SSL_LENIENT.verify_mode = ssl.CERT_NONE

# 东财 WAF 需要 cookie 会话保持（类似 requests.Session），
# 用 CookieJar + opener 实现跨请求 cookie 持久化。
import http.cookiejar
_em_cookie_jar = http.cookiejar.CookieJar()
_em_opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(_em_cookie_jar),
    urllib.request.HTTPSHandler(context=_SSL_LENIENT),
)

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

MIN_INTERVAL = 0.35  # 同一进程内两次请求的最小间隔（秒）
_last_call = 0.0


class FetchError(RuntimeError):
    pass


def _wait() -> None:
    global _last_call
    delta = time.time() - _last_call
    if delta < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - delta + random.uniform(0, 0.12))
    _last_call = time.time()


def fetch(url: str, headers: dict | None = None, tries: int = 3, timeout: int = 20) -> bytes:
    """带限速与指数退避的 GET，返回原始字节。"""
    hdrs = {"User-Agent": UA, "Accept": "*/*", "Connection": "close"}
    if headers:
        hdrs.update(headers)
    last: Exception | None = None
    for i in range(tries):
        _wait()
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                if not body:
                    raise FetchError("空响应")
                return body
        except (urllib.error.URLError, FetchError, TimeoutError, OSError) as exc:
            last = exc
            if i < tries - 1:
                time.sleep(0.8 * (2 ** i) + random.uniform(0, 0.3))
    raise FetchError(f"{type(last).__name__}: {last}")


def get_json(url: str, headers: dict | None = None, tries: int = 3) -> dict:
    body = fetch(url, headers=headers, tries=tries)
    try:
        return json.loads(body.decode("utf-8", "ignore"))
    except json.JSONDecodeError as exc:
        raise FetchError(f"JSON 解析失败: {exc}") from exc


def get_text(url: str, encoding: str = "utf-8", headers: dict | None = None, tries: int = 3) -> str:
    return fetch(url, headers=headers, tries=tries).decode(encoding, "ignore")


# ─── 东财专用限流通道 ────────────────────────────────────────────────
# 东财风控阈值（社区实测）：>5次/秒 或 1分钟≥200次 → 临时封IP（RemoteDisconnected/403/429）
# 被封后 push2 全系列不可用，持续数小时甚至 20h+。
# 铁律：串行、≥1.2s间隔+随机抖动、带 UA+Referer、不并发。
# 参考：github.com/simonlin1212/a-stock-data 的 em_get() 防封策略。

EM_MIN_INTERVAL = 1.2  # 两次东财请求最小间隔（秒）
_em_last_call = 0.0

# 东财子域列表（push2/push2his/push2ex/datacenter-web 等）
_EM_DOMAINS = (
    "eastmoney.com",
    "push2.eastmoney.com",
    "push2his.eastmoney.com",
    "push2ex.eastmoney.com",
    "datacenter-web.eastmoney.com",
    "np-anotice-stock.eastmoney.com",
    "reportapi.eastmoney.com",
)


def _em_wait() -> None:
    """东财专用限流等待。"""
    global _em_last_call
    now = time.time()
    elapsed = now - _em_last_call
    if elapsed < EM_MIN_INTERVAL:
        sleep_t = EM_MIN_INTERVAL - elapsed + random.uniform(0.1, 0.5)
        time.sleep(sleep_t)
    _em_last_call = time.time()


def _build_url(url: str, params: dict | None = None) -> str:
    """将 params dict 拼接到 URL query string（纯 stdlib）。"""
    if not params:
        return url
    from urllib.parse import urlencode, urlparse, parse_qs, urlunparse
    parsed = urlparse(url)
    # 合并已有 query 和新 params
    existing = parse_qs(parsed.query, keep_blank_values=True)
    for k, v in params.items():
        existing[k] = [str(v)]
    # 重新编码（parse_qs 返回 list，需要展平）
    flat = {k: v[0] if len(v) == 1 else v for k, v in existing.items()}
    new_query = urlencode(flat, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def em_fetch(url: str, params: dict | None = None, headers: dict | None = None,
             tries: int = 2, timeout: int = 15) -> bytes:
    """东财专用请求：限流 + cookie 会话 + 宽松SSL + 重试。

    与通用 fetch() 的区别：
      1. 间隔 ≥1.2s（通用的是 0.35s）
      2. 自动加 Referer: https://quote.eastmoney.com/
      3. 使用 _em_opener（cookie 持久化 + SSL 降级）
      4. 只重试 2 次（被封时快速失败）
    """
    full_url = _build_url(url, params)
    hdrs = {
        "User-Agent": UA,
        "Referer": "https://data.eastmoney.com/",
        "Accept": "application/json, text/plain, */*",
    }
    if headers:
        hdrs.update(headers)

    last: Exception | None = None
    for i in range(tries):
        _em_wait()
        try:
            req = urllib.request.Request(full_url, headers=hdrs)
            with _em_opener.open(req, timeout=timeout) as resp:
                body = resp.read()
                if not body:
                    raise FetchError("东财返回空响应")
                # 检测 WAF 拦截页（HTML 而非 JSON）
                if body[:15].lstrip().startswith(b"<!DOCTYPE") or body[:5].lstrip().startswith(b"<html"):
                    raise FetchError("东财 WAF 拦截（返回 HTML）")
                return body
        except FetchError:
            raise  # WAF 拦截不重试
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            if "RemoteDisconnected" in str(exc) or "ConnectionReset" in str(exc):
                log.warning("东财连接被断开（可能触发限流）: %s, 第%d次", full_url[:80], i + 1)
            if i < tries - 1:
                time.sleep(2.0 * (2 ** i) + random.uniform(0.5, 1.5))
    raise FetchError(f"东财请求失败 {type(last).__name__}: {last}")


def em_get_json(url: str, params: dict | None = None, headers: dict | None = None,
                tries: int = 2) -> dict:
    """东财专用 JSON 请求（限流 + 自动 Referer）。"""
    body = em_fetch(url, params=params, headers=headers, tries=tries)
    try:
        return json.loads(body.decode("utf-8", "ignore"))
    except json.JSONDecodeError as exc:
        raise FetchError(f"东财 JSON 解析失败: {exc}") from exc


def is_em_url(url: str) -> bool:
    """判断 URL 是否属于东财域名（用于自动选择限流策略）。"""
    return any(d in url for d in _EM_DOMAINS)
