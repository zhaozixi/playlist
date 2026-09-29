#!/usr/bin/env python3
"""一次性信源可达性探针。

只往 stdout 打日志，不参与抓取、不落盘、不影响 data.json。
用途：GitHub Actions runner 的出口 IP 与开发沙箱不同段，
跑一次就能看出哪些站在 runner 上是通的，值得接。
"""
from __future__ import annotations

import json
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

TARGETS = [
    ("大麦 searchajax",
     "https://search.damai.cn/searchajax.html?keyword=%E9%9F%B3%E4%B9%90%E5%89%A7"
     "&currPage=1&pageSize=30&order=1"),
    ("大麦 m 站搜索",
     "https://m.damai.cn/search.html?keyword=%E9%9F%B3%E4%B9%90%E5%89%A7"),
    ("聚橙 api", "https://api.juooo.com/project/list?page=1&rows=10"),
    ("秀动 api", "https://api.showstart.com/event/list?page=1&rows=10"),
    ("保利剧院", "https://www.polytheatre.com/polytheatre/index"),
    ("摩天轮", "https://www.motianlun.com/"),
]

# 阿里风控页的特征串，命中即判定为被封而非网络故障
BLOCK_SIGNS = ("rgv587", "punish", "bixi.alicdn.com", "unusual traffic",
               "x5secdata", "captcha", "_____tmd_____")


def probe(name: str, url: str) -> None:
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Accept": "*/*", "Accept-Language": "zh-CN",
        })
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read(400_000)
        text = raw.decode("utf-8", "ignore")
        low = text.lower()
        blocked = next((s for s in BLOCK_SIGNS if s in low), None)
        hint = ""
        if blocked is None:
            try:
                d = json.loads(text)
                hint = f" jsonkeys={list(d)[:4]}"
            except (json.JSONDecodeError, ValueError):
                hint = f" html={text[:40].strip()!r}"
        verdict = f"BLOCKED({blocked})" if blocked else "OPEN"
        print(f"[probe] {name:18s} {verdict:10s} len={len(text)}{hint}", flush=True)
    except Exception as e:  # noqa: BLE001 - 探针不能抛错影响主流程
        print(f"[probe] {name:18s} ERROR    {type(e).__name__}: {e}", flush=True)


def run_all() -> None:
    print("[probe] === 候选信源可达性 ===", flush=True)
    for n, u in TARGETS:
        probe(n, u)
    print("[probe] === 探针结束 ===", flush=True)


if __name__ == "__main__":
    run_all()
