# RedFox

调用 RedFox 豆包纯文字搜索，将单问题结果保存为 Markdown 快照。

## 调用

```bash
REDFOX_API_KEY=你的_key python3 run.py --question "225提领靠谱吗？"
```

## 入参

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `question` | `str` | 是 | 无 | 要搜索的单个问题 |
| `topic` | `str` | 否 | 自动分类 | 强制指定主分类 |
| `existing-task` | `str` | 否 | 无 | 复用 `QUESTION=TASK_ID` |
| `from-json` | `str` | 否 | 无 | 从已有 RedFox JSON 生成快照，不调用 API |
| `output-dir` | `str` | 否 | `data/redfox` | 快照输出目录 |
| `timeout` | `int` | 否 | `300` | 轮询超时秒数 |
| `interval` | `float` | 否 | `5` | 轮询间隔秒数 |
| `no-collections` | `bool` | 否 | `false` | 不生成分类目录页 |

## 出参

| 字段 | 类型 | 说明 |
|---|---|---|
| `output_dir` | `str` | 快照根目录 |
| `markdown_files` | `list[str]` | 新生成的 Markdown 相对路径列表 |

## 注意

- 真实调用会产生 RedFox 费用。
- API Key 只从 `REDFOX_API_KEY` 读取，不写入文件。
