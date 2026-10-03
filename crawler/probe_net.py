#!/usr/bin/env python3
"""诊断专用：在 GitHub runner 上探国内馆域名的连通性，只打日志不落盘。

沙箱和 runner 出口网络不是一回事——上海博物馆、苏州博物馆这类国内馆在沙箱
里怎么都通，runner 上却时通时不通，光看本地测试没法判断该不该接某个源。
把探测放到 runner 上跑，才能分清是「DNS 只给 IPv6」「IP 被墙」还是「单纯慢」。

正式抓取走 museum.fetch()；这里只是把同一批站点拆开量一遍。
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

HOSTS = [
    "www.xzmuseum.com",                 # 徐州博物馆
    "www.shh-shrhmuseum.org.cn",        # 上海市历史博物馆
    "www.czmuseum.cn",                  # 常州博物馆（已接入，做对照）
    "www.szmuseum.com",                 # 苏州博物馆（一直稳定，做对照）
]


def doh(host: str, rdtype: int) -> list[str]:
    url = f"https://223.5.5.5/resolve?name={host}&type={'A' if rdtype == 1 else 'AAAA'}"
    try:
        r = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(r, timeout=10) as f:
            d = json.loads(f.read().decode("utf-8", "ignore"))
        return sorted(a.get("data", "") for a in d.get("Answer") or [] if a.get("type") == rdtype)
    except Exception as e:  # noqa: BLE001
        return [f"DERR {type(e).__name__}"]


def local_resolve(host: str) -> str:
    try:
        fams = sorted({i[0].name for i in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})
        return "/".join(fams)
    except Exception as e:  # noqa: BLE001
        return f"getaddrinfo失败 {type(e).__name__}"


def tcp(ip: str, port: int = 443, timeout: int = 10) -> str:
    t = time.time()
    try:
        s = socket.create_connection((ip, port), timeout=timeout)
        s.close()
        return f"ok {time.time() - t:.1f}s"
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__} {str(e)[:40]} {time.time() - t:.1f}s"


def get(url: str, timeout: int) -> str:
    t = time.time()
    try:
        r = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(r, timeout=timeout) as f:
            body = f.read(400_000)
        return f"{f.status} {len(body)}B {time.time() - t:.1f}s"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code} {time.time() - t:.1f}s"
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__} {str(e)[:60]} {time.time() - t:.1f}s"


def main() -> int:
    print("=== 出口概况 ===", flush=True)
    print("egress ipv4:", get("https://api.ipify.org?format=json", 15), flush=True)

    for host in HOSTS:
        print(f"\n=== {host} ===", flush=True)
        print("  本地解析:", local_resolve(host), flush=True)
        v4, v6 = doh(host, socket.AF_INET), doh(host, socket.AF_INET6)
        print("  DoH A   :", v4, flush=True)
        print("  DoH AAAA:", v6, flush=True)
        for ip in [x for x in v4 if not x.startswith("DERR")]:
            print(f"  tcp443 {ip:<16}: {tcp(ip)}", flush=True)
        for ip in [x for x in v6 if not x.startswith("DERR")][:2]:
            # create_connection 按地址串自己认 AF_INET6，不用另写一遍
            print(f"  tcp443 {ip:<16}: {tcp(ip)}", flush=True)
        print("  https 20s :", get("https://" + host + "/", 20), flush=True)
        print("  https 60s :", get("https://" + host + "/", 60), flush=True)
        print("  http  20s :", get("http://" + host + "/", 20), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
