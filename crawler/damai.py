#!/usr/bin/env python3
"""大麦（阿里/淘系）信源适配。

与格瓦拉（美团系）是两家独立平台，重合度约三成，接进来能明显扩盘。

走的是大麦 H5 分类页调用的 mtop 接口：
    mtop.damai.wireless.search.project.classify
参数逆向自前端包 g.alicdn.com/alipay-movie-client/show-h5-next/*/*.js，
关键点是 groupId=2333（话剧歌剧类目）+ currentCityId（大麦自己的一套城市 ID）。

签名协议（淘系通用）：
    sign = md5(f"{_m_h5_tk 的前半段}&{t}&{appKey}&{data}")
首次请求没有 token 会回 ERR_TOKEN，同时通过 Set-Cookie 下发 _m_h5_tk，
带 token 重签一次即成功，所以同一个 cookiejar 要复用。

注意：mtop.damai.wireless.search.search 在云出口 IP 上会被风控拦
（RGV587），本模块用的是 project.classify，实测可正常返回。
"""
from __future__ import annotations

import hashlib
import http.cookiejar
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

SOURCE = "大麦"
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
APPKEY = "12574478"
CLASSIFY_API = "mtop.damai.wireless.search.project.classify"
DRAMA_GROUP = 2333          # 大麦类目「话剧歌剧」
EXPO_GROUP = 2370           # 大麦类目「展览」（艺术展/展览活动/二次元/集市快闪/实景互动混在一起）
DETAIL_URL = "https://m.damai.cn/shows/item.html?id={}"
PAGE_SIZE = 100
MAX_PAGES = 8               # 800 条上限，上海 365 条够用

# 大麦城市 ID：由 mtop.damai.wireless.cities.query 取回后固化，
# 与美团系（格瓦拉）那套 ID 完全不同，别混用
CITY_IDS = {
    "上海": 872, "杭州": 1580, "南京": 1038, "苏州": 1087, "宁波": 1597,
    "无锡": 1052, "常州": 1077, "南通": 1103, "温州": 1612, "徐州": 1063,
    "扬州": 1137, "盐城": 1125, "镇江": 1148, "泰州": 1158, "嘉兴": 1626,
    "湖州": 1637, "绍兴": 1643, "金华": 1653, "衢州": 1667, "舟山": 1675,
    "台州": 1680, "丽水": 1691,
}

# 大麦子类目 → 本站门类。只列出的才算数，其余（舞剧/芭蕾/现代舞/剧场放映/
# 儿童亲子/戏曲）一律排除——大麦这个 groupId 里混了不少舞蹈类，用黑名单会漏。
SUB_KIND = {
    "音乐剧": "音乐剧", "歌剧": "音乐剧",
    "话剧": "话剧", "新喜剧": "话剧", "沉浸式": "话剧",
}
# 子类目本身写作「话剧音乐剧」时判断不了，退回按标题分
AMBIGUOUS_SUB = ("话剧音乐剧",)
# 大麦的子类目偶尔把舞剧/越剧归进「沉浸式」，标题层再拦一道，口径与格瓦拉侧一致
TITLE_EXCLUDE = ("舞剧", "芭蕾", "京剧", "昆曲", "越剧", "豫剧", "黄梅戏", "评剧",
                 "川剧", "沪剧", "粤剧", "秦腔", "戏曲", "脱口秀", "相声", "音乐会",
                 "音乐节", "演唱会", "杂技", "马戏")

BLOCK_SIGNS = ("rgv587", "punish", "bixi.alicdn", "unusual traffic",
               "x5secdata", "_____tmd_____", "哎哟喂")


class Mtop:
    """带令牌握手的 mtop 客户端；失败不抛异常，返回 (ret, data)。"""

    def __init__(self, interval: float = 0.8) -> None:
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))
        self.interval = interval

    def _token(self) -> str:
        for c in self.cj:
            if c.name == "_m_h5_tk":
                return c.value.split("_")[0]
        return ""

    def call(self, api: str, data: dict, ver: str = "1.0", retries: int = 4):
        body = json.dumps(data, separators=(",", ":"))
        ret = ""
        for attempt in range(retries):
            t = str(int(time.time() * 1000))
            sign = hashlib.md5(f"{self._token()}&{t}&{APPKEY}&{body}".encode()).hexdigest()
            qs = urllib.parse.urlencode({
                "jsv": "2.6.1", "appKey": APPKEY, "t": t, "sign": sign,
                "api": api, "v": ver, "type": "originaljson", "dataType": "json", "data": body})
            url = f"https://mtop.damai.cn/h5/{api.lower()}/{ver}/?{qs}"
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Referer": "https://m.damai.cn/",
                "Accept": "*/*", "Accept-Language": "zh-CN"})
            try:
                with self.opener.open(req, timeout=25) as r:
                    text = r.read(2_000_000).decode("utf-8", "ignore")
            except Exception as e:  # noqa: BLE001
                ret = f"NETERR {type(e).__name__}: {e}"
                time.sleep(self.interval * (attempt + 1))
                continue
            if next((s for s in BLOCK_SIGNS if s in text.lower()), None):
                return "BLOCKED", None
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                ret = text[:80]
                time.sleep(self.interval)
                continue
            ret = str((obj.get("ret") or [""])[0])
            if "SUCCESS" in ret:
                return ret, obj.get("data") or {}
            # ret 含 TOKEN 时服务端已通过 Set-Cookie 下发新令牌，直接重试即可
            time.sleep(self.interval)
        return ret, None


