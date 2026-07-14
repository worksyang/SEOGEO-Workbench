# 写入SQLite

把统一数据按事务写入 GEOProMax SQLite 八表索引。

## 调用

```bash
python3 run.py --input 统一.json
```

## 入参

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `input` | `str` | 是 | 无 | 统一 JSON 路径 |
| `database` | `str` | 否 | `data/index/geopromax.sqlite` | SQLite 路径 |

## 出参

| 字段 | 类型 | 说明 |
|---|---|---|
| `status` | `str` | `imported` 或 `skipped` |
| `database` | `str` | SQLite 路径 |
| `counts` | `object` | 八张表当前行数 |
