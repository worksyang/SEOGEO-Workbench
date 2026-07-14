# 汇入源码说明

本目录保存统一内容工作台依赖的既有系统源码。各系统仍保持原有内部结构，当前只做集中归档，不在本次复制中重构。

| 目录 | 原始位置 | 本次内容 |
|---|---|---|
| `wechat-search-monitor/` | `/Users/works14/.claude/监控/wechat-ybxhyyh-top3/` | 微信搜一搜监控源码、脚本、模板、测试和文档 |
| `wechat-mp-monitor/` | `/Users/works14/Documents/zkcode/250626_mpGUI/` | 公众号监控前后端、文章工作流和转换工具 |
| `xhs-keyword-monitor/` | `/Users/works14/Documents/zkcode/取数/xhs-keyword-monitor/` | 小红书关键词监控源码、脚本、测试和文档 |
| `geopromax/` | `/Users/works14/Documents/zkcode/GEOProMax/` | GEOProMax Web、原子能力、脚本和架构文档 |
| `wiki-viewer/` | `/Users/works14/Documents/output_md/wiki-viewer/` | Wiki Viewer 源码和辅助工具 |
| `writing-money/` | `/Users/works14/Documents/output_md/wiki-viewer/WritingMoney/` | WritingMoney Demo 源码 |
| `wechat-publish-system/` | `/Users/works14/Documents/zkcode/YZKcode/1126WritePublish/` | 老写作与微信公众号发布系统源码 |
| `mother-article-library/` | `/Users/works14/Documents/output_md/` | 母文章库及现有 Markdown 内容，不重复包含 `wiki-viewer/` |

## 未复制内容

为保证仓库可推送、可审计且不泄露凭证，本次没有复制以下内容：

- 各项目内部 `.git/` 历史。
- `node_modules/`、虚拟环境、Python 缓存、测试缓存和浏览器自动化产物。
- 浏览器用户目录、Cookie、Token、API Key、`.env` 和私钥。
- 微信搜一搜、小红书及 GEO 的大体积原始抓取数据、标准化聚合数据、运行数据库和历史备份。
- 公众号监控的测试数据集与历史输出目录。
- 可重新生成的截图、日志和大型静态 Demo 产物。

原始数据仍保留在上表所列原始位置。本目录的目标是成为后续统一开发的源码基线，而不是替代现有生产数据仓库。

## 凭证处理

复制过程中发现的硬编码 API Key 已从副本移除，相关代码改为读取环境变量：

- `DASHSCOPE_API_KEY`
- `SILICONFLOW_API_KEY`
- `OPENAI_API_KEY`

原始系统中的旧密钥未被修改；如这些密钥仍有效，建议单独轮换。
