#!/usr/bin/env python3
"""
音乐剧 / 话剧 演出信息抓取（江浙沪多城市）

数据源：格瓦拉生活网（show.maoyan.com）的 /list/{categoryId} 页面。
该页面是 Next.js 服务端渲染，__NEXT_DATA__ 里带有结构化 JSON，
因此无需浏览器、无需签名接口即可拿到演出标题、场馆、地址、档期、
票价区间、销售状态和海报。

城市切换：SSR 读取 currentCity cookie（值为 JSON 字符串），
    currentCity={"id": 50, "nm": "杭州", "py": "hangzhou"}
不带该 cookie 时源站固定返回上海。

用法：
    python3 scrape.py                 # 抓取全部城市并更新 web/data.json
    python3 scrape.py --cities 上海,杭州
    python3 scrape.py --offline       # 不发请求，用缓存重建
    python3 scrape.py --stdout        # 打印摘要，不落盘

设计约束：
  * 礼貌抓取：串行、带延时（默认 1.2s/请求），失败保留上一次数据并标记 stale。
  * 只聚合公开元信息，不下载图片文件；海报走源站 CDN 外链，购票跳转源站。
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
from urllib.parse import quote

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
WEB_DIR = BASE_DIR / "web"
CACHE_DIR = DATA_DIR / "pages"
SHOWS_FILE = DATA_DIR / "shows.json"
CHANGELOG_FILE = DATA_DIR / "changelog.json"
OUT_FILE = WEB_DIR / "data.json"

SOURCE_NAME = "格瓦拉生活网"
DETAIL_URL = "https://show.maoyan.com/detail/{id}"
LIST_URL = "https://show.maoyan.com/list/{cat}"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)
CST = timezone(timedelta(hours=8))

# 源站分类：4 = 话剧/歌剧，是本次唯一需要的类目
DRAMA_CATEGORY = 4

REQUEST_INTERVAL = float(os.environ.get("TW_REQUEST_INTERVAL", "1.2"))

# 默认抓取范围：江浙沪地级市。县级市（昆山/慈溪等）多数无独立场馆数据，
# 且其演出会出现在所属地级市结果里，故不单列。
CITIES = {
    "上海": 10,
    "杭州": 50,
    "宁波": 51,
    "无锡": 52,
    "南京": 55,
    "温州": 112,
    "徐州": 119,
    "扬州": 120,
    "盐城": 181,
    "镇江": 182,
    "泰州": 183,
    "湖州": 186,
    "绍兴": 187,
    "嘉兴": 185,
    "金华": 188,
    "衢州": 189,
    "舟山": 190,
    "台州": 191,
    "丽水": 192,
    "常州": 89,
    "苏州": 80,
    "南通": 82,
}
DEFAULT_CITIES = [
    "上海", "杭州", "宁波", "苏州", "南京", "无锡", "常州", "南通",
    "嘉兴", "绍兴", "金华", "温州", "扬州", "镇江", "泰州", "徐州",
]

# 销售状态码 → 文案（取自源站前端映射）
TICKET_STATUS = {
    1: "即将开售", 2: "预售", 3: "在售中", 4: "已售罄",
    5: "已结束", 11: "暂不可售", 12: "演出延期",
}

MUSICAL_WORDS = ("音乐剧", "歌舞剧", "musical", "轻歌剧", "歌剧", "音乐剧场")
DRAMA_WORDS = (
    "话剧", "舞台剧", "戏剧", "独角戏", "默剧", "肢体剧",
    "环境戏剧", "先锋剧", "读剧本", "剧社", "小剧场",
)
# 硬噪声：脱口秀、魔术秀、VR 体验、演唱会、游戏剧场等
STRONG_NOISE = (
    "脱口秀", "漫才", "魔术", "VR", "剧本杀", "密室", "演唱会", "音乐会",
    "音乐节", "livehouse", "相声", "评弹", "鼓书", "杂技", "马戏",
    "展览", "市集", "工作坊", "体验", "通票", "游园", "电竞", "游戏",
    "高清放映", "影院直播", "影像放映",
)
# 戏曲、芭蕾等属其它舞台门类，本次只做音乐剧/话剧
OTHER_STAGE = ("京剧", "昆曲", "越剧", "豫剧", "黄梅戏", "评剧", "川剧", "沪剧",
               "粤剧", "秦腔", "梆子", "梨园", "戏曲", "芭蕾", "室内乐")
ORCHESTRA_NOISE = ("乐团", "协奏曲", "交响", "独奏", "钢琴", "小提琴")


def log(msg: str) -> None:
    print(f"[{datetime.now(CST):%H:%M:%S}] {msg}", flush=True)


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://show.maoyan.com/",
    })
    return s


def city_cookie(city_id: int, city_name: str) -> str:
    # 源站前端用 JSON.parse(cookie).id，因此值必须是合法 JSON 字符串
    return json.dumps({"id": city_id, "nm": city_name, "py": ""}, ensure_ascii=False)


def fetch(url: str, session: requests.Session, city_id: int | None = None,
          city_name: str = "", retries: int = 3) -> str | None:
    """带重试的 GET；失败返回 None 而不抛异常。"""
    headers = {}
    if city_id is not None:
        headers["Cookie"] = f"currentCity={quote(city_cookie(city_id, city_name))}"
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, timeout=25, headers=headers)
            if "rgv587" in r.text or "punish" in r.url:
                log(f"  被风控拦截: {url}")
                return None
            if r.status_code == 200 and "__NEXT_DATA__" in r.text:
                return r.text
            log(f"  HTTP {r.status_code} / {len(r.text)}b，重试 {attempt}/{retries}")
        except requests.RequestException as e:
            log(f"  请求异常 {type(e).__name__}: {e}，重试 {attempt}/{retries}")
        time.sleep(REQUEST_INTERVAL * attempt)
    return None


def extract_next_data(html: str) -> dict | None:
    """取 <script> 里的 __NEXT_DATA__，用括号配平定位边界。"""
    i = html.find("__NEXT_DATA__")
    if i < 0:
        return None
    s = html.find("{", i)
    if s < 0:
        return None
    depth, instr, esc = 0, False, False
    for j in range(s, len(html)):
        c = html[j]
        if instr:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                instr = False
            continue
        if c == '"':
            instr = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[s:j + 1])
                except json.JSONDecodeError:
                    return None
    return None


def parse_list_page(html: str) -> list[dict]:
    """从 /list/{cat} 的 SSR 数据里取出演出条目。"""
    data = extract_next_data(html)
    if not data:
        return []
    try:
        props = data["props"]["pageProps"]
    except (KeyError, TypeError):
        return []
    items = props.get("serverProducts")
    if isinstance(items, list):
        return items
    # 兜底：首页结构里条目挂在 categoryList 下
    out = []
    for cat in props.get("categoryList", []) or []:
        for key, val in cat.items():
            if isinstance(val, list) and val and isinstance(val[0], dict):
                out.extend(val)
    return out


def classify(title: str) -> str | None:
    """判定门类，返回 '音乐剧' / '话剧' / None（不相关）。"""
    low = title.lower()

    has_opera = "歌剧" in low and "歌剧院" not in low
    explicit_musical = ("音乐剧" in low) or has_opera or ("歌舞剧" in low)
    explicit_drama = any(w in low for w in ("话剧", "舞台剧", "独角戏", "肢体剧", "环境戏剧"))

    if any(w.lower() in low for w in STRONG_NOISE):
        return None
    if any(w in low for w in OTHER_STAGE):
        return None
    # 标题带「××歌剧院」的古典乐团巡演不算歌剧
    if any(w in low for w in ORCHESTRA_NOISE) and not (explicit_musical or explicit_drama):
        return None
    if "舞剧" in low and not explicit_musical:
        return None
    if explicit_musical:
        return "音乐剧"
    if explicit_drama or any(w in low for w in DRAMA_WORDS):
        return "话剧"
    return None


def parse_dates(text: str) -> tuple[str, str]:
    """'2026.11.19 - 11.22' / '2026.10.01 - 2026.10.31' → (起始, 结束) ISO。"""
    t = (text or "").strip()
    found = re.findall(r"(20\d{2})[.\-/年](\d{1,2})[.\-/月](\d{1,2})", t)
    if not found:
        return "", ""

    def iso(y, m, d):
        try:
            return datetime(int(y), int(m), int(d)).strftime("%Y-%m-%d")
        except ValueError:
            return ""

    start = iso(*found[0])
    if not start:
        return "", ""
    if len(found) >= 2:
        end = iso(*found[-1])
        return start, (end if end >= start else start)
    # 只写了「11.22」形式的结束日
    m2 = re.search(r"(\d{1,2})[.\-/月](\d{1,2})\s*$", t)
    if m2:
        y = start[:4]
        end = iso(y, m2.group(1), m2.group(2))
        if end and end >= start:
            return start, end
    return start, start


def days_left(iso_date: str) -> int | None:
    if not iso_date:
        return None
    try:
        d = datetime.strptime(iso_date, "%Y-%m-%d").date()
    except ValueError:
        return None
    return (d - datetime.now(CST).date()).days


def normalize(raw: dict, requested_city: str) -> dict | None:
    """把源站条目转成本地记录；非音乐剧/话剧返回 None。"""
    title = (raw.get("name") or "").strip()
    if not title or raw.get("performanceId") is None:
        return None
    kind = classify(title)
    if not kind:
        return None

    # 小城市会回落到邻市数据，只保留确实属于本次请求城市的条目
    actual = (raw.get("cityName") or "").strip()
    if raw.get("isCurrentCity") not in (1, None) and actual and actual != requested_city:
        return None

    pid = str(raw["performanceId"])
    date_text = (raw.get("showTimeRange") or "").strip()
    start, end = parse_dates(date_text)
    if not start:
        st = (raw.get("projectStartTime") or "")[:10]
        en = (raw.get("projectEndTime") or "")[:10]
        start = st if re.match(r"^\d{4}-\d{2}-\d{2}$", st) else ""
        end = en if re.match(r"^\d{4}-\d{2}-\d{2}$", en) else start

    shop = (raw.get("shopName") or "").strip()
    addr = (raw.get("address") or "").strip()
    venue = f"{shop}({addr})" if shop and addr and addr not in shop else (shop or addr)

    price = raw.get("priceRange") or ""
    lowest = raw.get("lowestPrice")

    return {
        "id": pid,
        "kind": kind,
        "title": title,
        "venue": venue,
        "shop": shop,
        "city": actual or requested_city,
        "city_id": raw.get("cityId"),
        "date_text": date_text or (f"{start} - {end}" if end and end != start else start),
        "date": start,
        "date_end": end or start,
        "status": TICKET_STATUS.get(raw.get("ticketStatus"), ""),
        "poster": (raw.get("posterUrl") or "").strip(),
        "price": str(price) if price else (str(lowest) if lowest else ""),
        "tags": [t for t in (raw.get("normalTags") or []) if isinstance(t, str)][:3],
    }


def dedupe(records: list[dict]) -> list[dict]:
    """按 id 合并，保留信息更全的字段。"""
    merged: dict[str, dict] = {}
    for r in records:
        cur = merged.get(r["id"])
        if cur is None:
            merged[r["id"]] = dict(r)
            continue
        for f in ("venue", "date_text", "status", "poster", "price"):
            if len(str(r.get(f) or "")) > len(str(cur.get(f) or "")):
                cur[f] = r[f]
    return list(merged.values())


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def apply_history(current: list[dict], previous: dict) -> tuple[list[dict], dict]:
    """标记新上架/已下架，生成 changelog 条目。"""
    prev = {s["id"]: s for s in previous.get("shows", [])}
    today = datetime.now(CST).strftime("%Y-%m-%d")

    for s in current:
        old = prev.get(s["id"])
        s["first_seen"] = old.get("first_seen", today) if old else today
        s["is_new"] = old is None

    cur_ids = {s["id"] for s in current}
    entry = {
        "date": today,
        "time": datetime.now(CST).strftime("%H:%M"),
        "added": [{"id": s["id"], "title": s["title"], "kind": s["kind"], "city": s["city"]}
                  for s in current if s["is_new"]],
        "removed": [{"id": i, "title": s["title"], "kind": s.get("kind", ""),
                     "city": s.get("city", "")} for i, s in prev.items() if i not in cur_ids],
    }
    return current, entry


def build(shows: list[dict], stale: bool, cities: list[str], note: str = "") -> dict:
    now = datetime.now(CST)
    today = now.date()
    out = []
    for s in shows:
        # 过滤已闭幕（结束日早于今天）
        if s.get("date_end"):
            try:
                if datetime.strptime(s["date_end"], "%Y-%m-%d").date() < today:
                    continue
            except ValueError:
                pass
        item = dict(s)
        item["days"] = days_left(s.get("date", ""))
        item["url"] = DETAIL_URL.format(id=s["id"])
        out.append(item)

    out.sort(key=lambda x: (x["date"] or "9999-12-31", x["city"], x["kind"]))
    return {
        "source": SOURCE_NAME,
        "source_url": LIST_URL.format(cat=DRAMA_CATEGORY),
        "updated_at": now.strftime("%Y-%m-%d %H:%M CST"),
        "updated_ts": int(now.timestamp()),
        "stale": stale,
        "note": note,
        "cities": cities,
        "counts": {
            "total": len(out),
            "音乐剧": sum(1 for x in out if x["kind"] == "音乐剧"),
            "话剧": sum(1 for x in out if x["kind"] == "话剧"),
            "new": sum(1 for x in out if x["is_new"]),
            "cities": len({x["city"] for x in out}),
        },
        "shows": out,
    }


def scrape_cities(cities: list[str], offline: bool) -> tuple[list[dict], bool]:
    """逐城抓取。返回 (记录列表, 是否降级为缓存)。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    session = make_session()
    records: list[dict] = []
    failed = 0

    for i, name in enumerate(cities):
        cid = CITIES[name]
        url = LIST_URL.format(cat=DRAMA_CATEGORY)
        cache = CACHE_DIR / f"{name}.json"

        html = None
        if not offline:
            html = fetch(url, session, city_id=cid, city_name=name)
        if html:
            raw = parse_list_page(html)
            cache.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        elif cache.exists():
            raw = json.loads(cache.read_text(encoding="utf-8"))
            log(f"  {name}：抓取失败，改用缓存 {len(raw)} 条")
            failed += 1
        else:
            log(f"  {name}：抓取失败且无缓存")
            failed += 1
            raw = []

        hits = 0
        for item in raw:
            # 首页兜底结构没有 isCurrentCity，按请求城市标注
            if item.get("isCurrentCity") is None:
                item = {**item, "cityName": item.get("cityName") or name}
            rec = normalize(item, name)
            if rec:
                records.append(rec)
                hits += 1
        log(f"  {name}：{len(raw)} 条 → 音乐剧/话剧 {hits} 条")
        if not offline and i < len(cities) - 1:
            time.sleep(REQUEST_INTERVAL)

    return records, failed > 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cities", help="逗号分隔的城市名，默认江浙沪主要城市")
    ap.add_argument("--offline", action="store_true", help="只用缓存重建")
    ap.add_argument("--stdout", action="store_true", help="只打印不落盘")
    args = ap.parse_args()

    cities = ([c.strip() for c in args.cities.split(",") if c.strip()]
              if args.cities else list(DEFAULT_CITIES))
    unknown = [c for c in cities if c not in CITIES]
    if unknown:
        log(f"未知城市（源站无此 ID）：{unknown}，已忽略")
        cities = [c for c in cities if c in CITIES]
    if not cities:
        log("没有可抓取的城市")
        return 1

    log(f"抓取 {len(cities)} 个城市：{'、'.join(cities)}")
    records, degraded = scrape_cities(cities, args.offline)

    shows = dedupe(records)
    log(f"合并去重后 {len(shows)} 条")

    previous = load_json(SHOWS_FILE, {"shows": []})
    shows, change = apply_history(shows, previous)
    payload = build(shows, degraded, cities)

    if args.stdout:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        return 0

    DATA_DIR.mkdir(exist_ok=True)
    SHOWS_FILE.write_text(json.dumps({"shows": shows}, ensure_ascii=False, indent=1),
                          encoding="utf-8")
    WEB_DIR.mkdir(exist_ok=True)
    OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8")

    history = load_json(CHANGELOG_FILE, {"entries": []})["entries"]
    if change["added"] or change["removed"] or not history:
        history.insert(0, change)
    CHANGELOG_FILE.write_text(json.dumps({"entries": history[:60]}, ensure_ascii=False, indent=1),
                              encoding="utf-8")

    c = payload["counts"]
    log(f"完成：{c['total']} 场（音乐剧 {c['音乐剧']} / 话剧 {c['话剧']}），"
        f"覆盖 {c['cities']} 城，新上架 {len(change['added'])}，下架 {len(change['removed'])}")
    if degraded:
        log("注意：部分城市抓取失败，已使用缓存数据")
    return 0


if __name__ == "__main__":
    sys.exit(main())
