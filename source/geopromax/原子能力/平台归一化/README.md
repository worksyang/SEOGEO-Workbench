# 平台归一化

保留采集平台原名，并映射为内容分析使用的标准平台。

## 调用

```bash
python3 run.py
python3 run.py "手机网易网"
```

## 入参

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `platform` | `str` | 否 | 无 | 单个平台原名；省略时统计 SQLite |
| `config` | `str` | 否 | `data/platforms.json` | 平台配置路径 |
| `database` | `str` | 否 | `data/index/geopromax.sqlite` | 来源数据库路径 |
| `limit` | `int` | 否 | `50` | 统计结果数量 |

## 出参

| 字段 | 类型 | 说明 |
|---|---|---|
| `raw_platform` | `str` | 原始平台名，单平台模式返回 |
| `platform` | `str` | 标准平台名，单平台模式返回 |
| `icon_url` | `str` | 可选稳定图标地址，单平台模式返回 |
| `sources` | `int` | 来源总数，统计模式返回 |
| `raw_platforms` / `platforms` | `int` | 聚合前后平台数，统计模式返回 |
| `mapped_sources` | `int` | 命中别名映射的来源数，统计模式返回 |
| `top` | `list` | 标准平台聚合结果，统计模式返回 |

## 注意

- 只使用显式别名，不按域名或相似文本自动合并。
- 归一化不会修改原始数据或来源 ID。
