#!/usr/bin/env python3
"""「展览」栏：票务双源的展览类目 + 判类。

信源与演出栏同族，只是换类目：
  * 格瓦拉生活网  cat=9「休闲展览」
  * 大麦          groupId=2370「展览」（艺术展/展览活动/二次元/集市快闪/实景互动混装）

两源里只有卖票展，免费博物馆展走 museum.py。

判类口径（与用户确认过）：
  * VR/XR、沉浸式、数字光影、IP/二次元、潮流艺术类 → 展览（本模块）
  * 确实发生在博物馆/纪念馆里的特展临展 → 博物馆，且只在「该城有馆方展讯源」时
    才归过去，否则留在展览栏（没有免费展源的城市本期不单独成表）。
  * 集市快闪、研学、娱乐休闲、儿童亲子、行业展这类不是「展」的活动直接剔除。
"""
from __future__ import annotations

import json
import re
import time
from datetime import date
from pathlib import Path

import damai
import museum

SOURCE_GW = "格瓦拉"
SOURCE_DM = "大麦"
GW_CATEGORY = 9                      # 格瓦拉「休闲展览」
DETAIL_URL = "https://show.maoyan.com/detail/{id}"

# 大麦/格瓦拉展览类目里的非展览活动
NOISE_SUBS = ("集市快闪", "研学", "娱乐休闲", "儿童亲子", "行业展", "节日庆典", "旅游")
# 明显是 DIY/摊位/夜场票一类，标题层再拦一道
NOISE_TITLE = ("摊位", "招商", "市集招募", "志愿者", "招聘", "场地租赁",
               "研学", "精讲", "导览", "讲解", "课程", "夏令营", "冬令营")

# 内容型展览：即使开在博物馆里也归「展览」栏。
# 「特展/大展」不在其中——那是馆方特展，按用户口径归博物馆栏。
VRISH = museum.VR_WORDS + ("艺术展", "美术展", "画展", "影像展", "摄影展", "装置",
                           "沉浸", "体验馆", "艺术大赏")

MUSEUM_WORDS = ("博物馆", "博物院", "纪念馆")


def classify(title: str, venue: str) -> str:
    """判「博物馆」还是「展览」。

    票务源条目要归「博物馆」栏，须同时满足：
      * 场馆是博物馆/博物院/纪念馆（美术馆按用户口径算艺术展 → 展览栏）
      * 确实是展览而非馆内其它服务：标题带「展」或「陈列」
      * 不是 VR/沉浸/数字这类内容型展览，也不是常设展（常设展不单列）
    其余全部进展览栏。
    """
    hay = f"{title} {venue}"
    if any(w in hay for w in VRISH):
        return "展览"
    if any(w in title for w in museum.PERMANENT_WORDS):
        return "展览"
    if any(w in venue for w in MUSEUM_WORDS):
        if "展" in title or "陈列" in title:
            return "博物馆"
    return "展览"


def gw_normalize(raw: dict, requested_city: str) -> dict | None:
    """格瓦拉 cat=9 条目 → 本站记录。"""
    title = (raw.get("name") or "").strip()
    pid = raw.get("performanceId")
    if not title or pid is None:
        return None
    actual = (raw.get("cityName") or "").strip()
    if raw.get("isCurrentCity") not in (1, None) and actual and actual != requested_city:
        return None
    sub = (raw.get("categoryName") or "").strip()
    if sub in NOISE_SUBS or any(w in title for w in NOISE_TITLE):
        return None

    date_text = (raw.get("showTimeRange") or "").strip()
    start, end = museum.parse_range(date_text)
    if not start:
        st = (raw.get("projectStartTime") or "")[:10]
        en = (raw.get("projectEndTime") or "")[:10]
        start = st if re.match(r"^\d{4}-\d{2}-\d{2}$", st) else ""
        end = en if re.match(r"^\d{4}-\d{2}-\d{2}$", en) else start
    if not start:
        return None

    shop = (raw.get("shopName") or "").strip()
    addr = (raw.get("address") or "").strip()
    venue = f"{shop}({addr})" if shop and addr and addr not in shop else (shop or addr)
    price = raw.get("priceRange") or ""
    lowest = raw.get("lowestPrice")
    return {
        "id": str(pid),
        "source": SOURCE_GW,
        "kind": classify(title, venue),
        "title": title,
        "venue": venue,
        "shop": shop or venue,
        "place": "",
        "city": actual or requested_city,
        "date_text": date_text or (f"{start} - {end}" if end and end != start else start),
        "date": start,
        "date_end": end or start,
        "status": "",
        "poster": (raw.get("posterUrl") or "").strip(),
        "price": str(price) if price else (str(lowest) if lowest else ""),
        "tags": [sub] if sub else [],
        "url": DETAIL_URL.format(id=pid),
        "links": [{"source": SOURCE_GW, "url": DETAIL_URL.format(id=pid)}],
    }


