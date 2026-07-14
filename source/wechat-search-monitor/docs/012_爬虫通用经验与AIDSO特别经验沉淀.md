# 012 爬虫经验与 AIDSO 爱搜特别经验沉淀

> 沉淀时间：2026-06-10
> 适用对象：所有使用 user-browser MCP 爬取任何网站的 Agent
> 文档目标：让新 Agent 第一次接触爬虫任务时，从容应对；让下次再来爬 AIDSO 的 Agent 不用重新踩坑
> 数据来源：本次抖音 20 个高客单价领域调研（约 200+ 次 MCP 调用，6 个 7 日精确数据采集）

---

## 引言：这份文档的写法

通用经验部分不求多、求准——只保留**跨网站通用的核心规则**。AIDSO 爱搜部分可以随便发挥——把所有现场经验、踩坑细节都写下来，下次再来可以直接复制粘贴使用。

如果你是第一次爬虫，先读第一部分（通用规则）。如果你要爬爱搜，按顺序读完。

---

## 第一部分：通用爬虫核心规则（5 条）

新 Agent 接触任何网站前的 5 条必读。

### 规则 1：先验证工具能用再开干

**为什么**：所有经验都假设 MCP 能用。如果 MCP 本身配置错误或网络不通，后面的所有操作都会失败。

**具体动作**：
```
1. browser_navigate → https://www.baidu.com
2. browser_get_text → 看是否能拿到 "<!DOCTYPE html>"
3. browser_evaluate → 看是否能返回 JS 执行结果
```

如果这3 步任何一步失败，停下来检查 MCP 配置，不要继续。

### 规则 2：先看 DOM 再写代码

**为什么**：现代网站 90% 的数据都在渲染后的 HTML/DOM 里。盲目 hook fetch 和 XHR经常漏数据。

**具体动作**：
- `browser_get_text` 抓全页文本，找你的目标字段在不在
- `browser_evaluate` 检查关键 DOM 节点是否出现
- 只有 DOM 里完全没有，再考虑 hook network

### 规则 3：SPA 一定要等

**为什么**：SPA 页面 `navigate` 后立刻 evaluate 大概率拿到空 DOM。Vue/React 异步渲染需要时间。

**具体动作**：
- `navigate` 后等 **5-8 秒**（不是越多越好，会浪费 token）
- 期间可以用 `browser_get_clickable_elements` 触发 DOM 探测
- 用 evaluate 检查关键元素是否出现再继续

### 规则 4：付费墙遇到就绕

**为什么**：硬刚登录/会员/验证码 90% 的情况会失败，且浪费大量时间。

**具体动作**：
- 遇到付费墙立刻停止当前数据流
- 切换 tab / 切换种子词 / 切换入口路径
- 寻找平台免费展示的其他数据源
- 永远不要尝试自动登录或绕过验证码

### 规则 5：永远交叉验证

**为什么**：任何页面显示的数字都可能被商业目的污染。AIDSO 平均值对高量级词做了向上加权就是典型案例。

**具体动作**：
- 至少用 2 个不同维度的数据互相校验
- 看到月覆盖就去算 7 日平均
- 看到平均就去算总和反推
- 不要相信任何"展示型字段"是真相

---

## 第二部分：user-browser MCP 工具使用经验（不针对爱搜）

爬取任何网站都可能用到的工具经验。

### 2.1 browser_evaluate 的 5 个铁律

1. **必须是箭头函数**：`() => {...}`，不能是 `function() {...}` 也不能是箭头函数赋给变量
2. **返回值必须是对象**：`return {key: value}`，不能 return 数字、字符串、数组（会触发 MCP 包装层 "is not a function" 错误）
3. **想异步就用 Promise**：`return (async () => {...})()`
4. **字符串内不要有 `$` 反引号转义**：用单引号包裹外层，里面用反引号
5. **大对象分批返回**：返回超过 50KB 的对象可能被截断，分成多次小调用

### 2.2 工具成本排序（从便宜到贵）

