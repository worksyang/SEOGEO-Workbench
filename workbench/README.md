# 全域内容工作台

这是七套旧系统之上的新应用层。`source/`、完整本地镜像和原始 Demo 均保持只读；工作台通过适配器读取旧系统事实，并写入新的 Hub。

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