def dm_normalize(raw: dict, requested_city: str) -> dict | None:
    """大麦 2370 条目 → 本站记录；不复用 damai.normalize，因为那边只收话剧歌剧。"""
    title = (raw.get("name") or "").strip()
    pid = raw.get("id")
    if not title or pid is None:
        return None
    sub = (raw.get("guideSubCategoryName") or "").strip()
    if sub in NOISE_SUBS or any(w in title for w in NOISE_TITLE):
        return None
    city = requested_city or (raw.get("cityName") or "").strip()
    date_text = (raw.get("showTime") or "").strip()
    start, end = museum.parse_range(date_text)
    if not start:
        ms = raw.get("nearestPerformTime")
        if isinstance(ms, (int, float)) and ms > 0:
            start = time.strftime("%Y-%m-%d", time.localtime(ms / 1000))
            end = start
        else:
            return None
    venue = (raw.get("venueName") or "").strip()
    addr = (raw.get("logicAddress") or "").strip()
    url = damai.DETAIL_URL.format(pid)
    return {
        "id": f"dm{pid}",
        "source": SOURCE_DM,
        "kind": classify(title, venue),
        "title": title,
        "venue": f"{venue}({addr})" if venue and addr and addr not in venue else (venue or addr),
        "shop": venue,
        "place": "",
        "city": city,
        "date_text": date_text,
        "date": start,
        "date_end": end or start,
        "status": (raw.get("showStatus") or {}).get("desc") or "",
        "poster": damai.pic_url(raw.get("verticalPic") or ""),
        "price": (raw.get("priceStr") or "").strip(),
        "tags": [sub] if sub else [],
        "url": url,
        "links": [{"source": SOURCE_DM, "url": url}],
    }


def core(title: str) -> str:
    """跨源比对用的展名核心。"""
    m = re.findall(r"[《“\"']([^》”\"']{1,26})[》”\"']", title)
    if m:
        return m[0].strip().lower()
    s = re.sub(r"[【\[（(][^】\])）]{0,22}[】\])）]", "", title)
    s = re.sub(r"[\s|｜·\-—,，。.【】]+", "", s)
    return s.lower()[:20]


def merge(rows: list[dict]) -> list[dict]:
    """同城同名同起始日合成一条，保留各源入口。"""
    grouped: dict[tuple[str, str, str], dict] = {}
    for r in rows:
        key = (core(r["title"]), r["city"], r.get("date") or "")
        cur = grouped.get(key)
        if cur is None:
            item = dict(r)
            item["links"] = list(r.get("links") or [{"source": r["source"], "url": r["url"]}])
            grouped[key] = item
            continue
        for l in (r.get("links") or [{"source": r["source"], "url": r["url"]}]):
            if l not in cur["links"]:
                cur["links"].append(l)
        # 票务平台的票价/海报信息更足，馆方记录没有海报时补上
        for f in ("venue", "date_text", "status", "price", "shop", "place"):
            if len(str(r.get(f) or "")) > len(str(cur.get(f) or "")):
                cur[f] = r[f]
        if not cur.get("poster") and r.get("poster"):
            cur["poster"] = r["poster"]
        # 两条都算数时以「博物馆」为准，别让一个售票展把免费特展挤到展览栏
        if r.get("kind") == "博物馆" and cur.get("kind") != "博物馆":
            cur["kind"] = "博物馆"
    return list(grouped.values())


