#!/usr/bin/env python3
"""大麦 mtop 接口探针。

为什么要跑在 GitHub Actions 上：开发沙箱的出口 IP 被阿里风控整段拉黑
（RGV587_ERROR / cloud_ip_bl），而 runner 是另一段 IP。同一个签名请求两边
各跑一次，就能区分「协议不对」和「IP 被拉黑」，避免把接不进来的源写进主线。

请求参数是从大麦 H5 前端包里逆向出来的：
  https://g.alicdn.com/alipay-movie-client/show-h5-next/<版本>/*.js
  调用点形如 {cityId, distanceCityId, pageIndex, pageSize, sortType,
              categoryId, dateType, option: 31, sourceType: 21, returnItemOption: 4}
只往 stdout 打日志，不落盘、不影响正式抓取。
"""
from __future__ import annotations

import hashlib
import http.cookiejar
import json
import time
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

# 阿里风控页特征，命中即判定为被封而非网络故障
BLOCK_SIGNS = ("rgv587", "punish", "bixi.alicdn.com", "unusual traffic",
               "x5secdata", "_____tmd_____", "哎哟喂")

SEARCH_API = "mtop.damai.wireless.search.search"
CITIES_API = "mtop.damai.wireless.cities.query"


def get(url: str, opener=None, headers=None) -> str:
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Accept": "*/*",
                          "Accept-Language": "zh-CN", **(headers or {})})
        with (opener.open if opener else urllib.request.urlopen)(req, timeout=25) as r:
            return r.read(800_000).decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001 - 探针不能抛错影响主抓取
        return f"__ERR__{type(e).__name__}: {e}"


class Mtop:
    """大麦/淘系 mtop 签名客户端。

    握手：首次请求没有 _m_h5_tk，服务端回 ERR_TOKEN_* 并下发 cookie；
    带 token 重新签名即成功。同一个实例复用 cookiejar，因此只握手一次。
    """

    APPKEY = "12574478"

    def __init__(self) -> None:
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))

    def token(self) -> str:
        for c in self.cj:
            if c.name == "_m_h5_tk":
                return c.value.split("_")[0]
        return ""

    def call(self, api: str, data: dict, ver: str = "1.0"):
        """返回 (ret 文案, 解析后的 JSON 或 None, 原始文本)。"""
        body = json.dumps(data, separators=(",", ":"))
        ret, text, parsed = "", "", None
        for _ in range(4):
            t = str(int(time.time() * 1000))
            sign = hashlib.md5(f"{self.token()}&{t}&{self.APPKEY}&{body}".encode()).hexdigest()
            qs = urllib.parse.urlencode({
                "jsv": "2.6.1", "appKey": self.APPKEY, "t": t, "sign": sign,
                "api": api, "v": ver, "type": "originaljson", "dataType": "json", "data": body})
            text = get(f"https://mtop.damai.cn/h5/{api.lower()}/{ver}/?{qs}",
                       self.opener, {"Referer": "https://m.damai.cn/"}) or ""
            if text.startswith("__ERR__"):
                return text[7:], None, text
            hit = next((s for s in BLOCK_SIGNS if s in text.lower()), None)
            if hit:
                return f"BLOCKED({hit})", None, text
            try:
                parsed = json.loads(text)
                ret = str((parsed.get("ret") or [""])[0])
            except (json.JSONDecodeError, ValueError):
                parsed, ret = None, text[:80]
            if "SUCCESS" in ret:
                return ret, parsed, text
            time.sleep(0.8)
        return ret, None, text


def dump(obj, depth: int = 0, maxd: int = 3) -> str:
    """把 JSON 结构压成几行，看成功响应里到底装了什么。"""
    pad = "  " * depth
    if depth >= maxd:
        return f"{pad}…{type(obj).__name__}"
    if isinstance(obj, dict):
        lines = []
        for k, v in list(obj.items())[:16]:
            if isinstance(v, (dict, list)):
                lines.append(f"{pad}{k}: {dump(v, depth + 1, maxd)}")
            else:
                lines.append(f"{pad}{k}={str(v)[:40]}")
        return "\n" + "\n".join(lines) if lines else f"{pad}{{}}"
    if isinstance(obj, list):
        out = f"{pad}[{len(obj)}]"
        if obj:
            out += " first=" + dump(obj[0], depth + 1, maxd)
        return out
    return f"{pad}{str(obj)[:60]}"


def probe_search(m: Mtop, city: int, cate: int, label: str) -> None:
    data = {"cityId": city, "distanceCityId": city, "pageIndex": 1, "pageSize": 15,
            "sortType": 3, "categoryId": cate, "dateType": 0,
            "option": 31, "sourceType": 21, "returnItemOption": 4}
    ret, j, raw = m.call(SEARCH_API, data)
    print(f"[probe] damai {label} city={city} cate={cate} -> {ret!r}", flush=True)
    if not j:
        print("   raw:", (raw or "")[:200], flush=True)
        return
    d = j.get("data") or {}
    print(dump(d, maxd=3), flush=True)
    items = d.get("projectInfo") or []
    if items:
        it = items[0]
        print("[probe] 首条字段:", sorted(it)[:30], flush=True)
        for x in items[:4]:
            print(f"   · {str(x.get('name'))[:30]} | {x.get('cityName')} | "
                  f"{x.get('venueName')} | {x.get('showTime')}", flush=True)


def run_all() -> None:
    eip = get("https://api.ipify.org?format=json") or "?"
    print(f"[probe] === 出口 IP {str(eip)[:60]}", flush=True)
    m = Mtop()

    # 先取城市 ID 表，上海在美团系是 10，大麦体系另有一套
    ret, j, raw = m.call(CITIES_API, {})
    print(f"[probe] cities.query -> {ret!r}", flush=True)
    if j:
        print(dump(j.get("data") or {}, maxd=3)[:1400], flush=True)
    else:
        print("   raw:", (raw or "")[:300], flush=True)

    for cate in (0, 3, 4, 5, 6):
        probe_search(m, 888, cate, f"cate{cate}")

    print("[probe] === 探针结束 ===", flush=True)


if __name__ == "__main__":
    run_all()
