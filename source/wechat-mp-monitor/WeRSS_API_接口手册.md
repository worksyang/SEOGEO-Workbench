# WeRSS (we-mp-rss) API 接口调用手册

> 项目地址：https://github.com/rachelos/we-mp-rss
> 本地部署地址：`http://192.168.31.89:8001`
> API 基础路径：`/api/v1/wx`
> 在线文档：`http://192.168.31.89:8001/api/docs`

---

## 一、认证机制

### 1.1 用户登录（获取 Bearer Token）

所有业务接口都需要在 Header 中携带 Token：

```
Authorization: Bearer {access_token}
```

**获取 Token：**

```bash
curl -X POST "http://192.168.31.89:8001/api/v1/wx/auth/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=admin&password=admin@123&grant_type=password"
```

**响应：**

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer",
  "expires_in": 259200
}
```

### 1.2 微信扫码登录（获取微信 Cookie）

这是系统正常工作的前提条件。微信公众号的搜索、文章抓取等功能都依赖微信 Cookie。

```
获取二维码 → 手机扫码 → 确认登录 → Cookie 自动保存
```

**相关接口：**

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/v1/wx/auth/qr/code` | GET | 获取登录二维码链接 |
| `/api/v1/wx/auth/qr/image` | GET | 获取二维码图片 |
| `/api/v1/wx/auth/qr/status` | GET | 查询扫码状态 |
| `/api/v1/wx/auth/qr/over` | GET | 完成扫码确认 |

**检查登录状态：**

```bash
TOKEN="你的access_token"

curl -s "http://192.168.31.89:8001/api/v1/wx/auth/qr/status" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{
  "code": 0,
  "data": {
    "login_status": false,
    "qr_code": false
  }
}
```

- `login_status: false` → 微信未登录
- `qr_code: false` → 二维码未生成或已过期

### 1.3 Access Key (API 密钥)

除了用户名密码登录，系统还支持创建长期有效的 API Key：

```bash
# 创建 AK
curl -X POST "http://192.168.31.89:8001/api/v1/wx/auth/ak/create" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "脚本调用", "description": "自动化脚本使用"}'

# 使用 AK 调用接口（在 Header 中）
Authorization: AK-SK {ak_sk_value}
```

---

## 二、公众号管理

### 2.1 获取公众号列表

```bash
curl -s "http://192.168.31.89:8001/api/v1/wx/mps?limit=100&offset=0" \
  -H "Authorization: Bearer $TOKEN"
```

**参数：**

| 参数 | 类型 | 说明 |
|------|------|------|
| limit | int | 每页数量，1-100 |
| offset | int | 偏移量 |
| kw | string | 搜索关键词（可选） |
| status | int | 1=启用，0=停用（可选） |

**响应字段：**

```json
{
  "id": "MP_WXS_3886557904",
  "mp_name": "木木小财女",
  "mp_cover": "封面图路径",
  "mp_intro": "公众号简介",
  "status": 1,
  "created_at": "2025-11-15T15:25:16"
}
```

### 2.2 搜索公众号（需要微信 Cookie）

```bash
curl -s "http://192.168.31.89:8001/api/v1/wx/mps/search/云林学社" \
  -H "Authorization: Bearer $TOKEN"
```

**需要微信已登录**。未登录时返回：

```json
{
  "detail": {
    "code": 50001,
    "message": "搜索公众号失败,请重新扫码授权！"
  }
}
```

### 2.3 添加公众号

```bash
curl -X POST "http://192.168.31.89:8001/api/v1/wx/mps" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "mp_name": "云林学社",
    "mp_id": "MzIxMjA3NTE4MA==",
    "mp_cover": "",
    "avatar": "",
    "mp_intro": ""
  }'
```

**参数说明：**

| 参数 | 必填 | 说明 |
|------|------|------|
| mp_name | 是 | 公众号名称 |
| mp_id | 是 | 公众号ID（Base64 编码），从搜索接口获取 |
| mp_cover | 否 | 封面图 URL |
| avatar | 否 | 头像 URL |
| mp_intro | 否 | 简介 |

添加成功后会**自动触发首次文章抓取**（默认抓取 2 页）。

### 2.4 通过文章链接添加公众号

```bash
curl -X POST "http://192.168.31.89:8001/api/v1/wx/mps/by_article?url=https://mp.weixin.qq.com/s/xxxxx" \
  -H "Authorization: Bearer $TOKEN"
```

自动从文章链接中提取公众号信息并添加。

### 2.5 更新公众号文章（触发爬虫）

```bash
curl -s "http://192.168.31.89:8001/api/v1/wx/mps/update/{mp_id}?start_page=0&end_page=1" \
  -H "Authorization: Bearer $TOKEN"
```

**注意：**
- 有频率限制（默认 60 秒间隔）
- 异步执行，调用后等待约 10 秒让爬虫完成

### 2.6 获取公众号详情

```bash
curl -s "http://192.168.31.89:8001/api/v1/wx/mps/{mp_id}" \
  -H "Authorization: Bearer $TOKEN"
```

### 2.7 更新公众号信息

