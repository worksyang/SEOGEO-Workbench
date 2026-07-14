# JSON转数据

把 `data/raw/` 内的原始 JSON 转为 Markdown 正文和 SQLite 索引。默认扫描全部原始数据，无需传入参数。

```bash
python3 原子能力/JSON转数据/run.py
```

Web 单文件导入调用 `run.ingest_bytes(content)`；文件会先按采集时间归档，再完成同一套入库流程。

## 子能力

- `拆解JSON`：将不稳定的原始结构归一化。
- `保存Markdown正文`：保存 AI 回答和来源正文。
- `写入SQLite`：写入八张关系索引表。

## 出参

| 字段 | 类型 | 说明 |
|---|---|---|
| `files` | `int` | 扫描到的 JSON 数 |
| `imported` | `int` | 新入库数 |
| `skipped` | `int` | 已入库跳过数 |

`ingest_bytes` 额外返回 `status`、`raw_file`、`archived`、`answers`、`sources`、`answer_files` 和 `source_files`。

## 注意

- 单文件默认归档到 `data/raw/豆包/mobile/<mode>/YYYY-MM-DD-HH-mm-ss.json`。
- 顶层和结果均未记录模式时按 `quick` 归档。
- 同一采集时间存在不同文件时拒绝覆盖。
