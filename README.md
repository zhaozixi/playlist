# 音乐剧 · 话剧 演出速览

每天 **08:00** 与 **16:00**（北京时间）自动抓取一次在演/待演的音乐剧与话剧信息，
生成一个手机端友好的静态页面，可公网访问、可订阅日历。

## 目录结构

```
crawler/scrape.py          抓取 + 解析 + 分类 + 增量对比
web/index.html             页面（含筛选、搜索、日历订阅）
web/build.py               把数据内联进 HTML，产出单文件版与 .ics
web/data.json              结构化数据
data/shows.json            上一轮结果，用于判断「新上架 / 已下架」
data/changelog.json        每轮变更记录
dist/                      构建产物（单文件版，双击即可打开）
.github/workflows/scrape.yml  定时任务 + Pages 发布
deploy/Dockerfile          自建服务器方案（cron + 静态服务）
```

## 数据源

主源为 **格瓦拉生活网（show.maoyan.com）** 首页。选它的原因：该页分类区块是服务端
渲染的，直接解析 HTML 即可拿到演出标题、场馆、档期，不需要浏览器、不需要签名接口，
也不碰大麦/猫眼的风控（实测沙箱 IP 访问大麦搜索接口会返回 `cloud_ip_bl` 拦截页）。

抓取范围 = 源站首页当前显示的城市（页面右上角那个城市，脚本会读取并记录）。
要换城市需在源站切换后重新抓取，或改用 `deploy/` 自建方案带 Cookie 请求。

详情页（`/detail/{id}`）用于补全完整场馆地址、精确档期区间和「在售中 / 即将开售」状态。
`--enrich` 会访问详情页，代价是请求数翻倍；不带该参数时沿用上一轮已补全的字段。

抓取节奏做了限流（默认间隔 1.5s，可用 `TW_REQUEST_INTERVAL` 调整），失败时保留上一次
数据并在页面顶部标注 stale，不会把站点刷成空白。

## 本地运行

```bash
pip install -r requirements.txt

python3 crawler/scrape.py --enrich   # 抓取并补全详情
python3 web/build.py                 # 构建 dist/ 与内联快照

python3 -m http.server 8000 -d web   # 本地预览
```

其他参数：`--offline` 仅用缓存重建，`--stdout` 只打印不落盘。

## 部署方案

### A. GitHub Actions + GitHub Pages（零成本，推荐）

1. 新建仓库并推送本目录，例如推送到 `main` 分支。
2. Settings → Pages → Source 选 **GitHub Actions**。
3. 之后每天 08:00、16:00（北京时间）自动抓取、提交并部署。
   地址形如 `https://<user>.github.io/<repo>/`。

cron 表达式写成 `0 0,8 * * *` 是因为 Actions 用 UTC：UTC 00:00 = CST 08:00，
UTC 08:00 = CST 16:00。

注意：Actions 的排程触发可能有几分钟到几十分钟延迟，且长期无活动的私有仓库会被暂停排程。

### B. 自建服务器 / VPS

```bash
docker build -f deploy/Dockerfile -t theatre-watch .
docker run -d --name theatre-watch -p 8080:8080 --restart unless-stopped theatre-watch
```

容器内 cron 按 `Asia/Shanghai` 在 08:00、16:00 执行抓取，同时对外提供静态服务。
日志：`docker exec theatre-watch cat /var/log/tw-scrape.log`。
公网访问建议前置 Nginx/Caddy 反代并配 HTTPS。

## 页面功能

- 按类型（音乐剧 / 话剧）、时间窗（一周内 / 一月内）、城市、关键词筛选
- 新上架条目带绿色 NEW 标记，近 7 天开演有高亮
- 点击任意条目跳转源站详情页购票
- `calendar.ics` 可导入手机日历，或作为订阅地址；每天更新后事件同步刷新

## 合规说明

仅聚合公开演出元信息（名称、档期、场馆、状态），不下载图片、不抓取票务价格、
不提供批量导出接口，购票一律跳转源站。数据版权归源站所有。
