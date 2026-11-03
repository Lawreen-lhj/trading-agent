"""模拟交易账户 —— A 股规则下的模拟资金管理。

设计原则：
  1. 严格模拟 A 股规则：T+1、涨跌停、100股整数倍、手续费、印花税、过户费
  2. 所有状态可持久化：SQLite 存储，重启不丢
  3. 不做任何真实下单：仅本地模拟，不接券商 API
  4. 支持多账户：可以并行测试多个策略

账户结构：
  - 现金（可用/冻结）
  - 持仓（可卖/待卖）
  - 交易历史
  - 每日净值曲线
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional


# ---------------- A 股交易费用 ----------------
FEES = {
    "commission_rate": 0.0003,    # 万三佣金（双边）
    "min_commission": 5.0,         # 最低 5 元
    "stamp_tax_rate": 0.0005,      # 千分之0.5 印花税（2023年8月减半，仅卖出）
    "transfer_fee_rate": 0.00001,  # 万0.1 过户费（双边，沪深都收）
    "slippage": 0.002,             # 0.2% 滑点（模拟真实成交价与预期价的偏差）
}


def calc_fees(amount: float, direction: str, config: dict = None) -> dict:
    """计算交易费用。

    amount: 成交金额
    direction: "buy" / "sell"

    返回：{"commission": x, "stamp_tax": y, "transfer_fee": z, "total": t}
    """
    cfg = {**FEES, **(config or {})}

    commission = max(amount * cfg["commission_rate"], cfg["min_commission"])
    transfer_fee = amount * cfg["transfer_fee_rate"]
    stamp_tax = amount * cfg["stamp_tax_rate"] if direction == "sell" else 0.0

    return {
        "commission": round(commission, 2),
        "stamp_tax": round(stamp_tax, 2),
        "transfer_fee": round(transfer_fee, 2),
        "total": round(commission + stamp_tax + transfer_fee, 2),
    }


# ---------------- 持仓 ----------------
@dataclass
class Position:
    """单个股票的持仓"""
    code: str
    name: str = ""
    quantity: int = 0             # 总持仓
    available: int = 0            # 可卖数量（T+1 后可用）
    frozen: int = 0               # 冻结数量（挂单中）
    cost_price: float = 0.0       # 成本价（含手续费）
    total_cost: float = 0.0       # 总成本
    open_date: str = ""           # 建仓日期
    last_update: str = ""         # 最后更新日期
    strategy: str = ""            # 建仓策略
    original_plan: dict = field(default_factory=dict)  # 原始计划

    # 实时数据（不持久化）
    current_price: float = 0.0
    market_value: float = 0.0
    profit_loss: float = 0.0
    profit_pct: float = 0.0

    def update_market(self, price: float):
        """更新市价"""
        self.current_price = price
        self.market_value = round(price * self.quantity, 2)
        if self.total_cost > 0:
            self.profit_loss = round(self.market_value - self.total_cost, 2)
            self.profit_pct = round((price / self.cost_price - 1) * 100, 2) if self.cost_price else 0

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------- 账户 ----------------
class Account:
    """模拟交易账户"""

    def __init__(
        self,
        account_id: str = "default",
        initial_capital: float = 100000.0,
        db_path: str | Path = None,
        fee_config: dict = None,
    ):
        self.account_id = account_id
        self.initial_capital = initial_capital
        self.cash = initial_capital          # 可用现金
        self.frozen_cash = 0.0               # 冻结现金（挂单中）
        self.positions: dict[str, Position] = {}
        self.fee_config = fee_config or {}
        self.db_path = Path(db_path) if db_path else None

        # 交易历史
        self.trade_history: list[dict] = []

        # 每日快照
        self.daily_snapshots: list[dict] = []

        if self.db_path:
            self._load_from_db()

    # ---------------- 账户属性 ----------------
    @property
    def total_assets(self) -> float:
        """总资产 = 现金 + 冻结现金 + 持仓市值"""
        position_value = sum(p.market_value for p in self.positions.values())
        return round(self.cash + self.frozen_cash + position_value, 2)

    @property
    def position_value(self) -> float:
        """持仓总市值"""
        return round(sum(p.market_value for p in self.positions.values()), 2)

    @property
    def position_ratio(self) -> float:
        """仓位比例"""
        total = self.total_assets
        return round(self.position_value / total, 4) if total > 0 else 0

    @property
    def total_pnl(self) -> float:
        """总盈亏（含已实现和未实现）"""
        return round(self.total_assets - self.initial_capital, 2)

    @property
    def total_return_pct(self) -> float:
        """总收益率"""
        if self.initial_capital <= 0:
            return 0
        return round((self.total_assets / self.initial_capital - 1) * 100, 2)

    # ---------------- 买入 ----------------
    def buy(
        self,
        code: str,
        name: str,
        price: float,
        quantity: int,
        trade_date: str = None,
        strategy: str = "",
        plan: dict = None,
        apply_slippage: bool = True,
    ) -> dict:
        """买入股票。

        返回：{"success": bool, "reason": str, "trade": {...}}
        """
        trade_date = trade_date or date.today().isoformat()

        # 1. 数量检查：必须是 100 的整数倍
        if quantity <= 0 or quantity % 100 != 0:
            return {"success": False, "reason": f"数量必须是100的整数倍，当前{quantity}"}

        # 2. 应用滑点
        actual_price = price
        if apply_slippage:
            slippage = self.fee_config.get("slippage", FEES["slippage"])
            actual_price = round(price * (1 + slippage), 2)

        # 3. 计算总金额和费用
        gross_amount = actual_price * quantity
        fees = calc_fees(gross_amount, "buy", self.fee_config)
        total_amount = gross_amount + fees["total"]

        # 4. 现金检查
        if self.cash < total_amount:
            return {
                "success": False,
                "reason": f"现金不足，需要{total_amount:.2f}，可用{self.cash:.2f}"
            }

        # 5. 扣款
        self.cash = round(self.cash - total_amount, 2)

        # 6. 更新持仓
        if code in self.positions:
            pos = self.positions[code]
            # 加仓：重新计算成本价
            new_quantity = pos.quantity + quantity
            new_cost = pos.total_cost + total_amount
            pos.cost_price = round(new_cost / new_quantity, 4)
            pos.quantity = new_quantity
            pos.total_cost = round(new_cost, 2)
            # T+1：新买入的部分不可卖
            pos.frozen += quantity
            pos.last_update = trade_date
        else:
            pos = Position(
                code=code,
                name=name,
                quantity=quantity,
                available=0,       # T+1 当日不可卖
                frozen=quantity,   # 冻结，次日解冻
                cost_price=round(total_amount / quantity, 4),
                total_cost=round(total_amount, 2),
                open_date=trade_date,
                last_update=trade_date,
                strategy=strategy,
                original_plan=plan or {},
                current_price=actual_price,
                market_value=round(actual_price * quantity, 2),
            )
            self.positions[code] = pos

        # 7. 记录交易
        trade = {
            "trade_id": f"{trade_date}_{code}_B_{len(self.trade_history)}",
            "account_id": self.account_id,
            "trade_date": trade_date,
            "trade_time": datetime.now().isoformat(),
            "code": code,
            "name": name,
            "direction": "buy",
            "price": actual_price,
            "orig_price": price,
            "quantity": quantity,
            "amount": round(gross_amount, 2),
            "fees": fees["total"],
            "total_amount": round(total_amount, 2),
            "strategy": strategy,
            "cash_after": self.cash,
        }
        self.trade_history.append(trade)

        if self.db_path:
            self._save_trade(trade)

        return {"success": True, "reason": "", "trade": trade}

    # ---------------- 卖出 ----------------
    def sell(
        self,
        code: str,
        price: float,
        quantity: int,
        trade_date: str = None,
        reason: str = "",
        apply_slippage: bool = True,
    ) -> dict:
        """卖出股票。

        返回：{"success": bool, "reason": str, "trade": {...}}
        """
        trade_date = trade_date or date.today().isoformat()

        # 1. 数量检查
        if quantity <= 0 or quantity % 100 != 0:
            return {"success": False, "reason": f"数量必须是100的整数倍，当前{quantity}"}

        # 2. 持仓检查
        pos = self.positions.get(code)
        if not pos:
            return {"success": False, "reason": f"未持有{code}"}

        # 3. T+1 检查：可卖数量
        if pos.available < quantity:
            return {
                "success": False,
                "reason": f"T+1限制，可卖{pos.available}，请求{quantity}"
            }

        # 4. 应用滑点（卖出滑点是负向）
        actual_price = price
        if apply_slippage:
            slippage = self.fee_config.get("slippage", FEES["slippage"])
            actual_price = round(price * (1 - slippage), 2)

        # 5. 计算金额和费用
        gross_amount = actual_price * quantity
        fees = calc_fees(gross_amount, "sell", self.fee_config)
        net_amount = gross_amount - fees["total"]

        # 6. 更新持仓
        pos.quantity -= quantity
        pos.available -= quantity
        pos.total_cost = round(pos.cost_price * pos.quantity, 2)
        pos.last_update = trade_date

        # 7. 计算实现盈亏
        cost_of_sold = pos.cost_price * quantity
        realized_pnl = round(net_amount - cost_of_sold, 2)
        realized_pct = round((net_amount / cost_of_sold - 1) * 100, 2) if cost_of_sold else 0

        # 8. 加钱
        self.cash = round(self.cash + net_amount, 2)

        # 9. 清空持仓（数量为0）
        if pos.quantity == 0:
            del self.positions[code]

        # 10. 记录交易
        trade = {
            "trade_id": f"{trade_date}_{code}_S_{len(self.trade_history)}",
            "account_id": self.account_id,
            "trade_date": trade_date,
            "trade_time": datetime.now().isoformat(),
            "code": code,
            "name": pos.name,
            "direction": "sell",
            "price": actual_price,
            "orig_price": price,
            "quantity": quantity,
            "amount": round(gross_amount, 2),
            "fees": fees["total"],
            "net_amount": round(net_amount, 2),
            "realized_pnl": realized_pnl,
            "realized_pct": realized_pct,
            "reason": reason,
            "strategy": pos.strategy,
            "hold_days": self._calc_hold_days(pos.open_date, trade_date),
            "cash_after": self.cash,
        }
        self.trade_history.append(trade)

        if self.db_path:
            self._save_trade(trade)

        return {"success": True, "reason": "", "trade": trade}

    def _calc_hold_days(self, open_date: str, close_date: str) -> int:
        """计算持仓交易日数（粗算：只数工作日）"""
        try:
            d0 = date.fromisoformat(open_date)
            d1 = date.fromisoformat(close_date)
        except (ValueError, TypeError):
            return 0
        if d1 <= d0:
            return 0
        n = 0
        cur = d0
        while cur < d1:
            cur = date.fromordinal(cur.toordinal() + 1)
            if cur.weekday() < 5:
                n += 1
        return n

    # ---------------- 日终处理 ----------------
    def on_new_day(self, trade_date: str = None):
        """新交易日开始时的处理：
        1. T+1 解冻（仅解冻「早于本交易日」买入的持仓；当日买入仍冻结）
        2. 更新持仓市价

        只解冻 open_date < trade_date 的持仓，避免同一天内重启进程时
        把当日刚买入、本应 T+1 冻结的股份错误放开。
        """
        trade_date = trade_date or date.today().isoformat()

        for pos in self.positions.values():
            opened_before = False
            if pos.open_date:
                try:
                    opened_before = date.fromisoformat(pos.open_date) < date.fromisoformat(trade_date)
                except (ValueError, TypeError):
                    opened_before = True  # 日期不可解析时保守解冻
            else:
                opened_before = True
            if opened_before and pos.frozen:
                pos.available += pos.frozen
                pos.frozen = 0

    def update_market_prices(self, prices: dict[str, float]):
        """更新所有持仓的市价"""
        for code, pos in self.positions.items():
            price = prices.get(code)
            if price:
                pos.update_market(price)

    def take_snapshot(self, snapshot_date: str = None):
        """生成每日快照"""
        snapshot_date = snapshot_date or date.today().isoformat()
        snapshot = {
            "date": snapshot_date,
            "account_id": self.account_id,
            "cash": self.cash,
            "frozen_cash": self.frozen_cash,
            "position_value": self.position_value,
            "total_assets": self.total_assets,
            "position_ratio": self.position_ratio,
            "total_pnl": self.total_pnl,
            "total_return_pct": self.total_return_pct,
            "position_count": len(self.positions),
            "positions": [pos.to_dict() for pos in self.positions.values()],
        }
        self.daily_snapshots.append(snapshot)

        if self.db_path:
            self._save_snapshot(snapshot)

        return snapshot

    # ---------------- 持久化 ----------------
    def _init_db(self):
        """初始化数据库表"""
        if not self.db_path:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sim_accounts (
            account_id TEXT PRIMARY KEY,
            initial_capital REAL,
            cash REAL,
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS sim_positions (
            account_id TEXT,
            code TEXT,
            name TEXT,
            quantity INTEGER,
            available INTEGER,
            frozen INTEGER,
            cost_price REAL,
            total_cost REAL,
            open_date TEXT,
            last_update TEXT,
            strategy TEXT,
            original_plan TEXT,
            PRIMARY KEY (account_id, code)
        );

        CREATE TABLE IF NOT EXISTS sim_trades (
            trade_id TEXT PRIMARY KEY,
            account_id TEXT,
            trade_date TEXT,
            trade_time TEXT,
            code TEXT,
            name TEXT,
            direction TEXT,
            price REAL,
            orig_price REAL,
            quantity INTEGER,
            amount REAL,
            fees REAL,
            net_amount REAL,
            realized_pnl REAL,
            realized_pct REAL,
            reason TEXT,
            strategy TEXT,
            hold_days INTEGER,
            cash_after REAL
        );

        CREATE TABLE IF NOT EXISTS sim_snapshots (
            account_id TEXT,
            date TEXT,
            cash REAL,
            position_value REAL,
            total_assets REAL,
            position_ratio REAL,
            total_pnl REAL,
            total_return_pct REAL,
            position_count INTEGER,
            positions_json TEXT,
            PRIMARY KEY (account_id, date)
        );

        CREATE INDEX IF NOT EXISTS idx_trades_account_date ON sim_trades(account_id, trade_date);
        CREATE INDEX IF NOT EXISTS idx_trades_code ON sim_trades(code);
        CREATE INDEX IF NOT EXISTS idx_snapshots_account_date ON sim_snapshots(account_id, date);
        """)
        conn.commit()
        conn.close()

    def _load_from_db(self):
        """从数据库加载账户状态"""
        self._init_db()
        conn = sqlite3.connect(self.db_path)

        # 加载账户
        row = conn.execute(
            "SELECT initial_capital, cash FROM sim_accounts WHERE account_id = ?",
            (self.account_id,)
        ).fetchone()
        if row:
            self.initial_capital = row[0]
            self.cash = row[1]
        else:
            # 新账户
            conn.execute(
                "INSERT INTO sim_accounts VALUES (?,?,?,?,?)",
                (self.account_id, self.initial_capital, self.cash,
                 datetime.now().isoformat(), datetime.now().isoformat())
            )

        # 加载持仓
        cur = conn.execute(
            "SELECT code, name, quantity, available, frozen, cost_price, total_cost,"
            " open_date, last_update, strategy, original_plan"
            " FROM sim_positions WHERE account_id = ?",
            (self.account_id,)
        )
        for r in cur.fetchall():
            pos = Position(
                code=r[0], name=r[1], quantity=r[2], available=r[3], frozen=r[4],
                cost_price=r[5], total_cost=r[6], open_date=r[7], last_update=r[8],
                strategy=r[9], original_plan=json.loads(r[10]) if r[10] else {},
            )
            self.positions[pos.code] = pos

        # 加载交易历史（最近100笔）
        cur = conn.execute(
            "SELECT trade_id, trade_date, trade_time, code, name, direction, price,"
            " quantity, amount, fees, net_amount, realized_pnl, realized_pct, reason,"
            " strategy, hold_days, cash_after FROM sim_trades"
            " WHERE account_id = ? ORDER BY trade_time DESC LIMIT 100",
            (self.account_id,)
        )
        for r in cur.fetchall():
            self.trade_history.append({
                "trade_id": r[0], "trade_date": r[1], "trade_time": r[2],
                "code": r[3], "name": r[4], "direction": r[5], "price": r[6],
                "quantity": r[7], "amount": r[8], "fees": r[9], "net_amount": r[10],
                "realized_pnl": r[11], "realized_pct": r[12], "reason": r[13],
                "strategy": r[14], "hold_days": r[15], "cash_after": r[16],
                "account_id": self.account_id,
            })
        self.trade_history.reverse()  # 恢复时间正序

        # 加载每日快照（按日期正序），供绩效指标与 WebUI 净值曲线使用
        cur = conn.execute(
            "SELECT date, cash, position_value, total_assets, position_ratio,"
            " total_pnl, total_return_pct, position_count, positions_json"
            " FROM sim_snapshots WHERE account_id = ? ORDER BY date ASC",
            (self.account_id,)
        )
        for r in cur.fetchall():
            self.daily_snapshots.append({
                "date": r[0], "account_id": self.account_id, "cash": r[1],
                "frozen_cash": 0.0, "position_value": r[2], "total_assets": r[3],
                "position_ratio": r[4], "total_pnl": r[5], "total_return_pct": r[6],
                "position_count": r[7],
                "positions": json.loads(r[8]) if r[8] else [],
            })

        conn.close()

    def _save_trade(self, trade: dict):
        """保存交易记录"""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT OR REPLACE INTO sim_trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                trade["trade_id"], trade["account_id"], trade["trade_date"], trade["trade_time"],
                trade["code"], trade["name"], trade["direction"], trade["price"],
                trade.get("orig_price", trade["price"]), trade["quantity"], trade["amount"],
                trade["fees"], trade.get("net_amount", trade["amount"]),
                trade.get("realized_pnl"), trade.get("realized_pct"),
                trade.get("reason", ""), trade.get("strategy", ""),
                trade.get("hold_days"), trade.get("cash_after"),
            )
        )
        # 同时更新账户和持仓
        self._save_account_state(conn)
        conn.commit()
        conn.close()

    def _save_snapshot(self, snapshot: dict):
        """保存每日快照"""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT OR REPLACE INTO sim_snapshots VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                snapshot["account_id"], snapshot["date"], snapshot["cash"],
                snapshot["position_value"], snapshot["total_assets"],
                snapshot["position_ratio"], snapshot["total_pnl"],
                snapshot["total_return_pct"], snapshot["position_count"],
                json.dumps(snapshot["positions"], ensure_ascii=False),
            )
        )
        conn.commit()
        conn.close()

    def _save_account_state(self, conn: sqlite3.Connection = None):
        """保存账户和持仓状态"""
        should_close = False
        if conn is None:
            conn = sqlite3.connect(self.db_path)
            should_close = True

        # 账户
        conn.execute(
            "INSERT OR REPLACE INTO sim_accounts VALUES (?,?,?,?,?)",
            (self.account_id, self.initial_capital, self.cash,
             datetime.now().isoformat(), datetime.now().isoformat())
        )

        # 持仓（先清空再插入，简单可靠）
        conn.execute("DELETE FROM sim_positions WHERE account_id = ?", (self.account_id,))
        for pos in self.positions.values():
            conn.execute(
                "INSERT INTO sim_positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.account_id, pos.code, pos.name, pos.quantity,
                    pos.available, pos.frozen, pos.cost_price, pos.total_cost,
                    pos.open_date, pos.last_update, pos.strategy,
                    json.dumps(pos.original_plan, ensure_ascii=False),
                )
            )

        if should_close:
            conn.commit()
            conn.close()

    def save(self):
        """手动保存账户状态"""
        if self.db_path:
            self._save_account_state()

    # ---------------- 报表 ----------------
    def get_summary(self) -> dict:
        """获取账户摘要"""
        realized_pnl = sum(t.get("realized_pnl", 0) or 0 for t in self.trade_history
                          if t.get("direction") == "sell")
        unrealized_pnl = sum(p.profit_loss for p in self.positions.values())

        return {
            "account_id": self.account_id,
            "initial_capital": self.initial_capital,
            "cash": round(self.cash, 2),
            "position_value": self.position_value,
            "total_assets": self.total_assets,
            "position_ratio": self.position_ratio,
            "total_pnl": self.total_pnl,
            "total_return_pct": self.total_return_pct,
            "realized_pnl": round(realized_pnl, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "position_count": len(self.positions),
            "trade_count": len(self.trade_history),
        }

    def get_positions(self) -> list[dict]:
        """获取所有持仓"""
        return [pos.to_dict() for pos in self.positions.values()]

    def get_trades(self, limit: int = 50, direction: str = None) -> list[dict]:
        """获取交易历史"""
        trades = self.trade_history
        if direction:
            trades = [t for t in trades if t.get("direction") == direction]
        return trades[-limit:][::-1]  # 最新在前

    def get_performance(self) -> dict:
        """计算绩效指标"""
        if not self.daily_snapshots:
            return {}

        returns = []
        for i in range(1, len(self.daily_snapshots)):
            prev = self.daily_snapshots[i-1]["total_assets"]
            curr = self.daily_snapshots[i]["total_assets"]
            if prev > 0:
                returns.append((curr - prev) / prev)

        if not returns:
            return {"total_return": self.total_return_pct / 100}

        # 年化收益
        total_return = self.total_return_pct / 100
        days = len(self.daily_snapshots)
        annual_return = (1 + total_return) ** (252 / max(1, days)) - 1 if days > 0 else 0

        # 波动率
        avg = sum(returns) / len(returns)
        variance = sum((r - avg) ** 2 for r in returns) / max(1, len(returns) - 1)
        volatility = (variance ** 0.5) * (252 ** 0.5)

        # 夏普比率（假设无风险利率 2%）
        sharpe = (annual_return - 0.02) / volatility if volatility > 0 else 0

        # 最大回撤
        peak = self.daily_snapshots[0]["total_assets"]
        max_dd = 0
        for s in self.daily_snapshots:
            peak = max(peak, s["total_assets"])
            dd = (peak - s["total_assets"]) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)

        # 胜率
        wins = sum(1 for t in self.trade_history
                   if t.get("direction") == "sell" and (t.get("realized_pnl") or 0) > 0)
        sells = sum(1 for t in self.trade_history if t.get("direction") == "sell")
        win_rate = wins / sells if sells > 0 else 0

        return {
            "total_return": round(total_return, 4),
            "annual_return": round(annual_return, 4),
            "volatility": round(volatility, 4),
            "sharpe_ratio": round(sharpe, 2),
            "max_drawdown": round(max_dd, 4),
            "win_rate": round(win_rate, 3),
            "total_trades": len(self.trade_history),
            "days": days,
        }
