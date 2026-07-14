# 小红书关键词监控系统 (xhs-keyword-monitor)

> 把微信公众号关键词监控系统 (`wechat-ybxhyyh-top3`) 跨平台复刻到小红书；
> 数据源：**TikHub 实时 API**。复用全部 UI / CSS / JS / 调度 / 关键词 CRUD 框架，
> 把事实层全部换成 TikHub 小红书接口。

---

## 0. 数据源说明

| 项 | 源 `wechat-ybxhyyh-top3` | 本项目 `xhs-keyword-monitor` |
| --- | --- | --- |
| 平台 | 微信公众号 | 小红书 |
| 数据源 | wechat_search_client.py (Markdown) | **TikHub `/api/v1/xiaohongshu/app_v2/*` (JSON)** |
| 端口 | 8765 | 8766 |
| 内嵌 scheduler | 默认开启 | 默认关闭（`ZK_MONITOR_DISABLE_SCHEDULER=1`） |
| 关键词启用 | enabled True: 133 | **133 enabled / 18 disabled（与源 verbatim）** |

> 历史 RedFox 适配已不作为默认 provider；旧 `data/raw/redfox/xhs/` 仅作历史审计保留，
> 不参与 normalized/monitor-data 重建。

---

## 1. 快速启动

```bash
# 1. 安装依赖
pip3 install -r requirements.txt

# 2. 配置 .env（自动加载父目录 .env）
cat > .env <<'EOF'
TIKHUB_API_TOKEN=<your_tikhub_token>      # 必填；可在 https://user.tikhub.io 申请
TIKHUB_BASE_URL=https://api.tikhub.io       # 默认
PORT=8766
ZK_MONITOR_DISABLE_SCHEDULER=1
FLASK_DEBUG=0
XHS_DATA_PROVIDER=tikhub                  # 默认
EOF
chmod 600 .env                            # 重要：权限 600

# 3. smoke probe（可选，单关键词验证 6 个端点可用）
python3 scripts/tikhub_probe.py --keyword "友邦环宇盈活"

# 4. 全部 151 词首抓（page1 general 20 条/词）
python3 scripts/import_tikhub.py            # 默认包含 disabled，~5min

# 5. 可选：Top 100 博主信息 enrich（断点续跑、限速）
python3 scripts/enrich_tikhub.py --limit 100 --inter-delay 0.3

# 6. 启动 Flask（端口 8766）
python3 run.py
# → http://127.0.0.1:8766
```

### macOS 常驻服务（推荐）

开发完成后不要再依赖 Codex/终端进程运行服务，使用 macOS `launchd` 用户服务：

```bash
# 安装并立即启动；登录后自动启动，异常退出自动重启
./scripts/xhs_service.sh install

# 查看状态
./scripts/xhs_service.sh status

# 重启/停止/卸载
./scripts/xhs_service.sh restart
./scripts/xhs_service.sh stop
./scripts/xhs_service.sh uninstall
```

服务标签为 `com.zk.xhs-keyword-monitor`，配置安装到
`~/Library/LaunchAgents/`，日志位于
`data/state/launchd/`。关闭 Codex 不会影响该服务；重新打开开发环境也不需要再手动
运行 `python3 run.py`。

---

## 2. TikHub 端点（主控实测全部成功）

| 端点 | 用途 |
| --- | --- |
| `GET /api/v1/xiaohongshu/app_v2/search_notes` | 搜索笔记（page + sort_type + note_type + time_filter） |
| `GET /api/v1/xiaohongshu/app_v2/get_image_note_detail` | 图文详情（首拉/抽屉懒加载） |
| `GET /api/v1/xiaohongshu/app_v2/get_video_note_detail` | 视频详情（同上） |
| `GET /api/v1/xiaohongshu/app_v2/get_user_info` | 博主完整资料（fans / follows / IP / verify / note_num_stat） |
| `GET /api/v1/xiaohongshu/app_v2/get_user_posted_notes` | 博主所有笔记（cursor 翻页） |
| `GET /api/v1/xiaohongshu/app_v2/search_users` | 搜索用户（关键词→博主） |

业务 envelope：
- top: `{code, request_id, message_zh, cache_url, router, data}`
- data: `{code=0, success=true, msg, search_id, search_session_id, page, next_page, data}`
- success 判定：`top.code==200 AND data.data.code==0 AND data.data.success==true`

---

## 3. 数据流与产物

```
scripts/import_tikhub.py        ───┐
scripts/enrich_tikhub.py        ───┤ (脚本 / 用户命令)
                                 │
                                 ▼
TikHubClient (Bearer auth, 429/5xx 指数退避, 限速)
                                 │
                ┌────────────────┼────────────────┐
                ▼                ▼                ▼
data/raw/tikhub/xhs/<kid>/       details/<note_id>.json
                ▼                ▼                users/<user_id>.json
                                 │
                                 ▼
entity_builder.build_entities(envelopes)
                                 │
                                 ▼
normalized/
  keywords.json (151), snapshots.json (151),
  accounts.json (1645), articles.json (2390),
  ranking_hits.json (3006), note_metric_observations.json (3006),
  monitor-data.json (派生层: 151 词 + 1645 博主 + 6 轴 XHS hexagon/三榜)
                                 │
                                 ▼
/api/monitor-data   /api/articles   /api/article-content
/api/article-hit-detail   /api/note-detail (懒加载)
/api/creator-detail (懒加载)   /api/keyword-manage
```

---

## 4. 字段映射（TikHub → 标准化）

