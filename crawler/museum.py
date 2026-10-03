#!/usr/bin/env python3
"""博物馆官网「特展 / 临展」信源（免费展只能逐馆抓，票务平台不收录）。

已实测可用的馆（2026-10 调研）：
  南京博物院      JSON  api/exhibition/list?pageNum=1&pageSize=100
  上海博物馆      JSON  …/pg/display/search-exhibit（接口不含票价，详情页也无票价字段）
  苏州博物馆      HTML  /Exhibition/Temporary?startYear=YYYY
  扬州中国大运河博物馆  HTML  /linzhantezhan.html（展期、地点写在简介正文里）
  徐州博物馆      HTML  /zl_list.aspx?category_id=496（列表无展期，需逐条进详情）
  中国丝绸博物馆  HTML  /yz/list_18.aspx + /jzNX/list_19.aspx
                      注意：它的「在展」页其实是基本陈列，按口径必须跳过
  温州博物馆      HTML  /Col/Col23/Index.aspx「近期展览」，标题+展期+地点+海报都在
                      静态 HTML 里；列表页 /Art/Art_23/ 是 403，只有这一栏能用
  江苏省美术馆    HTML  https://www.jssmsg.cn/Home/exhibit「现时」栏
                      老域名 jsmsg.com 已 302 到新域名。列表只给发布日，详情页是空壳，
                      拿不到展期 → date 留空，靠「馆方自己列为现时」这条口径判断在展
  宁波博物院      HTML  /col/col20679/index.html「特别展览」栏目，按发布日筛
                      首页轮播不能当在展清单（海报上是往年展期），展期在图里、
                      详情页没有 → 条目日期留空
  无锡博物院      HTML  /Exhibition/Temporary/TemporaryExhibition
                      静态展期+地点+海报，写法与苏博同款；域名是 wxmuseum.cn
  良渚博物院      HTML  /YinJinZhanLan/index.html「临展」栏，详情页有「展期」行
                      域名是 lzmuseum.cn（调研时猜的 lzmu.cn 不存在）
  常州博物馆      JSON  /api/exhibit/achieve_exhibit_category?pid=13「当前展览」
                      页面是 Vue 空壳，接口要 appkey/nonce/timestamp/sign 签名，
                      算法与密钥明文都在馆方打包 JS 里（馆方换密钥即失效）
  南通博物苑      JSON  http://uc.ntmuseum.com/webapi/exhibition/list（免鉴权 GET）
                      整站只有 HTTP，海报热链会被浏览器按混合内容拦掉 → 不收海报；
                      馆方数据滞后（最新 end_date 停在 2026-03-18），本轮 0 条属正常
  上海市历史博物馆 HTML  /historymuseum/…/dqzl/index.html「当前展览」，服务端渲染
                      区级调研（上海 16 个区）里 12 个区根本没有可静态抓的区级馆
                      展讯源，只有微信；青浦/奉贤可爬但 HTTP-only 或展期在正文里。
                      这一家是市级馆，但它是那轮调研唯一干净的静态源，就收了。
                      它的 /upload/image/ 全部 302 到坏路径 → 无海报

外加国家文物局「看展览｜博物馆展讯速览」做补录源：结构最规整但约每月一期，
用来兜住那些没有临展接口的馆。

口径（与用户确认过）：
  * 常设展 / 基本陈列一律不收；已闭幕的不收。
  * 只收录上面这些「有官网展讯源」的城市，其余城市在博物馆栏不显示。
  * VR/XR、数字展、沉浸展按内容归「展览」栏，场馆只当地点。
  * 美术馆按用户口径算艺术展 → 归「展览」栏，不进博物馆栏。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import random
import re
import socket
import time
import urllib.request
from datetime import date, timedelta
from html import unescape
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

SOURCE = "馆方官网"
NCHA_SOURCE = "文物局展讯"
# 有免费展讯源、因此「博物馆」栏单独成表的城市（其余城市的博物馆售票展
# 按用户口径留在「展览」栏，没有馆方展讯源的城市不单列）。
# 徐州：官网临展接口可用，只是本轮恰好全展完，coverage 仍算它。
# 温州 / 宁波：2026-10 新接，馆方只有栏目级清单、拿不到展期，条目日期留空。
# 无锡 / 常州 / 良渚：真实域名分别是 wxmuseum.cn、czmuseum.cn、lzmuseum.cn，
#   按惯例猜的 wuximuseum.* / lzmu.cn 之类根本不存在，曾被误记成「沙箱访问不了」。
# 南通：接口已接但馆方数据滞后，本轮 0 条 → 运行时不会成表，等它更新。
COVERED_CITIES = {"南京", "上海", "苏州", "扬州", "徐州", "杭州",
                  "温州", "宁波", "无锡", "常州", "南通"}
UA_TEXT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")
AJAX_HEADERS = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
INTERVAL = 0.8

WZ_HOME = "https://www.wzmuseum.cn"
WZ_LIST = WZ_HOME + "/Col/Col23/Index.aspx"        # 温州博物馆「近期展览」
JS_BASE = "https://www.jssmsg.cn"                  # 江苏省美术馆（jsmsg.com 已 302 过来）
JS_LIST = JS_BASE + "/Home/exhibit"                # 「现时」展览栏
NB_HOME = "https://www.nbmuseum.cn"
NB_LIST = NB_HOME + "/col/col20679/index.html"     # 宁波博物院「特别展览」栏目
WX_HOME = "https://www.wxmuseum.cn"                # 无锡博物院（不是 wuximuseum.*，那些域名不存在）
WX_LIST = WX_HOME + "/Exhibition/Temporary/TemporaryExhibition"
LZ_HOME = "https://www.lzmuseum.cn"                # 良渚博物院（调研里猜的 lzmu.cn 不存在）
LZ_LIST = LZ_HOME + "/YinJinZhanLan/index.html"    # 「临展」栏，倒序静态列表
CZ_HOME = "https://www.czmuseum.cn"                # 常州博物馆：Vue 壳，数据在 JSON 接口里
CZ_API = CZ_HOME + "/api/exhibit/achieve_exhibit_category"
CZ_PID_CURRENT = "13"                              # 「当前展览」栏目 id（12=常设，14/15=往年）
CZ_APPKEY = "adi5c90nmp6xwpqw44"                   # 下面两枚密钥是从馆方前端打包 JS
CZ_SECRET = "55aa969f2468ffd4cd13799bdcf806f7"     # 里抄出来的明文，馆方换密钥这条源就断
NT_API = "http://uc.ntmuseum.com/webapi/exhibition/list"   # 南通博物苑：整个站只有 http
SHH_HOME = "https://www.shh-shrhmuseum.org.cn"       # 上海市历史博物馆（市级，非区级）
SHH_LIST = SHH_HOME + "/historymuseum/historymuseum/zl/zlxx/dqzl/index.html"

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
# 美术馆/艺术馆的展按用户口径是艺术展，进「展览」栏而不是博物馆栏
ART_VENUE_WORDS = ("美术馆", "艺术馆", "画院")


DOH_SERVERS = ("https://223.5.5.5/resolve?name={host}&type=A",
               "https://120.53.53.53/resolve?name={host}&type=A")
_doh_cache: dict[str, list[str]] = {}


def _doh_ipv4s(host: str) -> list[str]:
    """用 DoH 向公共 DNS 要 A 记录。

    CI runner 上个别国内馆的域名只解析得出 IPv6，而 runner 没有 IPv6 出口，
    表现就是 [Errno 101] Network is unreachable（上海市历史博物馆、徐州博物馆
    都这样，沙箱里两条都有所以本地怎么都测不出来）。DoH 请求发的是 IP 字面量，
    本身不再依赖本地解析。Status=3 才是真的没有这个域名。
    """
    if host in _doh_cache:
        return _doh_cache[host]
    ips: list[str] = []
    for tpl in DOH_SERVERS:
        try:
            req = urllib.request.Request(tpl.format(host=host), headers={"User-Agent": UA_TEXT})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
            got = [a.get("data", "") for a in data.get("Answer") or [] if a.get("type") == 1]
            if got:
                ips = [ip for ip in got if ip]
                break
        except Exception:  # noqa: BLE001 - DoH 挂了就算了，下面照常报错
            continue
    _doh_cache[host] = ips
    return ips


@contextlib.contextmanager
def _pin_host(host: str, ips: list[str]):
    old = socket.getaddrinfo

    def patched(name, port, *a, **kw):
        if name == host and ips:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]
        return old(name, port, *a, **kw)

    socket.getaddrinfo = patched
    try:
        yield
    finally:
        socket.getaddrinfo = old


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def fetch(url: str, referer: str = "", ajax: bool = False, retries: int = 3) -> str | None:
    headers = {"User-Agent": UA_TEXT, "Accept-Language": "zh-CN,zh;q=0.9"}
    if referer:
        headers["Referer"] = referer
    if ajax:
        headers.update(AJAX_HEADERS)
    host = urlsplit(url).hostname or ""
    for attempt in range(1, retries + 1):
        # 第 1 次按系统解析；之后改用 DoH 查到的 A 记录硬连。
        # 系统解析若同时给了 v6/v4，socket.create_connection 会自己往下试到 IPv4，
        # 所以真救不了的是 runner 只解析出 AAAA 那一种——只能绕过本地 DNS。
        if attempt == 1:
            ctx = contextlib.nullcontext()
        else:
            ips = _doh_ipv4s(host)
            ctx = _pin_host(host, ips) if ips else contextlib.nullcontext()
        try:
            req = urllib.request.Request(url, headers=headers)
            with ctx:
                with urllib.request.urlopen(req, timeout=20) as r:
                    raw = r.read(3_000_000)
            return _decode(raw)
        except Exception as e:  # noqa: BLE001 - 单馆失败不能拖垮整轮
            if attempt == retries:
                fams = ""
                try:                     # 留一行证据：到底是只有 AAAA 还是两条都有
                    fams = "/".join(sorted({i[0].name for i in socket.getaddrinfo(
                        host, 443, type=socket.SOCK_STREAM)}))
                except Exception as e2:  # noqa: BLE001
                    fams = f"getaddrinfo失败{type(e2).__name__}"
                print(f"    {host} 抓取失败 {type(e).__name__}: {e} "
                      f"[本地解析{fams or '无'} / DoH {(_doh_ipv4s(host) or ['无'])[:2]}]",
                      flush=True)
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


def is_vrish(title: str, venue: str = "") -> bool:
    """内容型展览（VR/数字/沉浸）和美术馆的艺展都归「展览」栏。

    场馆维度也要看：江苏省美术馆这类「美术馆」按用户口径算艺术展，
    不能因为它是免费馆方源就混进博物馆栏。
    """
    hay = f"{title} {venue}"
    return any(w in title for w in VR_WORDS) or any(w in hay for w in ART_VENUE_WORDS)


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


def poster_url(url: str) -> str:
    """海报是页面里的热链，这里只做两件事：非 ASCII 路径百分号编码、
    有缩略图变体的用缩略图（宁波原图 1–12MB，卡片用不着）。"""
    u = (url or "").strip()
    if not u.startswith("http"):
        return ""
    if "/picture/0/" in u and "/s_" not in u:
        u = u.replace("/picture/0/", "/picture/0/s_")
    return quote(u, safe=":/?&=%")


def record(city: str, museum: str, title: str, place: str, date_text: str,
           price: str, url: str, poster: str = "", start: str = "", end: str = "",
           source: str = SOURCE) -> dict:
    return {
        "id": "mu" + stable_id(museum, title),
        "source": source,
        # VR/数字/沉浸这类内容型展、美术馆的艺展即使在馆里，也按用户口径归「展览」栏
        "kind": "展览" if is_vrish(title, museum) else "博物馆",
        "title": title,
        "venue": museum,
        "shop": museum,
        "place": place,
        "city": city,
        "date_text": date_text,
        "date": start,
        "date_end": end or start,
        "status": "",
        "poster": poster_url(poster),
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
    pages_ok = 0
    for path in ("yz/list_18.aspx", "jzNX/list_19.aspx"):
        body = fetch(f"https://www.chinasilkmuseum.com/{path}",
                     referer="https://www.chinasilkmuseum.com/")
        if not body:
            continue
        pages_ok += 1
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
    if not pages_ok:
        return None          # 两页都没抓到是源挂了，不是「今年真没临展」
    return out


def wz_museum(log) -> list[dict]:
    """温州博物馆：「近期展览」栏是静态 HTML，标题+展期+地点+海报一次给全。

    栏目页 /Art/Art_23/ 直接 403，只有 /Col/Col23/Index.aspx 这一栏能用，
    馆方只在这里挂当期临展（3 条左右），漏展风险由文物局补录源兜。
    """
    body = fetch(WZ_LIST, referer=WZ_HOME)
    if not body:
        return None
    today = date.today().strftime("%Y-%m-%d")
    out = []
    for href, img, title, when, place in re.findall(
            r"<a href='([^']+)'>\s*<div><img src='([^']+)'></div>\s*<h2>([^<]{3,60})</h2>"
            r"\s*<p>展览时间[：:]\s*([^<]{4,40})</p>\s*<p>展览地点[：:]\s*([^<]{2,30})</p>",
            body, re.S):
        title, when, place = text_of(title), text_of(when), text_of(place)
        if not title or is_permanent(title, when):
            continue
        start, end = parse_range(when, date.today().year)
        if end and end < today:
            continue
        poster = img.strip()
        if poster and not poster.startswith("http"):
            poster = WZ_HOME + poster if poster.startswith("/") else ""
        out.append(record("温州", "温州博物馆", title[:40], place[:30], when[:40],
                          "免费需预约", href if href.startswith("http") else WZ_HOME + href,
                          poster, start, end))
    return out


def js_art_museum(log) -> list[dict]:
    """江苏省美术馆：老域名 jsmsg.com 已 302 到 jssmsg.cn。

    「现时」栏只给发布日，详情页是空壳（任何 Id 都返回同一份 10KB 页面），
    拿不到展期。所以这里不编造日期：date / date_end 一律留空，
    页面上不会出现「剩 N 天」这类假标签，排序也自然垫底——
    相信馆方自己把这条留在「现时」栏，比按发布日硬猜闭幕日更稳。

    「现时」栏里也留着开春的老条目，所以再套一层和丝博一致的 6 个月窗口。
    """
    body = fetch(JS_LIST, referer=JS_LIST)
    if not body:
        return None
    floor = (date.today() - timedelta(days=182)).strftime("%Y-%m-%d")
    out = []
    seg = body[body.find('zl-box'):] or body
    for href, img, pub, title in re.findall(
            r'<a href="([^"]+)"[^>]*>\s*<div class="pic">\s*<img src="([^"]*)"[^>]*>'
            r'\s*</div>\s*<p>(\d{4}-\d{2}-\d{2})</p>\s*<h1>(.*?)</h1>', seg, re.S):
        title = text_of(title)
        # 「典藏精品陈列」这类是常设陈列，按口径不收
        if not title or is_permanent(title) or "典藏" in title or pub < floor:
            continue
        url = href if href.startswith("http") else f"{JS_BASE}/Home/{unescape(href)}"
        poster = img.strip()
        if poster and not poster.startswith("http"):
            poster = JS_BASE + poster if poster.startswith("/") else ""
        out.append(record("南京", "江苏省美术馆", title[:40], "江苏省美术馆",
                          f"{pub} 开展", "免费需预约", url, poster, "", ""))
    return out


def nb_museum(log) -> list[dict]:
    """宁波博物院：「特别展览」栏目按发布时间筛，近 4 个月内的算在展。

    踩过的坑：首页「特别展览」轮播**不能**用。它看着像在展清单，实际是常年
    不撤的宣传位——海报上印的展期是 吉金万里 2025.07.22-10.19、
    源同流异 2025.04.19-06.22、初渡行记 2025.11.25-2026.03.15、
    玉见五千年 2025.07.12-10.12，到 2026-10 全部闭展，只有仰望星空还在展。

    栏目页留着历届展览共 171 条，且 2026-01-15 那天一次性录入了 9 条
    （是迁移日不是开展日），所以窗口不能太宽；近 4 个月这个口径和丝博一致，
    宁缺毋滥。展期文字在详情页里也没有（正文只有前言），条目日期仍留空。
    """
    cat = fetch(NB_LIST, referer=NB_HOME)
    if not cat:
        return None
    floor = (date.today() - timedelta(days=122)).strftime("%Y-%m-%d")
    out, seen = [], set()
    for u, t, y, m, d, img in re.findall(
            r"urls\[i\]='([^']+)';\s*headers\[i\]=\"([^\"]*)\";\s*"
            r"year\[i\]='(\d{4})';\s*month\[i\]='(\d{2})';\s*day\[i\]='(\d{2})';\s*"
            r"imgstrs\[i\]='([^']*)'", cat):
        title = text_of(t)
        pub = f"{y}-{m}-{d}"
        if not title or pub < floor or is_permanent(title) or title in seen:
            continue
        seen.add(title)
        poster = img.strip()
        if poster.startswith("/"):
            poster = NB_HOME + poster
        out.append(record("宁波", "宁波博物院", title[:40], "宁波博物院",
                          f"{pub} 开展", "免费需预约", u,
                          poster if poster.startswith("http") else "", "", ""))
    return out


def wx_museum(log) -> list[dict]:
    """无锡博物院：「临时展览」栏静态 HTML，展期/地点/海报一次给全。

    注意官网域名是 wxmuseum.cn，不是按惯例猜的 wuximuseum.*（那些域名不存在）。
    展期写法与苏州博物馆同款（「2026年8月08日（周六） - 10月31日（周六）」），
    parse_range 直接可用。
    """
    body = fetch(WX_LIST, referer=WX_HOME)
    if not body:
        return None
    today = date.today().strftime("%Y-%m-%d")
    out = []
    for blk in re.findall(r"<li>(.*?)</li>", body, re.S):
        if "展览时间" not in blk:
            continue
        href = re.search(r'href=["\'](/Exhibition/TemporaryDetails/[^"\']+)', blk)
        title = re.search(r"<h2>\s*(.*?)\s*</h2>", blk, re.S)
        when = re.search(r"展览时间[：:]\s*(.{4,44}?)</span>", blk, re.S)
        place = re.search(r"展览地点[：:]\s*(.{2,30}?)</span>", blk, re.S)
        img = re.search(r'<img src="(https?://[^"]+)"', blk)
        if not (href and title and when):
            continue
        title, when = text_of(title.group(1)), text_of(when.group(1))
        if not title or is_permanent(title, when):
            continue
        start, end = parse_range(when, date.today().year)
        if end and end < today:
            continue
        # 简介里带「收费展」字样的按付费处理，其余是免费需预约
        price = "付费展" if "收费展" in blk else "免费需预约"
        out.append(record("无锡", "无锡博物院", title[:40],
                          text_of(place.group(1))[:30] if place else "", when[:40], price,
                          WX_HOME + href.group(1), (img.group(1) if img else ""),
                          start, end))
    return out


def lz_museum(log) -> list[dict]:
    """良渚博物院：「临展」栏 /YinJinZhanLan/ 静态列表 + 详情页里的「展期」行。

    官网域名是 lzmuseum.cn（调研时按惯例猜的 lzmu.cn 根本不存在，属于同一类
    「域名猜错」的假失败）。这一栏挂的是新闻稿而不是展览条目表，所以：
      * 标题从详情页的「展览名称：…」取，取不到再用 <title> 前缀；
      * 展期从「展期 / 展览时间 / 展览档期」行里 parse_range，拿不到就丢——
        新闻稿没有统一字段，编不出来也不编；
      * 列表缩略图 /upload/image/YYYYMMDD/s_xxx.jpg 只有 20KB 左右，直接热链。

    默认年份用发布日期而不是今年，否则 2024 年那批「8月30日至9月17日」这种
    不写年份的展期会被补成 2026，把闭展的展复活。再加一道 start 与发布日
    相差 30 天内的校验，认不出年份的一律丢。
    """
    body = fetch(LZ_LIST, referer=LZ_HOME)
    if not body:
        return None
    today = date.today().strftime("%Y-%m-%d")
    items = re.findall(r'<a href="(/YinJinZhanLan/(\d+)\.html)">\s*'
                       r'<img src="([^"]+)"\s*/><span>([^<]{3,40})</span></a>', body)
    out, seen = [], set()
    for href, _aid, img, snippet in items[:10]:
        page = fetch(LZ_HOME + href, referer=LZ_LIST)
        if not page:
            continue
        pub = re.search(r"发布时间[：:]\s*(\d{4}-\d{2}-\d{2})", page)
        if not pub:
            continue
        pub = pub.group(1)
        when = re.search(r"<p[^>]*>\s*(?:展\s*期|展览时间|展览档期|展出时间)"
                        r"[：:为]\s*([^<]{4,50})</p>", page)
        if not when:
            continue
        when = text_of(when.group(1))
        if is_permanent(when):
            continue
        start, end = parse_range(when, int(pub[:4]))
        if not start or not end:
            continue
        floor = (date.fromisoformat(pub) - timedelta(days=30)).strftime("%Y-%m-%d")
        if start < floor or start > pub[:4] + "-12-31" or end < today:
            continue
        named = re.search(r"<p[^>]*>\s*展览名\s*称[：:]\s*([^<]{3,60})</p>", page)
        title = text_of(named.group(1)).strip("“”\" ") if named else ""
        if not title:
            t = re.search(r"<title>([^<]{3,60}?)-", page)
            title = text_of(t.group(1)) if t else text_of(snippet)
        if is_permanent(title):
            continue
        place = re.search(r"<p[^>]*>\s*地\s*点[：:]\s*([^<]{2,40})</p>", page)
        key = title[:12] or start
        if key in seen:
            continue
        seen.add(key)
        poster = img.strip()
        if poster.startswith("/"):
            poster = LZ_HOME + poster
        out.append(record("杭州", "良渚博物院", title[:40],
                          text_of(place.group(1))[:30] if place else "良渚博物院",
                          when[:40], "免费需预约", LZ_HOME + href,
                          poster if poster.startswith("http") else "", start, end))
    return out


def cz_museum(log) -> list[dict]:
    """常州博物馆：页面是 Vue 空壳，但数据在同站 JSON 接口里，签名前端明文可抄。

    接口 /api/exhibit/achieve_exhibit_category 要 appkey + nonce + timestamp + sign，
    sign = MD5(按 key 排序后所有参数值拼接 + secret).upper()，密钥直接写在他们打包
    JS 里。这类依赖要留个心眼：馆方哪天换 appkey/secret，这条源立刻 401，
    届时的表现是「抓取失败改用缓存」，不是静默出 0 条。

    pid 13 是「当前展览」，12 是常设（口径上不收），14/15 是往年回顾（按展期筛
    就已经过滤掉了，多翻几条也不怕）。字段 ex_showtime / end_time 是
    「2026年07月12日」这种中文，parse_range 直接可用；end_time 为空的
    （XR 体验展这类长期项目）按宁波/江苏口径日期留空，不编闭幕日。
    """
    nonce = str(random.randint(100_000, 999_999))
    ts = str(int(time.time()))
    params = {"appkey": CZ_APPKEY, "terminal": "1", "pid": CZ_PID_CURRENT,
              "page": "1", "pageSize": "40", "nonce": nonce, "timestamp": ts}
    joined = "".join(params[k] for k in sorted(params))
    params["sign"] = hashlib.md5((joined + CZ_SECRET).encode()).hexdigest().upper()
    url = CZ_API + "?" + urlencode(params)
    body = fetch(url, referer=CZ_HOME)
    if not body:
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    if data.get("error_code") != 0:
        # 签名失效 / 栏目改 id：算源失败，别当成「常州今天没展」
        log(f"  常州接口返回 error_code={data.get('error_code')} {data.get('error_msg')}")
        return None
    rows = (data.get("data") or {}).get("data") or []
    if not rows:
        return None
    today = date.today().strftime("%Y-%m-%d")
    out = []
    for r in rows:
        title = text_of(r.get("ex_title") or "")[:40]
        if not title or is_permanent(title):
            continue
        start, end = parse_range(f"{r.get('ex_showtime') or ''} {r.get('end_time') or ''}"
                                 .strip(), date.today().year)
        if not r.get("end_time"):
            # 只有开展日、没有闭幕日（XR 长期体验项目这类）→ 整条按无展期处理，
            # 否则 record() 的 end or start 会把它变成「当天闭幕」。
            start = end = ""
        elif end and end < today:
            continue
        poster = (r.get("ex_pic") or "").strip()
        place = text_of(r.get("ex_addr") or "")[:30]
        out.append(record("常州", "常州博物馆", title, place,
                          f"{r.get('ex_showtime') or ''}-{r.get('end_time') or ''}".strip("-")[:40],
                          "免费需预约", f"{CZ_HOME}/exhibition?id={r.get('id')}",
                          poster, start, end))
    return out


def nt_museum(log) -> list[dict]:
    """南通博物苑：uc.ntmuseum.com/webapi/exhibition/list 是免鉴权 GET，字段规整。

    两个坑：
      1. 整站只有 HTTP，域名 443 直接连不通。海报热链过来是 http://，我们站点是
         https，浏览器会把混合内容拦掉 → poster 一律丢掉，只留文字条目。
      2. 这个接口里 type=1 是常设陈列（不收），type=2 是临展；但它更新滞后，
         实测最新一条的 end_date 停在 2026-03-18，而馆方国庆已经在办「经世济民」
         九馆联动展——也就是说按展期筛完这一轮出 0 条是正常的，南通暂不成表。
         留着这条源是为了下一档展开展当天能抓到，别因为当前 0 条就删掉。
    """
    body = fetch(NT_API + "?p=w&language=1&page=1&limit=300", referer="http://www.ntmuseum.com/")
    if not body:
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    rows = ((data.get("data") or {}).get("list")) or []
    if not rows:
        return None
    today = date.today().strftime("%Y-%m-%d")
    out = []
    for r in rows:
        if str(r.get("type")) != "2":        # 1 = 基本陈列
            continue
        title = text_of(r.get("exhibition_name") or "")[:40]
        if not title or is_permanent(title):
            continue
        start, end = (r.get("start_date") or ""), (r.get("end_date") or "")
        if not start or (end and end < today):
            continue                          # 没有展期不收
        if start > (date.today() + timedelta(days=90)).strftime("%Y-%m-%d"):
            continue                          # 太远的预告也不收
        out.append(record("南通", "南通博物苑", title,
                          text_of(r.get("place") or "")[:30],
                          f"{start} 至 {end}" if end else f"{start} 开展",
                          "免费需预约", "http://www.ntmuseum.com/pcweb/exhibition",
                          "", start, end))
    return out


def sh_history_museum(log) -> list[dict]:
    """上海市历史博物馆：服务端渲染的「当前展览」列表，展期+地点一页给全。

    列表里 <h1> 是新闻稿标题（「新展开幕|…」这种），真正展名在 exbitem-tt，
    但那个位置是 CSS 截断过的（「…上海·汉堡...」），所以逐条进详情页取 <h1>：
    详情页在「EXHIBIT INFORMATION」之后的第一个 <h1> 就是完整展名。
    只有 4 条左右，多 4 个请求换正常标题，值。详情页取不到就退回截断标题，
    绝不自己补全。

    海报必须留空：站里每张 /upload/image/ 图片都 302 到
    Location: D:/Myeclipse-workspace/.../page404.html ——馆方自己的 CMS 路径配错了，
    浏览器里也加载不出来（调研时误判成 HTTPS 直链可用，实测 4 种尺寸都 302）。
    """
    body = fetch(SHH_LIST, referer=SHH_HOME)
    if not body:
        return None
    today = date.today().strftime("%Y-%m-%d")
    out = []
    for blk in re.findall(r"<li>(.*?)</li>", body, re.S):
        if "exb-item-cn" not in blk or "时间" not in blk:
            continue
        href = re.search(r'href="(/historymuseum/[^"?]+)', blk)
        name = re.search(r'class="exbitem-tt">(.*?)</div>', blk, re.S)
        info = re.search(r"<p>(.*?)</p>", blk, re.S)
        if not (name and info):
            continue
        cut = text_of(name.group(1)).rstrip(".。 ")
        plain = text_of(info.group(1))
        when = re.search(r"时\s*间[：:]\s*(.+?)(?:地\s*点|$)", plain)
        place = re.search(r"地\s*点[：:]\s*(.+)$", plain)
        if not (when and cut):
            continue
        start, end = parse_range(when.group(1), date.today().year)
        if not start or (end and end < today):
            continue
        url = SHH_HOME + href.group(1) if href else SHH_LIST
        title = cut
        page = fetch(url, referer=SHH_LIST) if href else None
        if page:
            seg = page[page.find("EXHIBIT INFORMATION"):] if "EXHIBIT INFORMATION" in page else page
            full = re.search(r"<h1[^>]*>([^<]{4,60})</h1>", seg)
            if full:
                title = text_of(full.group(1))[:40] or cut
        out.append(record("上海", "上海市历史博物馆", title,
                          (place.group(1).strip()[:30] if place else "上海市历史博物馆"),
                          when.group(1).strip()[:40], "免费需预约", url,
                          "", start, end))
    return out


MUSEUM_SOURCES = [("南京博物院", nj_museum), ("上海博物馆", sh_museum),
                  ("苏州博物馆", sz_museum), ("扬州中国大运河博物馆", canal_museum),
                  ("徐州博物馆", xz_museum), ("中国丝绸博物馆", silk_museum),
                  ("温州博物馆", wz_museum), ("江苏省美术馆", js_art_museum),
                  ("宁波博物院", nb_museum), ("无锡博物院", wx_museum),
                  ("良渚博物院", lz_museum), ("常州博物馆", cz_museum),
                  ("南通博物苑", nt_museum), ("上海市历史博物馆", sh_history_museum)]


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
    """抓 14 馆官网 + 文物局补录。返回 (记录, 是否有源失败)。

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
        r["kind"] = "展览" if is_vrish(r["title"], r.get("venue", "")) else "博物馆"
    return records, failed


if __name__ == "__main__":
    recs, bad = scrape(Path(__file__).resolve().parent.parent / "data" / "pages", print)
    print(f"合计 {len(recs)} 条，失败={bad}")
    for r in recs[:14]:
        print(f"  {r['city']} {r['venue'][:10]} | {r['title'][:26]} | {r['place'][:14]} "
              f"| {r['date_text'][:26]} | {r['price']}")
