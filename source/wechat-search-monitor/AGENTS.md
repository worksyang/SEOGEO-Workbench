# 系统使命

这个系统有两个互信息的目的，你所写的每一行代码、每一个字段、每一个页面，都必须服务于这两个目的：

**目的一：破解微信搜一搜的排序算法，逼近真相。**
微信搜一搜是一个黑箱。你不知道它为什么让某篇文章排在前面、为什么一篇文章突然消失、为什么某个关键词下永远是同一批号。你不知道这些信号到底是"算法偏好"还是"用户行为"还是"商业规则"。
这个系统要做的事是：把黑箱变成可观测的实验室。通过持续抓取关键词排名、追踪每一个命中的时间序列、记录每一次替换和消失，积累足够厚的观测数据，让人类分析师能逼近"排序究竟是怎么工作的"。
你不需要判断结果。你只需要记录：什么时间、什么关键词、第几名、哪篇文章、哪个账号。人类会看这些数据，自己得出结论。

**目的二：通过关键词监控账号得到文章，通过文章，反推用户的潜在真需求。**
用户在微信搜一个词，不是因为他想买这个词。他搜"友邦财富盈活"是因为他遇到了某个情境——要移民了、要传承了、怕汇率风险了。这个情境才是真需求。
这个系统做的事是：把关键词变成探针，把文章变成样本。通过监控"什么人在写什么内容"，让人类分析师能从文章标题和内容中，反推出水面下用户真正在问的问题。同一个关键词下，今天和昨天出现的文章不同，不是系统错了，是有人在回答不同的问题。
你不需要读懂文章。你只需要记录：哪篇文章、在什么时间、因什么关键词被搜到、排名第几。人类会从这些记录中，看出需求的变化。

> 这两个目的交叉在一起，才是这个系统的完整杀伤力——知道"怎么排"和知道"为什么排"，才能从根本上消除内容决策中的不确定性。
> 这个系统的主体是 SEO 事实层：代码负责把微信搜一搜的搜索结果稳定地、可重复地、可追溯地转成可复用的数据资产；人类负责在这些数据上发现规律、形成判断、做出决策。

## 事实层边界

这个系统不是只能展示原始字段。它可以做公式、阈值、排序、颜色、分档和评分，因为这些是让事实可观察的仪表盘。真正的边界是：

> 如果这句话能用公式、阈值、排序规则解释清楚，它可以出现在代码里；如果这句话需要懂行业、懂用户、懂微信算法才成立，它必须留给人类。

按这个标准，代码可以做四类事：

1. **事实层。** 原样记录可观测事实：什么时间、什么关键词、第几名、哪篇文章、哪个账号、阅读量、点赞量、发布时间、正文路径。
2. **度量层。** 把事实加工成可复算指标：账号分、时效分、换新率、在榜天数、命中词数、排名变化、半衰期、连续命中天数。要求公式透明、参数可查、结果可复算。
3. **观察辅助层。** 用排序、筛选、颜色、标签、分档、Top N、趋势线帮助人类扫描数据。标签必须能落回明确阈值或规则，例如"最近 3 天 Top3 加权分""换新率 >= 40%""按命中词数降序"。
4. **控制层。** 允许人类留下主观痕迹：备注、置顶、topic 归并、keyword_bucket、白名单、停用启用。这些是人的判断，不是代码自动推导出来的结论。

代码不能做两类事：

1. **业务解释。** 不要把指标直接解释成行业结论，例如"用户开始焦虑传承""品类教育期结束""这个账号更值得研究""这说明内容粘性强"。这些话需要懂行业、懂用户、懂微信算法，必须留给人类。
2. **行动建议。** 不要输出"应该写这个选题""建议跟进这个号""三天内发补充稿""疑似被限流所以要换标题"。代码可以列出信号，不能替人决定动作。