| TikHub 字段 | 标准化字段 | 备注 |
| --- | --- | --- |
| `note.id` | `article_id` | 前缀 `xhs_tk_` 区分 provider |
| `note.title` | `title` | |
| `note.desc` | `summary` | 搜索摘要（截断）；详情接口拿完整 desc |
| `note.timestamp` | `published_at` | 毫秒 → ISO 8601 |
| `note.liked_count` | `liked_count` | |
| `note.collected_count` | `collected_count` | 核心信号 |
| `note.comments_count` | `comment_count` | |
| `note.shared_count` | `shared_count` | |
| `note.type` (`normal`/`video`) | `work_type` | |
| `note.images_list[0].url_size_large` | `cover_url` | |
| `note.user.userid` | `creator_id` | 24 字符 hex |
| `note.user.nickname` | `creator_name` | |
| `note.user.images.large` | `creator_avatar` | |
| `note.xsec_token` | `xsec_token` | 用 url encode 后拼入 URL |
| `user.desc` | `description` | |
| `user.fans` | `fans` | |
| `user.follows` | `follows_total` | |
| `user.ip_location` | `ip_location` | |
| `user.note_num_stat.posted` | `total_works` | |
| `user.red_official_verify_*` | `verify_info` | |

**原始链接生成**：`https://www.xiaohongshu.com/explore/<note_id>?xsec_token=<urlencoded>`
（避免浏览器侧防盗链；服务端代理 /api/article-cover-image 也支持）

---

## 5. 三榜 + 六边形口径

| 榜 | 窗口 | 6 轴 |
| --- | --- | --- |
| 账号分 (`score`) | 15 天 | 历史覆盖 / 近期覆盖 / 稳定笔记 / 持续经营 / 收藏矩阵 / 战场广度 |
| 时效分 | 3 天 | Top3 规模 / 新笔记冲榜 / 新进 Top3 / 连续冲榜 / 收藏动能 / 关键词扩散 |
| 当天分 | 1 天 | 今日 Top3 / 今日关键词 / 今日笔记 / 今日主题 / 排名质量 / 今日互动增长 |

互动主信号 = `collected + liked*0.7 + comment*1.5 + shared*1.5`；缺失字段保持 `null`。

---

## 6. 数据获取策略（避免高额计费）

| 时机 | 调 TikHub | 缓存到 |
| --- | --- | --- |
| 首抓/批量刷 | `search_notes page1 general` × 151 关键词 | `data/raw/tikhub/xhs/<kid>/<ts>_page_1.json` |
| 抽屉打开（详情懒加载） | `get_image_note_detail` 或 `get_video_note_detail` | `data/raw/tikhub/xhs/details/<note_id>.json` |
| 博主详情打开 | `get_user_info` | `data/raw/tikhub/xhs/users/<user_id>.json` |
| 大批 enrich（手动） | `enrich_tikhub.py --limit 100` 仅 Top 100/200 高频博主 | 同上 |

> 默认首抓**只调一次 search_notes × 151 关键词**（约 150 次调用）。
> 全文/详细博主资料等用户打开抽屉/详情时才按需拉，断点续跑写在 detail_service 层。

---

## 7. API 列表（13 类 + 38 路由）

见 `/api/monitor-data` (7.8MB) / `/api/articles` (with XHS 字段) / `/api/article-content?path=<id>`
/ `/api/article-hit-detail?article_id=...` / `/api/note-detail?note_id=...` / `/api/creator-detail?user_id=...`

---

## 8. 安全

- TIKHUB_API_TOKEN 仅在 `.env`（chmod 600），不入代码 / README / commit
- `/etc/.gitignore` 已加入 `.env`
- 请求头 `Authorization: Bearer <token>` 由 client 注入；日志仅记 status/error
- raw 文件保存只剥除 Authorization 头字段
- 服务端图片代理 `/api/article-cover-image` 仅放行 `xhscdn.net / xhscdn.com / xhs-img.com / xiaohongshu.com / rednotecdn.com / sns-img`

---

## 9. RedFox 历史数据隔离

- `data/raw/redfox/xhs/` 完整保留作历史审计
- `import_tikhub.py` 启动时自动 `.backup/redfox_<ts>/` 备份 old `normalized/*.json` 与 redfox raw
- `monitor-data.json` 与 `normalized/*` 完全由 TikHub 数据覆盖；upsert 模式下不会回填 RedFox 旧 ID
- provider 切换只改 `XHS_DATA_PROVIDER` 即可，默认 `tikhub`，旧 provider 仅作为运行时可选

---

## 10. 已知限制

| 项 | 现状 | 后续 |
| --- | --- | --- |
| 翻页（page>1） | TikHub 携带 search_id + search_session_id 可走；当前首抓只用 page1（20 条/词） | 加 `--offsets "0 20 40"` 跑多页 |
| 评论/收藏/关注数 | TikHub search 默认有 4 互动；详情完整字段 | – |
| 全文图片列表 | 抽屉懒加载 `get_image_note_detail` 才能拿到 | 已有缓存机制 |
| 100% 抓取率 | 少数特殊词返回空 items（status=empty） | 记录于 failures.jsonl，可后续重抓 |

---

## 11. 验收 / 自检

```bash
# 1. smoke probe
python3 scripts/tikhub_probe.py --keyword "友邦环宇盈活"

# 2. Flask test client contract check
python3 -c "
import sys; sys.path.insert(0, '.')
from app import create_app
app = create_app()
with app.test_client() as c:
    r = c.get('/api/monitor-data')
    print('monitor-data:', r.status_code, 'size:', len(r.data))
"

# 3. Headless browser verification (Playwright)
node /tmp/headless_correct.cjs   # 151 关键词/1645 博主/笔记列表 50 行/0 JS errors

# 4. 重要文件 / 目录
ls data/raw/tikhub/xhs/         # 151 关键词目录
ls normalized/                 # 7 个事实层 JSON
cat normalized/monitor-data.json | python3 -m json.tool | head
```
