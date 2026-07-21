# 微信港险早报 Agent 流水线：运行、迁移与回滚

## 边界

- 事实源：Hub SQLite 与 `127.0.0.1:8799`。
- 旧 top3、`source/**`、`legacy_mirrors/**` 永久只读。
- 生产器永不发送通知；通知仅由正式晨报 Task 在全部门禁通过后调用。
- 前端与既有 `/api/agent/*` 路由不变。

## 构建模式

```bash
# 只生成/校验，不公开、不通知
python3 workbench/scripts/build_wechat_agent_daily_brief.py --validate

# 生成/校验后原子发布到 AUX artifacts，不通知
python3 workbench/scripts/build_wechat_agent_daily_brief.py --validate --publish

# 可选影子文件，只能写项目 data/ 下
python3 workbench/scripts/build_wechat_agent_daily_brief.py \
  --validate \
  --output-dir data/agent/shadow-YYYYMMDD
```

发布语义：相同事实 fingerprint 重跑返回同一 published run；新 run 在 staging 中完成 schema、大小、证据闭包和新鲜度校验后，才持单写锁、单 SQLite 事务发布。中途失败继续提供上一份已发布观察包。

## 数据状态

- `normal`：稳定监控词近 24 小时覆盖率至少 90%，数据年龄不超过 24 小时，允许在监控样本边界内形成强结论。
- `cautious`：覆盖率 70%–90% 或数据年龄 24–48 小时，只能谨慎描述有限样本，不应形成强判断。
- `blocked`：覆盖率低于 70% 或数据年龄超过 48 小时，禁止行业/需求/排序强结论和业务推送。

Task 必须先读 manifest，再读 metric dictionary、brief 和最终采用候选的 evidence。任何端点非 200、brief/manifest 不一致、证据缺失或数据过期，均只写执行记录并静默退出。

## Decision / claims

正式业务推送成功后才应用 decision：

```bash
python3 workbench/scripts/build_wechat_agent_daily_brief.py \
  --apply-decision data/agent/morning-brief/decisions/<file>.json \
  --idempotency-key <brief-id>
```

Decision 必须位于受控目录，且 `run_id`、`brief_id` 必须匹配已发布 run。相同 idempotency key 重放不会重复推进 claims。

## 切换顺序

1. 在线备份 Hub，并做 integrity check/restore drill。
2. `--validate` 影子生成；禁止 Task、禁止通知。
3. 固定事实水位与旧投影做 golden compare。
4. 新四接口逐项验证：metric dictionary → evidence → brief → manifest。
5. 迁移 MEMORY、噪音库、Wiki、历史记录和 claims。
6. 用 Task 创建器重新生成同 ID wrapper/plist，保持 unloaded。
7. Prompt 只通过 `/api/tasks/wechat-ybxhyyh-top3/prompt` 保存。
8. 完成 NO_PUSH wrapper 预检、影子正文和幂等复跑。
9. 所有门通过后加载同一 Task；等待下一自然日 08:00 正式运行。

## 回滚

- 影子/预检失败：Task 保持 unloaded，保留失败 run 和日志。
- API 异常：Agent read contracts 切回 rollback mode；新 artifact 保留供复盘。
- Task 异常：unload 新 Task，先查 PushPlus trace 判断是否已经成功送达；已成功则禁止重试。
- Prompt 通过 Task Manager `/prompt` 恢复；wrapper/plist 使用实施前备份和创建器恢复。
- 默认不自动重新启用旧写链。
- 整库恢复只用于灾难恢复，必须停所有 writer 并单独确认。
