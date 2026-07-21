# Production OS 抽离实施方案

> 版本：v1.0
> 日期：2026-07-18
> 目标：在不影响 Content OS 微信关键词迁移、刷新和其他选题/采集模块的前提下，将内容资产、内容生产、内容发布从当前工作台抽离为独立的 Production OS。

## 1. 本次抽离后的产品边界

### Content OS（原项目，8799）

只保留：

- 统一首页
- 选题发现：微信关键词、小红书关键词、GEO 观察
- 内容采集：公众号监控

Content OS 不再暴露：

- 母文章 Wiki
- 母文章铸造
- 批量成稿
- 写作与发布
- 生产/发布相关前端、API、legacy 静态页面和生产专属服务

微信、小红书、GEO、公众号的现有代码、数据目录、旧系统代理和刷新链路不迁移、不重命名、不改业务逻辑。

### Production OS（新项目，建议 8780）

侧边栏只保留三个一级入口：

```text
内容资产   → 母文章 Wiki
内容生产   → 批量成稿
内容发布   → 写作与发布
```

Production OS 不提供统一首页、选题发现、内容采集，也不加载微信/小红书/GEO/公众号业务岛屿。

## 2. 抽离原则

1. **先复制、后裁剪，不在原项目内搬移业务源码。**
2. Content OS 的微信迁移 Agent 可能正在修改未提交文件；本次只修改导航、应用装配和明确的生产残留，不触碰微信服务、适配器、状态库和迁移数据。
3. 两个系统各自使用独立项目目录、独立启动脚本、独立端口、独立数据库和独立 lock 文件。
4. 两个系统都可以读取 `/Users/works14/Documents/output_md`；正文事实仍由 Wiki 服务按路径校验、原子写入、版本和审计处理。
5. MVP 阶段允许复制共享基础代码，不引入共享 Python 包，不做跨仓库同步。
6. 生产系统初始只使用 dry-run、草稿和 Fake Provider；不自动触发真实发布或计费动作。
7. 不删除 `output_md`、`asset_store`、微信迁移快照和真实运行数据。

## 3. 代码边界

### 3.1 Content OS 保留

前端：

```text
frontend/src/features/overview
frontend/src/features/wechat
frontend/src/features/xhs
frontend/src/features/geo
frontend/src/features/mp
frontend/src/features/systems      # 仅作为内部 API/状态能力；不进入主导航
frontend/src/api
frontend/src/hooks
frontend/src/styles
frontend/public/legacy/wechat
frontend/public/legacy/xhs
frontend/public/legacy/mp
```

后端：

```text
features/overview
features/system
features/wechat
features/xhs
features/geo
features/mp
features/contents
features/jobs
features/signals
features/governance
adapters/{base,geo,mp,wechat,wechat_search_api,xhs,xhs_search_provider}
services/{audit,backup,content,contract_diff,dual_write,geo,helpers,jobs,metrics,
          migration,mp_runtime,safety,search,search_runtime,signals,system_health,
          wechat_aux,wechat_refresh,wechat_state}
repositories/{wechat_legacy,wechat_state}
legacy_proxy（仅保留微信/小红书/GEO/公众号旧系统代理）
legacy_pages
```

### 3.2 Content OS 必须移除

```text
frontend/src/features/wiki
frontend/src/features/writing
frontend/src/features/publishing
frontend/public/legacy/wiki
frontend/public/legacy/writing
backend/content_hub/features/wiki
backend/content_hub/features/writing
backend/content_hub/features/publishing
backend/content_hub/adapters/wiki.py
backend/content_hub/adapters/writing.py
backend/content_hub/adapters/publishing.py
backend/content_hub/services/wiki.py
backend/content_hub/services/writing.py
backend/content_hub/services/publishing.py
```

同时从 Content OS 的 `app.py`、`run.py`、`App.tsx`、首页请求和 legacy proxy 中移除对应 import、router、启动回填、旧 Wiki API 和生产 fallback。

### 3.3 Production OS 必须包含

```text
frontend/src/features/wiki
frontend/src/features/writing/BatchPage.tsx
frontend/src/features/publishing
frontend/public/legacy/wiki
frontend/public/legacy/writing
backend/content_hub/features/wiki
backend/content_hub/features/writing
backend/content_hub/features/publishing
backend/content_hub/adapters/{wiki,writing,publishing}.py
backend/content_hub/services/{wiki,writing,publishing}.py
```

Production OS 的 `App.tsx` 只加载上述三个业务入口；`MotherPage` 不进入导航，也不作为新的业务入口。

## 4. 关键耦合审计结论

### 4.1 已确认的低耦合点

- `writing.py` 不直接依赖微信、小红书、GEO 或公众号服务。
- `publishing.py` 仅负责 Markdown 到公众号 HTML 的输出格式和发布运行层，不读取关键词监控服务。
- Wiki、Writing、Publishing 都通过本地 `output_md`、`asset_store` 和 SQLite 运行层协作，不需要 Content OS 页面参与。
- 当前前端的 Wiki、Writing、Publishing 主要通过 iframe 或独立 API 工作，组件级耦合有限。