异常检测的正确形态是输出信号，而不是输出解释。例如可以输出 `{article_id, keyword, disappeared_at, prior_top3_streak: 7}`，不要输出"可能被限流了"。可以展示"换新率 37%"或"换新率落在 25%-40% 区间"，不要输出"市场明显换挡"。

## 这个系统的本质形态

> 平台 = SEO 事实层。人类 = 决策者。

系统负责：抓取 → 解析 → 索引 → 派生 → 展示。
人类负责：看展示 → 发现规律 → 形成判断 → 做出决策。

AI agent 在这个项目中的角色是事实层的建造者和维护者，不是分析师。Agent 写代码时，应该反复问自己三个问题：

1. 我写的这段代码，是在记录"发生了什么"，还是在替人判断"这说明什么"？
2. 如果人类看到这个字段，能靠自己得出比代码更准确的判断吗？
3. 如果明天这个判断被证伪，是代码的错，还是人类的错？

答案应该是：人类永远能靠自己得出更准确的判断。如果判断被证伪，那是人类的错，不是代码的错。

# 项目级约束

本项目是“关键词监控系统”，长期目标是演进为正式的 Flask Python 项目，而不是一次性的静态页面脚本。

## 定时播报任务记忆层

- 当前定时任务名：`港险选题每日播报`（旧名“友邦微信榜单”只作为历史别名）。
- Agent 启动定时播报时，先读 `AGENTS.md` 与 `MEMORY.md`，再看榜单、关键词、账号和文章数据。
- 正式运行痕迹统一写入 `历史记录/`：`YYMMDD_执行记录_标题_HHMM.md`、`YYMMDD_推送正文_标题_HHMM.md`、必要时 `YYMMDD_记忆更新_标题_HHMM.md`。
- 截图、调试输出、临时草稿统一写入 `临时产物/`。
- 推送正文必须先落盘到 `历史记录/YYMMDD_推送正文_标题_HHMM.md`，再用 PushPlus 从该文件推送。

## 核心原则

1. 必须分层：优先按“事实层 / 派生层 / 控制层”思考与落代码。
2. 必须解耦：解析、聚合、Web、状态配置、调度不要混写在一个文件里。
3. 事实数据不可混入人工状态：抓取事实、统计派生、人工设置必须分开存放。
4. 先做最小可用，再做增强：避免一次性引入过多框架、抽象和依赖。
5. 前端风格尽量保持稳定，重构优先改结构和数据流，不随意重做视觉。

## MCP 调用约定

- 本项目当前只使用 Playwright MCP（server 名 `playwright`，固定 user-data-dir：`/Users/works14/.aidso-mcp-profile`）。
- **调用任何 MCP 工具前，必须先 `Ls` 一次项目工作区下的 `mcps/` 目录（或对应客户端缓存目录）拿到准确的 server 标识符**，再填 `server` 参数；禁止凭记忆/描述推断 server 名。
- MCP server 协议本身与客户端无关（Cursor / Codex / Claude Code / VS Code 都走 stdio + JSON-RPC），所以同一个 `@playwright/mcp@latest` 在所有客户端里提供的工具集一致，差异只在客户端侧的配置文件路径和注入方式。
- 排查 MCP 报错时优先看：① server 名是否填对；② 客户端是否已经按修改 `mcp.json` / `codex mcp add` 后**完全重启**；③ `npx @playwright/mcp@latest` 是否能独立启动（先排除包本身问题）。

## 默认架构思维

- `raw data`：微信搜索结果 Markdown（`微信搜索结果/`）、正文 Markdown，视为原始事实输入。
- `normalized/ cache`：可重建的结构化 JSON，当前实际文件包括：
  - `keywords.json` — 监控关键词主表
  - `snapshot_terms.json` — 微信搜索下拉词 / 相关词
  - `snapshots.json` — 每次抓取的快照元数据
  - `ranking_hits.json` — 搜索结果排名详情
  - `accounts.json` / `articles.json` — 账号与文章主数据
  - `monitor-data.json` — 页面派生层聚合数据
