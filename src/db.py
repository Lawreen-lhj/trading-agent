"""本地存储 —— SQLite 存业务状态，够轻但能支撑后续复盘。

为什么第一天就要落库：不存历史，三个月后你无法回答"这套规则到底有没有用"。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_date      TEXT PRIMARY KEY,
    generated_at  TEXT NOT NULL,
    state         TEXT,
    up_count      INTEGER,
    down_count    INTEGER,
    total_amount  REAL,
    status        TEXT
);

CREATE TABLE IF NOT EXISTS watch_rows (
    run_date   TEXT NOT NULL,
    code       TEXT NOT NULL,
    name       TEXT,
    close      REAL,
    pct_chg    REAL,
    score      INTEGER,
    label      TEXT,
    snap_json  TEXT,
    PRIMARY KEY (run_date, code)
);

CREATE TABLE IF NOT EXISTS alerts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date   TEXT NOT NULL,
    code       TEXT,
    name       TEXT,
    level      TEXT,
    rule       TEXT,
    message    TEXT,
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_alerts_date ON alerts(run_date);

-- 交易计划必须落库：短线唯一能验证"这套规则有没有用"的办法，
-- 就是把每天的买点/止损/目标存档，事后核对能不能成交、有没有被打掉。
CREATE TABLE IF NOT EXISTS plans (
    run_date   TEXT NOT NULL,
    code       TEXT NOT NULL,
    name       TEXT,
    ready      INTEGER,
    score      INTEGER,
    buy_lo     REAL,
    buy_hi     REAL,
    stop       REAL,
    target1    REAL,
    target2    REAL,
    rr         REAL,
    risk_pct   REAL,
    shares     INTEGER,
    amount     REAL,
    why        TEXT,
    PRIMARY KEY (run_date, code)
);

-- 多策略信号记录：每天各策略扫描产出的信号全部落库，
-- 事后可以回溯"为什么当时给了这个信号"，也可以统计各策略的信号质量。
CREATE TABLE IF NOT EXISTS strategy_signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date    TEXT NOT NULL,
    strategy    TEXT NOT NULL,
    code        TEXT NOT NULL,
    name        TEXT,
    direction   TEXT,
    strength    REAL,
    price       REAL,
    buy_lo      REAL,
    buy_hi      REAL,
    stop        REAL,
    target1     REAL,
    target2     REAL,
    rr          REAL,
    shares      INTEGER,
    hold_days   INTEGER,
    confidence  REAL,
    market_phase TEXT,
    reason      TEXT,
    extra_json  TEXT,
    created_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_signals_date ON strategy_signals(run_date);
CREATE INDEX IF NOT EXISTS idx_signals_strategy ON strategy_signals(strategy, run_date);
CREATE INDEX IF NOT EXISTS idx_signals_code ON strategy_signals(code, run_date);

-- 每日情绪快照：跟踪情绪周期变化，用于复盘"情绪判断准不准"。
CREATE TABLE IF NOT EXISTS sentiment_history (
    run_date        TEXT PRIMARY KEY,
    score           REAL,
    phase           TEXT,
    limit_up        INTEGER,
    limit_down      INTEGER,
    up_ratio        REAL,
    median_pct      REAL,
    board_height    INTEGER,
    yesterday_perf  REAL,
    total_amount    REAL,
    detail_json     TEXT,
    created_at      TEXT
);

-- 选股漏斗结果：三层筛选的产出，记录哪些票通过了哪些层。
CREATE TABLE IF NOT EXISTS screening_results (
    run_date    TEXT NOT NULL,
    code        TEXT NOT NULL,
    name        TEXT,
    layer1_pass INTEGER,
    layer2_pass INTEGER,
    layer3_pass INTEGER,
    patterns    TEXT,
    score       REAL,
    rank        INTEGER,
    detail_json TEXT,
    PRIMARY KEY (run_date, code)
);

CREATE INDEX IF NOT EXISTS idx_screening_date ON screening_results(run_date);

-- 复盘记录：四维复盘的结构化存储。
CREATE TABLE IF NOT EXISTS reviews (
    run_date        TEXT PRIMARY KEY,
    overall_score   REAL,
    market_json     TEXT,
    stocks_json     TEXT,
    strategies_json TEXT,
    behavior_json   TEXT,
    insights_json   TEXT,
    actions_json    TEXT,
    created_at      TEXT
);

-- 盘中监控告警持久化（与 alerts 表不同，那个是简报产出的提醒，这个是盘中实时的）。
CREATE TABLE IF NOT EXISTS monitor_alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id    TEXT UNIQUE,
    run_date    TEXT NOT NULL,
    level       TEXT,
    category    TEXT,
    rule        TEXT,
    code        TEXT,
    name        TEXT,
    message     TEXT,
    data_json   TEXT,
    notified    INTEGER DEFAULT 0,
    created_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_monitor_alerts_date ON monitor_alerts(run_date);
CREATE INDEX IF NOT EXISTS idx_monitor_alerts_level ON monitor_alerts(level, run_date);

-- 资金脉搏盘中快照：每次 /api/pulse 采集成功落一行，供日内趋势 sparkline。
CREATE TABLE IF NOT EXISTS pulse_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date       TEXT NOT NULL,
    ts             TEXT NOT NULL,
    north_net      REAL,
    zt_count       INTEGER,
    broken_count   INTEGER,
    ld_count       INTEGER,
    top_sector     TEXT,
    top_sector_net REAL,
    created_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_pulse_date ON pulse_snapshots(run_date);

-- 系统活动流：run_sim_live / 采集器每轮 append，WebUI 实时展示"系统在干什么"。
CREATE TABLE IF NOT EXISTS activity_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date    TEXT NOT NULL,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    strategy    TEXT,
    message     TEXT,
    detail_json TEXT,
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_activity_date ON activity_log(run_date, id);
"""


