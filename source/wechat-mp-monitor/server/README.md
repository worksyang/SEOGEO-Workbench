# MP GUI Web Backend

本目录是第一版网页控制台后端，负责调用 WeRSS、维护本地 SQLite 状态、启动现有抓取工作流。

```bash
python3 -m uvicorn server.app:app --reload --host 127.0.0.1 --port 28765 --no-access-log
```

`--no-access-log` 用来避免启动终端或管道断开后，频繁轮询 `/api/jobs/{job_id}` 时触发 Uvicorn 访问日志的 `BrokenPipeError`。
