# GEOProMax

GEO 公域引用观察台 Demo：用于观察问题、AI 回答快照、引用源、平台、作者与引用位次随时间变化的事实数据。

## 运行

```bash
python3 run.py
```

默认地址：<http://127.0.0.1:8790/>。`run.py` 是唯一对外启动入口。

如需只启动 8788 Demo：

```bash
python3 run.py --demo
```

## Web

Web 代码统一位于 `web/`：`ui.py` 提供共用 UI 和 Demo，`run.py` 提供正式页面、API 和单问题 RedFox 刷新。目录说明见 [`web/README.md`](web/README.md)。

如果要真实调用 RedFox，启动前设置环境变量：

```bash
REDFOX_API_KEY=你的_key python3 run.py
```

刷新入口只做**单个问题手动刷新**，没有全局刷新、批量刷新或自动刷新。

macOS 如需后台运行：

```bash
./web/install_macos.sh
```

## 当前口径

- 这是事实观察 Demo，不是策略建议平台。
- 核心对象：问题、采集时间点、AI 回答原文、引用源、平台、作者、引用位次。
- 时间维度：用 answer snapshot 展示约 3 小时粒度的采集形态。

## 数据

当前使用 `data/raw/豆包/mobile/quick/` 下的完整手机端豆包 JSON。运行以下命令可将原始 JSON 拆解为 Markdown 正文和 SQLite 八表索引：

```bash
python3 原子能力/JSON转数据/run.py
```

目录和表结构见 [`data/README.md`](data/README.md) 和 [`docs/数据架构与入库说明.md`](docs/数据架构与入库说明.md)。

平台分析使用独立的显式别名表，不修改原始平台名和来源 ID：

```bash
python3 原子能力/平台归一化/run.py
python3 原子能力/平台归一化/run.py "手机网易网"
```

规则位于 `data/platforms.json`，详细用法见 [`原子能力/平台归一化/README.md`](原子能力/平台归一化/README.md)。

## RedFox

RedFox 是独立原子能力，仅保留单问题手动刷新，快照保存在本地 `data/redfox/` 且不进入 Git。详细用法见 [`原子能力/RedFox/README.md`](原子能力/RedFox/README.md)。