```
browser_get_text        最便宜，纯文本
browser_get_markdown    次便宜，结构化文本
browser_read_links      提取所有链接
browser_evaluate (读)   中等，执行 JS 读 DOM
browser_hover           中等，真实鼠标事件
browser_click           中等，真实点击
browser_evaluate (改)   较贵，修改 DOM 影响后续
browser_screenshot      最贵，返回 base64 大对象
```

**原则：能用便宜工具解决的，绝不用贵的。** 这次项目中 80% 的数据靠 `browser_get_text` + `browser_evaluate (读)` 拿到。

### 2.3 hover / click 的真实事件特性

**`browser_hover` 触发的是真实鼠标事件**（经过操作系统级别的事件循环）。这意味着：
- ECharts / zrender / d3 这类基于真实事件的图表库会响应
- 简单的 `dispatchEvent(new MouseEvent(...))` **不会被这些库识别**

**反推**：如果某个图表库的 tooltip 在你 dispatchEvent 时不更新，说明它依赖真实事件。必须用 `browser_hover` 工具。

### 2.4 多个 tooltip/弹窗的处理

同一页面可能有 N 个独立 tooltip（每个 chart 一个）。**不要假定只有一个**。

**正确做法**：
```js
const tts = [];
document.querySelectorAll('div[style*="z-index: 9999999"]').forEach(d => {
  tts.push({text: d.textContent.replace(/\s+/g,' ').trim(), style: d.getAttribute('style').slice(0, 100)});
});
return tts;
```

通过对比前后快照（哪些 tooltip 变了）推断是哪个 chart 触发了变化。

### 2.5 MutationObserver 的真实限制

**MutationObserver 在以下场景失效**：
- ECharts 等图表库**重建 DOM 节点**而非修改 textContent
- 工具库的内部 mutation 没有触发标准事件
- observer 配置的 `attributes: true` 不包含 `childList: true`

**替代方案**：前后快照对比法（最稳）。`var prev = ...; hover; var curr = ...; diff(prev, curr);`

### 2.6 路径相关操作的 token 经济性

`browser_screenshot` 会返回 base64 编码的大图片，token 消耗是其他工具的 10-100 倍。**只在必须视觉验证时用**。

`browser_get_text` 对大页面会被截断（约 32000 字节）。**遇到大页面就分区域 evaluate 读**。

---

## 第三部分：AIDSO 爱搜特别经验沉淀（随便发挥版）

下面所有内容都是本次 011 调研的现场经验，下次来爬 AIDSO 可以直接复制粘贴使用。

### 3.1 AIDSO 的"数据陷阱"清单（按重要性排序）

#### 陷阱 1：AIDSO 月覆盖 ≠ 7 日搜索人次

- **月覆盖**是过去 30 天累计搜索人次
- **7 日搜索人次**是过去 7 天每天的搜索人次（折线图）
- 两者**可以相差几百倍**

实证案例：
- "全红婵戴劳力士"月覆盖 174.61w，但 7 日搜索 **0-3 次**
- "试管婴儿长大后容易出现的问题"月覆盖 10.36w，7 日有 06-05 13,205 的尖峰
- 劳力士绿水鬼月覆盖约 26w，7 日平稳在 1500-2200

**结论**：仅看月覆盖会被严重误导到"已经过气的旧词"或"今天刚爆的尖峰词"。**必须用 7 日精确数据过滤**。

#### 陷阱 2：AIDSO 显示的"平均"对万级以上词做了向上加权

实测 6 个样本：

| 关键词 | 月覆盖档 | AIDSO 显示平均 | 实算平均 | 差 |
|---|---|---|---|---|
| 香港保险 | 4w（万级） | 477 | 477.28 | 吻合 |
| 做试管婴儿 | 15w（万级） | 32 | 32.00 | 吻合 |
| 试管婴儿长大后 | 10w（万级） | 4634 | 4634.14 | 吻合 |
| 试管婴儿主词 | **174w（十万级）** | **6880** | **6635.14** | **+245（+3.7%）** |