class Store:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def save_run(self, brief: dict) -> None:
        m = brief["market"]
        b = m.get("breadth") or {}
        self.conn.execute(
            "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?)",
            (
                brief["date"],
                brief["generated_at"],
                m.get("state", {}).get("state"),
                b.get("up"),
                b.get("down"),
                b.get("total_amount"),
                "ok" if len(b) else "degraded",
            ),
        )
        for row in brief["watch"]:
            snap = row.get("snap") or {}
            self.conn.execute(
                "INSERT OR REPLACE INTO watch_rows VALUES (?,?,?,?,?,?,?,?)",
                (
                    brief["date"],
                    row["code"],
                    row["name"],
                    snap.get("close"),
                    snap.get("pct_chg"),
                    row.get("score"),
                    row.get("label"),
                    json.dumps(snap, ensure_ascii=False),
                ),
            )
        self.conn.execute("DELETE FROM alerts WHERE run_date = ?", (brief["date"],))
        for a in brief["alerts_all"]:
            self.conn.execute(
                "INSERT INTO alerts (run_date, code, name, level, rule, message, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    brief["date"],
                    a.get("code"),
                    a.get("name"),
                    a["level"],
                    a["rule"],
                    a["message"],
                    brief["generated_at"],
                ),
            )
        self.conn.execute("DELETE FROM plans WHERE run_date = ?", (brief["date"],))
        for row in brief["watch"]:
            p = row.get("plan")
            if not p:
                continue
            self.conn.execute(
                "INSERT OR REPLACE INTO plans (run_date, code, name, ready, score,"
                " buy_lo, buy_hi, stop, target1, target2, rr, risk_pct, shares, amount, why)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    brief["date"], row["code"], row["name"],
                    1 if p.get("ready") else 0, row.get("score"),
                    p.get("buy_lo"), p.get("buy_hi"), p.get("stop"),
                    p.get("target1"), p.get("target2"), p.get("rr"),
                    p.get("risk_pct"), p.get("shares"), p.get("amount"),
                    p.get("why"),
                ),
            )
        self.conn.commit()

    def history(self, code: str, limit: int = 30) -> list[dict]:
        cur = self.conn.execute(
            "SELECT run_date, close, pct_chg, score, label FROM watch_rows"
            " WHERE code = ? ORDER BY run_date DESC LIMIT ?",
            (code, limit),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ---------------- 以下供 Web UI 读取 ----------------
    def runs(self, limit: int = 90) -> list[dict]:
        cur = self.conn.execute(
            "SELECT run_date, generated_at, state, up_count, down_count,"
            " total_amount, status FROM runs ORDER BY run_date DESC LIMIT ?",
            (limit,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def alert_counts(self) -> dict:
        row = self.conn.execute(
            "SELECT"
            " SUM(CASE WHEN level='P0' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN level='P1' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN level='P2' THEN 1 ELSE 0 END)"
            " FROM alerts"
        ).fetchone()
        recent = self.conn.execute(
            "SELECT run_date,"
            " SUM(CASE WHEN level='P0' THEN 1 ELSE 0 END) FROM alerts"
            " GROUP BY run_date ORDER BY run_date DESC LIMIT 1"
        ).fetchone()
        return {
            "p0": int(row[0] or 0),
            "p1": int(row[1] or 0),
            "p2": int(row[2] or 0),
            "last_date": recent[0] if recent else None,
            "last_p0": int(recent[1] or 0) if recent else 0,
        }

    def alerts_recent(
        self, limit: int = 300, level: str | None = None, run_date: str | None = None
    ) -> list[dict]:
        sql = (
            "SELECT run_date, code, name, level, rule, message, created_at"
            " FROM alerts WHERE 1=1"
        )
        args: list = []
        if level:
            sql += " AND level = ?"
            args.append(level)
        if run_date:
            sql += " AND run_date = ?"
            args.append(run_date)
        sql += (
            " ORDER BY run_date DESC,"
            " CASE level WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 ELSE 2 END, id DESC LIMIT ?"
        )
        args.append(limit)
        cur = self.conn.execute(sql, args)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def plans_recent(self, limit: int = 200) -> list[dict]:
        cur = self.conn.execute(
            "SELECT run_date, code, name, ready, score, buy_lo, buy_hi, stop,"
            " target1, target2, rr, risk_pct, shares, amount, why FROM plans"
            " ORDER BY run_date DESC, ready DESC, score DESC LIMIT ?",
            (limit,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def plan_stats(self) -> dict:
        """计划的历史统计：一共给了多少可执行计划，平均盈亏比多少。

        不做收益归因（那需要次日真实走势），只回答「这套规则出手频率如何」。
        """
        row = self.conn.execute(
            "SELECT COUNT(*), SUM(ready), AVG(CASE WHEN ready=1 THEN rr END),"
            " AVG(CASE WHEN ready=1 THEN risk_pct END) FROM plans"
        ).fetchone()
        days = self.conn.execute("SELECT COUNT(DISTINCT run_date) FROM plans").fetchone()
        return {
            "total_rows": int(row[0] or 0),
            "ready_rows": int(row[1] or 0),
            "avg_rr": round(row[2], 2) if row[2] is not None else None,
            "avg_risk_pct": round(row[3], 2) if row[3] is not None else None,
            "days": int(days[0] or 0),
        }

    # ---------------- 策略信号 ----------------
    def save_signals(self, run_date: str, signals: list[dict]) -> None:
        """保存多策略扫描产出的信号。"""
        self.conn.execute(
            "DELETE FROM strategy_signals WHERE run_date = ?", (run_date,)
        )
        for s in signals:
            self.conn.execute(
                "INSERT INTO strategy_signals"
                " (run_date, strategy, code, name, direction, strength, price,"
                "  buy_lo, buy_hi, stop, target1, target2, rr, shares, hold_days,"
                "  confidence, market_phase, reason, extra_json, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_date, s.get("strategy"), s.get("code"), s.get("name"),
                    s.get("direction"), s.get("strength"), s.get("price"),
                    s.get("buy_lo"), s.get("buy_hi"), s.get("stop"),
                    s.get("target1"), s.get("target2"), s.get("rr"),
                    s.get("shares"), s.get("hold_days"), s.get("confidence"),
                    s.get("market_phase"), s.get("reason"),
                    json.dumps(s.get("extra") or {}, ensure_ascii=False),
                    s.get("created_at", run_date),
                ),
            )
        self.conn.commit()

    def signals_by_date(self, run_date: str, strategy: str = None) -> list[dict]:
        sql = (
            "SELECT strategy, code, name, direction, strength, price,"
            " buy_lo, buy_hi, stop, target1, target2, rr, shares, hold_days,"
            " confidence, market_phase, reason, extra_json, created_at"
            " FROM strategy_signals WHERE run_date = ?"
        )
        args: list = [run_date]
        if strategy:
            sql += " AND strategy = ?"
            args.append(strategy)
        sql += " ORDER BY strength DESC"
        cur = self.conn.execute(sql, args)
        cols = [d[0] for d in cur.description]
        rows = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["extra"] = json.loads(d.pop("extra_json") or "{}")
            rows.append(d)
        return rows

    def signal_stats(self, strategy: str = None, days: int = 30) -> dict:
        """策略信号统计：最近 N 天各策略的信号数量与平均强度。"""
        sql = (
            "SELECT strategy, COUNT(*), AVG(strength), AVG(confidence)"
            " FROM strategy_signals"
            " WHERE run_date >= date('now', ?)"
        )
        args: list = [f"-{days} days"]
        if strategy:
            sql += " AND strategy = ?"
            args.append(strategy)
        sql += " GROUP BY strategy"
        cur = self.conn.execute(sql, args)
        return {
            r[0]: {"count": r[1], "avg_strength": round(r[2], 3), "avg_confidence": round(r[3], 3)}
            for r in cur.fetchall()
        }

    # ---------------- 情绪历史 ----------------
    def save_sentiment(self, run_date: str, data: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO sentiment_history VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_date, data.get("score"), data.get("phase"),
                data.get("limit_up"), data.get("limit_down"),
                data.get("up_ratio"), data.get("median_pct"),
                data.get("board_height"), data.get("yesterday_perf"),
                data.get("total_amount"),
                json.dumps(data, ensure_ascii=False),
                data.get("created_at", run_date),
            ),
        )
        self.conn.commit()

    def sentiment_history(self, limit: int = 60) -> list[dict]:
        cur = self.conn.execute(
            "SELECT run_date, score, phase, limit_up, limit_down, up_ratio,"
            " median_pct, board_height, yesterday_perf, total_amount"
            " FROM sentiment_history ORDER BY run_date DESC LIMIT ?",
            (limit,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def sentiment_latest(self) -> dict | None:
        rows = self.sentiment_history(1)
        return rows[0] if rows else None

    # ---------------- 选股漏斗 ----------------
    def save_screening(self, run_date: str, results: list[dict]) -> None:
        self.conn.execute(
            "DELETE FROM screening_results WHERE run_date = ?", (run_date,)
        )
        for i, r in enumerate(results):
            self.conn.execute(
                "INSERT INTO screening_results VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    run_date, r.get("code"), r.get("name"),
                    1 if r.get("layer1_pass") else 0,
                    1 if r.get("layer2_pass") else 0,
                    1 if r.get("layer3_pass") else 0,
                    ",".join(r.get("patterns") or []),
                    r.get("score"), i + 1,
                    json.dumps(r, ensure_ascii=False),
                ),
            )
        self.conn.commit()

    def screening_by_date(self, run_date: str, passed_only: bool = True) -> list[dict]:
        sql = "SELECT code, name, layer1_pass, layer2_pass, layer3_pass, patterns, score, rank FROM screening_results WHERE run_date = ?"
        if passed_only:
            sql += " AND layer3_pass = 1"
        sql += " ORDER BY rank"
        cur = self.conn.execute(sql, (run_date,))
        cols = [d[0] for d in cur.description]
        rows = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["patterns"] = d["patterns"].split(",") if d["patterns"] else []
            rows.append(d)
        return rows

    def screening_with_detail(self, run_date: str) -> list[dict]:
        """选股结果（含 detail_json 解析出的理由/计划），供 Web 选股页展示。

        与 screening_by_date 的区别：把 detail_json 整个解出来合并进每条记录，
        这样前端能拿到 build_reasons() 的逐条理由和交易计划，而不只是分数。
        """
        cur = self.conn.execute(
            "SELECT rank, code, name, score, patterns, detail_json "
            "FROM screening_results WHERE run_date = ? ORDER BY rank",
            (run_date,),
        )
        rows = []
        for rank, code, name, score, patterns, detail in cur.fetchall():
            d = json.loads(detail) if detail else {}
            d.setdefault("rank", rank)
            d.setdefault("code", code)
            d.setdefault("name", name)
            d.setdefault("score", score)
            d.setdefault("patterns", patterns.split(",") if patterns else [])
            rows.append(d)
        return rows

    def screening_dates(self, limit: int = 30) -> list[str]:
        """有选股记录的日期（新→旧），供 Web 选股页做日期切换。"""
        cur = self.conn.execute(
            "SELECT DISTINCT run_date FROM screening_results ORDER BY run_date DESC LIMIT ?",
            (limit,),
        )
        return [r[0] for r in cur.fetchall()]

    # ---------------- 复盘 ----------------
    def save_review(self, review_data: dict) -> None:
        """保存四维复盘结果。review_data 是 DailyReview 的 asdict() 产出。"""
        self.conn.execute(
            "INSERT OR REPLACE INTO reviews VALUES (?,?,?,?,?,?,?,?,?)",
            (
                review_data["date"],
                review_data.get("overall_score"),
                json.dumps(review_data.get("market") or {}, ensure_ascii=False),
                json.dumps(review_data.get("stocks") or [], ensure_ascii=False),
                json.dumps(review_data.get("strategies") or [], ensure_ascii=False),
                json.dumps(review_data.get("behavior") or {}, ensure_ascii=False),
                json.dumps(review_data.get("key_insights") or [], ensure_ascii=False),
                json.dumps(review_data.get("action_items") or [], ensure_ascii=False),
                review_data.get("created_at", review_data["date"]),
            ),
        )
        self.conn.commit()

    def review_by_date(self, run_date: str) -> dict | None:
        cur = self.conn.execute(
            "SELECT run_date, overall_score, market_json, stocks_json,"
            " strategies_json, behavior_json, insights_json, actions_json, created_at"
            " FROM reviews WHERE run_date = ?",
            (run_date,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "date": row[0],
            "overall_score": row[1],
            "market": json.loads(row[2] or "{}"),
            "stocks": json.loads(row[3] or "[]"),
            "strategies": json.loads(row[4] or "[]"),
            "behavior": json.loads(row[5] or "{}"),
            "key_insights": json.loads(row[6] or "[]"),
            "action_items": json.loads(row[7] or "[]"),
            "created_at": row[8],
        }

    def reviews_recent(self, limit: int = 30) -> list[dict]:
        cur = self.conn.execute(
            "SELECT run_date, overall_score, created_at FROM reviews"
            " ORDER BY run_date DESC LIMIT ?",
            (limit,),
        )
        return [{"date": r[0], "overall_score": r[1], "created_at": r[2]} for r in cur.fetchall()]

    # ---------------- 盘中监控告警 ----------------
    def save_monitor_alerts(self, run_date: str, alerts: list[dict]) -> None:
        """批量保存监控告警（增量，不删旧的）。"""
        for a in alerts:
            self.conn.execute(
                "INSERT OR IGNORE INTO monitor_alerts"
                " (alert_id, run_date, level, category, rule, code, name,"
                "  message, data_json, notified, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    a.get("alert_id"), run_date, a.get("level"),
                    a.get("category"), a.get("rule"), a.get("code"),
                    a.get("name"), a.get("message"),
                    json.dumps(a.get("data") or {}, ensure_ascii=False),
                    1 if a.get("notified") else 0,
                    a.get("created_at"),
                ),
            )
        self.conn.commit()

    def monitor_alerts_recent(
        self, limit: int = 200, level: str = None, run_date: str = None
    ) -> list[dict]:
        sql = (
            "SELECT alert_id, run_date, level, category, rule, code, name,"
            " message, data_json, notified, created_at"
            " FROM monitor_alerts WHERE 1=1"
        )
        args: list = []
        if level:
            sql += " AND level = ?"
            args.append(level)
        if run_date:
            sql += " AND run_date = ?"
            args.append(run_date)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        cur = self.conn.execute(sql, args)
        cols = [d[0] for d in cur.description]
        rows = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["data"] = json.loads(d.pop("data_json") or "{}")
            d["notified"] = bool(d["notified"])
            rows.append(d)
        return rows

    def monitor_alert_stats(self, days: int = 7) -> dict:
        """最近 N 天的监控告警统计。"""
        cur = self.conn.execute(
            "SELECT level, COUNT(*) FROM monitor_alerts"
            " WHERE run_date >= date('now', ?) GROUP BY level",
            (f"-{days} days",),
        )
        stats = {r[0]: r[1] for r in cur.fetchall()}
        return {"p0": stats.get("P0", 0), "p1": stats.get("P1", 0), "p2": stats.get("P2", 0)}

    # ── 资金脉搏快照 ────────────────────────────────────────────────
    def save_pulse_snapshot(self, run_date: str, ts: str, snap: dict) -> None:
        self.conn.execute(
            "INSERT INTO pulse_snapshots"
            " (run_date, ts, north_net, zt_count, broken_count, ld_count,"
            "  top_sector, top_sector_net, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                run_date, ts,
                snap.get("north_net"), snap.get("zt_count"),
                snap.get("broken_count"), snap.get("ld_count"),
                snap.get("top_sector"), snap.get("top_sector_net"),
                datetime.now().isoformat(),
            ),
        )
        self.conn.commit()

    def pulse_snapshots_today(self, run_date: str, limit: int = 120) -> list[dict]:
        cur = self.conn.execute(
            "SELECT ts, north_net, zt_count, broken_count, ld_count,"
            " top_sector, top_sector_net FROM pulse_snapshots"
            " WHERE run_date=? ORDER BY id DESC LIMIT ?",
            (run_date, limit),
        )
        rows = [
            {
                "ts": r[0], "north_net": r[1], "zt_count": r[2],
                "broken_count": r[3], "ld_count": r[4],
                "top_sector": r[5], "top_sector_net": r[6],
            }
            for r in cur.fetchall()
        ]
        rows.reverse()
        return rows

    # ── 系统活动流 ──────────────────────────────────────────────────
    def log_activity(self, run_date: str, ts: str, kind: str,
                     message: str, strategy: str = None, detail: dict = None) -> None:
        self.conn.execute(
            "INSERT INTO activity_log"
            " (run_date, ts, kind, strategy, message, detail_json, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                run_date, ts, kind, strategy, message,
                json.dumps(detail, ensure_ascii=False) if detail else None,
                datetime.now().isoformat(),
            ),
        )
        self.conn.commit()

    def activity_recent(self, run_date: str = None, limit: int = 60) -> list[dict]:
        if run_date:
            cur = self.conn.execute(
                "SELECT run_date, ts, kind, strategy, message, detail_json"
                " FROM activity_log WHERE run_date=? ORDER BY id DESC LIMIT ?",
                (run_date, limit),
            )
        else:
            cur = self.conn.execute(
                "SELECT run_date, ts, kind, strategy, message, detail_json"
                " FROM activity_log ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        rows = [
            {
                "date": r[0], "ts": r[1], "kind": r[2], "strategy": r[3],
                "message": r[4],
                "detail": json.loads(r[5]) if r[5] else None,
            }
            for r in cur.fetchall()
        ]
        rows.reverse()
        return rows

    def activity_day_stats(self, run_date: str) -> dict:
        """当日活动流聚合：全局扫描/补扫轮数 + 每策略 skip/buy/sell 计数。

        run_sim_live 每轮对每个策略最多落一条 skip（弃权原因）或若干 buy/sell，
        所以某策略的 skip 计数 ≈ 它当日"扫过但没出手"的轮数；scan/rescan 不带
        strategy（是整轮级别的），单独计全局轮数。WebUI 模拟交易页用它在策略卡片
        上直接回答"今天这个策略到底动没动、扫了几轮"。
        """
        cur = self.conn.execute(
            "SELECT kind, strategy, COUNT(*) FROM activity_log"
            " WHERE run_date=? GROUP BY kind, strategy",
            (run_date,),
        )
        stats = {"date": run_date, "scan_rounds": 0, "rescan_rounds": 0,
                 "snapshot": 0, "per_strategy": {}}
        for kind, strategy, n in cur.fetchall():
            if not strategy:
                if kind == "scan":
                    stats["scan_rounds"] += n
                elif kind == "rescan":
                    stats["rescan_rounds"] += n
                elif kind == "snapshot":
                    stats["snapshot"] += n
                continue
            rec = stats["per_strategy"].setdefault(
                strategy, {"skip": 0, "buy": 0, "sell": 0})
            if kind in rec:
                rec[kind] += n
        return stats

    def close(self) -> None:
        self.conn.close()
