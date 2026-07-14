# Web

本地观测页面、API 和共用 UI。

```text
web/
├── __init__.py       # Python 包标记
├── run.py            # 8790 Web 服务、SQLite 读取、API 和本地导入
├── ui.py             # 共用 UI、展示数据与 8788 Demo
└── install_macos.sh  # macOS launchd 安装与重启
```

项目只从根入口启动：

```bash
python3 run.py         # http://127.0.0.1:8790/
python3 run.py --demo  # http://127.0.0.1:8788/
```

macOS 后台运行：

```bash
./web/install_macos.sh
```

服务标签为 `com.geopromax.web`，日志位于 `/tmp/geopromax_web_8790.launchd.log` 和 `/tmp/geopromax_web_8790.launchd.error.log`。

页面按 `data/platforms.json` 的显式别名聚合平台，同时在 API 中保留 `raw_platform` / `raw_platforms`。平台聚合不会修改 SQLite、Markdown 或来源 ID。

8790 仅从 `data/index/geopromax.sqlite` 读取本地采集快照，回答正文按 SQLite 记录的路径读取 Markdown；当前不合并 RedFox 快照。

顶部的“导入本地数据”可选择豆包 GEO JSON，调用 `JSON转数据` 原子能力完成原始归档、Markdown 和 SQLite 入库；单文件上限 100 MB。
