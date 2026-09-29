#!/usr/bin/env python3
"""
音乐剧 / 话剧 演出信息抓取

主数据源：格瓦拉生活网（show.maoyan.com）首页的服务端渲染内容。
该页面的分类区块（含「热门话剧/歌剧」）是 SSR 输出的，因此无需浏览器、
无需签名接口即可稳定解析。

用法：
    python3 scrape.py                 # 抓取并更新 data/ + web/data.json
    python3 scrape.py --enrich        # 额外访问详情页补全场馆/档期/状态（较慢）
    python3 scrape.py --offline       # 不发请求，仅用缓存重建页面数据
    python3 scrape.py --stdout        # 打印结果摘要，不落盘（用于自检）

设计约束：
  * 礼貌抓取：单源、串行、带延时，失败时保留上一次数据并标记 stale。
  * 只做元信息聚合，不下载海报等图片资源，购票请跳转到源站页面。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
WEB_DIR = BASE_DIR / "web"
CACHE_FILE = DATA_DIR / "raw_cache.html"
SHOWS_FILE = DATA_DIR / "shows.json"
CHANGELOG_FILE = DATA_DIR / "changelog.json"
OUT_FILE = WEB_DIR / "data.json"

SOURCE_NAME = "格瓦拉生活网"
LIST_URL = "https://show.maoyan.com/"
DETAIL_URL = "https://show.maoyan.com/detail/{id}"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)
CST = timezone(timedelta(hours=8))

# 抓取节奏：两次请求之间的最小间隔（秒），避免给源站造成压力
REQUEST_INTERVAL = float(os.environ.get("TW_REQUEST_INTERVAL", "1.5"))
# --enrich 时最多访问多少个详情页
MAX_ENRICH = int(os.environ.get("TW_MAX_ENRICH", "40"))

# 分类判定：优先按标题关键词，其次按所在栏目
MUSICAL_WORDS = ("音乐剧", "歌舞剧", "musical", "轻歌剧", "歌剧", "音乐剧场")
DRAMA_WORDS = (
    "话剧", "舞台剧", "戏剧", "独角戏", "默剧", "肢体剧",
    "环境戏剧", "先锋剧", "读剧本", "剧社", "小剧场",
)
# 强噪声：出现即判定为非音乐剧/话剧（脱口秀、魔术秀、VR 体验、演唱会等）
STRONG_NOISE = (
    "脱口秀", "漫才", "魔术", "VR", "剧本杀", "密室", "演唱会", "音乐会",
    "音乐节", "livehouse", "相声", "评弹", "鼓书", "杂技", "马戏",
    "展览", "市集", "工作坊", "体验", "通票", "游园", "电竞", "游戏",
    "高清放映", "影院直播", "影像放映",
)
# 古典音乐会类：标题常带「××歌剧院」，容易被当成歌剧/音乐剧
ORCHESTRA_NOISE = ("乐团", "协奏曲", "交响", "独奏", "钢琴", "小提琴", "室内乐")
# 戏曲、芭蕾等属其他舞台门类，本场只做音乐剧/话剧，一并排除
OTHER_STAGE = ("京剧", "昆曲", "越剧", "豫剧", "黄梅戏", "评剧", "川剧", "沪剧",
               "粤剧", "秦腔", "梆子", "梨园", "戏曲", "芭蕾", "交响", "室内乐")
DRAMA_SECTION_HINT = ("话剧", "歌剧", "剧场", "戏剧")


def log(msg: str) -> None:
    print(f"[{datetime.now(CST):%H:%M:%S}] {msg}", flush=True)


def fetch(url: str, session: requests.Session, retries: int = 3) -> str | None:
    """带重试的 GET，失败返回 None 而不抛异常。"""
    for attempt in range(1, retries + 1):
        try:
            r = session.get(
                url,
                timeout=20,
                headers={
                    "User-Agent": UA,
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    "Referer": LIST_URL,
                },
            )
            # 大麦/美团系风控页特征：punish / rgv587
            if "rgv587" in r.text or "punish" in r.url:
                log(f"  被风控拦截: {url}")
                return None
            if r.status_code == 200 and len(r.text) > 20000:
                return r.text
            log(f"  HTTP {r.status_code} / {len(r.text)}b, 重试 {attempt}/{retries}")
        except requests.RequestException as e:
            log(f"  请求异常 {type(e).__name__}: {e}, 重试 {attempt}/{retries}")
        time.sleep(REQUEST_INTERVAL * attempt)
    return None


def parse_section(html: str) -> list[dict]:
    """解析首页各分类区块下的 hotlist（热门）与 newlist（最新上架）条目。"""
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for cat in soup.find_all("section", "category"):
        head = cat.find(class_="hotlist-title")
        section = head.get_text(strip=True) if head else ""

        for item in cat.find_all("div", class_="hotlist-item"):
            el_id = (item.get("id") or "").split("hotlist")[0]
            name_el = item.find(class_="hotlist-item-name")
            loc_el = item.find("p", class_="location")
            date_el = item.find("p", class_="date")
            if not el_id or not name_el:
                continue
            out.append(
                {
                    "id": el_id,
                    "title": name_el.get_text(" ", strip=True),
                    "venue": loc_el.get_text(" ", strip=True) if loc_el else "",
                    "date_text": date_el.get_text(" ", strip=True) if date_el else "",
                    "section": section,
                    "list_kind": "hot",
                }
            )

        for item in cat.find_all("li", class_="newlist-item"):
            el_id = (item.get("id") or "").split("newlist")[0]
            name_el = item.find(class_="newlist-item-name")
            time_el = item.find(class_="newlist-item-time")
            if not el_id or not name_el:
                continue
            out.append(
                {
                    "id": el_id,
                    "title": name_el.get_text(" ", strip=True),
                    "venue": "",
                    "date_text": time_el.get_text(" ", strip=True) if time_el else "",
                    "section": section,
                    "list_kind": "new",
                }
            )
    return out


def parse_detail(html: str) -> dict:
    """从详情页补全场馆、精确档期与销售状态。"""
    soup = BeautifulSoup(html, "html.parser")
    info: dict = {}
    h1 = soup.find("h1")
    if h1:
        info["title"] = h1.get_text(" ", strip=True)
    loc = soup.find("p", class_="location")
    if loc:
        info["venue"] = loc.get_text(" ", strip=True)
    date = soup.find("p", class_="date")
    if date:
        info["date_text"] = date.get_text(" ", strip=True)
    sale = soup.find(class_="salelabel")
    if sale:
        info["status"] = sale.get_text(" ", strip=True)
    return info


def classify(rec: dict) -> str | None:
    """判定条目类型，返回 '音乐剧' / '话剧' / None（不相关）。

    格瓦拉首页栏目划分可靠：「热门话剧/歌剧」下的条目基本都是舞台剧；
    其它栏目（演唱会、戏曲、亲子、展览等）只有标题明确写了「音乐剧」「话剧」
    「舞台剧」才收录，避免把魔术秀、金曲演唱会误收进来。
    """
    title = rec["title"]
    low = title.lower()
    section = rec.get("section", "")
    in_drama_section = any(h in section for h in DRAMA_SECTION_HINT)

    # 「歌剧」要排除「××歌剧院」这种乐团巡演命名
    has_opera = "歌剧" in low and "歌剧院" not in low
    explicit_musical = ("音乐剧" in low) or has_opera or ("歌舞剧" in low)
    explicit_drama = any(w in low for w in ("话剧", "舞台剧", "独角戏", "肢体剧", "环境戏剧"))

    # 硬噪声（含「游戏剧场」「密室」等）优先排除
    if any(w.lower() in low for w in STRONG_NOISE):
        return None
    if any(w in low for w in OTHER_STAGE):
        return None
    # 古典乐团/协奏曲类，除非标题明确写了音乐剧或话剧，否则不算
    if any(w in low for w in ORCHESTRA_NOISE) and not (explicit_musical or explicit_drama):
        return None
    # 舞剧、纯舞蹈类不属于本次范围
    if "舞剧" in low and not explicit_musical:
        return None

    if explicit_musical:
        return "音乐剧"
    if explicit_drama or any(w in low for w in DRAMA_WORDS):
        return "话剧"
    # 在话剧/歌剧栏目内、标题没写门类的，按栏目归类
    return "话剧" if in_drama_section else None


CITIES = (
    "北京", "上海", "广州", "深圳", "杭州", "南京", "苏州", "成都", "重庆", "武汉",
    "西安", "天津", "长沙", "青岛", "厦门", "合肥", "郑州", "宁波", "无锡", "佛山",
    "东莞", "济南", "福州", "昆明", "沈阳", "大连", "哈尔滨", "南昌", "贵阳", "南宁",
    "兰州", "太原", "长春", "石家庄", "嘉兴", "绍兴", "金华", "台州", "泉州", "烟台",
    "常州", "南通", "扬州", "镇江", "芜湖", "洛阳",
)
_CITY_ALT = "|".join(sorted(CITIES, key=len, reverse=True))
# 「-上海站」「·北京站」「（深圳站）」——巡演才会标注城市
STATION_RE = re.compile(rf"[-—·（(\s]({_CITY_ALT})站")
# 场馆名开头（不含括号内地址）才是城市，避免把「北京东路」读成北京
VENUE_HEAD_RE = re.compile(rf"^({_CITY_ALT})")


def detect_page_city(html: str) -> str:
    """源站首页会显示当前城市，列表内容即该城市的场次。"""
    soup = BeautifulSoup(html, "html.parser")
    el = soup.find(class_="city-current") or soup.find(class_="city-label")
    if el:
        txt = el.get_text(" ", strip=True)
        m = re.search(rf"({_CITY_ALT})", txt)
        if m:
            return m.group(1)
    return ""


def extract_city(title: str, venue: str, page_city: str = "") -> str:
    """巡演按标题标注的城市，否则用场馆名前缀，最后回落到页面当前城市。"""
    m = STATION_RE.search(title)
    if m:
        return m.group(1)
    head = re.split(r"[(（]", venue.strip())[0] if venue else ""
    m = VENUE_HEAD_RE.match(head)
    if m:
        return m.group(1)
    return page_city


def parse_dates(date_text: str) -> tuple[str, str]:
    """把 '2026.11.19 - 2026.11.22' / '2026.11.14 / 11.15' / '2026.12.04 19:30 周五'
    归一成 (起始日, 结束日) 的 ISO 字符串，取不到则留空。"""
    t = date_text.strip()
    found = re.findall(r"(20\d{2})[.\-/年](\d{1,2})[.\-/月](\d{1,2})", t)
    if not found:
        return "", ""

    def iso(y, m, d):
        try:
            return datetime(int(y), int(m), int(d)).strftime("%Y-%m-%d")
        except ValueError:
            return ""

    start = iso(*found[0])
    if len(found) < 2:
        # '2026.11.14 / 11.15' 这类只写了月日的结束日
        m2 = re.search(r"(\d{1,2})[.\-/月](\d{1,2})\s*$", t)
        if m2 and start:
            y, mo, d = start.split("-")
            end = iso(y, m2.group(1), m2.group(2))
            if end and end >= start:
                return start, end
        return start, start
    end = iso(*found[-1])
    return start, (end if end >= start else start)


def parse_start_date(date_text: str) -> str:
    return parse_dates(date_text)[0]


def days_left(iso: str) -> int | None:
    if not iso:
        return None
    try:
        d = datetime.strptime(iso, "%Y-%m-%d").date()
    except ValueError:
        return None
    return (d - datetime.now(CST).date()).days


def dedupe(records: list[dict]) -> list[dict]:
    """按 id 合并；同一演出可能同时出现在 hot/new 列表里。"""
    merged: dict[str, dict] = {}
    for r in records:
        cur = merged.get(r["id"])
        if cur is None:
            merged[r["id"]] = dict(r)
            continue
        # 保留信息更全的那条
        if len(r.get("venue", "")) > len(cur.get("venue", "")):
            cur["venue"] = r["venue"]
        if len(r.get("date_text", "")) > len(cur.get("date_text", "")):
            cur["date_text"] = r["date_text"]
        if r.get("list_kind") == "new":
            cur["list_kind"] = "new"
    return list(merged.values())


def build(shows: list[dict], stale: bool, note: str = "", city: str = "") -> dict:
    now = datetime.now(CST)
    stage = []
    for s in shows:
        kind = classify(s)
        if not kind:
            continue
        start, end = parse_dates(s.get("date_text", ""))
        # 已闭幕（结束日早于今天）的条目不再展示
        if end:
            try:
                if datetime.strptime(end, "%Y-%m-%d").date() < datetime.now(CST).date():
                    continue
            except ValueError:
                pass
        item = {
            "id": s["id"],
            "kind": kind,
            "title": s["title"],
            "venue": s.get("venue", ""),
            "city": s.get("city", ""),
            "date_text": s.get("date_text", ""),
            "date": start,
            "date_end": end,
            "days": days_left(start),
            "status": s.get("status", ""),
            "section": s.get("section", ""),
            "url": DETAIL_URL.format(id=s["id"]),
            "first_seen": s.get("first_seen", now.strftime("%Y-%m-%d")),
            "is_new": bool(s.get("is_new")),
        }
        stage.append(item)

    stage.sort(key=lambda x: (x["date"] or "9999-12-31", x["kind"], x["title"]))
    musical = [x for x in stage if x["kind"] == "音乐剧"]
    drama = [x for x in stage if x["kind"] == "话剧"]

    return {
        "source": SOURCE_NAME,
        "source_url": LIST_URL,
        "city": city,
        "updated_at": now.strftime("%Y-%m-%d %H:%M CST"),
        "updated_ts": int(now.timestamp()),
        "stale": stale,
        "note": note,
        "counts": {
            "total": len(stage),
            "音乐剧": len(musical),
            "话剧": len(drama),
            "new": sum(1 for x in stage if x["is_new"]),
        },
        "shows": stage,
    }


def apply_history(current: list[dict], previous: dict) -> tuple[list[dict], dict]:
    """比对上一次结果，标记新上架/已下架，并生成 changelog。"""
    prev_shows = {s["id"]: s for s in previous.get("shows", [])}
    today = datetime.now(CST).strftime("%Y-%m-%d")

    merged = []
    for s in current:
        old = prev_shows.get(s["id"])
        if old:
            s["first_seen"] = old.get("first_seen", today)
            s["is_new"] = False
        else:
            s["first_seen"] = today
            s["is_new"] = True
        merged.append(s)

    added = [s for s in merged if s["is_new"]]
    removed = [
        {"id": i, "title": s["title"], "kind": classify(s) or ""}
        for i, s in prev_shows.items()
        if i not in {c["id"] for c in current}
    ]
    removed = [r for r in removed if r["kind"]]

    entry = {
        "date": today,
        "time": datetime.now(CST).strftime("%H:%M"),
        "added": [
            {"id": s["id"], "title": s["title"], "kind": classify(s) or ""}
            for s in added
        ],
    }
    entry["added"] = [a for a in entry["added"] if a["kind"]]
    entry["removed"] = removed
    return merged, entry


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--enrich", action="store_true", help="访问详情页补全字段")
    ap.add_argument("--offline", action="store_true", help="只用缓存重建")
    ap.add_argument("--stdout", action="store_true", help="只打印摘要")
    args = ap.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    WEB_DIR.mkdir(exist_ok=True)

    session = requests.Session()
    stale = False
    html = None

    if args.offline:
        html = CACHE_FILE.read_text(encoding="utf-8") if CACHE_FILE.exists() else None
        if not html:
            log("离线模式且无缓存，退出")
            return 1
    else:
        log(f"抓取 {LIST_URL}")
        html = fetch(LIST_URL, session)
        if html:
            CACHE_FILE.write_text(html, encoding="utf-8")
        else:
            stale = True
            if CACHE_FILE.exists():
                html = CACHE_FILE.read_text(encoding="utf-8")
                log("抓取失败，改用上次缓存")
            else:
                log("抓取失败且无缓存")
                return 1

    raw = parse_section(html)
    log(f"解析到条目 {len(raw)} 条（含各分类 hot/new 列表）")
    shows = dedupe(raw)

    target_ids = [s["id"] for s in shows if classify(s)]
    log(f"命中音乐剧/话剧 {len(target_ids)} 条")

    if args.enrich and not args.offline:
        done = 0
        for s in shows:
            if s["id"] not in target_ids or done >= MAX_ENRICH:
                continue
            time.sleep(REQUEST_INTERVAL)
            det = fetch(DETAIL_URL.format(id=s["id"]), session)
            if det:
                info = parse_detail(det)
                if info.get("title"):
                    s["title"] = info["title"]
                s["venue"] = info.get("venue") or s.get("venue", "")
                s["date_text"] = info.get("date_text") or s.get("date_text", "")
                s["status"] = info.get("status", "")
                s["enriched"] = True
                done += 1
            if done % 10 == 0 and done:
                log(f"  详情补全 {done}/{min(len(target_ids), MAX_ENRICH)}")
        log(f"详情补全完成 {done} 条")

    previous = load_json(SHOWS_FILE, {"shows": []})

    # 本轮没抓详情页时，沿用上一轮已补全的字段，避免退回列表里的简略信息
    prev_map = {p["id"]: p for p in previous.get("shows", [])}
    if not args.enrich:
        carried = 0
        for s in shows:
            p = prev_map.get(s["id"])
            if not p or not p.get("enriched"):
                continue
            for f in ("venue", "date_text", "status"):
                if not s.get(f) and p.get(f):
                    s[f] = p[f]
                    s["enriched"] = True
            if p.get("enriched") and len(p.get("venue", "")) > len(s.get("venue", "")):
                s["venue"] = p["venue"]
                s["enriched"] = True
            carried += 1
        if carried:
            log(f"沿用上一轮详情字段 {carried} 条（本轮未 --enrich）")

    page_city = detect_page_city(html)
    log(f"源站当前城市：{page_city or '未识别'}")
    for s in shows:
        s["city"] = extract_city(s["title"], s.get("venue", ""), page_city)

    shows, change = apply_history(shows, previous)

    if not args.stdout:
        SHOWS_FILE.write_text(
            json.dumps({"shows": shows}, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    payload = build(shows, stale, city=page_city)

    if args.stdout:
        print(json.dumps(payload, ensure_ascii=False, indent=1)[:4000])
        return 0

    OUT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )

    history = load_json(CHANGELOG_FILE, {"entries": []})["entries"]
    if change["added"] or change["removed"] or not history:
        history.insert(0, change)
    CHANGELOG_FILE.write_text(
        json.dumps({"entries": history[:60]}, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    c = payload["counts"]
    log(
        f"完成：共 {c['total']} 条（音乐剧 {c['音乐剧']} / 话剧 {c['话剧']}），"
        f"新上架 {len(change['added'])}，下架 {len(change['removed'])} → {OUT_FILE.name}"
    )
    if stale:
        log("注意：本轮为缓存数据（页面已标记 stale）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