**结论**：高量级词的"平均"展示有商业目的——让大词看起来热度更高，吸引用户投流。**监控系统不能直接用 AIDSO 显示的平均值，要自己算**。

#### 陷阱 3：0 + "点击收录"状态 ≠ 真的零搜索

AIDSO 对很多新词显示 `0` + "数据更新中，点击收录完整信息，收录完成即可查看"。

**这不是真的零搜索，是 AIDSO 还没收录这个关键词的等待状态**。

实证案例：
- 干细胞抗衰老的"多少钱一针"、"针价格"、"靠谱吗"等具体词全部 0+点击收录
- 海外辅助生殖的 10 个具体词全部 0+点击收录
- 母婴/儿童/宠物等行业关键词经常出现这个状态

#### 陷阱 4：付费墙只在特定 tab 触发

AIDSO 的付费墙（会员购买弹窗）**只在"行业词" tab 出现**。其他 tab（搜索词/相关词/下拉词/电商词）正常开放。

实证：
- DSO 行业词 → 付费墙
- RSO 行业词 → 付费墙
- WSO 行业词 → 付费墙
- 所有平台的"搜索词/相关词/下拉词/电商词" → 正常

**遇到付费墙立刻切换 tab，不要硬刚**。

#### 陷阱 5：URL 命名不是按"平台简称"猜的

真实路由矩阵：

| 板块 | 真实 URL 前缀 | 别被简称骗 |
|---|---|---|
| 抖音 | `/KeywordDouyin/` | 不是 `KeywordDSO` |
| 小红书 | `/KeywordXhs/` | 不是 `KeywordRSO` |
| 快手 | `/KeywordKSO/` | ✓ 按简称 |
| 微信 | `/KeywordWSO/` | ✓ 按简称 |
| 哔哩哔哩 | `/KeywordBSO/` | ✓ 按简称 |

**错误猜 URL 会直接 404，不要凭直觉命名**。

#### 陷阱 6：详情页路由也不同

| 板块 | 详情页路由 |
|---|---|
| BSO | `/BsoKeyWordDetail/...` |
| KSO | `/KsoKeyWordDetail/...` |
| RSO | `/RsoKeyWordDetail/...` |
| TSO | `/TsoKeyWordDetail/...` |
| WSO | `/WsoKeyWordDetail/...` |
| DSO | 通用 `keyWordDetail/...` |

**详情页字段模型与搜索词页完全不同**，不要假设字段一致。

#### 陷阱 7：每个搜索词都有 7 日折线图

**"7 日搜索人次"列不是一个数字，而是一个迷你 ECharts 折线图**。SVG 已经渲染好了，但 ECharts 全局对象不可用，tooltip 是唯一的真实数据出口。

#### 陷阱 8：每个页面有多个独立 tooltip

ECharts 实例在 AIDSO 是按 cell 渲染的，每个 chart 一个 tooltip div。**同页可能有 3-4 个独立 tooltip 并存**。

区分方法：
- 通过 `style*="z-index: 9999999"` 找所有 tooltip
- 通过对比 hover 前后哪些 tooltip 文本变了，确定是哪个 chart 被触发

#### 陷阱 9：marker 是 path 不是 circle

ECharts 的数据点 marker 用 `<path d="M1 0A1 1 0 1 1 1 -0.0001">` 渲染（圆圈），不是 `<circle>`。识别数据点的关键是 path d 属性以 `M1 0A1 1` 开头。

#### 陷阱 10：marker 的 transform matrix 含真实坐标

每个 marker 的 transform 是 `matrix(2,0,0,2,X,Y)`，X 和 Y 是像素坐标。可用于：
- 反推折线图的形状
- 验证 hover 是否在正确位置
- 不需要 hover 就能拿到视觉坐标（但拿不到真实数值）

### 3.2 AIDSO 7 日精确数据采集完整流程（复制即用）