- `app state`：置顶、白名单、启用停用、备注、用户设置等，必须独立于抓取事实。
- `web app`：当前已是 Flask 项目（`app/` 目录，ingest / repositories / services / web 分层），不把 Parser 直接塞进路由。

## 渐进式披露

- 做项目全局理解、需求来时路、评分体系演化前，先看：
  - [004_ 2026-06-07_关键词监控系统从0到V2全记录_AI交接版.md](/Users/works14/.claude/监控/wechat-ybxhyyh-top3/004_%202026-06-07_关键词监控系统从0到V2全记录_AI交接版.md)
- 做账号评分、竞品分析相关工作前，按需看：
  - [005_ 2026-06-07_账号评分V2方案与决策记录.md](/Users/works14/.claude/监控/wechat-ybxhyyh-top3/005_%202026-06-07_账号评分V2方案与决策记录.md)
  - [006_ 2026-06-07_接入公众号详细数据的7种新增价值.md](/Users/works14/.claude/监控/wechat-ybxhyyh-top3/006_%202026-06-07_接入公众号详细数据的7种新增价值.md)
- 做字段、入库、解析相关工作前，按需看：
  - [003_ 字段数据字典 v1.md](/Users/works14/.claude/监控/wechat-ybxhyyh-top3/003_%20字段数据字典%20v1.md)
  - [002_ 数据字段与架构计划.md](/Users/works14/.claude/监控/wechat-ybxhyyh-top3/002_%20数据字段与架构计划.md)
  - [001_ 待定问题点.md](/Users/works14/.claude/监控/wechat-ybxhyyh-top3/001_%20待定问题点.md)
  - [007_关键词计划.md](/Users/works14/.claude/监控/wechat-ybxhyyh-top3/007_%20关键词计划.md)

## Demo / 原型组织规范

- 每个 Demo/原型页面独立建文件夹，命名格式：`YYMMDD_功能描述`，例如 `260614_知识图谱`
- 只放入与该 Demo **强相关**的文件（HTML、JSON 数据、脚本等），与项目核心逻辑不耦合的均可放入
- 根目录不保留散落的原型文件，保持根目录整洁

## 自动刷新调度器

- 调度器是 Flask 进程内的 daemon thread（`app/services/scheduler_service.py`），通过 HTTP 调用本地 `/api/refresh-all` 触发批量刷新。
- **重启 Flask 不影响正在运行的批次**：runner/watchdog 是 `start_new_session=True` 的独立进程，状态写在 `data/runs/{batch_id}/state.json`。
- **调度器状态持久化**：`data/state/scheduler.json` 保存 `enabled` 和 `interval_hours`，Flask 重启后自动恢复，不需要手动重新开启。
- **启动时自动接管**：如果 Flask 重启时有活跃批次，调度器会自动检测并轮询它直到完成，完成后再等 `interval_hours` 触发下一轮。
- **API 端点**：`GET /api/scheduler/status`、`POST /api/scheduler/config`（`{"enabled": true, "interval_hours": 3}`）、`POST /api/scheduler/trigger`（立即触发，测试用）。
- **日志**：`/tmp/wechat-monitor-8765.log` 中 `[SCHEDULER]` 前缀的行。
- **修改调度器代码时注意**：不要在 `_run_loop` 或 `_do_refresh` 中引入需要 Flask app context 的逻辑（如 `current_app`），因为 daemon thread 没有 app context。

## Flask 重启约定

