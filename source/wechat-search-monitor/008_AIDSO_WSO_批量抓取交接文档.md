# AIDSO WSO 批量抓取任务交接文档

> 生成时间：2026-06-23  
> 最后更新：2026-06-23  
> 当前状态：**Playwright MCP 直接调用已验证可用，直接用 Playwright MCP 抓取；会员账号已升级为会员版本**

## 0. 状态修正（2026-06-23）

本文档早期版本的核心前提——"Playwright MCP 直接调用不可用、必须用 browser-use subagent"——**已被推翻**：

- 当前环境（Claude Code + `mcp__playwright__browser_*` 工具集）Playwright MCP 完全可用：导航、`browser_type` 填值、`browser_click`、`browser_evaluate` 提取表格，整条链路全程通畅。
- 早期所谓 `tool not found` 是 Cursor 客户端的工具名映射问题，与 Playwright MCP 本身无关；在 Claude Code 下不存在此问题。
- 账号已从临时账号升级为**会员版本**，核心字段（月均搜索量/月均点击量/下拉词数量）可正常返回；"竞争程度"列对部分词仍显示 `-`，疑似需更高会员等级，但不影响主数据抓取。
- 之前 `normalized/aidso_wso_heat.json` 中大量 `error: no_data` 的条目，根因正是当时无会员登录态；**本次需用会员账号重抓这批 `no_data` 词**。

---

## 1. 任务目标

从 AIDSO 爱搜（`dso.aidso.com/KeywordWSO/searchWord`）批量抓取微信 WSO 关键词月均搜索量，增量合并到 `normalized/aidso_wso_heat.json`。

---

## 2. 已完成的准备工作

### 2.1 输入框操作验证（关键突破）

- **问题**：早期手动拼接 URL 导致中文乱码/错字（如"创逸致富"→"創逸致富"、"万通尚裕"→"万通尚装"）
- **修复方案**：改为页面输入框操作
  - 定位输入框：`placeholder = "输入关键词，可多词联查，例:\"服装+定制\""`（页面第 3 个 input，索引 `inputs[2]`，visible=true）
  - 操作流：`fill(关键词) → 校验输入框值 == 关键词（防乱码）→ 点击"挖掘"按钮(div.search_r) → 等待表格加载 → 提取数据`
- **验证结果（2026-06-23 复测，会员账号）**：
  - 填入"创逸致富"后，输入框回读值为 `创逸致富`，**无乱码**
  - 结果表格正常返回 4 条相关结果，主词"创逸致富" `月均搜索量=0 / 月均点击量=0 / 下拉词数量=3`
  - 页面 URL 正确编码：`keyword=%E5%88%9B%E9%80%B8%E8%87%B4%E5%AF%8C`
  - 提取方式：`browser_evaluate` 读取第 2 个 `<table>`（index=1，含数据行），第 1 个 table 是表头，第 2 个是数据，第 3/4 个是操作列

### 2.2 词表筛选

- 总监控词：**145 个**（来自 `data/config/keywords.json`）
- 已抓取词：**87 个**（来自 `normalized/aidso_wso_heat.json`，含 `fetched_at` 且 `error != no_data`）
- 已搜主词：**26 个**（第一轮主词，见下方列表）
- **待抓取词表**：**61 个**（2~6 字，排除已搜 + 已抓取）

### 2.3 已搜 26 个主词（第一轮）

```
友邦环宇盈活、友邦财富盈活、安盛盛利2、保诚信守明天、富卫盈聚天下2、
永明星河尊享2、匠心传承2、匠心飞越、国寿傲珑盛世、友邦 财富盈活、
周大福匠心传承 2、周大福匠心飞越、安盛盛利 2、财富盈活 保险、鑫安逸、
保诚骏誉财富、太保世代悦享3、万通富饶千秋、太保鑫安逸、国寿智裕世代、
香港友邦、香港安盛、香港保诚、香港宏利、香港富卫、香港永明
```

---

## 3. 待抓取词表（61 个）

| 序号 | 关键词 | 备注 |
|------|--------|------|
| 1 | 香港中银人寿 | |
| 2 | 香港忠意保险 | |
| 3 | 香港太平人寿 | |
| 4 | 香港苏黎世 | |
| 5 | 财富盈活环宇 | |
| 6 | 港险提领密码 | |
| 7 | 盛利2提领 | |
| 8 | 星河2提领 | |
| 9 | 258提领 | |
| 10 | 财富盈活 提取 | 含空格 |
| 11 | 港险分红实现 | |
| 12 | 港险保证现价 | |
| 13 | 港险退保价值 | |
| 14 | 港险避坑 | |
| 15 | 香港保险返佣 | |
| 16 | 不赴港投保 | |
| 17 | 香港保险经纪 | |
| 18 | 港险退保 | |
| 19 | 广发保费融资 | |
| 20 | 中银保费融资 | |
| 21 | 蚂蚁保费融资 | |
| 22 | 活然人生 | |
| 23 | 永明卓裕 | |
| 24 | 宏就显誉 | |
| 25 | 自主未来2 | |
| 26 | 创逸致富 | 已验证输入无乱码 |
| 27 | 盈传创富3 | |
| 28 | 香港保单传承 | |
| 29 | 香港离岸信托 | |
| 30 | 香港类信托 | |
| 31 | 香港保单分拆 | |
| 32 | 香港红利锁定 | |
| 33 | 香港红利解锁 | |
| 34 | 香港价值保障 | |
| 35 | 香港双货币户 | |
| 36 | 香港自主入息 | |
| 37 | 香港未来心愿 | |
| 38 | 香港传承守护 | |
| 39 | 信守明天分红 | |
| 40 | 信守明天收益 | |
| 41 | 财富盈活 收益 | 含空格 |
| 42 | 香港2年缴 | |
| 43 | 香港3年缴 | |
| 44 | 香港5年缴 | |
| 45 | 香港趸交 | |
| 46 | 香港短缴储蓄 | |
| 47 | 香港高客保单 | |
| 48 | 香港整付保单 | |
| 49 | 香港10年缴 | |
| 50 | 宏利 Signature Legacy Harvest 分红险 | 含英文 |
| 51 | 新加坡_Singlife Legacy IUL | 含英文 |
| 52 | 新加坡_宏利 Signature Legacy Harvest | 含英文 |
| 53 | 港险教育金 | |
| 54 | 港险压岁钱 | |
| 55 | 港险提前还贷 | |
| 56 | 香港保险躺平 | |
| 57 | 香港保险优惠 | |
| 58 | 港险预缴利率 | |
| 59 | 港险6月优惠 | |
| 60 | 港险汇率风险 | |
| 61 | 香港保险CRS | 含英文 |

