# T003–T010、T018、T019 静态基线与安全审计证据

> 生成时间：2026-07-16T01:27:22+08:00。本证据由只读脚本 `workbench/scripts/static_baseline_audit.py` 生成；脚本不修改 `source/`、Demo 或原系统。

## 结论

> **开发前后不变：NOT RUN。** 本次任务开始前没有同口径、已签名的前置快照，因此当前 hash/文件数只能证明现状，不能证明“开发前后”不变。

| 验收项 | 状态 | 证据与边界 |
|---|---|---|
| T003 | NOT RUN | 当前 `source/` 快照已保存，但缺少开发前快照。 |
| T004 | NOT RUN | `source/_local_full_backup/demo` 当前不存在；缺少开发前快照。 |
| T005 | NOT RUN | Demo 当前 hash 已保存；缺少开发前 hash。 |
| T006 | NOT RUN | 八个系统/资产目录当前快照已保存；无法证明未被写入。 |
| T007 | NOT RUN | 已生成八个来源标签的当前 hash/文件数摘要；缺少开发前 provenance 与复制时点证据。 |
| T008 | NOT RUN | 本次未执行 Codex 内置浏览器九页面正常态/详情态/弹窗态基线采集。 |
| T009 | NOT RUN | 已做静态路由摘要；未启动原服务、未采集参数/响应/错误样例。 |
| T010 | NOT RUN | 已保存文件扩展名、SQLite 表/行数、JSON 顶层键摘要；缺少开发前数据快照与对账。 |
| T018 | NOT RUN | Git/工作树/API/日志扫描已执行，但有命中待人工复核，不能据此 PASS。 |
| T019 | PASS | 本次工作树改动仅在 `docs/` 与 `workbench/scripts/`；提交前仍需复核暂存区。 |

## 1. 当前目录/文件基线

| 对象 | 存在 | 文件数 | 字节数 | 当前 SHA-256 |
|---|---:|---:|---:|---|
| `source/**` | 是 | 126107 | 4688099077 | `932e285c74a093ed58f22b21ce7432581bbc7e998b7e3b996e2bebe19acc4e5d` |
| `source/_local_full_backup/demo` | 否 | 0 | 0 | `—` |
| `unified-content-platform-demo.html` | 是 | 1 | 284748 | `68b8fbc37ae3891fe64672f7871f8fdfab6827c57499751125d78b03a23a173d` |

## 2. 八个原系统/资产的当前基线

路径故意不写入本文；使用系统标签对应项目协作规则中的来源。文件 hash 是“相对路径 + 文件 SHA-256”聚合 hash。

| 系统标签 | 文件数 | 字节数 | 当前 SHA-256 | SQLite 库/表 | 静态路由数 |
|---|---:|---:|---|---:|---:|
| 微信关键词 (`wechat_search`) | 109482 | 2938416390 | `e5cf27c8b4912d40572c1980dab087d47abe6f7b9ad5ef4eb175610fb486ea8e` | 17/45 | 46 |
| 公众号监控 (`wechat_mp`) | 7062 | 453605918 | `c35a88907268bc60a80364f43bb64db9bbaaab0361c4ab5d453c2b94c771d8bd` | 2/15 | 168 |
| 小红书关键词 (`xhs`) | 2186 | 928999631 | `4bc5f377a51c2cc45386470e6f360a5c218c24ae010ca0372b48ff0204e3b0aa` | 1/1 | 40 |
| GEO 观察 (`geo`) | 7946 | 344717141 | `047aace531c956c8f59ce4a1c2efa26701ffc4d96b2f18f8d8cfc73dd025f8c3` | 1/8 | 0 |
| 母文章 Wiki (`wiki`) | 25 | 4411078 | `db4ccb213b09008af009a7ff026ce864a65a1d0b512c39e95c883d70409f3248` | 0/0 | 0 |
| 母文章铸造/批量成稿 (`writing_money`) | 5 | 190030 | `d57b8d352d8076fa24e5c0308360ab30def7b588be9d35d0a9bc257a357270c6` | 0/0 | 0 |
| 写作与发布 (`wechat_publish`) | 650 | 64952852 | `57c51ce8c7f5d2685fddff859376af88cb0bff9699f2942c34bd2ea9c72d98eb` | 2/9 | 0 |
| 母文章库 (`mother_library`) | 1296 | 29688044 | `38544856c4d38755914b16dcb8dd55c63deb475b08cf400a104c59c0bed51bcd` | 0/0 | 0 |

### 数据基线摘要

脚本只保存数量、大小、SQLite 表名/行数和 JSON 顶层键计数，不保存正文、主键值、凭证或外部绝对路径。

