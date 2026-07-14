# GEOProMax 项目协作规则

## 本地 Demo 交付硬性要求
- 每次修改 `web/ui.py` 或任何会影响页面展示、数据生成、静态 HTML 的代码后，必须确保 Demo 服务可用。
- 默认服务地址：`http://127.0.0.1:8788/`。
- 交付前必须完成三步：
  1. 运行语法检查：`python3 -m py_compile run.py web/ui.py`
  2. 启动或重启服务：`python3 run.py --demo`，如需后台运行可用 `nohup python3 run.py --demo > /tmp/geopromax_demo_8788.log 2>&1 &`
  3. 验证端口和页面：`lsof -nP -iTCP:8788 -sTCP:LISTEN` + `curl -fsS http://127.0.0.1:8788/ >/dev/null`
- 如果端口未监听、页面无法访问、浏览器报 `ERR_CONNECTION_REFUSED`，不能说已交付；必须先修复并重新验证。
- 最终回复用户时必须明确说明：服务是否已启动、访问地址、验证结果。

## 正式入口交付硬性要求
- `run.py` 是唯一对外启动入口，`web/run.py` 是 Web 服务，`web/ui.py` 是共用 UI；不要另起一套 UI，不要引入新的设计系统。
- `web/run.py` 只允许做最小工程化增强：读取真实/半真实数据、提供 API、在问题详情页增加单问题“刷新 RedFox”按钮。
- 默认正式入口地址：`http://127.0.0.1:8790/`。
- 每次修改 `web/run.py`、RedFox 接入、Markdown 快照读取、或任何会影响 8790 页面/API 的代码后，交付前必须完成：
  1. 运行语法检查：`python3 -m py_compile run.py web/run.py web/ui.py`
  2. 启动或重启服务：`python3 run.py`，如需后台运行可用 `nohup python3 run.py > /tmp/geopromax_web_8790.log 2>&1 &`
  3. 验证端口、健康检查和数据接口：`lsof -nP -iTCP:8790 -sTCP:LISTEN` + `curl -fsS http://127.0.0.1:8790/health` + `curl -fsS http://127.0.0.1:8790/api/data >/dev/null`
- RedFox 刷新会产生成本；除非用户明确要求真实刷新，否则不要主动调用真实 RedFox 搜索，只测试缺少 `REDFOX_API_KEY` 时的友好报错和页面按钮存在。
- 不允许增加全局刷新、批量刷新、自动刷新；当前阶段只保留问题详情页的单问题手动刷新。
- 最终回复用户时必须明确说明：8790 服务是否已启动、访问地址、验证结果。

## 产品原则
- 这是 GEO 公域事实观察 Demo，不是策略建议平台。
- MVP 优先记录事实：问题、采集时间点、引用源、平台、作者、引用位次、首次出现、最近出现、快照命中。
- 避免在核心页面引入过早价值判断词：机会、信任、建议、缺口、我方占位、作者分、平台可信度。

## 架构参考
- 未来开发数据接入、Markdown 正文、SQLite 或双向索引时，先参考：
  - [`docs/数据架构与入库说明.md`](docs/数据架构与入库说明.md)
- 未来理解、复刻或调整当前 Demo 界面时，先参考：
  - [`docs/Demo界面大白话描述.md`](docs/Demo界面大白话描述.md)
- 未来调整 RedFox 接入时，先参考：
  - [`原子能力/RedFox/README.md`](原子能力/RedFox/README.md)

## Git / GitHub 协作要求
- 每次修改代码、文档、数据结构或会影响 Demo 的文件后，都必须提交 Git 并推送到 GitHub；如果没有实际文件改动，明确说明“无改动，不提交”。
- 提交前必须先执行：`git fetch origin`、`git status --short --branch`、`git log --oneline HEAD..origin/main`，确认远端是否已有别人先提交。
- 如果发现 `origin/main` 已经领先本地，必须先提醒用户“远端已有新提交”，并检查是否和本地改动存在重合文件或重合片段；不能直接覆盖、强推或悄悄合并。
- 多开发者协作时，任何开发者提交前都要特别检查：别人是否已提交、自己是否基于最新 `origin/main`、改动文件是否重合、同一段逻辑是否被两边同时改过。
- 如需同步远端，优先使用安全方式：先看远端提交内容和本地 diff，再决定 `git pull --rebase` 或人工合并；禁止使用 `git push --force`。
- 最终回复用户时必须说明：是否已提交、commit id、是否已推送、当前本地与 `origin/main` 是否一致。

- 提交语言：**所有 commit message 统一使用中文**（含标题行），不写英文。两人协作用中文最直观。