```text
第 1 步：导航到目标页面
browser_navigate(url="https://dso.aidso.com/KeywordDouyin/searchWord?keyword={URL_ENCODED_KEYWORD}")
# 等 5-8 秒

第 2 步：给所有图表的 marker 加 className
browser_evaluate(script="""
() => {
  const divs = document.querySelectorAll('div[_echarts_instance_]');
  divs.forEach((div, chartIdx) => {
    const paths = div.querySelectorAll('path');
    let n = 0;
    paths.forEach(p => {
      const d = p.getAttribute('d');
      if (d && d.startsWith('M1 0A1 1')) {
        p.setAttribute('data-chart-idx', chartIdx);
        p.setAttribute('data-day-idx', n);
        p.setAttribute('class', 'dp-marker c' + chartIdx + '-d' + n);
        n++;
      }
    });
  });
  return {totalMarkers: document.querySelectorAll('.dp-marker').length};
}
""")

第 3 步：初始化 prev 快照
browser_evaluate(script="""
() => {
  window.__dp_prev__ = Array.from(
    document.querySelectorAll('div[style*="z-index: 9999999"]')
  ).map(d => d.textContent.replace(/\\s+/g, ' ').trim());
  return {prev: window.__dp_prev__};
}
""")

第 4 步：依次 hover 7 个 markers（每次 hover 一个 chart 的 d0-d6）
for day in [0, 1, 2, 3, 4, 5, 6]:
  browser_hover(selector=".c0-d{day}")
  browser_evaluate(script="""
  () => {
    const ttDivs = Array.from(document.querySelectorAll('div[style*="z-index: 9999999"]'));
    const curr = ttDivs.map(d => d.textContent.replace(/\\s+/g, ' ').trim());
    const prev = window.__dp_prev__ || [];
    const changed = [];
    for (let i = 0; i < curr.length; i++) {
      if (curr[i] !== prev[i]) changed.push({i, prev: prev[i], curr: curr[i]});
    }
    window.__dp_prev__ = curr;
    return {changed, allCurrent: curr};
  }
  """)

第 5 步：解析输出
返回的 changed 数组里 [{i, prev, curr}] 就是 (chartIdx, dayIdx) → (日期, 数值) 的映射
按 chartIdx 分组，按 dayIdx 排序，得到完整的 (keyword → [7个 (date, value)]) 数据
```

### 3.3 AIDSO 工具栏与按钮清单

- 顶部导航：DSO / GEO / 首页 / 关键词 / 达人&素材 / AI工具 / 监测 / DSO 游学 / API服务
- 频道条：全站找词（41亿/日）/ DSO（6亿/日）/ RSO（7亿/日）/ KSO（10亿/日）/ WSO（2亿/日）/ BSO
- 搜索模式：单个 / 批量
- 核心指标筛选：月覆盖人次 / 字数 / 下拉词数量 / 下拉词月覆盖 / 竞争度 / 类型
- 操作：屏蔽关键词 / 添加关键词 / 是否 AI 排词 / 列表配置
- 表格列：关键词 / 月覆盖人次 / 7 日搜索人次 / 字数 / 下拉词数量 / 下拉词月覆盖 / 类型 / 竞争度 / 操作
- 每行操作：详情 / 标题 / 收藏 / 剧本 / 视频 / 做图
- 悬浮客服：DSO 工作流 / 提取+改写 / 帮助 / 客服 / 公众号 / 小程序
- 安全验证：拖动下方拼图完成验证（右下角，不影响数据采集）

### 3.4 AIDSO 类型标签全集

AIDSO 给搜索词打了丰富的类型标签，理解这些标签对筛选有商业价值的关键词至关重要：

| 标签 | 含义 | 商业价值 |
|---|---|---|
| **商机词** | 含"多少钱/多少/费用/机构/条件"等成交意图词 | **高** |
| **蓝海词** | 搜索量尚低但有潜力的词 | **中** |
| **黑马词** | 近期快速上升的词 | **中** |
| **高点击词** | 用户点击率高的词 | 中 |
| **低成本词** | 投放成本低的词 | 中 |
| **地域词** | 绑定具体地理位置 | 地域投放 |
| **新闻词** | 与新闻事件相关 | 短期流量 |
| **同行买词** | 已有竞争对手在为这个词投流 | 商业化已成熟 |
| **概念股词** | 与资本市场事件联动 | 投资人群 |

