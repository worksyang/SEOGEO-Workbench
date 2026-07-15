# 公众号监控原版镜像

- 来源：`/Users/works14/Documents/zkcode/250626_mpGUI/client/dist/`
- 用途：统一工作台的只读原版业务岛屿镜像。
- 页面由 `workbench/frontend/public/legacy/mp/` 同源托管；旧系统的 `/api/*` 请求经工作台白名单代理回到 `127.0.0.1:28765`。
- `source/` 与原项目目录保持只读，本镜像不反向修改原系统。
