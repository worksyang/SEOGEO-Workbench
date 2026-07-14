# 新公众号爬取与 Markdown 入库 SOP

## 前置条件
- 微信已扫码登录 WeRSS（`/api/v1/wx/auth/qr/status` 返回 `login_status: true`）
- 项目根目录存在 `SomeURL2MD/wechat_to_markdown.py`

## 操作流程

### 1. 搜索并添加公众号
```
POST /api/v1/wx/mps/search/{关键词}     → 获取 mp_id（Base64 编码）和头像
POST /api/v1/wx/mps                    → 提交 mp_name, mp_id, avatar
```

### 2. 等待初始抓取
首次添加后系统自动抓取约 26 篇文章，等待 15 秒。

### 3. 触发历史抓取
```
GET /api/v1/wx/mps/update/{mp_id}?start_page=0&end_page=20
```
异步执行，等待 60 秒。

### 4. 获取全部文章列表并提取 URL
```
GET /api/v1/wx/articles?limit=100&mp_id={mp_id}
```
遍历所有页，收集每篇文章的 `url` 和 `title`。

### 5. 用 URL 直接下载 Markdown（关键步骤）
**不要依赖 API 返回的 content 字段**——WeRSS 首次抓取通常只存摘要，`has_content` 也不可靠。

必须使用 `SomeURL2MD/wechat_to_markdown.py` 的 `url_to_markdown_content()` 方法，逐篇通过微信原文 URL 直接抓取 HTML 并转为 Markdown，写入本地文件。

### 6. 检查
确认本地文件数 = API 文章总数，且每个文件 > 800 字节。