**筛选策略**：商机词 × 蓝海词交叉 = 最优内容切入口。黑马词 × 7 日高量 = 时间窗口最短的红利机会。

### 3.5 AIDSO 6 个平台的特殊经验

#### 抖音（DSO）
- 抖音不是"纯搜索平台"，是"看后搜杂音场"
- 非常多的搜索词是看完某条视频后系统自动生成的
- 需要用 7 日精确数据区分"真实需求"和"事件泡沫"
- 蓝海词特别丰富（30w+ 搜索结果里有 245,448 个蓝海词）

#### 小红书（RSO）
- 独有"市场出价"字段（小红书广告价格信号）
- 行业词被会员弹窗拦截
- 标签特征：蓝海词、同行买词、高点击词

#### 快手（KSO）
- 用"近 7 天搜索量"指标
- 额外有"搜索飙升榜"
- 标签高频出现"低成本词"

#### 微信（WSO）
- 用"月均搜索量 / 月均点击量"
- 当前仓库唯一已落地抓取板块
- 行业词没有数据（已知差异）

#### 哔哩哔哩（BSO）
- 页面显示"哔·关键词"（不是"拼"！）
- 只有 3 个 tab，没有"行业词"
- 详情页有"新增相关 UP 主指数"等视频生态指标

#### 头条（TSO）
- 独立 TSO 关键词线，页面顶端显示"30亿/日 / TSO"
- 独有国家选择（巴西、美国等）
- 创意灵感话题榜是独特品类

#### 全站找词
- 没有右侧 4 tab 结构
- 多平台聚合比较视图（抖/快/红/微/哔多平台指标并列）
- 表头：关键词 / 字数 / 全站下拉词数量 / 抖日均 / 快日均 / 红日均 / 微月均 / 哔搜索指数

### 3.6 AIDSO 这次调研发现的 21 个反常识（快速回顾）

下次来爬 AIDSO 时这 21 个反常识可以快速索引：

1. 香港保险在抖音是事件+IP驱动，不是产品驱动
2. 希腊移民在抖音完全是反面警示市场
3. 用户搜"干细胞抗衰老"第一反应是搜"李连杰"
4. 试管婴儿在抖音是担忧型科普市场
5. 私人银行在抖音是科普+八卦双轨
6. 家族办公室在抖音是 89 词的小众科普品类
7. 迪拜房产在抖音完全是中介个人 IP 矩阵
8. ICL 19.38w 但 95% 是科普词
9. 长江商学院被副院长事件劫持
10. 劳力士在抖音被彻底梗化娱乐化
11. 私人飞机在抖音几乎完全是明星八卦+网红揭秘
12. 高端婚礼在抖音完全县城化
13. 南极旅游被俞敏洪全员信事件劫持
14. 家族信托在抖音完全被富豪八卦占据
15. 早癌筛查在抖音彻底荒漠（入口词错位，真入口是胃肠镜）
16. 免疫细胞存储在抖音几乎不存在
17. 三代试管是真大词但被偏见压制
18. 国际学校在抖音是学校品牌广告 vlog
19. 海外辅助生殖在抖音完全真空
20. 高端体检在抖音彻底荒漠（入口词错位，真入口是全身体检）
21. **月覆盖不等于当前活跃度（全红婵戴劳力士 174w vs 7 日 0-3）**

### 3.7 AIDSO 真实成交土壤判定标准

经过这次 011 调研总结出 5 种抖音高客单价生意类型：

