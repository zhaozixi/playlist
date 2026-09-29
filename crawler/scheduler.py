#!/usr/bin/env python3
"""
常驻调度器：每天 08:00 与 16:00（北京时间）执行一次抓取 + 构建。

沙箱镜像里没有 cron，用这个进程代替。启动时先跑一轮，避免空页面。
日志写到 /tmp/tw-scheduler.log，抓取日志写 /tmp/tw-scrape.log。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CST = timezone(timedelta(hours=8))
HOURS = [int(h) for h in os.environ.get("TW_HOURS", "8,16").split(",")]
SCRAPE_LOG = Path("/tmp/tw-scrape.log")


def now_cst() -> datetime:
    return datetime.now(CST)


def next_run() -> datetime:
    base = now_cst()
    cands = []
    for day_off in (0, 1):
        d = (base + timedelta(days=day_off)).date()
        for h in HOURS:
            t = datetime(d.year, d.month, d.day, h, 0, tzinfo=CST)
            if t > base:
                cands.append(t)
    return min(cands)


def run(label: str) -> None:
    ts = now_cst().strftime("%Y-%m-%d %H:%M:%S")
    with SCRAPE_LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"\n===== {ts} {label} =====\n")
        fh.flush()
        for cmd in (
            [sys.executable, "crawler/scrape.py"],
            [sys.executable, "web/build.py"],
        ):
            subprocess.run(cmd, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT)


def main() -> int:
    print(f"[scheduler] 启动，抓取时刻 {HOURS}:00 CST", flush=True)
    run("startup")
    while True:
        nxt = next_run()
        wait = (nxt - now_cst()).total_seconds()
        print(
            f"[scheduler] 下次运行 {nxt:%Y-%m-%d %H:%M CST}（{wait/3600:.2f} 小时后）",
            flush=True,
        )
        # 分段 sleep，便于收到信号后尽快退出
        while wait > 0:
            time.sleep(min(wait, 300))
            wait = (nxt - now_cst()).total_seconds()
        run("scheduled")
        time.sleep(60)  # 避免同一分钟内重复触发


if __name__ == "__main__":
    raise SystemExit(main())
