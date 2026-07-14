# 项目级 AGENTS 约定

## 项目识别与启动优先级
- 本仓库默认要操作的 GUI / HTML 项目是根目录这套“公众号抓取控制台”，不是 `TEST/we-mp-rss`。
- 正确网页控制台由以下部分组成：
  - 前端：`client/`
  - 后端：`server/`
  - 网页标题：`公众号抓取控制台`
  - 后端标题：`MP GUI Web Console`
- 默认启动方式：
  - 后端：`python3 -m uvicorn server.app:app --reload --host 127.0.0.1 --port 28765 --no-access-log`
  - 前端：`cd client && npm run dev`
  - 浏览器访问：`http://127.0.0.1:5173`
- `client/vite.config.ts` 会把 `/api` 和 `/health` 代理到 `http://127.0.0.1:28765`，默认把本地网页控制台后端放到 `28765`，避免占用历史监控端口。
- 如果用户说“启动服务器”“打开网页”“打开 GUI”“我要用一下这个网站”，默认指的是这套根目录网页控制台，除非用户明确点名别的子项目。

## 项目地图
- `client/`
  - 本地网页控制台前端，Vite + Vue。
  - 这是用户实际在浏览器里打开和操作的 HTML 页面。
- `server/`
  - 本地网页控制台后端，FastAPI。
  - 负责保存本地设置、读取本地 SQLite、管理任务状态、调用下方工作流和内网 WeRSS。
- `workflow_service.py`
  - 根目录网页控制台和 CLI 共用的工作流编排层。
  - 负责把“刷新公众号文章”“登录校验”“AI 标题分类”“Markdown 落盘”等步骤串起来。
- `wechat-mp-fetch.py`
  - 独立 CLI 入口。
  - 用于命令行运行“微信公众号刷新 + Article Workflow 一体化任务”。
- `article_workflow.py`
  - 独立文章处理工作流核心。
  - 负责近 N 天文章筛选、去重、AI 分类、Markdown 保存等。
- `SomeURL2MD/`
  - URL 转 Markdown、图片处理、OCR、AI 分类等基础能力模块。
  - 供 `article_workflow.py` 和 `workflow_service.py` 调用。
- `werss_client.py`
  - WeRSS API 客户端。
  - 根目录网页控制台、CLI、workflow 都通过它访问内网 WeRSS 服务。
- `TEST/we-mp-rss/`
  - 上游 WeRSS 项目副本 / 测试副本，不是本仓库默认要启动的 GUI。
  - 它主要用于参考接口、调试、查看上游实现，默认不要把它当成本项目网页端启动目标。
- `main.py`
  - 旧脚本型入口，偏 API 示例 / 报表 / 历史逻辑，不是当前默认网页控制台入口。

## 运行关系
- 本仓库的“公众号抓取控制台”是一个本地网页壳层，不负责替代内网 WeRSS 部署。
- 内网 WeRSS 是外部依赖服务，默认地址固定为 `http://192.168.31.89:8001`。
- 本地网页控制台通过 `server/config.py` 中的默认配置连接内网 WeRSS，再调用其接口完成：
  - 微信登录状态检查
  - 扫码二维码获取
  - 公众号列表读取
  - 指定公众号刷新
  - 文章拉取
- 文章筛选、AI 分类、Markdown 输出是在本仓库本地执行，不在内网 WeRSS 服务器上执行。

## 默认配置来源
- 根目录网页控制台默认配置以 `server/config.py` 为准。
- 当前默认值包括：
  - WeRSS 地址：`http://192.168.31.89:8001`
  - 用户名：`admin`
  - 密码：`admin@123`
  - 探测关键词：`大湾通一峰火燎源`
  - 默认输出目录：`/Users/works14/Documents/output_md`
  - 默认拒绝记录：`/Users/works14/Documents/zkcode/250626_mpGUI/rejected_articles.csv`
  - 默认选中公众号：`MP_WXS_3921283819`
- 本地网页控制台自己的状态存储在：
  - `.web_console/app.db`

## Agent 执行约束
- 未经用户明确要求，不要启动 `TEST/we-mp-rss/web_ui` 或 `TEST/we-mp-rss/main.py` 作为本项目网页。
- 未经用户明确要求，不要把 `TEST/we-mp-rss` 误判为“公众号抓取控制台”。
- 如果 `28765` 端口被别的无关服务占用，优先停掉该无关服务后再启动根目录 `server.app`，除非用户明确要求再改端口。
- 如果用户要“打开网站”，优先检查并启动：
  1. `server.app` on `127.0.0.1:28765`
  2. `client` Vite on `127.0.0.1:5173`
  3. 浏览器打开 `http://127.0.0.1:5173`
- 如果用户要“跑 workflow”“批量抓取”“命令行执行一体化任务”，优先考虑：
  - `wechat-mp-fetch.py`
  - `workflow_service.py`
  - `article_workflow.py`
- 如果用户讨论的是内网已部署好的 WeRSS，本仓库默认不需要重复启动 `TEST/we-mp-rss`，而应把它当成远端依赖服务使用。

## 公众号抓取默认配置
- WeRSS 内网地址固定使用：`http://192.168.31.89:8001`
- 旧地址 `http://192.168.31.90:8001` 已失效，不再使用
- 默认管理账号：
  - 用户名：`admin`
  - 密码：`admin@123`

## 目标公众号
- 公众号名称：`大湾通一峰火燎源`
- WeRSS `mp_id`：`MP_WXS_3921283819`
- fakeid：`MzkyMTI4MzgxOQ==`
- 抓取时间范围：`2025-10-01` 之后全部历史文章
- Markdown 输出目录：`/Users/works14/Documents/zkcode/250626_mpGUI/大湾通一峰火燎源`
- 专用脚本：`/Users/works14/Documents/zkcode/250626_mpGUI/fetch_dawantong_yifeng.py`

## 抓取流程约定
1. 先确认 `/api/v1/wx/auth/qr/status` 返回 `login_status: true`
2. 再调用 `/api/v1/wx/mps/update/{mp_id}?start_page=0&end_page=20`
3. 用 `/api/v1/wx/articles?limit=100&mp_id={mp_id}` 拉全量文章
4. 不依赖 API 返回的摘要正文，必须使用 `SomeURL2MD/wechat_to_markdown.py` 按微信原文 URL 转 Markdown
5. 本地文件校验标准：单文件大于 `800` 字节
