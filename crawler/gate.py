#!/usr/bin/env python3
"""排程补漏闸门：判断「这个北京时间整点，本轮抓取是否已经成功落盘」。

背景：Actions 的 schedule 是尽力而为，实测会被延迟数小时甚至整槽跳过
（2026-09-29 的 16:00 CST 槽位实际 22:42 才跑，09-30 08:00 那一槽压根没出现）。
所以工作流里除了 00:00/08:00 UTC 两个正点，还挂 +1h/+2h 的补跑槽位；
补跑槽位进来先问这里：正点那轮要是已经成功过，就直接跳过，不重复抓。

输出：向 stdout 打 `yes`（该抓）或 `no`（已新鲜，跳过）；
带 --explain 时额外打一行原因到 stderr，便于在日志里排查。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))
DATA_FILE = Path(__file__).resolve().parent.parent / "web" / "data.json"
SLOTS = (8, 16)          # 北京时间排程整点
GRACE_MIN = 45           # 补跑槽位判定「正点已过」的余量，避开同一槽的并发重复
DEFAULT_SKEW_MIN = 5     # runner 时钟/提交延迟容忍


def current_slot(now: datetime, slots=SLOTS) -> datetime | None:
    """返回 now 之前（含此刻）最近的排程槽位；当天还没到第一槽则取昨天最后一槽。"""
    days = [now.date(), now.date() - timedelta(days=1)]
    cands = [datetime.combine(d, datetime.min.time())
                 .replace(hour=h, tzinfo=CST) for d in days for h in slots]
    past = [c for c in cands if c <= now]
    return max(past) if past else None


def needs_scrape(data: dict, now: datetime, grace_min: int = GRACE_MIN,
                 skew_min: int = DEFAULT_SKEW_MIN) -> tuple[bool, str]:
    slot = current_slot(now)
    if slot is None:
        return True, "无法确定槽位，照常抓取"
    # 正点那轮自己也属于「已过」，用 grace 把它排除掉：
    # 只有过了 slot+grace 还没数据，才认为是漏跑，需要补。
    due = slot + timedelta(minutes=grace_min)
    if now < due:
        return True, f"正点槽位 {slot:%H:%M}，正常抓取"
    ts = data.get("updated_ts")
    if not isinstance(ts, (int, float)):
        return True, "data.json 没有 updated_ts，需抓取"
    fresh_from = slot - timedelta(minutes=skew_min)
    if ts >= fresh_from.timestamp():
        age_min = (now - datetime.fromtimestamp(ts, CST)).total_seconds() / 60
        return False, (f"槽位 {slot:%H:%M} 已有数据（{age_min:.0f} 分钟前更新），跳过")
    return True, f"槽位 {slot:%H:%M} 的数据缺失（updated_ts={ts}），补跑"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--explain", action="store_true", help="额外打印判断原因")
    ap.add_argument("--now", help="覆盖当前时间（ISO，测试用）")
    ap.add_argument("--data", help="data.json 路径，默认 web/data.json")
    args = ap.parse_args()

    now = datetime.fromisoformat(args.now).astimezone(CST) \
        if args.now else datetime.now(CST)
    path = Path(args.data) if args.data else DATA_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}

    run, reason = needs_scrape(data, now)
    if args.explain:
        print(f"[gate] {now:%m-%d %H:%M CST} → {reason}", file=sys.stderr)
    print("yes" if run else "no")
    return 0


if __name__ == "__main__":
    sys.exit(main())
