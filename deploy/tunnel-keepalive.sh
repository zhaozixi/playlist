#!/bin/sh
# 保持 serveo 反向隧道存活：进程退出后 15 秒重连。
# 隧道 URL 由远端分配，重连后会变，新地址追加写入 /tmp/tw-tunnel-url.txt
PORT="${1:-8099}"
LOG=/tmp/tw-tunnel.log
URLS=/tmp/tw-tunnel-url.txt
while :; do
  ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes \
      -R 80:localhost:"$PORT" nokey@serveo.net </dev/null 2>>"$LOG" \
  | tee -a "$LOG" | grep -o 'https://[^ ]*serveousercontent.com' >> "$URLS"
  echo "[keepalive] tunnel down, retry in 15s" >> "$LOG"
  sleep 15
done
