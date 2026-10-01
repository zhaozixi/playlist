#!/usr/bin/env python3
"""博物馆官网「特展 / 临展」信源（免费展只能逐馆抓，票务平台不收录）。

已实测可用的 6 个馆（2026-10 调研）：
  南京博物院      JSON  api/exhibition/list?pageNum=1&pageSize=100
  上海博物馆      JSON  …/pg/display/search-exhibit（接口不含票价，详情页也无票价字段）
  苏州博物馆      HTML  /Exhibition/Temporary?startYear=YYYY
  扬州中国大运河博物馆  HTML  /linzhantezhan.html（展期、地点写在简介正文里）
  徐州博物馆      HTML  /zl_list.aspx?category_id=496（列表无展期，需逐条进详情）
  中国丝绸博物馆  HTML  /yz/list_18.aspx + /jzNX/list_19.aspx
                      注意：它的「在展」页其实是基本陈列，按口径必须跳过

外加国家文物局「看展览｜博物馆展讯速览」做补录源：结构最规整但约每月一期，
用来兜住那些没有临展接口的馆（例如良渚博物院的特展只出现在新闻稿里）。

口径（与用户确认过）：
  * 常设展 / 基本陈列一律不收；已闭幕的不收。
  * 只收录上面这些「有官网展讯源」的城市，其余城市在博物馆栏不显示。
  * VR/XR、数字展、沉浸展按内容归「展览」栏，场馆只当地点。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.request
from datetime import date, timedelta
from html import unescape
from pathlib import Path

SOURCE = "馆方官网"
NCHA_SOURCE = "文物局展讯"
# 有免费展讯源、因此「博物馆」栏单独成表的城市（其余城市的博物馆售票展
# 按用户口径留在「展览」栏，16 个无源城市不单列）。
# 徐州：官网临展接口可用，只是本轮恰好全展完，coverage 仍算它。
COVERED_CITIES = {"南京", "上海", "苏州", "扬州", "徐州", "杭州"}
UA_TEXT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")
AJAX_HEADERS = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
INTERVAL = 0.8

# 江苏 / 浙江 / 上海 的地级市名，用于从聚合源的文本里认城市
JZH_CITIES = ("上海", "南京", "苏州", "无锡", "常州", "南通", "扬州", "镇江", "泰州",
              "徐州", "盐城", "杭州", "宁波", "温州", "嘉兴", "湖州", "绍兴", "金华",
              "衢州", "舟山", "台州", "丽水")

# 聚合源里只给馆名不给城市，靠这张表认江浙沪的馆；表外的直接丢弃，
# 免得把「长沙博物馆」这类误判进来
CITY_BY_MUSEUM = {
    "南京博物院": "南京", "苏州博物馆": "苏州", "无锡博物院": "无锡", "常州博物馆": "常州",
    "南通博物苑": "南通", "扬州博物馆": "扬州", "镇江博物馆": "镇江", "泰州博物馆": "泰州",
    "徐州博物馆": "徐州", "盐城博物馆": "盐城", "连云港博物馆": "连云港",
    "扬州市中国大运河博物馆": "扬州", "扬州中国大运河博物馆": "扬州",
    "南京大屠杀": "南京", "南京市博物总馆": "南京", "太平天国历史博物馆": "南京",
    "南京城墙博物馆": "南京", "六朝博物馆": "南京", "江宁织造博物馆": "南京",
    "上海博物馆": "上海", "上海市历史博物馆": "上海", "中国航海博物馆": "上海",
    "浙江省博物馆": "杭州", "浙江自然博物院": "杭州", "良渚博物院": "杭州",
    "杭州博物馆": "杭州", "中国丝绸博物馆": "杭州", "中国动漫博物馆": "杭州",
    "中国湿地博物馆": "杭州", "南宋德寿宫遗址博物馆": "杭州", "杭州工艺美术博物馆": "杭州",
    "宁波博物院": "宁波", "宁波博物馆": "宁波", "温州博物馆": "温州",
    "嘉兴博物馆": "嘉兴", "湖州市博物馆": "湖州", "绍兴市博物馆": "绍兴",
    "金华博物馆": "金华", "衢州博物馆": "衢州", "舟山博物馆": "舟山",
    "台州博物馆": "台州", "丽水市博物馆": "丽水", "叶浅予艺术馆": "桐庐",
}

# 判给「展览」栏的关键词（VR/数字/沉浸式这类内容型展览）
VR_WORDS = ("VR", "vr", "XR", "xr", "沉浸式", "沉浸体验", "数字展", "光影", "全息",
            "裸眼3D", "元宇宙", "科创展", "动画", "动漫", "漫画", "潮玩", "手办", "IP")
PERMANENT_WORDS = ("常设", "基本陈列", "固定陈列", "常驻")


def fetch(url: str, referer: str = "", ajax: bool = False, retries: int = 3) -> str | None:
    headers = {"User-Agent": UA_TEXT, "Accept-Language": "zh-CN,zh;q=0.9"}
    if referer:
        headers["Referer"] = referer
    if ajax:
        headers.update(AJAX_HEADERS)
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read(3_000_000)
            for enc in ("utf-8", "gbk"):
                try:
                    return raw.decode(enc)
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", "ignore")
        except Exception as e:  # noqa: BLE001 - 单馆失败不能拖垮整轮
            if attempt == retries:
                print(f"    {url.split('/')[2]} 抓取失败 {type(e).__name__}: {e}", flush=True)
                return None
            time.sleep(INTERVAL * attempt)
    return None


def text_of(html: str) -> str:
    s = unescape(re.sub(r"<[^>]+>", " ", html or ""))
    return re.sub(r"\s+", " ", s).strip()


def to_iso(y, m, d) -> str:
    try:
        return date(int(y), int(m), int(d)).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def parse_range(text: str, default_year: int | None = None) -> tuple[str, str]:
    """展期字符串 → ISO 起止。

    馆方写法五花八门，同一句里可能混着带年份和不带年份的：
      '2026.8.15 - 2026.11.15'  '2026年9月29日（周二） - 11月01日（周日）'
      '2026.9.25-2027.1.3'      '2026年7月15日开幕'   '9.12—11.29'
    所以按出现顺序抓「带年」和「只有月日」两种 token：首 token 定开始，
    末 token 定结束；末 token 缺年份时沿用开始年的，若反而早于开始就 +1 年。
    """
    # 截到「地点/位置/展厅/购票」之前，否则 "11-12号展厅" 会被当成日期
    cut = re.split(r"(?:展览地\s*点|展览位\s*置|地\s*点|位\s*置|在\s*线\s*购\s*票)", text or "")[0]
    tokens = [m.groups() for m in re.finditer(
        r"(?:(20\d{2})[.\-/年])?(\d{1,2})[.\-/月](\d{1,2})日?", cut)]
    # 缺年份的 token 先用「句中最先出现的年份」补，没有就用 default_year
    years = [t[0] for t in tokens if t[0]]
    base_year = int(years[0]) if years else (default_year or date.today().year)
    filled: list[str] = []
    for y, mo, d in tokens:
        iso = to_iso(y or base_year, mo, d)
        if iso:
            filled.append(iso)
    if not filled:
        return "", ""
    start = filled[0]
    end = filled[-1] if len(filled) > 1 else start
    if end < start:
        # 只补了 default_year 的尾段（例如 12 月开展、次年 3 月闭幕）
        y = int(start[:4])
        alt = to_iso(y + 1, end[5:7], end[8:10])
        end = alt or end
    return start, end


def is_permanent(*fields: str) -> bool:
    joined = " ".join(f or "" for f in fields)
    return any(w in joined for w in PERMANENT_WORDS)


def is_vrish(title: str) -> bool:
    return any(w in title for w in VR_WORDS)


def classify_price(raw: str) -> str:
    """馆方写法太杂：免费 / 单人票158元<br>… / <img 票价图> / 空。"""
    s = (raw or "").strip()
    if not s or s.startswith("<img"):
        return "付费展" if s else ""
    plain = text_of(s)
    if "免费" in plain:
        return "免费"
    m = re.search(r"(\d+(?:\.\d+)?)\s*元", plain)
    if m:
        return f"¥{m.group(1)}起"
    return "付费展"


def stable_id(*parts: str) -> str:
    """str 的 hash() 每次进程都随机（PYTHONHASHSEED），会把历史 id 全打乱，
    所以这里用 md5 保证同一条展览在任何一次运行里 id 一致。"""
    return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()[:10]


def record(city: str, museum: str, title: str, place: str, date_text: str,
           price: str, url: str, poster: str = "", start: str = "", end: str = "",
           source: str = SOURCE) -> dict:
    return {
        "id": "mu" + stable_id(museum, title),
        "source": source,
        # VR/数字/沉浸这类内容型展即使在馆里，也按用户口径归「展览」栏
        "kind": "展览" if is_vrish(title) else "博物馆",
        "title": title,
        "venue": museum,
        "shop": museum,
        "place": place,
        "city": city,
        "date_text": date_text,
        "date": start,
        "date_end": end or start,
        "status": "",
        "poster": poster,
        "price": price,
        "tags": [],
        "url": url,
        "links": [{"source": source, "url": url}],
    }


# ---------------------------------------------------------------- 各馆适配器

def nj_museum(log) -> list[dict]:
    """南京博物院：结构化最好的一路，连票价都是字段。"""
    body = fetch("https://www.njmuseum.com/api/exhibition/list?pageNum=1&pageSize=100",
                 referer="https://www.njmuseum.com/", ajax=True)
    if not body:
        return None                      # None = 抓取失败，交给缓存兜底并标 stale
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    out = []
    for it in ((data.get("data") or {}).get("list") or []):
        title = text_of(it.get("title", ""))
        timedesc = text_of(it.get("timedesc", ""))
        if not title or is_permanent(timedesc):
            continue
        start, end = parse_range(timedesc, date.today().year)
        img = (it.get("imgSrc") or [""])[0] if isinstance(it.get("imgSrc"), list) else ""
        out.append(record("南京", "南京博物院", title, text_of(it.get("position", "")),
                          timedesc, classify_price(it.get("price") or ""),
                          f"https://www.njmuseum.com/zh/exhibitionDetail?id={it.get('id')}",
                          img.startswith("http") and img or "", start, end))
    return out


def sh_museum(log) -> list[dict]:
    """上海博物馆：接口混了中英双语、历史展与数字展，按展期与语言筛。"""
    base = ("https://www.shanghaimuseum.net/mu/frontend/pg/display/search-exhibit"
            "?langCode=CHINESE&exhibitTypeCode=OFFLINE_EXHIBITION"
            "&offlineExhibitionType=PRESENT&limit=100&page={}")
    items: list[dict] = []
    for page in range(1, 5):
        body = fetch(base.format(page), referer="https://www.shanghaimuseum.net/", ajax=True)
        if not body:
            if page == 1:
                return None               # 首页都没拿到才算失败
            break
        try:
            batch = (json.loads(body).get("data") or [])
        except json.JSONDecodeError:
            if page == 1:
                return None
            break
        items += batch
        if len(batch) < 100:
            break
        time.sleep(INTERVAL)
    today = date.today()
    out = []
    for it in items:
        name = text_of(it.get("name", ""))
        if not name or not re.search(r"[\u4e00-\u9fa5]", name):
            continue
        range_text = text_of(it.get("exhibitDateRange", ""))
        start, end = parse_range(range_text)
        if not start or not end:
            continue                       # 无展期的多是数字展/线上展
        if not (start <= today.strftime("%Y-%m-%d") <= end):
            continue
        pic = (it.get("picPath") or "").strip()
        code = it.get("code") or ""
        out.append(record("上海", "上海博物馆", name, text_of(it.get("exhibitPlace", "")),
                          range_text, "以馆方为准",
                          f"https://www.shanghaimuseum.net/mu/frontend/pg/article/id/{code}",
                          f"https://www.shanghaimuseum.net/mu/{pic}" if pic else "",
                          start, end))
    return out


def sz_museum(log) -> list[dict]:
    """苏州博物馆：临时展览页，本馆与西馆同页，全馆免费需预约。"""
    year = date.today().year
    body = fetch(f"http://www.szmuseum.com/Exhibition/Temporary?startYear={year}",
                 referer="http://www.szmuseum.com/")
    if not body:
        return None
    today = date.today().strftime("%Y-%m-%d")
    out = []
    for blk in re.findall(r"<li[^>]*>(.*?)</li>", body, re.S):
        href = re.search(r"href=['\"]([^'\"]*TemporaryDetails[^'\"]*)", blk)
        if not href:
            continue
        plain = text_of(blk)
        m = re.search(r"展览时间[：:](.+?)展览地点[：:](.+?)(?:展览简介|$|序)", plain)
        if not m:
            continue
        range_text, place = m.group(1).strip(), m.group(2).strip()
        title = plain[:m.start()].strip()
        # 地点后面常跟着简介，截掉常见简介起始词
        place = re.split(r"(?:今年|序|一窗|本次|20\d\d)", place)[0].strip(" 、，")
        if not title or is_permanent(title, range_text):
            continue
        start, end = parse_range(range_text, year)
        if end and end < today:
            continue
        out.append(record("苏州", "苏州博物馆", title[:40], place[:30], range_text[:40],
                          "免费需预约", "http://www.szmuseum.com" + href.group(1).split("?")[0],
                          "", start, end))
    return out


def canal_museum(log) -> list[dict]:
    """扬州中国大运河博物馆：展期、地点埋在简介正文里，靠关键词捞。"""
    body = fetch("https://canalmuseum.net/linzhantezhan.html", referer="https://canalmuseum.net/")
    if not body:
        return None
    today = date.today().strftime("%Y-%m-%d")
    out = []
    for href, inner in re.findall(r'<li>\s*<a href="(/linzhantezhan/\d+\.html)">(.*?)</a>\s*</li>',
                                  body, re.S):
        t = re.search(r'<div class="title">(.*?)</div>', inner, re.S)
        j = re.search(r'<div class="jianjie">(.*?)</div>', inner, re.S)
        title = text_of(t.group(1)) if t else ""
        intro = text_of(j.group(1)) if j else ""
        if not title or is_permanent(title, intro):
            continue
        m = re.search(r"(?:展期|展览时间|时间)[：:]\s*([^\u3002 ]{4,44})", intro)
        p = re.search(r"(?:展览地点|展览位置|地点)[：:]\s*([^\u3002 ]{2,26})", intro)
        place = re.split(r"(?:在\s*线\s*购\s*票|小程序)", p.group(1))[0].strip() if p else ""
        # 展期文案后面常连着「地点：…在线购票：…」，展示时截掉
        range_text = re.split(r"(?:展览地\s*点|展览位\s*置|地\s*点|位\s*置|在\s*线\s*购\s*票)",
                              m.group(1) if m else "")[0].strip()
        start, end = parse_range(range_text, date.today().year)
        if end and end < today:
            continue
        price = "付费展" if "在线购票" in intro else "免费需预约"
        out.append(record("扬州", "扬州中国大运河博物馆", title, place,
                          range_text, price, "https://canalmuseum.net" + href, "", start, end))
    return out


def xz_museum(log) -> list[dict]:
    """徐州博物馆：列表只有标题，展期在详情页；详情页是整页 HTML，取一次算一次。"""
    body = fetch("https://www.xzmuseum.com/zl_list.aspx?category_id=496",
                 referer="https://www.xzmuseum.com/")
    if not body:
        return None
    today = date.today().strftime("%Y-%m-%d")
    out = []
    pairs = re.findall(r"href=['\"](/zl_detail\.aspx\?id=\d+)['\"][^>]*>([^<]{4,40})</a>", body)
    for href, title in pairs:
        title = text_of(title)
        if not title or not re.search(r"展|陈列", title):
            continue
        page = fetch("https://www.xzmuseum.com" + href, referer="https://www.xzmuseum.com/")
        time.sleep(INTERVAL)
        if not page:
            continue
        plain = text_of(page[:60000])
        m = re.search(r"(?:展览时间|展出时间|展期)[：:]\s*([^\s]{4,44})", plain)
        p = re.search(r"(?:展览地点|展出地点|地点)[：:]\s*([^\s]{2,26})", plain)
        range_text = m.group(1) if m else ""
        start, end = parse_range(range_text, date.today().year)
        if not end or end < today:
            continue                       # 该栏目留着历年展，只收在展的
        out.append(record("徐州", "徐州博物馆", title, text_of(p.group(1)) if p else "",
                          range_text, "免费需预约", "https://www.xzmuseum.com" + href,
                          "", start, end))
    return out


def silk_museum(log) -> list[dict]:
    """中国丝绸博物馆：「在展」页是基本陈列必须跳过，临展在「已展 / 将展」。

    这两个页只写开幕日，所以按「开幕在近 6 个月内」算在展，避免把几年前的
    旧展捞进来。
    """
    today = date.today()
    floor = (today - timedelta(days=182)).strftime("%Y-%m-%d")
    out = []
    for path in ("yz/list_18.aspx", "jzNX/list_19.aspx"):
        body = fetch(f"https://www.chinasilkmuseum.com/{path}",
                     referer="https://www.chinasilkmuseum.com/")
        if not body:
            continue
        for href, title, d, place in re.findall(
                r"<a href='(/(?:yz|jzNX)/info_\d+\.aspx\?itemid=\d+)'>([^<]{3,50})</a>"
                r".{0,200}?展览时间：([^<]{3,40}).{0,120}?展览地点：([^<]{2,30})", body, re.S):
            title, d = text_of(title), text_of(d)
            if not title or is_permanent(title, d):
                continue
            start, _end = parse_range(d, today.year)
            if not start or start < floor:
                continue
            out.append(record("杭州", "中国丝绸博物馆", title, text_of(place), d,
                              "免费需预约", "https://www.chinasilkmuseum.com" + href,
                              "", start, start))
        time.sleep(INTERVAL)
    return out


MUSEUM_SOURCES = [("南京博物院", nj_museum), ("上海博物馆", sh_museum),
                  ("苏州博物馆", sz_museum), ("扬州中国大运河博物馆", canal_museum),
                  ("徐州博物馆", xz_museum), ("中国丝绸博物馆", silk_museum)]


# -------------------------------------------------- 国家文物局「看展览」补录源

NCHA_LIST = "http://www.ncha.gov.cn/col/col722/index.html"


def ncha_supplement(log, known_titles: set[str]) -> list[dict]:
    """每月一期的展讯速览，用来兜没有临展接口的馆（如良渚博物院）。

    只收江浙沪、且与馆方官网不重名的条目；展期不明的直接丢弃，
    宁缺毋滥。
    """
    body = fetch(NCHA_LIST)
    if not body:
        return None
    # 列表条目挂在 JS 字符串里，标签被转义成 &lt;a …，必须先 unescape 再取链接
    body = unescape(body)
    arts = sorted(set(re.findall(r"href=['\"](/art/\d{4}/\d+/\d+/art_\d+_(\d+)\.html)['\"]", body)),
                  key=lambda x: int(x[1]), reverse=True)
    today = date.today().strftime("%Y-%m-%d")
    out: list[dict] = []
    for path, _ in arts[:12]:
        page = fetch("http://www.ncha.gov.cn" + path)
        if not page:
            continue
        title_tag = re.search(r"<title>([^<]{2,60})</title>", page)
        if not title_tag or "看展览" not in title_tag.group(1):
            continue
        plain = text_of(re.sub(r"<(script|style).*?</\1>", " ", page, flags=re.S))
        seg = plain[plain.find("走近一馆一展"):] if "走近一馆一展" in plain else plain
        # 每条格式：…馆名 展名 地点：X 时间：Y 简介：Z（简介到下一家为止）
        for m in re.finditer(
                r"([\u4e00-\u9fa5]{2,24}(?:博物馆|博物院|纪念馆|美术馆))\s*"
                r"([^\s][^：]{3,50}?)\s*(?:地\s*点|地点)[：:]\s*([^\s时]{2,30}?)\s*"
                r"时\s*间[：:]\s*(.+?)\s*简\s*介[：:]", seg):
            museum, title, place, when = (m.group(1), m.group(2).strip(),
                                          m.group(3).strip(), m.group(4).strip()[:44])
            city = next((c for k, c in CITY_BY_MUSEUM.items() if k in museum), "")
            if not city:
                city = next((c for c in JZH_CITIES if c in place), "")
            if not city or title[:12] in known_titles:
                continue
            if is_permanent(when, title):
                continue
            start, end = parse_range(when, date.today().year)
            if not start or (end and end < today) or start > today:
                continue
            free = "免费" if "免费" in seg[m.end():m.end() + 260] else "以馆方为准"
            out.append(record(city, museum, title[:40], place[:26], when[:40], free,
                              "http://www.ncha.gov.cn" + path, "", start, end,
                              source=NCHA_SOURCE))
        break          # 只用最新一期
    return out


# ---------------------------------------------------------------- 编排入口

def scrape(cache_dir: Path, log=lambda *_: None, offline: bool = False,
           force: bool = False) -> tuple[list[dict], bool]:
    """抓 6 馆官网 + 文物局补录。返回 (记录, 是否有源失败)。

    缓存沿用 data/pages/museum_<slug>.json，某馆挂了就用上次抓到的。
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    failed = False
    for name, fn in MUSEUM_SOURCES:
        cache = cache_dir / f"museum_{stable_id(name)}.json"
        got = None
        if not offline:
            try:
                got = fn(log)
            except Exception as e:  # noqa: BLE001
                log(f"  博物馆·{name}：异常 {type(e).__name__}: {e}")
                got = None
            if got is not None:
                cache.write_text(json.dumps(got, ensure_ascii=False), encoding="utf-8")
        if got is None:
            if cache.exists():
                got = json.loads(cache.read_text(encoding="utf-8"))
                if not offline:
                    log(f"  博物馆·{name}：抓取失败，改用缓存 {len(got)} 条")
                    failed = True
            else:
                if not offline:
                    log(f"  博物馆·{name}：抓取失败且无缓存")
                    failed = True
                got = []
        log(f"  博物馆·{name}：{len(got)} 条特展/临展")
        records += got

    # 补录源同样落缓存，失败不影响主源
    cache = cache_dir / "museum_ncha.json"
    known = {r["title"][:12] for r in records}
    got = None
    if not offline:
        try:
            got = ncha_supplement(log, known)
        except Exception as e:  # noqa: BLE001
            log(f"  文物局展讯：异常 {type(e).__name__}: {e}")
            got = None
        if got:
            cache.write_text(json.dumps(got, ensure_ascii=False), encoding="utf-8")
    if not got and cache.exists():
        got = json.loads(cache.read_text(encoding="utf-8"))
    got = got or []
    log(f"  文物局展讯（补录）：{len(got)} 条")
    records += got
    # 缓存可能是判类口径之前写的，出口统一重算一次
    for r in records:
        r["kind"] = "展览" if is_vrish(r["title"]) else "博物馆"
    return records, failed


if __name__ == "__main__":
    recs, bad = scrape(Path(__file__).resolve().parent.parent / "data" / "pages", print)
    print(f"合计 {len(recs)} 条，失败={bad}")
    for r in recs[:14]:
        print(f"  {r['city']} {r['venue'][:10]} | {r['title'][:26]} | {r['place'][:14]} "
              f"| {r['date_text'][:26]} | {r['price']}")
