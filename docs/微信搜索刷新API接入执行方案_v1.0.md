# 微信搜索刷新 API 接入执行方案 v1.0

> 范围：将远端微信搜索服务 `http://192.168.31.238:8000` 接入 Hub 的微信关键词刷新链路。本方案只允许修改 `workbench/backend`、`workbench/tests`、`docs`，不写入 `source/**`、Demo、旧系统或冻结数据。

## 1. 远端契约与 Provider 边界

远端服务为 FastAPI，已核对 `GET /openapi.json`：

- `POST /search`：请求 `{"keywords": [string], "top_k": integer, "async_mode": boolean, ...}`；异步响应包含 `status`、`request_id`、`poll_url`。
- `GET /search/result/{request_id}`：轮询 `queued`、`processing`、`completed`、`failed`、`cancelled` 等状态，完成态承载关键词级搜索结果。
- `POST /cancel/{request_id}`：超时/取消时尽力取消远端任务；取消失败仍必须保留 Hub 失败证据。
- `GET /article?url=...`：可选正文补取接口。本次 Provider 默认不调用正文补取，避免把搜索刷新误变成大规模正文抓取；因此搜索结果 Markdown 产物数明确为 0。

Provider 只负责 HTTP、远端响应 Schema/语义校验和标准化，不直接写 SQLite；Hub `WechatRefreshService` 负责任务、快照、命中、内容身份、Markdown 索引、幂等、审计和恢复。

## 2. 字段映射

| 远端字段 | Hub 字段 | 规则 |
|---|---|---|
| 请求 `keywords[0]` | `keywords.keyword` | 使用 Hub 当前关键词事实，不接受远端回显覆盖 |
| 请求返回 `request_id` | `source_ref` / 运行 payload | 保存为 `remote:192.168.31.238:8000:<request_id>`，用于追溯 |
| 关键词级结果状态 | `search_refresh_items.status` | `completed`→`succeeded`；`failed`→`failed`；`cancelled`/超时→`failed`，保留 reason_code |
| 结果中的排名 | `search_hits.rank` | 缺失时按数组顺序从 1 开始；非法/非正排名拒绝该条 |
| `url` / `url_raw` / `link` | `search_hits.url_raw`、`contents.canonical_url` | URL 标准化后作为跨关键词文章唯一身份 |
| `title` / `title_raw` / `name` | `search_hits.title_raw`、`contents.title` | 保留原始标题，不做猜测性清洗 |
| `account` / `creator` / `author` | `search_hits.creator_name_raw`、`contents.author_name` | 仅映射存在的字符串 |
| `published_at` / `publish_time` | `contents.published_at` | 仅在可解析时写入；原值保留在 payload |
| 远端完整关键词结果 | `search_snapshots.payload_json` | 原始标准化结果快照，不丢字段 |
| `result_count` 或命中数组长度 | `search_snapshots.result_count` | 优先远端明确值，缺失时使用去重后命中数 |
| `features` / suggestions 等 | `search_snapshots.features_json` | 对象化保存，禁止把正文混入 features |
| 远端搜索结果正文/Markdown | 不写正文资产 | 本接入默认 `markdown_count=0`；只有显式独立正文任务才能写 Markdown |

## 3. URL 去重与 content_id

1. 对 `url`、`url_raw`、`link` 取第一个非空 URL，要求 `http/https`；去掉 fragment，保留 query（微信文章 query 可能承载身份），统一 scheme/host 大小写并移除默认端口。
2. 单次远端结果按 canonical URL 去重，首次出现的 rank 保留；重复 URL 的后续展示事实进入该命中的 `payload_json.duplicate_of_rank`，不再生成第二个文章身份。
3. Hub 先查 `contents.canonical_url` 唯一索引；存在则复用其 `content_id`，不存在才创建 `wechat_article_<sha256(canonical_url)>`。跨关键词相同 URL 必须得到同一 `content_id`。
4. 不因标题、作者或抓取时间变化新建内容；这些变化作为当前命中 payload/内容可变字段更新证据。

## 4. 无搜索结果与 Markdown 规则