- **微信关键词**：扩展名文件计数 `.ak8jym=1, .baf=3, .baj=3, .before_170_keyword_reclass_20260712_090544=1, .before_first_probe_noise_cleanup_20260712_085210=1, .before_keyword_discovery_20260712_013428=1, .bf=3, .css=4, .csv=4, .db=17`；SQLite 表行数合计 `14827`；JSON 顶层键摘要已写入机器证据。
- **公众号监控**：扩展名文件计数 `.0=2, .1=3, .11=1, .2=4, .3=4, .4=4, .5=4, .6=4, .7=4, .7z=1`；SQLite 表行数合计 `41`；JSON 顶层键摘要已写入机器证据。
- **小红书关键词**：扩展名文件计数 `.bak=1, .before=7, .css=1, .db=1, .example=1, .flag=2, .html=5, .js=6, .json=2056, .jsonl=22`；SQLite 表行数合计 `0`；JSON 顶层键摘要已写入机器证据。
- **GEO 观察**：扩展名文件计数 `.html=2, .json=14, .md=7893, .png=17, .py=10, .sh=1, .sqlite=1, <no_extension>=8`；SQLite 表行数合计 `44588`；JSON 顶层键摘要已写入机器证据。
- **母文章 Wiki**：扩展名文件计数 `.bak=1, .css=1, .html=4, .js=2, .json=3, .log=1, .md=3, .png=5, .py=4, <no_extension>=1`；SQLite 表行数合计 `0`；JSON 顶层键摘要已写入机器证据。
- **母文章铸造/批量成稿**：扩展名文件计数 `.css=1, .html=1, .js=1, .md=2`；SQLite 表行数合计 `0`；JSON 顶层键摘要已写入机器证据。
- **写作与发布**：扩展名文件计数 `.7z=1, .baf=1, .baj=1, .dat=1, .db=2, .db-journal=2, .gz=1, .json=4, .ldb=1, .log=12`；SQLite 表行数合计 `6`；JSON 顶层键摘要已写入机器证据。
- **母文章库**：扩展名文件计数 `.bak=1, .css=1, .html=4, .jpg=145, .js=2, .json=8, .log=11, .md=1051, .pdf=5, .png=19`；SQLite 表行数合计 `0`；JSON 顶层键摘要已写入机器证据。

## 3. API 契约静态摘要

> 仅是源码正则摘要，不能替代 T009 要求的运行态路由、方法、参数、响应、错误和样例。完整静态路由列表在机器证据 JSON 的 `systems.*.api_contract_summary.routes`。

| 系统标签 | 扫描文件数 | 路由数 |
|---|---:|---:|
| 微信关键词 | 100609 | 46 |
| 公众号监控 | 4853 | 168 |
| 小红书关键词 | 2139 | 40 |
| GEO 观察 | 7920 | 0 |
| 母文章 Wiki | 18 | 0 |
| 母文章铸造/批量成稿 | 5 | 0 |
| 写作与发布 | 78 | 0 |
| 母文章库 | 1113 | 0 |

## 4. T018 安全审计

扫描范围：Git tracked 内容、当前工作树文本/API/日志候选文件，以及八个来源目录中疑似 API/请求/响应/历史/日志文件。**不输出原始命中内容**。

| 范围 | 扫描文件/候选数 | 命中摘要 |
|---|---:|---|
| Git tracked + 当前工作树 | 110936 | {'absolute_sensitive_path': 13222, 'api_key': 16, 'cookie': 67, 'password': 22, 'token': 2}；Git 历史状态 `FAIL`，疑似命中提交数 `1`。 |
| 原系统 API/日志候选 | 18322 候选 / 18073 已扫描 | {'absolute_sensitive_path': 13750, 'api_key': 1, 'cookie': 5, 'password': 21}。 |

> 这些命中可能是变量名、文档说明、路径或真实凭证；由于本证据不保留原文，必须人工复核后才能将 T018 标 PASS。当前按规则维持 **NOT RUN**，不把扫描结果冒充通过。

## 5. T019 写入边界

- 本次新增文件：`workbench/scripts/static_baseline_audit.py`、`docs/static_baseline_audit_20260716.json`、本文档。
- 本次未改 `source/**`、`unified-content-platform-demo.html`、原系统目录或业务代码。
- 运行脚本只写入指定证据 JSON；它对外部目录和 Demo 只读。
- 最终提交前必须再次执行 `git diff --name-only`、暂存区检查和 `git status --short --branch`。

## 6. 复核命令

```bash
python3 workbench/scripts/static_baseline_audit.py
python3 -m json.tool docs/static_baseline_audit_20260716.json >/dev/null
git diff --name-only
git status --short --branch
```

## 7. 证据限制

- 本证据生成于 2026-07-16；原系统可能继续运行或写入，hash/行数是采集时点快照。
- `source/_local_full_backup/demo` 不存在，因此 T004 只能记录当前缺失，不能推断历史状态。
- 未执行真实 API 调用、真实抓取、写入/恢复、浏览器点击或开发前后双快照对账。
