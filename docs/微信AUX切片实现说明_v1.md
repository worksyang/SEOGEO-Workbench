# 微信迁移 AUX 切片实现说明

本切片新增 `legacy_aux_router.py` 与 `services/wechat_aux.py`，覆盖迁移计划 R05–R10、R13–R14、W01、W07；路由由后续集成代理注册，不修改 `app.py`、旧读取路由或旧服务。

- R05–R10 优先读取 `wechat_aux_artifacts` 冻结投影；未导入时仅从冻结目录的只读 JSON 回退。R07 固定返回冻结 `agent_projection_service` 的 8 项业务解释字典，不得用核心 `metric_definitions` 替代；R08 缺失证据精确返回 `{"error":"evidence not found: <id>"}`。证据 ID 使用 `[A-Za-z0-9_-]{3,180}`，不把绝对路径返回给客户端。
- R13 仅允许公开 HTTP/HTTPS 地址，拒绝回环、私网、链路本地、保留地址、非图片 MIME、魔数不匹配和超过 8 MiB 的响应；图片按 SHA-256 写入 `asset_store/wechat/cover/`，元数据进入 `wechat_aux_cover_cache`。
- W01 只使用冻结 normalized 文章记录，按输入顺序返回旧 shape，并通过 `command_runs`/`audit_log` 支持幂等重放；不重复下载。
- W07 使用 `AidsoProvider` 接口，默认 `disabled`，也支持录制 JSON provider；不会启动浏览器。`profile_dir`、Cookie、Token、API Key 等敏感字段进入持久化前会被清洗。
- 迁移冻结验收期间，R13/R14 GET 在 Hub 模式也固定返回与 8774 一致的 `REFERENCE_EXTERNAL_BLOCKED` 409 隔离回执，不执行 DNS、图片下载、浏览器或 Aidso provider；legacy/compare 原样透传 8774。W01/W07 仍固定走 Hub 受控服务。
- R13 的图片缓存/provider 安全实现保留给后续显式解除隔离后的阶段；provider 必须确认连接地址属于 URL 解析出的公共 IP 集合，响应禁止重定向并按流式 chunk 限制大小。
- `import_frozen_artifacts()` 会把 manifest、daily brief、metric dictionary、evidence、penalty signals、account aliases 全量导入 AUX 表；导入后冻结目录不可用仍可从 Hub 读取。

`0015_wechat_aux_runtime.sql` 只创建 AUX 运行表，不改已有迁移文件或 checksum。

> `20260716T050010` shadow 暴露的 R07/R08/R13/R14 差异已按冻结源码与隔离回执完成专项修复和单元/路由回归；本轮遵守护栏，未重新执行完整 shadow。
