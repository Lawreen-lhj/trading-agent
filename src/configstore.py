"""config.yaml 的读写与校验 —— Web UI 的唯一写入口。

为什么不让前端直接写 YAML：一个 capital=0 或 min_rr=0 会让后面所有仓位计算
出 NaN 或无穷大，而界面上不会有任何报错，只会安静地给出错误建议。
所以写入前必须逐字段校验。

两个约定：
1. 覆盖前把旧文件备份到 data/backup/，因为 yaml 重写会丢掉注释。
2. 临时文件 + os.replace 原子替换，避免写到一半中断留下半个配置文件。
"""
from __future__ import annotations

import os
import re
import shutil
from datetime import datetime
from pathlib import Path

import yaml

CODE_RE = re.compile(r"^\d{6}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

HEADER = """# 交易 Agent 配置 —— 由 Web 控制台管理（python server.py）
# 手动编辑同样生效；注意界面保存时会重写本文件（旧版本备份在 data/backup/）。

"""

NUM_RANGES = {
    # key: (min, max, 中文名)
    "capital": (1000, 1e9, "总资金"),
    "single_risk_pct": (0.1, 10, "单笔风险占比"),
    "max_hold_days": (1, 60, "最长持有天数"),
    "atr_stop_mult": (0.5, 5, "ATR 止损倍数"),
    "min_rr": (0.5, 10, "最低盈亏比"),
    "min_score": (0, 100, "最低分数线"),
}


class ConfigError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("；".join(errors))


def load(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def dump_text(cfg: dict) -> str:
    body = yaml.safe_dump(
        cfg, allow_unicode=True, sort_keys=False, default_flow_style=False, width=100
    )
    return HEADER + body


def backup(path: str | Path, backup_dir: str | Path) -> str | None:
    src = Path(path)
    if not src.exists():
        return None
    dst_dir = Path(backup_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"config-{datetime.now():%Y%m%d-%H%M%S}.yaml"
    shutil.copy2(src, dst)
    # 只留最近 30 份，避免无限堆积
    olds = sorted(dst_dir.glob("config-*.yaml"))
    for old in olds[:-30]:
        old.unlink(missing_ok=True)
    return str(dst)


def _num(value, label: str, lo: float, hi: float, errors: list[str]) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        errors.append(f"{label}必须是数字")
        return None
    if v < lo or v > hi:
        errors.append(f"{label}需在 {lo:g} ~ {hi:g} 之间（当前 {v:g}）")
    return v


def _clean_watchlist(items, errors: list[str]) -> list[dict]:
    if not isinstance(items, list):
        errors.append("自选池必须是列表")
        return []
    out, seen = [], set()
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict):
            errors.append(f"自选池第 {i} 行格式错误")
            continue
        code = str(it.get("code", "")).strip().zfill(6)
        name = str(it.get("name", "")).strip()
        tag = str(it.get("tag", "") or "").strip()
        if not CODE_RE.match(code):
            errors.append(f"自选池第 {i} 行代码「{it.get('code')}」不是 6 位数字")
            continue
        if not name:
            # 名称留空会让简报变成只有代码，读起来很费劲，直接补成代码
            name = code
        if code in seen:
            continue
        seen.add(code)
        out.append({"code": code, "name": name[:20], "tag": tag[:12]})
    if not out:
        errors.append("自选池不能为空，至少保留 1 只")
    if len(out) > 60:
        errors.append("自选池最多 60 只，再多看不过来")
    return out


def _clean_holdings(items, errors: list[str]) -> list[dict]:
    if items in (None, ""):
        return []
    if not isinstance(items, list):
        errors.append("持仓必须是列表")
        return []
    out, seen = [], set()
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict):
            errors.append(f"持仓第 {i} 行格式错误")
            continue
        code = str(it.get("code", "")).strip().zfill(6)
        if not CODE_RE.match(code):
            errors.append(f"持仓第 {i} 行代码「{it.get('code')}」不是 6 位数字")
            continue
        if code in seen:
            errors.append(f"持仓里 {code} 重复了")
            continue
        seen.add(code)
        qty = _num(it.get("qty"), f"持仓 {code} 的数量", 1, 1e8, errors)
        cost = _num(it.get("cost"), f"持仓 {code} 的成本", 0.01, 1e6, errors)
        if qty is None or cost is None:
            continue
        opened = str(it.get("opened") or "").strip()
        if opened and not DATE_RE.match(opened):
            errors.append(f"持仓 {code} 的开仓日期应形如 2026-10-24")
            opened = ""
        row = {"code": code, "qty": int(qty) if qty == int(qty) else qty,
               "cost": round(cost, 3)}
        if opened:
            row["opened"] = opened
        if it.get("name"):
            row["name"] = str(it["name"]).strip()[:20]
        out.append(row)
    return out


def normalize(cfg: dict) -> dict:
    """校验并返回一份干净的配置。有问题抛 ConfigError，不静默修正关键字段。"""
    errors: list[str] = []
    cfg = dict(cfg or {})

    meta = dict(cfg.get("meta") or {})
    style = meta.get("style", "short")
    if style not in ("short", "swing"):
        errors.append("风格只能是 short（短线）或 swing（波段）")
    meta["style"] = style if style in ("short", "swing") else "short"
    meta.setdefault("name", "A股短线简报")
    meta.setdefault("timezone", "Asia/Shanghai")
    meta.setdefault("source_primary", "东方财富 push2 接口")
    meta.setdefault("source_secondary", "腾讯行情（备源）")
    cfg["meta"] = meta

    cfg["watchlist"] = _clean_watchlist(cfg.get("watchlist"), errors)
    cfg["holdings"] = _clean_holdings(cfg.get("holdings"), errors)

    risk_in = dict(cfg.get("risk") or {})
    risk = {}
    for key, (lo, hi, label) in NUM_RANGES.items():
        default = {"capital": 100000, "single_risk_pct": 1.0, "max_hold_days": 5,
                   "atr_stop_mult": 1.5, "min_rr": 1.8, "min_score": 48}[key]
        v = _num(risk_in.get(key, default), label, lo, hi, errors)
        if v is not None and key in ("capital", "max_hold_days", "min_score") and v == int(v):
            v = int(v)  # 这些是整数语义，别写成 120000.0
        risk[key] = v if v is not None else default
    cfg["risk"] = risk

    th_in = dict(cfg.get("thresholds") or {})
    th = {}
    for key, val in th_in.items():
        try:
            th[key] = float(val)
        except (TypeError, ValueError):
            errors.append(f"阈值 {key} 必须是数字")
    cfg["thresholds"] = th or th_in

    n_in = dict(cfg.get("notify") or {})
    cfg["notify"] = {
        "macos": bool(n_in.get("macos", True)),
        "title": str(n_in.get("title") or "A股短线简报")[:40],
        "sound": str(n_in.get("sound") or "Glass"),
        "only_on_p0": bool(n_in.get("only_on_p0", False)),
    }

    if "global_watch" not in cfg:
        cfg["global_watch"] = {
            "us_indices": [{"symbol": ".IXIC", "name": "纳斯达克"},
                           {"symbol": ".INX", "name": "标普500"},
                           {"symbol": ".DJI", "name": "道琼斯"}],
            "hk_indices": ["HSI", "HSTECH"],
            "crypto": ["BTC", "ETH"],
        }

    if errors:
        raise ConfigError(errors)
    return cfg


def save(path: str | Path, cfg: dict, backup_dir: str | Path) -> dict:
    clean = normalize(cfg)
    p = Path(path)
    bak = backup(p, backup_dir)
    tmp = p.with_suffix(".yaml.tmp")
    tmp.write_text(dump_text(clean), encoding="utf-8")
    os.replace(tmp, p)
    return {"path": str(p), "backup": bak, "config": clean}
