> [!IMPORTANT]
> **当前原型状态记录，不是现行产品设计基准**：本目录中的已有实现保留用于技术复用和差异复盘，但不得继续从当前简化 UI 演化产品。后续开发必须遵循根目录的 `全域内容资产与观测架构方案_v3.3.md`、`全域内容工作台开发总计划_v2.md` 与 `全域内容工作台验收矩阵_v2.md`：Demo 提供统一外壳，原系统前端完整迁入业务岛屿，再逐接口替换数据底层。

# 全域内容工作台

这是七套旧系统之上的新应用层。除 Wiki 的明确授权例外外，`source/`、完整本地镜像和原始 Demo 均保持只读；母文章 Wiki 直接读写 `/Users/works14/Documents/output_md`，工作台通过 Hub 负责路径校验、原子写入、版本和审计。

## 当前 M0 能力

- FastAPI 同源服务、结构化错误、请求 ID、安全响应头。
- SQLite WAL、进程级 `flock` 单写锁、校验和迁移。
- v3.2 的 14 张核心表与 13 张工程/注册/审计支持表。
- NULL 安全的表达式唯一索引、GEO 部分唯一索引、生产任务状态与锁字段。
- JSON Schema、UTC 时间、URL canonicalization 和路径根目录校验。
- React + TypeScript + Vite 真实总览壳；页面数据全部来自 Hub API。

## 本机启动

```bash
cd /Users/works14/Documents/zkcode/260712_SEO-GEO/workbench/frontend
npm install
npm run build

cd /Users/works14/Documents/zkcode/260712_SEO-GEO
python3 workbench/run.py
```

访问：`http://127.0.0.1:8799/`

只运行 API：

```bash
python3 workbench/run.py --api-only
```

检查数据库与构建状态：

```bash
python3 workbench/run.py --check
```

执行在线备份：

```bash
python3 workbench/run.py --backup
```

## macOS 常驻运行

工作台和微信定时刷新分别由两个用户级 LaunchAgent 管理。工作台登录后自动启动并在异常退出后拉起；微信调度每 5 分钟检查一次状态，只在到期、没有活跃批次且调度开关启用时调用 Hub。

```bash
cp workbench/launchd/com.workbench.contentos.plist ~/Library/LaunchAgents/
cp workbench/launchd/com.workbench.wechat-scheduler.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.workbench.contentos.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.workbench.wechat-scheduler.plist
```

旧微信 `com.local.wechat-monitor-8765` 和旧 Agent 任务 `com.claude.schedule.wechat-ybxhyyh-top3` 必须保持 disabled，防止旧 Markdown/排名数据继续写入。

## 测试

```bash
cd /Users/works14/Documents/zkcode/260712_SEO-GEO/workbench
python3 -m pytest
cd frontend && npm run typecheck && npm run build
```

## 关键边界

- 不修改或在 `source/` 中运行新代码。
- 不修改 `unified-content-platform-demo.html`。
- 默认只监听本机回环地址。
- 不把 Cookie、Token、API Key、原始大数据或 Hub 运行库提交 Git。
- 真发布和计费刷新必须显式确认；自动测试只允许 dry-run。
