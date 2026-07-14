# 全域内容工作台 · 项目协作规则

> 当前目标：在本机交付一个真正可用的统一工作台，让微信搜一搜、公众号监控、小红书、GEOProMax、Wiki/母文章库、WritingMoney、旧写作与发布七套系统通过同一前端和 v3.2 数据底座运行。

## 1. 职责与协作

- **Codex 主代理是唯一总负责人**：亲自制定和维护总计划，决定核心架构与数据契约，拆分任务，审查全部 diff，完成集成、测试、浏览器验收、提交与交付。
- **Luna/子代理只是执行助手**：只能在主代理划定的边界内做只读探索或局部实现，不负责总计划，不得自行改变核心 schema、目录架构、迁移路线或验收标准。
- 子代理结果只是输入证据；主代理必须回到真实源码、运行态或测试结果复核后才能纳入项目。
- 最终判断以客观证据为准。旧依赖不可用时必须显示真实阻塞状态，禁止用假数据、假回执或 Toast 冒充成功。

## 2. 永久只读边界

- `source/**`
- `source/_local_full_backup/**`
- `unified-content-platform-demo.html`
- 七套原始项目目录中的历史事实文件

`source/` 是原汁原味的参考与本地恢复层；新代码只能写入 `workbench/`、`data/`、`asset_store/`、`docs/` 和测试临时目录。Demo 只作为布局、配色和交互语言参考，不再修改。

## 3. 架构事实基准

- 首要依据：`全域内容资产与观测架构方案_v3.2.md`
- 执行计划：`全域内容工作台开发总计划_v1.md`
- 字段审计：`系统数据字段全景.md`
- v3.2 的 14 张业务核心表保持独立；迁移、注册表、审计、锁和任务事件属于工程支持表，不得混称核心表。
- Markdown 是正文事实载体；SQLite 是身份、关系、快照、指标、任务与审计事实层。
- 历史数据必须支持增量导入、幂等重放、对账、备份与恢复；不得为了新架构丢弃不可再生的旧快照。

## 4. 开发与安全规则

- 新应用统一放在 `workbench/`，默认只监听 `127.0.0.1:8799`。
- Hub 采用 SQLite WAL 和进程级单写锁；所有写入统一经过持久任务或 Repository。
- JSON 在适配器边界执行 Schema 校验；SQL 必须参数化；Markdown 必须防 XSS；文件访问必须防路径穿越和软链接逃逸。
- Cookie、Token、API Key、浏览器配置、原始大数据、Hub 运行库和本地完整镜像不得提交 GitHub。
- 真发布、付费 GEO 刷新和其他不可逆/计费动作必须显式确认；自动化测试只能使用 dry-run、草稿或模拟提供方。
- 每个里程碑先打通最小真实链路，再扩展 UI；失败必须保留错误、重试与审计证据。

## 5. 测试与验收

- 当前总验收矩阵为 `T001–T180`，不得在未获用户同意时缩减。
- 主代理亲自执行最终自动化测试和浏览器模拟点击验收，子代理测试不能替代主代理验收。
- 每批改动至少完成适用的语法、类型、单元、契约和回归测试；涉及页面时必须验证真实加载、空态、错误态和关键交互。
- 完成标准：180 项全部通过、零已知 P0/P1、七系统真实链路有证据、数据可备份恢复。

## 6. Git / GitHub

- 每个完整改动完成后自动提交并推送；commit message 使用中文并准确描述实际改动。
- 提交前必须执行 `git fetch origin`、`git status --short --branch`、`git log --oneline HEAD..origin/main`，先检查远端和重合改动。
- 远端领先时先审查差异，再决定 rebase 或人工合并；禁止覆盖他人提交和 `git push --force`。
- 提交前检查暂存区，确认没有秘密、大数据、运行库、缓存或本地镜像。
- 最终汇报必须说明：改了什么、测试结果、commit id、推送状态、本地是否与 `origin/main` 一致。

## 7. 七套真实系统路径

```text
微信搜一搜:      /Users/works14/.claude/监控/wechat-ybxhyyh-top3/
公众号监控:      /Users/works14/Documents/zkcode/250626_mpGUI/
小红书:         /Users/works14/Documents/zkcode/取数/xhs-keyword-monitor/
GEOProMax:       /Users/works14/Documents/zkcode/GEOProMax/
Wiki:            /Users/works14/Documents/output_md/wiki-viewer/
WritingMoney:    /Users/works14/Documents/output_md/wiki-viewer/WritingMoney/
发布系统:         /Users/works14/Documents/zkcode/YZKcode/1126WritePublish/
母文章库:         /Users/works14/Documents/output_md/
```
