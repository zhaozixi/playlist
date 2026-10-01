# 江浙沪-音乐剧·话剧-速览

每天 **08:00** 与 **16:00**（北京时间）自动抓取一次在演/待演的音乐剧、话剧，
以及博物馆特展/临展和各类展览信息，生成一个手机端友好的静态页面，
可公网访问、可订阅日历。页面分四个栏目：音乐剧 / 话剧 / 博物馆 / 展览。

## 目录结构

```
crawler/scrape.py          主流程：格瓦拉抓取 + 解析 + 分类 + 多源合并 + 增量对比
crawler/damai.py           大麦（mtop）信源适配：签名协议、类目过滤、城市 ID
crawler/museum.py          博物馆官网展讯（免费特展/临展）＋文物局补录
crawler/exhibits.py        票务双源的展览类目（格瓦拉 cat=9 / 大麦 2370）＋判类
crawler/gate.py            排程补漏闸门：判断最近一个整点槽是否已有数据
web/index.html             页面（四栏切换、筛选、搜索、多源入口、日历订阅）
web/build.py               把数据内联进 HTML，产出单文件版与 .ics
web/data.json              结构化数据
data/shows.json            上一轮结果，用于判断「新上架 / 已下架」
data/changelog.json        每轮变更记录
dist/                      构建产物（单文件版，双击即可打开）
.github/workflows/scrape.yml  定时任务 + Pages 发布
deploy/Dockerfile          自建服务器方案（cron + 静态服务）
```

## 更新为什么不是「准点」跑

GitHub Actions 的 `schedule` 是尽力而为，不保证到点必跑：实测 2026-09-29 的 16:00
（北京）槽位拖到 22:42 才执行，次日 08:00 那一槽干脆没出现。所以工作流排成
**每 2 小时醒一次**（`cron: "0 */2 * * *"`，正好覆盖 CST 08:00 与 16:00），
由 `crawler/gate.py` 决定这轮要不要真抓：

- CST 08:00 / 16:00 的整点 → 直接抓
- 其它时段 → 只有该槽漏跑才补，数据已新鲜就在几秒内退出（不重复抓、不重复部署）
- `push` / `workflow_dispatch` 触发 → 一律抓，另有 `force` 输入可强制

页面头部也会提示：数据落后于最近一个排程槽时标橙，超过一天标红，
避免把「Actions 没跑」看成「今天没有新演出」。

## 数据源

演出走两个相互独立的平台，同一演出会合并成一条、分别给出购票入口：

- **格瓦拉生活网（show.maoyan.com，美团系）** — 走其列表页前端调用的 JSON 接口
  `m.dianping.com/myshow/ajax/performances/4;…;p={页};s=100`，单页 100 条、可翻页；
  接口不通时回退 SSR 页面 `__NEXT_DATA__`（只给前 10 条）。城市靠 `currentCity` cookie 切换。
- **大麦（mtop.damai.cn，阿里系）** — 淘系 mtop 签名协议
  `sign = md5(_m_h5_tk 前半段 & t & appKey & data)`，首次请求无令牌会回 ERR_TOKEN 并下发
  cookie，复用同一 cookiejar 重签即可。类目用 `groupId=2333`（话剧歌剧）＋
  `currentCityId`，接口 `mtop.damai.wireless.search.project.classify`，单页最多 100 条。

展览另走三路：

- **博物馆官网展讯**（`crawler/museum.py`）— 只收免费特展/临展，常设展与已闭幕展不收。
  目前 6 个馆有可用接口：南京博物院、上海博物馆、苏州博物馆、扬州中国大运河博物馆、
  徐州博物馆、中国丝绸博物馆；另有国家文物局「看展览｜博物馆展讯速览」每月一期做补录
  （兜住良渚博物院这类只发新闻稿的馆）。**「博物馆」栏只在这些有展讯源的城市成表**，
  其余 16 城本期不单独显示。丝绸博物馆的「在展」页其实是基本陈列，必须跳过，
  改取「已展 + 将展」两页并只留开展时间在近半年内的。
