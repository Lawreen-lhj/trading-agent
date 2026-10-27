"""本地存储 —— SQLite 存业务状态，够轻但能支撑后续复盘。

为什么第一天就要落库：不存历史，三个月后你无法回答"这套规则到底有没有用"。
"""
from __future__ import annotations

import json
import sqlite3
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

    def close(self) -> None:
        self.conn.close()
