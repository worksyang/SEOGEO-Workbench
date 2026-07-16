# 微信搜一搜原版镜像

这里保存从旧系统复制出的原始模板、CSS 和 JavaScript，只读用于回溯与差异核验。

- 来源：`/Users/works14/.claude/监控/wechat-ybxhyyh-top3/app/`
- 原始模板：`source/templates/monitor.html`
- 辅助模板：`source/templates/keyword_turnover.html`、`article_hit_detail.html`、`account_score_analysis.html`、`account_score_formula.html`
- 原始样式：`source/static/css/monitor.css`
- 原始脚本：`source/static/js/turnover-utils.js`、`monitor.js`、`article-list.js`、`keyword-turnover.js`、`article-hit-detail.js`、`article-list-demo.js`
- 工作台运行副本：`/Users/works14/Documents/zkcode/260712_SEO-GEO/workbench/frontend/public/legacy/wechat/`

运行副本只对辅助模板中的静态资源地址做了同源路径替换，使其由工作台托管；业务 HTML、CSS、JavaScript 和旧 API 契约不重写。账号评分两页依赖旧服务端 Jinja 上下文，由工作台只读代理旧 GET，服务不可用时保留真实 502，不伪造数据。旧系统目录本身保持不变。
