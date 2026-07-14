# 保存Markdown正文

把统一数据中的 AI 回答和来源正文保存为纯 Markdown，不写 frontmatter。

## 调用

```bash
python3 run.py --input 统一.json
```

## 入参

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `input` | `str` | 是 | 无 | 统一 JSON 路径 |
| `root` | `str` | 否 | 项目根目录 | Markdown 路径根目录 |

## 出参

| 字段 | 类型 | 说明 |
|---|---|---|
| `answer_files` | `int` | 本次新增或更新的回答 Markdown 数 |
| `source_files` | `int` | 本次新增或更新的来源 Markdown 数 |