- 远端完成且没有合法 URL 命中是正常成功快照：`result_count=0`、`hits=[]`、`status=succeeded`，不得伪造文章。
- 无结果也必须生成搜索快照、source manifest、刷新 item、刷新历史和审计记录。
- Provider 不请求 `/article`，本刷新不会生成/更新 Markdown；每次刷新回执显式包含 `markdown_count=0`。
- 若远端声称命中但命中项没有合法 URL，保留原始 payload，计入 `invalid_hit_count`，不得写入 `contents` 或 Markdown。

## 5. 幂等、刷新历史与失败恢复

- HTTP 写请求必须带 `confirm=true` 与非空 `Idempotency-Key`；相同 key + 相同输入返回原命令/批次回执，不重复调用远端、不重复快照/文章。
- 相同 key + 不同输入返回冲突，禁止覆盖原结果。
- Provider 调用前先创建 Hub command/job/item；远端失败、超时、取消都落 `failed`/`blocked`、`error_json`、事件、审计和运行投影，不能只返回 Toast。
- 刷新历史以 `search_refresh_jobs`、`search_refresh_items`、`search_refresh_events`、`command_runs`、`dual_write_receipts`、`audit_log` 为事实来源；历史必须可由新进程从 SQLite 读取。
- 远端任务超时先调用取消接口，随后 Hub 标记失败并保留远端 request_id；恢复通过新的幂等键重试，已完成历史项不重写。
- 数据库写入遵循 WAL + 进程级 writer lock；Provider 不在事务之外写 Hub，失败不会产生成功快照。

## 6. 验证门禁

按以下顺序执行，未通过不得进入下一道门：

1. **契约单测门禁**：临时/隔离 SQLite；覆盖远端 payload 转换、URL 去重、同 URL 同 `content_id`、无结果 `markdown_count=0`、幂等重放、远端失败/超时/取消、刷新历史和重启读取。
2. **两词真实门禁**：真实刷新 `友邦财富盈活`、`财富盈活 收益`；必须有远端调用证据、搜索命中或真实零结果、URL 唯一、跨词相同 URL 同 `content_id`、Markdown 数为 0、刷新历史匹配。不得启动正式全量刷新。
3. **10 轮真实小规模门禁**：每轮只提交受控小批关键词，默认每轮最多 2 词、串行/低并发；记录远端 request_id、每词状态、命中数、唯一 URL 数、Markdown 数、失败与耗时。10 轮全部完成且 API 层测试通过后停止。
4. **正式全量门禁**：本执行方案不执行；须由主代理完成前端真实态验收并显式决定。

## 7. 证据要求与已知限制

真实证据必须至少包含：远端 OpenAPI 摘要、请求/轮询 request_id、Hub API 回执、SQLite 查询结果、URL/content_id 对账、Markdown 计数、刷新历史和失败恢复记录。API 响应字段若发生版本漂移，Provider 必须 fail-closed 并保留原始安全摘要；不得用假数据或模拟成功替代远端证据。

## 8. 前端刷新状态/历史契约补充（2026-07-16 审计纳入）

微信 iframe 前端通过 `POST /api/refresh-all` 发起批次，约每 3 秒读取 `/api/refresh-all/status`，历史读取 `/api/refresh-all/history`。Hub 对外状态统一为：

- `completed`：全部关键词成功；
- `completed_with_failures`：批次完成但存在失败项，不能视为全成功；
- `cancelled`：取消完成；
- `failed`：批次失败或 Provider 被阻断；
- `running`：queued/running/cancelling 的前端统一态。

Hub 内部状态保留在 `hub_status`（`succeeded`、`partial_failed`、`blocked` 等），防止内部枚举污染旧 iframe 契约。`failed_keywords` 统一为 `[{"keyword":"...","reason":"..."}]`，并补充 `failure_reasons`、`cancel_reason`、`snapshot_count`。失败/部分失败必须通过状态码和字段可识别，不能返回 `completed` 或只给成功 Toast。

当前旧微信页面没有 resume 路由/UI；本任务不修改 `source/**`、旧系统源码或冻结数据，也不擅自新增永久只读前端入口。恢复仍通过 Hub 任务历史、checkpoint 和新的幂等键重试，待主代理后续冻结前端契约后再单独设计 resume API/UI。