- **票务平台展览类目**（`crawler/exhibits.py`）— 格瓦拉 `cat=9`「休闲展览」＋
  大麦 `groupId=2370`「展览」，只有卖票展。集市快闪、研学、导览、儿童亲子这类
  不是「展」的活动按子类目和标题词剔除。

判类口径：VR/XR、数字光影、沉浸式、IP/二次元、艺术展这类内容型展览一律归「展览」栏，
场馆只当地点；确实发生在博物馆/纪念馆里的特展临展才归「博物馆」栏。
两栏合并键同为 `(展名核心, 城市, 起始日)`，馆方免费展与票务平台的同一特展撞车时
以「博物馆」为准，避免一个售票入口把免费特展挤到展览栏。

大麦的搜索类接口（`search.search`）与 `search.damai.cn` 在云出口 IP 上会被风控拦
（`RGV587` / `cloud_ip_bl`），但 `project.classify` 实测可正常返回，因此列表抓取走后者。
两套平台的城市 ID 完全不同，各自固化在 `scrape.py::CITIES` 与 `damai.py::CITY_IDS`。
大麦会把昆山/常熟/张家港等县级市单独标名但 cityId 仍属苏州，记录统一归到所查地级市，
县级市信息保留在场馆名里。

合并键为 `(剧名核心, 城市, 开演/开展日)` —— 日期必须参与，否则同一剧目的巡演不同站次会被并成一条。
主 id 优先取格瓦拉的纯数字 id，保证历史 `shows.json` 能接上，不会整批误判为新上架；
博物馆条目用 `md5(馆名|展名)` 生成稳定 id（Python 的 `hash()` 每进程随机，会把历史打乱）。

抓取范围 = 22 个江浙沪地级市（可用 `--cities` 覆盖）。限流默认 1.2s/请求
（`TW_REQUEST_INTERVAL`）。票务主源任一城市失败会退回该城缓存并整轮标注 stale；
博物馆/展览两路属于附加栏目，失败只在页面上挂一行提示（`note`），不把整站标成过期。
一轮完整抓取（演出双源 + 6 馆 + 展览双源）实测约 100 秒，在 Actions 15 分钟超时内。

## 本地运行

```bash
pip install -r requirements.txt

python3 crawler/scrape.py            # 抓取四路信源并合并
python3 crawler/scrape.py --cities 上海,杭州
python3 crawler/scrape.py --no-damai # 本轮只抓格瓦拉
python3 crawler/scrape.py --no-museum --no-expo   # 只要演出栏
python3 web/build.py                 # 构建 dist/ 与内联快照

python3 -m http.server 8000 -d web   # 本地预览
```

其他参数：`--offline` 仅用缓存重建，`--stdout` 只打印不落盘。
博物馆展讯也可以单独跑一轮看结果：`python3 crawler/museum.py`。

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

- 四个栏目：全部 / 音乐剧 / 话剧 / 博物馆 / 展览，可横向滑动切换
- 按时间窗（一周内 / 一月内）、城市、关键词筛选；城市下拉跟着当前栏目走
  （「博物馆」栏只列有展讯源的城市）
- 演出按开演日排序并显示「N 天后」；展览按闭幕日排序、按闭幕日算「剩 N 天」，
  时间窗也按闭幕日筛，避免把上个月开展的在展直接过滤掉
- 卡片显示海报、档期、场馆、票价区间与来源平台
- 同一内容在两平台都在售时给出多个入口；免费展入口写作「预约 ·」
- 新上架条目带绿色 NEW 标记，7 天内到期/开演有高亮
- `calendar.ics` 可导入手机日历，或作为订阅地址；演出与博物馆特展进日历，
  商业展览不进（数量太大，会把日历刷满）

## 合规说明

仅聚合公开演出与展览元信息（名称、档期、场馆、状态、票面价），不下载图片文件
（海报走源站 CDN 外链）、不提供批量导出接口，购票与预约一律跳转源站。
数据版权归源站所有。
