#!/usr/bin/env python3
"""本地 Web 控制台 —— 给交易 Agent 一个能点的界面。

    .venv/bin/python server.py            # 打开 http://127.0.0.1:8765
    .venv/bin/python server.py --port 9000 --no-open

只监听 127.0.0.1，不对外暴露。所有数据读写都落回 config.yaml / data/，
界面本身不持有任何状态，关掉就没了，不会出现"界面上改了但文件没改"的情况。
"""
from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import re
import subprocess
import threading
import traceback
import webbrowser
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import run_brief
from src import configstore, notify
from src.db import Store

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DATA = ROOT / "data"
CONFIG = ROOT / "config.yaml"
BACKUP = DATA / "backup"
BRIEFINGS = DATA / "briefings"
DB_PATH = DATA / "market.db"

BRIEF_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}|demo)\.html$")


# ---------------------------------------------------------------- 运行管理
class _LogHandler(logging.Handler):
    def __init__(self, sink):
        super().__init__()
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.sink(record.getMessage())
        except Exception:  # 日志本身不能把主流程搞崩
            pass


class RunManager:
    """同一时刻只允许一次运行：并发跑会同时打同一个接口，容易被限流。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.running = False
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.result: dict | None = None
        self.log: deque[str] = deque(maxlen=300)

    def add(self, msg: str) -> None:
        self.log.append(f"{datetime.now():%H:%M:%S}  {msg}")

    def start(self, notify_enabled: bool) -> bool:
        with self._lock:
            if self.running:
                return False
            self.running = True
            self.result = None
            self.log.clear()
            self.started_at = datetime.now().strftime("%H:%M:%S")
            self.finished_at = None
        threading.Thread(target=self._work, args=(notify_enabled,), daemon=True).start()
        return True

    def _work(self, notify_enabled: bool) -> None:
        handler = _LogHandler(self.add)
        root_logger = logging.getLogger()
        old_level = root_logger.level
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.INFO)
        try:
            self.add("开始采集行情（主源东财，失败自动切腾讯）…")
            result = run_brief.run_once(CONFIG, notify_enabled=notify_enabled, quiet=True)
            self.result = result
            self.add(
                f"完成：{result['watch_count']} 只标的，"
                f"{result['plans_ready']} 个可执行计划，{result['alerts']} 条提醒"
            )
            if result.get("data_health"):
                self.add("数据缺口：" + "；".join(result["data_health"]))
        except Exception as exc:  # noqa: BLE001 —— 界面要把真实错误显示出来
            self.result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self.add(f"失败：{type(exc).__name__}: {exc}")
            self.add(traceback.format_exc(limit=3).replace("\n", " | "))
        finally:
            root_logger.removeHandler(handler)
            root_logger.setLevel(old_level)
            with self._lock:
                self.running = False
                self.finished_at = datetime.now().strftime("%H:%M:%S")

    def status(self) -> dict:
        return {
            "running": self.running,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "log": list(self.log),
        }


RUNNER = RunManager()


# ---------------------------------------------------------------- 数据汇总
def _scan_briefings() -> tuple[Path | None, list[dict]]:
    if not BRIEFINGS.exists():
        return None, []
    items = []
    for p in BRIEFINGS.glob("*.html"):
        if not BRIEF_FILE_RE.match(p.name):
            continue
        j = p.with_suffix(".json")
        items.append(
            {
                "date": p.stem,
                "html": f"/briefings/{p.name}",
                "has_json": j.exists(),
                "size": p.stat().st_size,
                "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M"),
                "is_demo": p.stem == "demo",
            }
        )
    # 真实日期简报按日期倒序在前，排版演示样例沉底
    items.sort(key=lambda it: (it["is_demo"], "" if it["is_demo"] else ""), reverse=False)
    dated = sorted((it for it in items if not it["is_demo"]),
                   key=lambda it: it["date"], reverse=True)
    demos = [it for it in items if it["is_demo"]]
    items = dated + demos

    latest = next(
        (BRIEFINGS / f"{it['date']}.json" for it in items
         if (BRIEFINGS / f"{it['date']}.json").exists()),
        None,
    )
    return latest, items


def _read_brief(path: Path | None) -> dict | None:
    if not path or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def collect_state() -> dict:
    cfg = configstore.load(CONFIG)
    latest, history = _scan_briefings()
    brief = _read_brief(latest)

    store = Store(DB_PATH)
    try:
        runs = store.runs(60)
        alerts = store.alerts_recent(300)
        plans = store.plans_recent(200)
        stats = store.plan_stats()
        counts = store.alert_counts()
    finally:
        store.close()

    return {
        "config": cfg,
        "brief": brief,
        "history": history,
        "runs": runs,
        "alerts": alerts,
        "plans": plans,
        "stats": stats,
        "alert_counts": counts,
        "meta": {
            "root": str(ROOT),
            "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "config_path": str(CONFIG),
        },
    }


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "TradingAgentUI/1.0"

    # ---------- 基础 ----------
    def log_message(self, fmt: str, *args) -> None:  # 静音默认访问日志
        return

    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(
            json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
            "application/json; charset=utf-8",
            code,
        )

    def _err(self, msg: str, code: int = 400, extra: dict | None = None) -> None:
        payload = {"ok": False, "error": msg}
        if extra:
            payload.update(extra)
        self._json(payload, code)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"请求体不是合法 JSON：{exc}") from exc

    def _same_origin(self) -> bool:
        """挡住别的网页偷偷 POST 到本地端口（CSRF）。"""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host") or ""
        return origin.endswith(host) or "127.0.0.1" in origin or "localhost" in origin

    # ---------- GET ----------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/api/"):
                return self._api_get(path, parse_qs(parsed.query))
            if path.startswith("/briefings/"):
                return self._briefing_file(path.rsplit("/", 1)[-1])
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            return self._static(path.lstrip("/") or "index.html")
        except Exception as exc:  # noqa: BLE001
            self._err(f"服务器错误：{type(exc).__name__}: {exc}", 500)

    def _api_get(self, path: str, qs: dict) -> None:
        if path == "/api/state":
            return self._json({"ok": True, "data": collect_state()})
        if path == "/api/run/status":
            return self._json({"ok": True, "data": RUNNER.status()})
        if path == "/api/alerts":
            limit = int((qs.get("limit") or ["200"])[0])
            level = (qs.get("level") or [None])[0]
            day = (qs.get("date") or [None])[0]
            store = Store(DB_PATH)
            try:
                rows = store.alerts_recent(limit, level, day)
            finally:
                store.close()
            return self._json({"ok": True, "data": rows})
        if path == "/api/config":
            return self._json({"ok": True, "data": configstore.load(CONFIG)})
        return self._err("未知接口", 404)

    def _briefing_file(self, name: str) -> None:
        if not BRIEF_FILE_RE.match(name):
            return self._err("文件名不合法", 400)
        p = BRIEFINGS / name
        if not p.exists():
            return self._err("简报不存在", 404)
        self._send(p.read_bytes(), "text/html; charset=utf-8")

    def _static(self, rel: str) -> None:
        rel = rel.split("?")[0]
        target = (WEB / rel).resolve()
        try:
            target.relative_to(WEB.resolve())
        except ValueError:
            return self._err("路径越界", 403)
        if target.is_dir():
            target = target / "index.html"
        if not target.exists():
            return self._err(f"文件不存在：{rel}", 404)
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(target.read_bytes(), ctype)

    # ---------- POST ----------
    def do_POST(self) -> None:  # noqa: N802
        if not self._same_origin():
            return self._err("拒绝跨站请求", 403)
        path = urlparse(self.path).path
        try:
            body = self._body()
        except ValueError as exc:
            return self._err(str(exc))

        try:
            if path == "/api/config":
                return self._save_config(body)
            if path == "/api/run":
                return self._start_run(body)
            if path == "/api/notify/test":
                return self._test_notify()
            if path == "/api/reveal":
                return self._reveal(body)
        except configstore.ConfigError as exc:
            return self._err("配置校验未通过", 400, {"errors": exc.errors})
        except Exception as exc:  # noqa: BLE001
            return self._err(f"服务器错误：{type(exc).__name__}: {exc}", 500)
        return self._err("未知接口", 404)

    def _save_config(self, body: dict) -> None:
        current = configstore.load(CONFIG)
        patch = body.get("patch") if isinstance(body.get("patch"), dict) else body.get("config")
        if not isinstance(patch, dict):
            return self._err("缺少 patch 字段")
        merged = _deep_merge(current, patch)
        result = configstore.save(CONFIG, merged, BACKUP)
        self._json({"ok": True, "data": {"backup": result["backup"]}})

    def _start_run(self, body: dict) -> None:
        notify_enabled = bool(body.get("notify", True))
        if not RUNNER.start(notify_enabled):
            return self._err("已有任务在运行，请稍候", 409)
        self._json({"ok": True, "data": {"started": True}}, 202)

    def _test_notify(self) -> None:
        cfg = configstore.load(CONFIG)
        n = cfg.get("notify") or {}
        notify.macos_notify(n.get("title", "交易 Agent"), "通知测试成功：这是本地提醒。", n.get("sound"))
        self._json({"ok": True, "data": {"sent": True}})

    def _reveal(self, body: dict) -> None:
        target = str(body.get("target") or "data")
        mapping = {"data": DATA, "briefings": BRIEFINGS, "config": CONFIG.parent}
        path = mapping.get(target, DATA)
        subprocess.run(["open", str(path)], check=False)
        self._json({"ok": True, "data": {"path": str(path)}})


def main() -> int:
    ap = argparse.ArgumentParser(description="交易 Agent 本地控制台")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"交易 Agent 控制台已启动：{url}")
    print("按 Ctrl+C 停止。")
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
