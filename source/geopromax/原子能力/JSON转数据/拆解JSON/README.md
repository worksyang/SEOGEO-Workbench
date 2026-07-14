# 拆解JSON

把一份原始豆包 GEO JSON 拆成统一的批次、回答、工具、来源和关系结构。

会计学堂的创作者以页面“来源”字段为准，当前为“网友分享”；不使用正文中的“我是……”自称。

## 调用

```bash
python3 run.py --input 原始.json --output 统一.json
```

## 入参

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `input` | `str` | 是 | 无 | 原始 JSON 路径 |
| `output` | `str` | 是 | 无 | 统一 JSON 临时路径 |

## 出参

| 字段 | 类型 | 说明 |
|---|---|---|
| `output` | `str` | 统一 JSON 路径 |
| `answers` | `int` | 回答数量 |
| `sources` | `int` | 来源数量 |
