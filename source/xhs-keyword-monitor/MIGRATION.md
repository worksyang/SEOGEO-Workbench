# 跨平台复刻迁移说明（TikHub 切换后）

## 来源 vs 目标

| 项 | 源 `wechat-ybxhyyh-top3` | 目标 `xhs-keyword-monitor` |
| --- | --- | --- |
| 平台 | 微信公众号 | 小红书 |
| 数据源 | wechat_search_client.py (Markdown) | **TikHub `/api/v1/xiaohongshu/app_v2/*` (JSON)** |
| 端口 | 8765 | 8766 |
| 内嵌 scheduler | 默认开启 | 默认关闭（`ZK_MONITOR_DISABLE_SCHEDULER=1`） |
| 三榜六边形 | 历史/近期/经典/持续/矩阵/广度 | 历史覆盖/近期覆盖/稳定笔记/持续经营/收藏矩阵/战场广度 |
| 关键词启用 | 133 enabled / 18 disabled（源 verbatim） | 同源 |
| raw 路径 | – | `data/raw/<provider>/xhs/<kid>/<ts>_page_<n>.json`<br>+ `details/<note_id>.json` / `users/<user_id>.json` |

## TikHub 数据源（不混 RedFox）

- `app/ingest/tikhub/client.py`：Bearer auth + 指数退避（429/5xx）+ 限速；只暴露 envelope dict
- `app/ingest/tikhub/envelope.py`：标准 ContentItem / CreatorItem / SnapshotEnvelope + url 编码辅助
- `app/ingest/tikhub/parser.py`：从原始 envelope 提取 notes / detail / creator
- `app/ingest/tikhub/detail_service.py`：笔记/博主懒加载 + 缓存
- `app/services/refresh_service.py`：刷新改走 TikHub（兼容旧命名）
- `app/ingest/builders/entity_builder.py`：基于 envelope 构造 normalized
- `app/services/article_cover_service.py`：XHS CDN 代理（xhscdn.net/com、xhs-img.com、xiaohongshu.com、rednotecdn.com、sns-img）

## 数据获取策略（避免高额计费）

| 时机 | TikHub 端点 | 缓存到 |
| --- | --- | --- |
| `scripts/import_tikhub.py` 首抓 | `search_notes page1 general` × 151 关键词 | `data/raw/tikhub/xhs/<kid>/` |
| `scripts/enrich_tikhub.py` 手动批量 | `get_user_info` × Top N 高频博主 | `data/raw/tikhub/xhs/users/<user_id>.json` |
| 抽屉打开（按需懒加载） | `get_image_note_detail` / `get_video_note_detail` | `data/raw/tikhub/xhs/details/<note_id>.json` |
| 博主透视（按需懒加载） | `get_user_info` | `data/raw/tikhub/xhs/users/<user_id>.json` |

APIs（按需触发，前端调用即可）：
- `GET /api/note-detail?note_id=...` 走 detail_service 缓存
- `GET /api/creator-detail?user_id=...` 走 detail_service 缓存

## 字段映射详表

| 源字段（微信） | 目标字段（小红书 / TikHub） | 来源 | 备注 |
| --- | --- | --- | --- |
| `article.read_count` | `article.read_count` (null) | XHS 不公开 | 字段保留，留 null |
| `article.like_count` | `article.liked_count` | `note.liked_count` | |
| `article.collected_count` | `article.collected_count` | `note.collected_count` | 核心信号 |
| `article.comment_count` | `article.comment_count` | `note.comments_count` | |
| `article.shared_count` | `article.shared_count` | `note.shared_count` | |
| `article.cover_url` | `article.cover_url` | `note.images_list[0].url_size_large` | |
| — | `article.work_type` | `note.type` `normal`/`video` | |
| — | `article.xsec_token` | `note.xsec_token` | url encode |
| `account.biz` | — | 删除 | |
| `account.headimg_url` | `account.headimg_url` | `user.images.large` | |
| — | `account.fans / follows / total_works` | `user.fans / follows / note_num_stat.posted` | |
| — | `account.ip_location / verify_info` | `user.ip_location / red_official_verify_content` | |
| — | `account.description` | `user.desc` | |

## RedFox 历史数据隔离

| 路径 | 当前行为 |
| --- | --- |
| `data/raw/redfox/xhs/` | 仅作历史审计保留，不再参与默认构建 |
| `.backup/redfox_<ts>/` | 切换首次导入时自动备份旧 `normalized/*.json` 与 redfox raw |
| `XHS_DATA_PROVIDER` | 默认 `tikhub`；设为 `redfox` 仅在运行时受 env 控制 |
| `monitor-data.json` | 完全由 TikHub 数据覆盖；upsert 不回填 RedFox 旧 ID |

## 配置 (.env)

```
TIKHUB_API_TOKEN=<your_token>          # /Users/works14/user.tikhub.io
TIKHUB_BASE_URL=https://api.tikhub.io
TIKHUB_TIMEOUT=60
TIKHUB_INTER_REQUEST_DELAY=0.3
TIKHUB_MAX_RETRIES=3
PORT=8766
ZK_MONITOR_DISABLE_SCHEDULER=1
XHS_DATA_PROVIDER=tikhub
```

## 安全

- Token 仅在 `.env`（`chmod 600`），不入 git / README / commit
- `client.py` 日志不打印 Authorization header
- `import_tikhub.py` 写入 raw 前显式剔除 `Authorization` 键
- `/api/article-cover-image` 域名白名单（xhscdn.net/com、xhs-img.com、xiaohongshu.com、rednotecdn.com、sns-img）

## 验证清单

- [x] 源项目目录 mtime 与 git 状态未变
- [x] 151 关键词（14 组、133 enabled / 18 disabled）配置完整
- [x] TikHub 6 端点全部可用，0 业务错误
- [x] raw 落盘 `data/raw/tikhub/xhs/` (151 关键词目录) + `details/` + `users/`
- [x] normalized 全量 TikHub 重建（articles `xhs_tk_*` 前缀 100%；accounts 24 字符 hex 100%）
- [x] Flask 在 8766 启动（默认 scheduler 关闭）
- [x] 4 视图隔离正常，0 JS errors
- [x] UI 文案清理：小红书 / 笔记 / 博主 / 点赞 / 收藏 / 评论 / 分享
- [x] 三榜 6 轴：历史覆盖 / 近期覆盖 / 稳定笔记 / 持续经营 / 收藏矩阵 / 战场广度
- [x] 抽屉展示 workDesc + 互动 + 封面 + 博主 + 原文链接
- [x] Account enrich（用户资料）懒加载 + 缓存
- [x] Note detail 懒加载 + 缓存
- [x] relevance_score / is_relevant 派生标记
- [x] dedup + signal 字段准确
- [x] Python 编译 + JS node --check 通过
