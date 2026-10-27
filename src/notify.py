"""macOS 本地通知。用 osascript，不依赖第三方库。

注意：首次使用可能需要在「系统设置 → 通知」里允许「脚本编辑器」或终端发送通知。
"""
from __future__ import annotations

import logging
import subprocess

log = logging.getLogger(__name__)


def macos_notify(title: str, message: str, sound: str | None = "Glass") -> bool:
    # 消息里的双引号会破坏 AppleScript，先转义
    safe_title = title.replace('"', "'")
    safe_msg = message.replace('"', "'")
    script = f'display notification "{safe_msg}" with title "{safe_title}"'
    if sound:
        script += f' sound name "{sound}"'
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=True,
            capture_output=True,
            timeout=10,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("通知发送失败: %s", exc)
        return False


def build_message(brief: dict) -> tuple[str, str]:
    """把简报压成一条能一眼看完的通知。

    短线模式下首行给「赚钱效应 + 可执行计划数」，而不是指数涨跌——
    指数涨不涨和短线能不能做，经常是两件事。
    """
    m = brief["market"]
    state = m.get("state", {}).get("state", "未知")
    temp = m.get("temp") or {}
    b = m.get("breadth") or {}
    idx = {i["code"]: i for i in m.get("indices", [])}
    sh = idx.get("000001", {})
    sh_pct = sh.get("pct_chg")
    sh_txt = f"{sh_pct:+.2f}%" if sh_pct is not None else "—"

    p0 = [a for a in brief["alerts_all"] if a["level"] == "P0"]
    p1 = [a for a in brief["alerts_all"] if a["level"] == "P1"]
    ready = [r for r in brief.get("watch", []) if (r.get("plan") or {}).get("ready")]

    if temp:
        head = (f"{temp.get('tag', '')} {temp.get('score', 0):.0f}分 · 涨停{temp.get('limit_up', 0)}"
                f" · 上证 {sh_txt}")
    else:
        head = f"上证 {sh_txt} · {state}"
        if b:
            head += f" · 涨{b.get('up', 0)}/跌{b.get('down', 0)}"

    lines = [head]
    if p0:
        lines.append("P0：" + "；".join(f"{a['name']}{a['rule']}" for a in p0[:2]))
    elif ready:
        r0 = max(ready, key=lambda r: r["score"])
        p = r0["plan"]
        lines.append(
            f"计划 {len(ready)} 只｜{r0['name']} 买 {p['buy_lo']:.2f}-{p['buy_hi']:.2f} "
            f"损 {p['stop']:.2f} 标 {p['target1']:.2f}"
        )
    elif p1:
        lines.append("P1：" + "；".join(f"{a['name']}{a['rule']}" for a in p1[:2]))
    else:
        top = sorted(brief["watch"], key=lambda x: -x["score"])[:2]
        if top:
            lines.append("最强：" + "、".join(f"{t['name']} {t['score']}分" for t in top))
    return head, " / ".join(lines[1:]) if len(lines) > 1 else head