| 类型 | 判断标准 | 例子 |
|---|---|---|
| 真高成交意图 | 商机词占比 ≥ 15% | 私人银行（67%） |
| 种草大品类 | 月覆盖 ≥ 10w + 商机词占比 < 5% | 试管婴儿、三代试管、ICL、国际学校、胃肠镜、全身体检 |
| 事件泡沫型 | 月覆盖大但 7 日搜索归零 | 劳力士、长江商学院（副院长劫持期）、南极旅游（俞敏洪劫持期） |
| 地域/IP 矩阵型 | 大量 IP 词或地域词 | 迪拜房产、高端婚礼、香港保险 |
| 真荒漠 | 多个种子词验证均为小品类 | 海外辅助生殖、干细胞回输 |

**商机词占比 ≥ 15% 是商业化成熟度的硬指标**。

### 3.8 AIDSO 反向追踪爆款视频的方法

**核心洞察**：7 日曲线出现 1 天尖峰 = 当天有视频爆了。

**用法**：
1. 找到某个词的 7 日数据
2. 找到尖峰那天（如试管婴儿长大后问题 06-05 13,205，平时 2000-3000）
3. 当天去抖音搜这个关键词的热门视频
4. 那条视频就是引发"看后搜"的源头

**业务价值**：可以反向发现某个品类最近一周的爆款选题。

---

## 第四部分：本次踩过的坑（教训）

下次不要再犯。

### 坑 1：以为 SVG 没 polyline 就读不到数据

ECharts 用 path + Bezier 曲线绘制折线，没有 polyline 节点。识别数据点的关键是 marker path（d 以 `M1 0A1 1` 开头）。

**教训**：不要被 HTML 结构迷惑，找数据要看 SVG 的整体绘制模式。

### 坑 2：用 dispatchEvent 触发 ECharts tooltip

`evaluate` 里 `dispatchEvent(new MouseEvent('mousemove'))` 和 `dispatchEvent(new PointerEvent('pointermove'))` **都不能触发 ECharts tooltip 更新**。

**教训**：zrender 用真实 PointerEvent 监听，必须用 `browser_hover` 工具。

### 坑 3：MutationObserver 失效

设置 observer 监听 tooltip div 后，hover 时 observer 不触发回调。原因可能是 ECharts 创建新 DOM 节点而非修改 textContent。

**教训**：observer 在异步渲染的 SPA 上不可靠，优先用前后快照对比法。

### 坑 4：evaluate 返回数字报错

```js
() => document.querySelectorAll('table').length  // ❌ 返回数字，触发 "not a function"
() => ({tableCount: document.querySelectorAll('table').length})  // ✓ 返回对象
```

**教训**：evaluate 返回值必须是对象，即使只是一个数字。

### 坑 5：hover 一次多次读 tooltip

hover c0-d0 后多次 evaluate 读 tooltip，永远是同一个值（c0-d0 对应的日期+数值）。

**教训**：必须 hover + 立刻 evaluate 配对。串行执行才能拿全 7 天数据。

### 坑 6：截图被截断

`browser_screenshot` 返回的 base64 图片超过 32768 字节会被截断。

**教训**：大页面不要用 screenshot 验证，用 evaluate 读 DOM 关键元素确认状态。

### 坑 7：把"伪荒漠"判断当成结论

V2 报告里把"高端体检/早癌筛查/海外辅助生殖"归类为"伪荒漠"（假设入口词可能错），但没做换种子词验证。

**教训**：任何"伪荒漠"判断必须先用 3-5 个口语化种子词验证后才能下结论。这次 V2.1 验证后发现：
- 高端体检→全身体检：33w 月覆盖（真大品类）
- 早癌筛查→胃肠镜：60w 月覆盖（真大品类）
- 海外辅助生殖→美国试管 23 词 / 泰国试管 0 词（**真荒漠**）

### 坑 8：评分矩阵两套标准混用

V2 报告自己定义了 X 轴（累计月覆盖对数）和 Y 轴（商机词占比），但打出来的分数和这两个维度对不上。私人银行 67% 商机词打了 A-，国际学校 13w 月覆盖打了 B（应该远超私人银行）。

**教训**：要么用清晰二维定位，要么明说"主观综合判断"，不能两套标准混用。

### 坑 9：ROI 用没有依据的转化率