- **生产服务唯一入口**：`8765` 端口归 launchd 自动任务 `com.local.wechat-monitor-8765` 管理，配置文件是 `/Users/works14/Library/LaunchAgents/com.local.wechat-monitor-8765.plist`。重启电脑后应由 launchd 自动拉起，不要让 Cursor 手动占用 `8765`。
- **Cursor / Agent 开发时禁止直接跑生产端口**：不要执行 `python3 run.py` 来开发或临时验证，因为默认端口就是 `8765`，会和 launchd 抢端口，导致 `Address already in use` 和 Python 退出崩溃弹窗。
- **开发调试端口避开 8765**：需要临时启动开发服务时，优先使用 `cd /Users/works14/.claude/监控/wechat-ybxhyyh-top3 && PORT=8766 ZK_MONITOR_DISABLE_SCHEDULER=1 python3 run.py`。如果 `8766` 被 Demo 或其他服务占用，依次改用 `PORT=8767`、`PORT=8768`。`PORT` 避免抢生产端口，`ZK_MONITOR_DISABLE_SCHEDULER=1` 避免开发实例触发自动批跑。
- **生产重启方式**：先确认 `8765` 当前是不是 launchd 进程：`lsof -nP -iTCP:8765 -sTCP:LISTEN`。如果被 Cursor 手动进程占用，先停掉那个手动进程；然后用 `launchctl kickstart -k gui/501/com.local.wechat-monitor-8765` 让 launchd 接管。
- **禁止用管道启动**：`python3 run.py | head -20` 会在管道关闭后触发 SIGPIPE 杀死 Flask。
- **端口冲突兜底**：`run.py` 会在创建 Flask app 前检查端口；如果目标端口已被占用，会提前退出，不再启动 scheduler daemon thread。
- **重启不影响批次**：runner/watchdog 是独立进程（`start_new_session=True`），状态写在 `data/runs/{batch_id}/state.json`，Flask 重启后调度器会自动接管活跃批次。
- **生产重启后验证**：`sleep 4 && curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8765/`，期望返回 `200`。
- **开发启动后验证**：按实际开发端口验证，例如 `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8766/`，期望返回 `200`。
- **旧进程清理**：清理前必须确认命令行路径包含 `/Users/works14/.claude/监控/wechat-ybxhyyh-top3/run.py` 或工作目录是本项目，避免误杀 `/Users/works14/Documents/zkcode/wechat-edit/run.py` 这类不同项目进程。

## Git 提交与推送约定

- **入 `.gitignore` 必做项**：`data/state/aidso_*_profile/`（浏览器登录态 cookie，1.3G 级）、`raw/`（微信群成员名单，隐私）、`normalized/*.json`（派生层，rebuild 可重建）、`data/runs|refresh_jobs|aidso_heat|tmp/`（运行产物）、`.playwright-mcp|cli/`、`test-results/`、`*.png` 调试截图。
- **保留入库**：`app/` `scripts/` `run.py` `requirements.txt` `AGENTS.md` `CLAUDE.md` `data/config/` `data/state/app.db` `data/keyword_lists/` `normalized/_schema/` 全部 `*.md` 文档、`归档/`、`apifox-api-docs/`、Demos。
- **首次大推送走 HTTPS，不要走 SSH**：本项目首次 commit 含 ~57MB / 4242 对象，SSH 协议会被 GitHub 多次中断（`Connection to github.com closed by remote host` / `RPC failed`），切换到 HTTPS + `x-access-token` 一次成功。常规后续 push 仍可走 SSH。
- **HTTPS 推完务必清理 remote URL**：HTTPS 临时 URL 会把 token 明文写进 `git remote -v`，推完后用 `git remote set-url origin git@github.com:worksyang/wechat-ybxhyyh-top3.git` 改回 SSH。
- **推送前必看 `git status --short | grep -iE 'aidso_|all_groups_with_members|member_index'`**，结果必须为空，确认登录态和群数据没被误入。
- **提交信息必须大白话纯中文**：commit message 用通俗易懂的中文写，不用英文、不用技术黑话。每条提交信息开头一句话概括改了啥，后面列出具体改了哪几个地方。让人在 GitHub 上看一眼就知道这次提交干了什么。
- **本仓库**：Private，origin = `git@github.com:worksyang/wechat-ybxhyyh-top3.git`，用户 `worksyang`。
