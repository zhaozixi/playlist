FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install -y --no-install-recommends cron tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir requests beautifulsoup4 lxml

WORKDIR /app
COPY crawler /app/crawler
COPY web /app/web

# 抓取 → 构建静态页；日志留在 /var/log/tw-scrape.log
RUN printf '%s\n' \
  '#!/bin/sh' \
  'cd /app' \
  'echo "[$(date)] run start" >> /var/log/tw-scrape.log' \
  'python crawler/scrape.py --enrich >> /var/log/tw-scrape.log 2>&1' \
  'python web/build.py >> /var/log/tw-scrape.log 2>&1' \
  > /usr/local/bin/tw-run && chmod +x /usr/local/bin/tw-run

# 每天 08:00 与 16:00（容器时区已设为 Asia/Shanghai），并在容器启动时先跑一轮
RUN printf '%s\n' \
  'CRON_TZ=Asia/Shanghai' \
  '0 8,16 * * * root /usr/local/bin/tw-run' \
  > /etc/cron.d/theatre-watch && chmod 0644 /etc/cron.d/theatre-watch

EXPOSE 8080

CMD ["/bin/sh", "-c", "/usr/local/bin/tw-run & cron -f & cd /app/web & exec python -m http.server 8080 --bind 0.0.0.0"]