### 4.2 必须特别处理的耦合点

- 当前 `app.py` 同时启动微信刷新恢复线程和 WritingMoney 运行层回填；拆分后 Content OS 只能保留微信恢复，Production OS 才执行写作回填。
- 当前 `legacy_proxy.py` 同时包含 Wiki API 和微信/小红书/GEO/公众号代理；Content OS 必须删除 Wiki 分支，Production OS 复制保留 Wiki 兼容实现。
- 当前首页会同时请求 Wiki、Writing、Publishing；Content OS 首页必须删除这些请求，否则虽然侧边栏隐藏，仍然存在运行时耦合。
- 当前 `config.py` 同时包含采集和生产配置；两边复制后各自使用自己的运行库路径和端口，不能共用默认 SQLite。
- 当前数据库迁移文件包含跨模块表。MVP 不删除既有数据库中的历史表，也不做破坏性 DROP；但 Content OS 不再注册生产路由和生产服务。后续若需要“纯 Content OS schema”，另开迁移版本处理。
- Wiki 仍直接写 `/Users/works14/Documents/output_md`；两个进程必须共用同一进程级 lock 语义，不能各自对正文做绕过 Hub 的写入。

## 5. 运行配置

### Content OS

```text
项目：/Users/works14/Documents/zkcode/260712_SEO-GEO
端口：127.0.0.1:8799
数据库：项目 data/hub/content_hub.sqlite
前端：workbench/frontend/dist
```

### Production OS

```text
项目：/Users/works14/Documents/zkcode/260718_ProductionOS
端口：127.0.0.1:8780
数据库：ProductionOS/data/hub/content_hub.sqlite
前端：ProductionOS/workbench/frontend/dist
正文根：/Users/works14/Documents/output_md
```

Production OS 使用独立数据库，避免写作任务、发布尝试和 Content OS 的刷新任务争用同一个 SQLite 文件；两边只共享正文文件根和必要的外部旧系统配置。

## 6. 执行顺序

1. 记录当前 Git 状态，不 reset、不 checkout、不清理其他 Agent 的未提交改动。
2. 写入本方案。
3. 复制现有 `workbench` 到 Production OS，排除 `.venv`、`node_modules`、`dist`、缓存和运行库。
4. 在 Production OS 中裁剪为 Wiki / Batch / Publishing，并设置 8780。
5. 在 Content OS 中裁剪前端导航、首页请求、生产 router、生产 legacy 静态页和生产服务。
6. 构建两套前端。
7. 运行 Content OS 语法/类型/适用测试。
8. 运行 Production OS 生产相关测试。
9. 启动 8799 和 8780，检查健康接口、API 路由隔离、前端页面和导航隔离。
10. 检查两个系统均能读取 Wiki，生产系统可打开批量成稿和发布页，Content OS 不再暴露生产页面/API。
11. 仅在 Production OS 首次验收通过后初始化其独立 Git 仓库并创建首个检查点提交。

## 7. 验收矩阵

### Content OS 8799

- `GET /health` 成功。
- `GET /api/v1/system/status` 成功。
- 主页可加载。
- 主导航仅出现：统一首页、微信关键词、小红书关键词、GEO 观察、公众号监控。
- `#/wiki`、`#/mother`、`#/batch`、`#/publish` 不再是有效页面入口。
- `/api/v1/wiki/*`、`/api/v1/writing/*`、`/api/v1/publishing/*` 不再由 Content OS 注册。
- 微信关键词、小红书关键词、GEO、公众号 API 路由仍成功。
- 不触碰微信迁移 Agent 当前正在修改的业务文件和数据。

### Production OS 8780

- `GET /health` 成功。
- `GET /api/v1/system/status` 成功。
- 主导航仅出现：内容资产、内容生产、内容发布。
- 默认页面可打开 Wiki。
- Wiki 能读取 `/Users/works14/Documents/output_md`。
- 批量成稿页面能打开 WritingMoney legacy 页面。
- 发布页面能读取账号列表，并保持预览、dry-run、草稿门禁。
- Production OS 不注册微信、小红书、GEO、公众号业务路由。
- Production OS 使用独立数据库和 lock 文件。
- 两个系统同时运行时，8799 的微信接口和 8780 的 Wiki/Writing/Publishing 接口互不影响。

## 8. 不在本次 MVP 做的事情

- 不重写 Wiki 编辑器。
- 不重做批量成稿的 Agent 编排内核。
- 不实现 Content OS 到 Production OS 的自动“收录”按钮。
- 不抽共享 Python 包。
- 不合并两个数据库。
- 不删除历史迁移 SQL 中已经存在的跨模块表。
- 不启用真实群发、付费 GEO 刷新或其他不可逆动作。

## 9. 回滚方式

- Content OS 的生产代码删除前保留在 Git 工作区或恢复点中；若验收失败，恢复 `App.tsx`、`app.py`、`run.py` 和被删除目录即可。
- Production OS 是独立目录；删除该目录不影响原项目。
- 运行数据不放入 Git；数据库和正文不删除。