def pic_url(raw: str) -> str:
    """大麦的 verticalPic 是 bao/uploaded/ 前缀拼真图地址，去掉前缀更短更稳。"""
    s = (raw or "").strip()
    if not s:
        return ""
    m = re.search(r"(https?://[^/]+/bao/uploaded/)(https?/.*)$", s)
    return m.group(2) if m else s


def normalize(raw: dict, requested_city: str) -> dict | None:
    """大麦条目 → 本站记录结构；非音乐剧/话剧返回 None。"""
    title = (raw.get("name") or "").strip()
    pid = raw.get("id")
    if not title or pid is None:
        return None

    sub = (raw.get("guideSubCategoryName") or "").strip()
    # 「歌舞剧」含「舞剧」子串，先摘掉再判排除，避免误杀
    probe_title = title.replace("歌舞剧", "")
    if any(w in probe_title for w in TITLE_EXCLUDE):
        return None
    kind = SUB_KIND.get(sub)
    if kind is None:
        if sub not in AMBIGUOUS_SUB:
            return None
        # 少数条目标作「话剧音乐剧」，按标题再判一次
        kind = "音乐剧" if ("音乐剧" in title or "歌剧" in title) else "话剧"

    # 大麦把昆山/常熟/张家港这类县级市单独标名，但 cityId 仍属所查地级市；
    # 统一归到查询城市，县级市名保留在场馆字段里
    city = requested_city or (raw.get("cityName") or "").strip()
    date_text = (raw.get("showTime") or "").strip()
    start, end = _dates(date_text, raw)
    if not start:
        return None

    venue = (raw.get("venueName") or "").strip()
    addr = (raw.get("logicAddress") or "").strip()
    return {
        "id": f"dm{pid}",
        "source": SOURCE,
        "kind": kind,
        "title": title,
        "venue": f"{venue}({addr})" if venue and addr and addr not in venue else (venue or addr),
        "shop": venue,
        "city": city,
        "date_text": date_text,
        "date": start,
        "date_end": end or start,
        "status": (raw.get("showStatus") or {}).get("desc") or "",
        "poster": pic_url(raw.get("verticalPic") or ""),
        "price": (raw.get("priceStr") or "").strip(),
        "tags": [t for t in (raw.get("commonTags") or []) if isinstance(t, str)][:3],
        "url": DETAIL_URL.format(pid),
    }


def _dates(show_time: str, raw: dict) -> tuple[str, str]:
    """'2026.09.29-12.27' → ('2026-09-29','2026-12-27')；解析失败退时间戳。"""
    found = re.findall(r"(20\d{2})[.\-/年](\d{1,2})[.\-/月](\d{1,2})", show_time or "")
    def iso(y, mo, d):
        try:
            return time.strftime("%Y-%m-%d", (int(y), int(mo), int(d), 0, 0, 0, 0, 0, -1))
        except ValueError:
            return ""
    if found:
        start = iso(*found[0])
        end = iso(*found[-1]) if len(found) > 1 else start
        if start:
            return start, (end if end >= start else start)
    ms = raw.get("nearestPerformTime")
    if isinstance(ms, (int, float)) and ms > 0:
        return time.strftime("%Y-%m-%d", time.localtime(ms / 1000)), ""
    return "", ""


def fetch_city(city: str, m: Mtop, log=lambda *_: None,
               group: int = DRAMA_GROUP) -> list[dict] | None:
    """翻页取某城指定类目全量；None 表示失败（区别于 0 条）。"""
    cid = CITY_IDS.get(city)
    if not cid:
        return None
    out: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        data = {"currentCityId": cid, "cityOption": 0, "pageIndex": page,
                "pageSize": PAGE_SIZE, "sortType": 10, "returnItemOption": 4,
                "dateType": 0, "groupId": group}
        ret, body = m.call(CLASSIFY_API, data)
        if body is None:
            return None if page == 1 else (out or None)
        batch = body.get("currentCity") or []
        out.extend(batch)
        total = body.get("total") or 0
        if not batch or len(out) >= total:
            break
        time.sleep(m.interval)
    return out


def scrape(cities: list[str], cache_dir: Path, log=lambda *_: None,
          offline: bool = False, interval: float = 0.8) -> tuple[list[dict], bool]:
    """抓取全部城市并落缓存。返回 (记录列表, 是否有城市失败)。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    m = Mtop(interval=interval)
    records: list[dict] = []
    failed = False
    for name in cities:
        cache = cache_dir / f"damai_{name}.json"
        raw = None
        if not offline:
            raw = fetch_city(name, m, log)
            if raw:
                cache.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        if raw is None:
            if cache.exists():
                raw = json.loads(cache.read_text(encoding="utf-8"))
                if not offline:
                    log(f"  大麦·{name}：抓取失败，改用缓存 {len(raw)} 条")
                    failed = True
            else:
                if not offline:
                    log(f"  大麦·{name}：抓取失败且无缓存")
                    failed = True
                raw = []
        hits = 0
        for item in raw:
            rec = normalize(item, name)
            if rec:
                records.append(rec)
                hits += 1
        log(f"  大麦·{name}：{len(raw)} 条 → 音乐剧/话剧 {hits} 条")
    return records, failed


if __name__ == "__main__":
    import sys

    def p(msg):
        print(msg, flush=True)

    cs = (sys.argv[1].split(",") if len(sys.argv) > 1 else ["上海"])
    recs, bad = scrape(cs, Path(__file__).resolve().parent.parent / "data" / "pages", p)
    p(f"大麦合计 {len(recs)} 条，失败={bad}")
    for r in recs[:6]:
        p(f"  {r['city']} {r['kind']} {r['title'][:26]} | {r['date']} | {r['price']} | {r['venue'][:20]}")