```bash
curl -X PUT "http://192.168.31.89:8001/api/v1/wx/mps/{mp_id}" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"mp_name": "新名称", "status": 1}'
```

### 2.8 删除公众号

```bash
curl -X DELETE "http://192.168.31.89:8001/api/v1/wx/mps/{mp_id}" \
  -H "Authorization: Bearer $TOKEN"
```

---

## 三、文章管理

### 3.1 获取文章列表

```bash
curl -s "http://192.168.31.89:8001/api/v1/wx/articles?limit=100&offset=0&mp_id={mp_id}" \
  -H "Authorization: Bearer $TOKEN"
```

**参数：**

| 参数 | 类型 | 说明 |
|------|------|------|
| limit | int | 每页数量，1-100 |
| offset | int | 偏移量 |
| mp_id | string | 筛选指定公众号（可选） |
| keyword | string | 搜索关键词（可选） |

**文章字段：**

```json
{
  "id": "文章ID",
  "title": "文章标题",
  "url": "https://mp.weixin.qq.com/s/xxx",
  "content": "Markdown/HTML 内容",
  "description": "文章摘要",
  "publish_time": 1712345678,
  "pic_url": "封面图",
  "mp_id": "公众号ID",
  "status": 1
}
```

### 3.2 添加精选文章（单篇抓取）

```bash
curl -X POST "http://192.168.31.89:8001/api/v1/wx/mps/featured/article" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://mp.weixin.qq.com/s/xxxxx"}'
```

返回任务 ID，异步抓取：

```json
{
  "task_id": "uuid",
  "url": "文章链接",
  "status": "pending"
}
```

**查询任务状态：**

```bash
curl -s "http://192.168.31.89:8001/api/v1/wx/mps/featured/article/tasks/{task_id}" \
  -H "Authorization: Bearer $TOKEN"
```

### 3.3 刷新文章内容

```bash
curl -X POST "http://192.168.31.89:8001/api/v1/wx/articles/refresh/{article_id}" \
  -H "Authorization: Bearer $TOKEN"
```

### 3.4 清理无效文章

```bash
curl -X DELETE "http://192.168.31.89:8001/api/v1/wx/articles/clean" \
  -H "Authorization: Bearer $TOKEN"
```

删除 mp_id 不再存在于公众号列表中的文章。

---

## 四、导出与导入

### 4.1 导出公众号列表（CSV）

```bash
curl -s "http://192.168.31.89:8001/api/v1/wx/export/mps/export" \
  -H "Authorization: Bearer $TOKEN" -o mps.csv
```

### 4.2 导入公众号列表（CSV）

```bash
curl -X POST "http://192.168.31.89:8001/api/v1/wx/export/mps/import" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@mps.csv"
```

CSV 必需列：`公众号名称`、`封面图`、`简介`

### 4.3 导出 OPML（RSS 订阅格式）

```bash
curl -s "http://192.168.31.89:8001/api/v1/wx/export/mps/opml" \
  -H "Authorization: Bearer $TOKEN" -o subscriptions.opml
```

可直接导入 Folo、NetNewsWire 等 RSS 阅读器。

---

## 五、RSS 订阅

每个公众号都有独立的 RSS Feed：

```
http://192.168.31.89:8001/feed/{mp_id}.atom
```

例如：
```
http://192.168.31.89:8001/feed/MP_WXS_3886557904.atom
```

---

## 六、系统信息

```bash
curl -s "http://192.168.31.89:8001/api/v1/wx/sys/info" \
  -H "Authorization: Bearer $TOKEN"
```

**关键字段：**

```json
{
  "core_version": "1.5.0",
  "wx": {
    "login": false,
    "token": "微信Token",
    "expiry_time": "Cookie 过期时间",
    "info": {
      "expiry": {
        "remaining_seconds": 345597,
        "expiry_time": "2026-04-09 16:30:38"
      }
    }
  }
}
```

- `wx.login` → 微信是否已登录
- `wx.info.expiry.expiry_time` → Cookie 过期时间
- `wx.info.expiry.remaining_seconds` → 剩余有效秒数

---

## 七、完整操作流程

### 添加新公众号的标准流程

```
步骤 1：确保微信已扫码登录
  → GET /api/v1/wx/auth/qr/code    获取二维码
  → 手机扫码确认
  → GET /api/v1/wx/auth/qr/status  确认登录成功

步骤 2：搜索公众号
  → GET /api/v1/wx/mps/search/{关键词}
  → 获取 mp_id（Base64 编码的公众号 ID）

步骤 3：添加公众号
  → POST /api/v1/wx/mps
  → 自动触发首次文章抓取

步骤 4：等待抓取完成后查看文章
  → GET /api/v1/wx/articles?mp_id={mp_id}
```

### 当前状态快照（2026-04-10）

| 项目 | 状态 |
|------|------|
| 服务运行 | 正常 |
| 用户登录 | 已登录（admin） |
| 微信 Cookie | **已过期**（2026-04-09 16:30:38） |
| 微信登录 | **未登录** |
| 已关注公众号 | 21 个 |
| 搜索"云林学社" | **失败**（需先扫码登录微信） |