def gw_fetch_city(city: str, city_id: int, session, log, interval: float,
                  max_pages: int = 4) -> list[dict] | None:
    """格瓦拉某城展览类目翻页。"""
    from scrape import API_URL, API_HEADERS, API_PAGE_SIZE  # 复用同一套接口约定
    items: list[dict] = []
    for page in range(1, max_pages + 1):
        url = API_URL.format(cat=GW_CATEGORY, sort=0, page=page,
                             size=API_PAGE_SIZE, cid=city_id)
        payload = None
        for attempt in range(1, 4):
            try:
                r = session.get(url, timeout=25, headers=API_HEADERS)
                if "rgv587" in r.text or "punish" in r.url:
                    log(f"  格瓦拉·{city}：被风控拦截")
                    return None
                if r.status_code == 200:
                    body = r.json()
                    if body.get("code") == 200 or body.get("success"):
                        payload = body
                        break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(interval * attempt)
        if payload is None:
            return None if page == 1 else items
        batch = payload.get("data") or []
        items += batch
        if not (payload.get("paging") or {}).get("hasMore") or not batch:
            break
        time.sleep(interval)
    return items


def scrape(cities: list[str], city_ids: dict[str, int], session, cache_dir: Path,
          log=lambda *_: None, offline: bool = False,
          interval: float = 1.0, museum_cities: set[str] | None = None) -> tuple[list[dict], bool]:
    """抓两源展览类目并判类。返回 (记录, 是否有源失败)。

    museum_cities：有馆方展讯源的城市。落在这些城市的博物馆售票展归「博物馆」栏，
    其余城市的展览一律留在「展览」栏。
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    failed = False

    for i, name in enumerate(cities):
        cid = city_ids.get(name)
        if not cid:
            continue
        cache = cache_dir / f"expo_gw_{name}.json"
        raw = None
        if not offline:
            raw = gw_fetch_city(name, cid, session, log, interval)
            if raw:
                cache.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        if raw is None:
            if cache.exists():
                raw = json.loads(cache.read_text(encoding="utf-8"))
                if not offline:
                    log(f"  格瓦拉·{name}：抓取失败，改用缓存 {len(raw)} 条")
                    failed = True
            else:
                if not offline:
                    log(f"  格瓦拉·{name}：抓取失败且无缓存")
                    failed = True
                raw = []
        hits = 0
        for item in raw:
            if item.get("isCurrentCity") is None:
                item = {**item, "cityName": item.get("cityName") or name}
            rec = gw_normalize(item, name)
            if rec:
                rows.append(rec)
                hits += 1
        log(f"  格瓦拉·展览·{name}：{len(raw)} 条 → 收录 {hits} 条")
        if not offline and i < len(cities) - 1:
            time.sleep(interval)

    m = damai.Mtop(interval=min(0.8, interval))
    for name in cities:
        cache = cache_dir / f"expo_dm_{name}.json"
        raw = None
        if not offline:
            raw = damai.fetch_city(name, m, log, group=damai.EXPO_GROUP)
            if raw:
                cache.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        if raw is None:
            if cache.exists():
                raw = json.loads(cache.read_text(encoding="utf-8"))
                if not offline:
                    log(f"  大麦·展览·{name}：抓取失败，改用缓存 {len(raw)} 条")
                    failed = True
            else:
                if not offline:
                    log(f"  大麦·展览·{name}：抓取失败且无缓存")
                    failed = True
                raw = []
        hits = 0
        for item in raw:
            rec = dm_normalize(item, name)
            if rec:
                rows.append(rec)
                hits += 1
        log(f"  大麦·展览·{name}：{len(raw)} 条 → 收录 {hits} 条")

    merged = merge(rows)
    covered = museum_cities or set()
    for r in merged:
        if r["kind"] == "博物馆" and r["city"] not in covered:
            r["kind"] = "展览"        # 该城没有馆方展讯源，不单列博物馆栏
    return merged, failed


if __name__ == "__main__":
    import scrape as sc

    log = lambda *a: print(*a, flush=True)
    s = sc.make_session()
    recs, bad = scrape(sc.DEFAULT_CITIES, sc.CITIES, s,
                       Path(__file__).resolve().parent.parent / "data" / "pages", log,
                       interval=0.6, museum_cities={"南京", "上海", "苏州", "扬州", "徐州", "杭州"})
    print(f"展览栏合计 {len(recs)} 条，失败={bad}")
    for r in sorted(recs, key=lambda x: (x["kind"], x["city"]))[:10]:
        print(f"  {r['kind']} {r['city']} | {r['title'][:26]} | {r['venue'][:18]} "
              f"| {r['date_text'][:22]} | ¥{r['price']} | {len(r['links'])}源")
