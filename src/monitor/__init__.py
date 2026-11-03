"""盘中监控模块 —— 15秒级轮询，触发止损/止盈/异动提醒。

设计原则：
- 监控线程与主流程解耦，崩溃不影响简报生成
- 告警有冷却期，避免同一信号反复轰炸
- 状态持久化到 SQLite，重启后能恢复上下文
"""
from __future__ import annotations

from .watcher import Alert, AlertNotifier, Monitor, MonitorConfig

__all__ = ["Alert", "AlertNotifier", "Monitor", "MonitorConfig"]
