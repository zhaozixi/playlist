#!/usr/bin/env python3
"""候选信源可达性探针（v2）。

只往 stdout 打日志，不参与抓取、不落盘、不影响 data.json。

为什么要跑在 GitHub Actions 上：开发沙箱的出口 IP 被阿里风控整段拉黑
（RGV587_ERROR / cloud_ip_bl），而 runner 是另一段 IP。同一个请求两边各跑
一次，就能区分「协议不对」和「IP 被拉黑」，避免把接不进来的源写进主线代码。
"""
from __future__ import annotations

import hashlib
import http.cookiejar
import json
import re
import time
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

# 阿里风控页特征，命中即判定为被封而非网络故障
BLOCK_SIGNS = ("rgv587", "punish", "bixi.alicdn.com", "unusual traffic",
               "x5secdata", "_____tmd_____", "captcha", "哎哟喂")

# (名称, URL, 期望出现的字段/文本片段)
TARGETS = [
    (" damai m站搜索壳", "https://m.damai.cn/search.html?keyword=%E9%9F%B3%E4%B9%90%E5%89%A7", "data-"),
    ("damai m站搜索页2", "https://m.damai.cn/shows/pages/search.html?keyword=%E9%9F%B3%E4%B9%90%E5%89%A7", "data-"),
    ("damai searchajax", "https://search.damai.cn/searchajax.html?keyword=%E9%9F%B3%E4%B9%90%E5%89%A7"
                         "&currPage=1&pageSize=30&order=1", "noJSONp"),
    ("聚橙 项目列表 ", "https://api.juooo.com/project/list?page=1&rows=10&city_id=1", "code"),
    ("聚橙 分类接口 ", "https://api.juooo.com/project/category?caid=37&page=1&rows=10", "code"),
    ("聚橙 首页接口 ", "https://api.juooo.com/index/projectList?city_id=1", "code"),
    ("秀动 演出列表 ", "https://api.showstart.com/event/list?page=1&rows=10", "state"),
    ("秀动 演出API2 ", "https://showstart.com/api/event/list?page=1", "state"),
    ("摩天轮 域名   ", "https://www.motianlun.com/", "html"),
    ("摩天轮 www    ", "https://motianlun.com/", "html"),
    ("保利剧院列表  ", "https://www.polytheatre.com/polytheatre/index", "html"),
    ("上海文化行政  ", "http://wglj.sh.gov.cn/", "html"),
    ("文旅部演出   ", "https://yyfw.mct.gov.cn/", "html"),
]


def describe(text: str) -> str:
    """把响应压缩成一行可判读的证据。"""
    titles = re.findall(r"《[^》]{2,24}》", text)[:4]
    if titles:
        return f"titles={titles}"
    head = text[:400].lower()
    if "<!doctype html" in head or "<div" in head or "<html" in head:
        return f"html shell, len={len(text)}"
    try:
        d = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return f"len={len(text)} head={text[:70]!r}"
    if isinstance(d, dict):
        body = d.get("data")
        n = len(body) if isinstance(body, list) else (
            len(body.get("list") or body.get("rows") or []) if isinstance(body, dict) else 0)
        return f"json keys={list(d)[:5]} items={n} msg={str(d.get('msg') or d.get('message'))[:20]}"
    return f"json len={len(text)}"


def get(url: str, opener=None, headers=None) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*",
                                                   "Accept-Language": "zh-CN", **(headers or {})})
        with (opener.open if opener else urllib.request.urlopen)(req, timeout=25) as r:
            return r.read(500_000).decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001 - 探针不能抛错
        return f"__ERR__{type(e).__name__}: {e}"


def probe(name: str, url: str) -> None:
    text = get(url)
    if text is None:  # pragma: no cover
        return
    if text.startswith("__ERR__"):
        print(f"[probe] {name} ERROR   {text[7:]}", flush=True)
        return
    hit = next((s for s in BLOCK_SIGNS if s in text.lower()), None)
    tag = f"BLOCKED({hit})" if hit else "OPEN"
    print(f"[probe] {name} {tag:16s} {describe(text)}", flush=True)


def mtop(api: str, data: dict, ver: str = "1.0") -> None:
    """大麦/淘系 mtop 签名请求：验证 runner 出口能否过阿里网关。"""
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    appkey = "12574478"
    body = json.dumps(data, separators=(",", ":"))
    last = ""
    for _ in range(3):
        token = ""
        for c in cj:
            if c.name == "_m_h5_tk":
                token = c.value.split("_")[0]
        t = str(int(time.time() * 1000))
        sign = hashlib.md5(f"{token}&{t}&{appkey}&{body}".encode()).hexdigest()
        qs = urllib.parse.urlencode({"jsv": "2.6.1", "appKey": appkey, "t": t, "sign": sign,
                                     "api": api, "v": ver, "type": "originaljson",
                                     "dataType": "json", "data": body})
        url = f"https://mtop.damai.cn/h5/{api.lower()}/{ver}/?{qs}"
        text = get(url, opener, {"Referer": "https://m.damai.cn/"}) or ""
        hit = next((s for s in BLOCK_SIGNS if s in text.lower()), None)
        try:
            j = json.loads(text)
            ret = (j.get("ret") or [""])[0]
        except (json.JSONDecodeError, ValueError):
            ret = text[:60]
        last = f"ret={ret!r} blocked={hit or '-'} {describe(text) if not hit else ''}"
        if "SUCCESS" in str(ret):
            break
        time.sleep(1)
    print(f"[probe] mtop {api[:34]:34s} {last[:150]}", flush=True)


def run_all() -> None:
    eip = get("https://api.ipify.org?format=json") or "?"
    print(f"[probe] === 出口 IP {eip[:60]}", flush=True)
    for n, u, _ in TARGETS:
        probe(n, u)
    kw = {"keyword": "音乐剧", "pageNum": "1", "pageSize": "12"}
    for api in ("mtop.damai.wireless.search.search",
                "mtop.damai.wireless.project.searchProject",
                "mtop.damai.search.ticket.get"):
        mtop(api, kw)
    print("[probe] === 探针结束 ===", flush=True)


if __name__ == "__main__":
    run_all()
