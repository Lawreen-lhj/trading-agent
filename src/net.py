"""HTTP 基础设施：全局限速 + 重试 + 退避。

为什么必须限速：东方财富会对高频请求直接掐连接（RemoteDisconnected），
我在开发时就因为连发 20 多个请求被临时封了 IP。定时任务里更要克制。
"""
from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.request

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