---

## 4. 执行方案

### 4.1 执行路径

**Playwright MCP 直接调用可用**，直接用 `mcp__playwright__browser_*` 工具抓取即可，无需 browser-use subagent。

> 历史背景：早期在 Cursor 客户端下 `list_tools`/`browser_type` 等返回 `tool not found`，是 Cursor 的工具名映射问题，当时只能绕道 browser-use subagent。在 Claude Code 下无此问题。

### 4.2 单次抓取流程（已验证，Playwright MCP）

```
1. browser_navigate 到 https://dso.aidso.com/KeywordWSO/searchWord
2. browser_type 填入关键词（target = input[placeholder*="可多词联查"]）
3. browser_evaluate 校验输入框值 == 关键词（防乱码）
4. browser_click 点击"挖掘"按钮（target = div.search_r）
5. browser_wait_for 等待 2-3 秒表格加载
6. browser_evaluate 提取第 2 个 table(index=1) 的数据行
   - 每行字段：[关键词, 月均搜索量, 月均点击量, 下拉词数量, 字数, 竞争程度, 推荐理由]
   - 第一行即为主词本身
7. 返回结构化结果
```

### 4.3 批量执行策略

- **顺序执行**：按词表顺序逐个抓，确保可复现；每个词之间等 2-3 秒避免请求过快触发风控
- **结果格式**：每个词返回 `{keyword, month_cover_count, month_click_count, down_keyword_count, down_keyword_month_covercount, error}`
- **会员账号已生效**：核心字段正常返回；`no_data` 应大幅减少

---

## 5. 后续步骤

### 5.1 立即执行

1. **逐个抓取 61 个词**（用 Playwright MCP + 会员账号）
   - 其中 42 个已在 `aidso_wso_heat.json` 中但为 `error: no_data`，需重抓为有效数据
   - 19 个是全新词，需新增条目
2. **增量合并到 `aidso_wso_heat.json`**
   - 按 `keyword_text` 去重
   - 新字段覆盖旧字段，保留 `keyword_id`
   - 更新 `fetched_at` 为本次时间戳
3. **统计新增命中数**（见 5.2）

### 5.2 合并后验证

- 总抓取词数
- `error: null` 且 `month_cover_count != null` 的有效数据数（对比 61 个里 `no_data` 减少了多少）
- 与 145 个监控词的命中重叠数

### 5.3 长期优化（可选）

4. **自动化脚本化**：将验证后的操作流固化为 Python 脚本，后续可直接通过 Playwright MCP 驱动执行批量抓取。

---

## 6. 关键文件索引

| 文件 | 作用 |
|------|------|
| `data/config/keywords.json` | 145 个监控词主表 |
| `normalized/aidso_wso_heat.json` | WSO 数据存储（增量更新目标） |
| `scripts/filter_short_words.py` | 词表筛选脚本（已调整为 2-6 字） |
| `微信搜索结果/批量抓取/` | 历史抓取结果 Markdown |
| `.cursor/mcp.json` | Playwright MCP 配置 |
| `/Users/works14/.aidso-mcp-profile` | Playwright 浏览器登录态 |

---

## 7. 已知问题与风险

1. ~~**Playwright MCP 直接调用失效**~~：已排除，Claude Code 下可直接用 Playwright MCP 工具
2. **会员登录态依赖**：AIDSO 搜索需要会员，登录态保存在 `/Users/works14/.aidso-mcp-profile`（Playwright user-data-dir）。账号已于 2026-06-23 升级为会员版本，核心字段可正常返回
3. **竞争程度列**：部分词显示 `-`，疑似需更高会员等级，但月均搜索/点击/下拉词数量不受影响
4. **安全验证/拼图验证**：抓取过程中可能出现，需人工介入或自动跳过
5. **每日搜索上限**：AIDSO 约 200 次/天，61 个词预计消耗在安全范围内

---

## 8. 给新 Agent 的指令

> 当你接手这个任务时：
> 1. 先读本文档，理解上下文
> 2. **直接使用 Playwright MCP 工具**（`mcp__playwright__browser_*`），不要再尝试 browser-use subagent 或代码脚本
> 3. 确认会员登录态生效：导航到 searchWord 页面抓一个词，看核心字段是否返回（非 null）
> 4. 按词表顺序逐个抓取，每个词间隔 2-3 秒
> 5. 抓取完成后增量合并到 `aidso_wso_heat.json`（保留 keyword_id，覆盖旧字段）
> 6. 统计命中数并报告

---

*文档结束*