# GEOProMax 原版镜像说明

- 原系统入口：`/Users/works14/Documents/zkcode/GEOProMax/web/run.py`
- 真实运行端口：`127.0.0.1:8790`
- 工作台承载入口：`/legacy/geo/index.html`
- 页面采用原系统服务端生成的完整 HTML 原样代理，避免复制 35MB 运行快照或篡改原页面；其 `/api/data`、`/api/import-json` 请求经工作台白名单代理返回原系统真实结果。
- `provenance.json` 保存原入口与 UI 模板的 SHA-256 证据；原项目目录保持只读。
