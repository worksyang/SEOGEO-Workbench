# 统一内容工作台 · 项目协作规则

> 本项目覆盖微信搜一搜、小红书、公众号监控、GEOProMax、Wiki 母文章库、WritingMoney、发布系统的全链路内容工作台。  
> 当前阶段核心目标：统一数据字段全景，建立跨系统事实层，交付可交互的 HTML 演示。

---

## 本地 Demo 交付硬性要求

- 每次修改 `unified-content-platform-demo.html` 或任何会影响页面展示的数据文件后，必须确保 Demo 可用。
- 默认服务地址：`http://127.0.0.1:8788/`（如启用本地服务）。
- 无本地服务时，直接在浏览器打开 `unified-content-platform-demo.html` 验证可用性。
- 最终回复用户时必须明确说明：页面是否可用、访问方式、验证结果。

## Git / GitHub 协作要求

- 每次修改代码、文档、数据结构或任何会影响项目的文件后，都必须提交 Git 并推送到 GitHub；如果没有实际文件改动，明确说明"无改动，不提交"。
- 提交前必须先执行：`git fetch origin`、`git status --short --branch`、`git log --oneline HEAD..origin/main`，确认远端是否已有别人先提交。
- 如果发现 `origin/main` 已经领先本地，必须先提醒用户"远端已有新提交"，并检查是否和本地改动存在重合文件或重合片段；不能直接覆盖、强推或悄悄合并。
- 多开发者协作时，任何开发者提交前都要特别检查：别人是否已提交、自己是否基于最新 `origin/main`、改动文件是否重合、同一段逻辑是否被两边同时改过。
- 如需同步远端，优先使用安全方式：先看远端提交内容和本地 diff，再决定 `git pull --rebase` 或人工合并；禁止使用 `git push --force`。
- 最终回复用户时必须说明：是否已提交、commit id、是否已推送、当前本地与 `origin/main` 是否一致。
- **提交语言**：所有 commit message 统一使用中文（含标题行），不写英文。两人协作用中文最直观。

## 自动提交规则

- 每次 Codex 执行完一个完整操作（修改文件、新增文件、删除文件）后，**必须自动执行一次 Git 提交并推送**。
- 如果本轮操作没有产生实际文件改动，明确说明"无改动，不提交"。
- 自动提交的 commit message 必须**描述本次操作的实际内容**，不能写"自动提交"或"update"这类无意义消息。
- 提交前必须遵循上述 Git 协作要求（检查远端等）。

## 产品原则

- 这是一个**事实层统一工程**，不是策略建议平台。
- 记录事实优先：系统名称、数据源、存储格式、字段定义、时间切片、变化维度。
- 避免过早引入价值判断：机会、信任、建议、缺口、我方占位、排名质量评分。
- 跨系统字段统一时，保留原始字段名，新增 `unified_` 前缀的派生字段。

## 架构参考

- 未来开发 HTML Demo、数据接入、统一字段设计时，先参考：
  - [`系统数据字段全景.md`](系统数据字段全景.md) — 7 个系统的数据字段完整全景
  - [`README.md`](README.md) — 项目总览
- 未来开发跨系统数据流转时，先参考各系统的原始代码和标准化 JSON 文件。

## 文件结构

```
/Users/works14/Documents/zkcode/260712_SEO-GEO/
├── README.md                          # 项目总览
├── AGENTS.md                          # 本文件 - 协作规则
├── 系统数据字段全景.md                 # 7 个系统的字段全景
├── MD架构设计初版方案.md               # 统一数据架构方案
├── unified-content-platform-demo.html  # 统一 HTML 演示
├── unified-content-platform-demo.png   # 演示截图
├── audit-*.png                         # 各系统审计截图
├── demo-v3-*.png                       # 演示 v3 截图
└── .playwright-cli/                    # 浏览器自动化快照
```

## 引用系统真实路径

```
微信搜一搜:      /Users/works14/.claude/监控/wechat-ybxhyyh-top3/
公众号监控:      /Users/works14/Documents/zkcode/250626_mpGUI/
小红书:         /Users/works14/Documents/zkcode/取数/xhs-keyword-monitor/
GEOProMax:     /Users/works14/Documents/zkcode/GEOProMax/
Wiki:          /Users/works14/Documents/output_md/wiki-viewer/
WritingMoney:  /Users/works14/Documents/output_md/wiki-viewer/WritingMoney/
发布系统:       /Users/works14/Documents/zkcode/YZKcode/1126WritePublish/
母文章库:       /Users/works14/Documents/output_md/
```

## 验证清单

每次交付前必须完成：
1. 语法检查（如适用）：`python3 -m py_compile <文件>`
2. 页面检查：打开 `unified-content-platform-demo.html` 确认页面正常
3. Git 提交：`git add .` → `git commit -m "中文描述"` → `git push`
4. 最终回复时说明：改了什么、是否提交推送、commit id
