# 汇入源码说明

本目录保存统一内容工作台依赖的既有系统源码。各系统仍保持原有内部结构，当前只做集中归档，不在本次复制中重构。

## 两层保留策略

- 顶层八个系统目录：可开发、可提交 Git 的源码基线，排除了大型运行数据与真实凭证。
- `_local_full_backup/`：八套原始目录的完整本地镜像，包括历史 Markdown、抓取快照、数据库、运行数据、缓存和原项目 Git 历史。该目录整体被根目录 `.gitignore` 忽略，不上传 GitHub。

`_local_full_backup/` 是历史恢复层，不建议直接开发；后续开发仍在顶层系统目录进行。需要恢复遗漏数据时，从完整镜像复制或通过迁移脚本读取。

完整镜像可通过下面的脚本增量更新：

```bash
bash source/sync_local_full_backup.sh
```

微信搜一搜、小红书和 GEO 可能仍在持续写入。**停用或删除旧目录前必须先停止相关采集任务，再运行一次同步脚本**，确认无差异后才能迁移。

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

以下内容没有进入**可提交源码层**，但会完整保存在本地 `_local_full_backup/`：

- 各项目内部 `.git/` 历史。
- `node_modules/`、虚拟环境、Python 缓存、测试缓存和浏览器自动化产物。
- 浏览器用户目录、Cookie、Token、API Key、`.env` 和私钥。
- 微信搜一搜、小红书及 GEO 的大体积原始抓取数据、标准化聚合数据、运行数据库和历史备份。
- 公众号监控的测试数据集与历史输出目录。
- 可重新生成的截图、日志和大型静态 Demo 产物。

原始数据既保留在原位置，也保存在 `_local_full_backup/`。因此删除或停用旧项目目录前，应先执行完整性校验，并另外建立磁盘外备份。

## 凭证处理

复制过程中发现的硬编码 API Key 已从副本移除，相关代码改为读取环境变量：

- `DASHSCOPE_API_KEY`
- `SILICONFLOW_API_KEY`
- `OPENAI_API_KEY`

原始系统中的旧密钥未被修改；如这些密钥仍有效，建议单独轮换。
