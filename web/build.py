#!/usr/bin/env python3
"""
把 web/data.json 内联进 web/index.html，产出：
  * web/index.html          —— 带数据快照的源码页（fetch data.json 作为刷新兜底）
  * dist/index.html         —— 单文件版，双击即可在浏览器打开（无 fetch 依赖）
  * dist/data.json
  * dist/calendar.ics       —— 可订阅的演出日历
  * dist/README.txt

用法： python3 build.py
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
DIST = ROOT / "dist"
CST = timezone(timedelta(hours=8))

SNAPSHOT_RE = re.compile(
    r'(<script id="snapshot" type="application/json">).*?(</script>)', re.S
)


def ics_escape(s: str) -> str:
    return (
        s.replace("\\", "\\\\").replace(";", "\\;")
        .replace(",", "\\,").replace("\n", "\\n")
    )


def fold(line: str) -> str:
    """RFC 5545 要求行长不超过 75 字节。"""
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode("utf-8")) > 73:
            out.append(cur)
            cur = " " + ch
        else:
            cur += ch
    out.append(cur)
    return "\r\n".join(out)


def build_ics(data: dict) -> str:
    now = datetime.now(CST)
    stamp = now.strftime("%Y%m%dT%H%M%S")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//theatre-watch//music-theatre//CN",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:江浙沪-音乐剧·话剧-速览",
        f"X-WR-TIMEZONE:Asia/Shanghai",
    ]
    for s in data.get("shows", []):
        if not s.get("date"):
            continue
        if s.get("kind") == "展览":
            continue            # 日历只提醒演出与博物馆特展，普通商业展览不进日历
        uid = f"theatrewatch-{s['id']}@local"
        start = s["date"].replace("-", "")
        end = (s.get("date_end") or s["date"]).replace("-", "")
        # 全天事件：结束日 +1 天（VCALENDAR 的 DTEND 是排他的）
        try:
            e = datetime.strptime(end, "%Y%m%d") + timedelta(days=1)
            end_excl = e.strftime("%Y%m%d")
        except ValueError:
            end_excl = start
        desc = f"{s.get('kind','')}｜{s.get('venue','') or '场馆待定'}｜{s.get('date_text','')}"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{stamp}Z",
            f"DTSTART;VALUE=DATE:{start}",
            f"DTEND;VALUE=DATE:{end_excl}",
            fold(f"SUMMARY:{ics_escape('【' + s['kind'] + '】' + s['title'])}"),
            fold(f"DESCRIPTION:{ics_escape(desc)}"),
            fold(f"LOCATION:{ics_escape(s.get('venue') or s.get('city') or '')}"),
            f"URL:{s['url']}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def inline(page: str, data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # JSON 里出现 </script> 会截断标签
    payload = payload.replace("</", "<\\/")
    return SNAPSHOT_RE.sub(lambda m: m.group(1) + payload + m.group(2), page, count=1)


def main() -> int:
    data = json.loads((WEB / "data.json").read_text(encoding="utf-8"))
    page = (WEB / "index.html").read_text(encoding="utf-8")

    # 源码页也带快照，保证 file:// 打开或 data.json 404 时有内容
    (WEB / "index.html").write_text(inline(page, data), encoding="utf-8")

    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir()

    standalone = inline(page, data).replace(
        "fetch('data.json', {cache:'no-store'})", "Promise.resolve(null)"
    )
    (DIST / "index.html").write_text(standalone, encoding="utf-8")
    (DIST / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    (DIST / "calendar.ics").write_text(build_ics(data), encoding="utf-8")
    # 同步到 web/ 目录，便于本地直接起静态服务时链接不断
    (WEB / "calendar.ics").write_text(build_ics(data), encoding="utf-8")
    n = len(data.get("shows", []))
    (DIST / "README.txt").write_text(
        "江浙沪-音乐剧·话剧-速览\n\n"
        f"本轮收录 {n} 场，数据更新于 {data.get('updated_at','?')}。\n\n"
        "index.html   单文件版，直接双击或本地打开即可，无需服务器\n"
        "data.json    结构化数据\n"
        "calendar.ics 导入手机日历，或作为订阅地址使用\n",
        encoding="utf-8",
    )
    print(f"[build] {n} 场 → dist/ (index.html {len(standalone)//1024}KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