V2 报告里"按 0.5% 抖音→线下转化率"、"按 0.1% 抖音→私域转化率"没有任何数据支撑。

**教训**：ROI 数字必须明确标注"未经验证的假设估算"，不能伪装成基于数据的预测。

### 坑 10：心智图格式不一致

V2 报告里只有前 2 个领域用了完整流程图，剩下 14 个用文字描述。

**教训**：同一类内容必须格式一致，否则显得粗糙。

---

## 第五部分：速查清单

新 Agent 接到爬虫任务时的 30 秒检查清单：

```
□ 工具能用吗？先 navigate 百度 + get_text 验证
□ SPA 等够了吗？等 5-8 秒
□ 数据在 DOM 里吗？先 get_text 抓全页看
□ 遇到付费墙了吗？立即切换 tab/种子词
□ 单一数据源验证了吗？至少 2 个维度交叉
□ 字符编码对了吗？中文要用 URL encode
□ ECharts/图表库的 tooltip 真实数据吗？hover 试试
□ 多个 tooltip 区分清楚了吗？按 index 区分
□ observer 失效了吗？用前后快照对比法
□ evaluate 返回值是对象吗？数字会报错
□ 写入文档了吗？给后续 Agent 留经验
```

---

## 第六部分：AIDSO 速查表

来爬 AIDSO 必看的关键信息：

| 项 | 值 |
|---|---|
| 域名 | https://dso.aidso.com |
| 抖音搜索 URL | `/KeywordDouyin/searchWord?keyword={URL_ENCODED}` |
| 抖音相关词 URL | `/KeywordDouyin/correlationWord?keyword={...}` |
| 抖音下拉词 URL | `/KeywordDouyin/sugWord?keyword={...}` |
| 抖音行业词 URL | `/KeywordDouyin/industryWord?keyword={...}` （付费墙） |
| 抖音电商词 URL | `/KeywordDouyin/commerce?keyword={...}` |
| 抖音详情页 URL | `/keyWordDetail/detail?keyword={...}` |
| 小红书搜索 URL | `/KeywordXhs/searchWord?keyword={...}` |
| 小红书详情页 URL | `/RsoKeyWordDetail/detail?keyword={...}` |
| 快手搜索 URL | `/KeywordKSO/searchWord?keyword={...}` |
| 快手详情页 URL | `/KsoKeyWordDetail/detail?keyword={...}` |
| 微信搜索 URL | `/KeywordWSO/searchWord?keyword={...}` |
| 哔哩哔哩搜索 URL | `/KeywordBSO/searchWord?keyword={...}` |
| 头条搜索 URL | `/KeywordTSO/searchWord?keyword={...}` |
| 全站找词 URL | `/KeywordAll/searchWord?keyword={...}` |
| 个人版用户限制 | 批量模式最多 5 个词/文件上传 |
| 行业词付费墙 | DSO/RSO/WSO 三平台都有 |
| 0+点击收录状态 | 表示待采集，不是真的零搜索 |
| 月覆盖口径 | 过去 30 天累计搜索人次 |
| 7 日搜索口径 | 过去 7 天每日搜索人次（折线图） |
| 平均值口径 | 低量级词算术平均，高量级词向上加权 |
| 折线图位置 | 表格"7 日搜索人次"列 |
| marker 标识 | `div[_echarts_instance_]` 里的 path d 以 `M1 0A1 1` 开头 |
| tooltip 标识 | `div[style*="z-index: 9999999"]` |
| tooltip 格式 | `YYYY-MM-DD value` |

---

## 结语

这份文档的核心目的是**给后续 Agent 省下重新踩坑的时间**。

通用部分要少而准——5 条规则覆盖 80% 场景。AIDSO 部分要全而细——每个陷阱、每个 URL 模板、每个采集步骤都写下来，下次复制粘贴即可。

爬虫经验的积累不是一次性的，是每次爬新网站都更新一次本文件的过程。如果你爬了一个新网站发现了新的反常识，请在本文档追加章节。