const stages = [
  { id: 'decision', name: '写作决策', desc: 'AI 一次性预跑素材、URL、模板和初版写作方案；你只需要确认哪些采用、哪个模板更合适、方案方向对不对。' },
  { id: 'package', name: '写作包', desc: '把选题、素材三态、模板、方案和目录约定整理成可交给 Agent 的输入包。' },
  { id: 'done', name: '成稿', desc: '未来写入 output，同时复制到本篇文章的工作目录里留档。' }
];

const batchStages = [
  { id: 'batch-config', name: '批次配置', desc: '关键词配置、母文章匹配、篇数调整。' },
  { id: 'batch-done', name: '成稿队列', desc: '多篇成稿的运行、完成、返工管理。' }
];


/* ====== Batch Mode Data ====== */

const batchMotherLibrary = [
  { id: 'aia-living', title: '友邦环宇盈活', path: 'wiki/产品母页/友邦环宇盈活.md', aliases: ['友邦', '环宇'] },
  { id: 'withdrawal-compare', title: '港险提领功能横评', path: 'wiki/产品母页/港险提领功能横评.md', aliases: ['提领', '现金流', '225', '258', '567', '51010'] },
  { id: 'sunlife-galaxy', title: '永明星河尊享2', path: 'wiki/产品母页/永明星河尊享2.md', aliases: ['永明', '星河'] },
  { id: 'axa-thrive', title: '安盛盛利2', path: 'wiki/产品母页/安盛盛利2.md', aliases: ['安盛', '盛利'] },
  { id: 'prudential-prime', title: '保诚信守明天', path: 'wiki/产品母页/保诚信守明天.md', aliases: ['保诚', '信守明天'] }
];

function matchMothersForKeyword(keyword) {
  return batchMotherLibrary.filter(function(m) {
    var terms = [m.title].concat(m.aliases || []);
    return terms.some(function(t) { return keyword.indexOf(t) >= 0; });
  });
}

const batchHooks = [
  { id: 'hook-pressure', title: '提领压力测试', reason: '直接回答「能不能持续领」，适合高意向咨询。' },
  { id: 'hook-plan', title: '计划书解读', reason: '把判断翻译成读者手里那份计划书的对话入口。' },
  { id: 'hook-resource', title: '资料领取', reason: '低门槛互动，适合社媒分发和社群裂变。' },
  { id: 'hook-discount', title: '优惠折扣', reason: '触发即时行动，适合做引流首屏。' },
  { id: 'hook-cross', title: '产品横评', reason: '通过对比制造决策冲突，适合高客单转化。' }
];

const batchSignalConfig = {
  'hot': { label: '热点', class: 'badge-hot', reason: '当前搜索热度高，建议多篇覆盖不同角度' },
  'medium': { label: '普通', class: 'badge-medium', reason: '稳定搜索量，建议单篇精准覆盖' },
  'high-priority': { label: '高优先', class: 'badge-high', reason: '选题策略指定加量，建议覆盖多角度' }
};

const demoBatches = [
  {
    id: 'batch-hot-dividend',
    name: '分红实现率热点追踪',
    source: '选题监控',
    brief: '今天分红实现率是热点，相关关键词多写几篇提高覆盖。重点关注友邦、保诚、安盛的分红实现率数据。',
    status: 'pending',
    createdAt: '今天 09:30',
    updatedAt: '今天 09:30',
    outputDir: 'WritingSpace/BatchQueue/240712_分红实现率/',
    publishHandoff: false,
    keywords: [
      { id: 'b1kw1', keyword: '友邦分红实现率2026', purpose: '写给关注友邦分红兑现能力的人，强调友邦百年品牌和分红实现率稳定性。', signal: 'hot', signalReason: '分红实现率年度发布热点', count: 2, recommendedCount: 2, hookId: 'hook-resource', motherMatches: [{ motherId: 'aia-living', role: '品牌信任背书与产品事实', confidence: 0.92 }], readiness: 'ready' },
      { id: 'b1kw2', keyword: '保诚分红实现率低于预期怎么办', purpose: '写给担心保诚分红兑现力的客户，解释分红波动和长期复利边界。', signal: 'hot', signalReason: '保诚分红实现率引发讨论', count: 1, recommendedCount: 1, hookId: 'hook-pressure', motherMatches: [{ motherId: 'prudential-prime', role: '产品事实与分红机制详解', confidence: 0.88 }], readiness: 'ready' },
      { id: 'b1kw3', keyword: '安盛盛利2分红实现率测算', purpose: '', signal: 'medium', signalReason: '稳定自然搜索', count: 1, recommendedCount: 1, hookId: 'hook-plan', motherMatches: [{ motherId: 'axa-thrive', role: '产品数据与提领表现', confidence: 0.85 }], readiness: 'ready' },
      { id: 'b1kw4', keyword: '分红实现率低于100%会亏钱吗', purpose: '写给担心分红实现率波动的入门客户，解释分红机制的基本逻辑。', signal: 'hot', signalReason: '热点衍生搜索', count: 3, recommendedCount: 3, hookId: 'hook-cross', motherMatches: [], readiness: 'needs-mother' }
    ]
  },
  {
    id: 'batch-cashflow',
    name: '提领现金流长尾补位',
    source: '手动创建',
    brief: '针对提领现金流相关长尾关键词补位，每篇一篇精准覆盖。',
    status: 'pending',
    createdAt: '今天 11:15',
    updatedAt: '今天 11:15',
    outputDir: 'WritingSpace/BatchQueue/240712_提领长尾/',
    publishHandoff: false,
    keywords: [
      { id: 'b2kw1', keyword: '每年提5万港险够养老吗', purpose: '写给临近退休、想靠港险补充养老金的中产。', signal: 'medium', signalReason: '稳定长尾搜索', count: 1, recommendedCount: 1, hookId: 'hook-pressure', motherMatches: [{ motherId: 'withdrawal-compare', role: '提领横评数据与对比口径', confidence: 0.9 }], readiness: 'ready' },
      { id: 'b2kw2', keyword: '225提领和567提领有什么区别', purpose: '', signal: 'medium', signalReason: '功能对比搜索', count: 1, recommendedCount: 1, hookId: 'hook-plan', motherMatches: [{ motherId: 'withdrawal-compare', role: '提领密码定义与对比矩阵', confidence: 0.91 }], readiness: 'ready' },
      { id: 'b2kw3', keyword: '星河尊享2提领能力测评', purpose: '写给对星河尊享2感兴趣、但担心提领能力的客户。', signal: 'medium', signalReason: '产品长尾搜索', count: 1, recommendedCount: 1, hookId: 'hook-cross', motherMatches: [{ motherId: 'sunlife-galaxy', role: '产品母页核心事实与缺陷', confidence: 0.93 }], readiness: 'ready' },
      { id: 'b2kw4', keyword: '提领断单风险最大的产品', purpose: '', signal: 'medium', signalReason: '风险意识搜索', count: 0, recommendedCount: 1, hookId: 'hook-pressure', motherMatches: [{ motherId: 'withdrawal-compare', role: '提领横评断单数据', confidence: 0.84 }], readiness: 'ready' }
    ]
  },
  {
    id: 'batch-cross-review',
    name: '产品横评专项',
    source: '手动创建',
    brief: '多产品横评对比，覆盖不同维度。一个关键词匹配两篇母文章。',
    status: 'pending',
    createdAt: '本周一 14:20',
    updatedAt: '本周一 16:45',
    outputDir: 'WritingSpace/BatchQueue/240710_产品横评/',
    publishHandoff: false,
    keywords: [
      { id: 'b3kw1', keyword: '友邦环宇盈活vs安盛盛利2', purpose: '写给正在对比这两款产品的客户，重点讲清楚各自适用场景。', signal: 'medium', signalReason: '产品对比搜索', count: 2, recommendedCount: 2, hookId: 'hook-cross', motherMatches: [{ motherId: 'aia-living', role: '环宇盈活产品事实与专属缺陷', confidence: 0.94 }, { motherId: 'axa-thrive', role: '盛利2产品数据与提领表现', confidence: 0.91 }], readiness: 'ready' },
      { id: 'b3kw2', keyword: '星河尊享2 vs 盛利2 提领谁更强', purpose: '', signal: 'medium', signalReason: '产品对比搜索', count: 1, recommendedCount: 1, hookId: 'hook-cross', motherMatches: [{ motherId: 'sunlife-galaxy', role: '星河防守型定位与保证底座', confidence: 0.89 }, { motherId: 'axa-thrive', role: '盛利2提领数据与对比', confidence: 0.87 }], readiness: 'ready' },
      { id: 'b3kw3', keyword: '港险提领能力哪家最强2026', purpose: '写给想看结论的客户，用横评数据说话。', signal: 'hot', signalReason: '年度对比热点', count: 2, recommendedCount: 2, hookId: 'hook-resource', motherMatches: [{ motherId: 'withdrawal-compare', role: '提领横评完整数据矩阵', confidence: 0.95 }], readiness: 'ready' },
      { id: 'b3kw4', keyword: '保证收益最高的港险产品', purpose: '', signal: 'medium', signalReason: '功能搜索', count: 1, recommendedCount: 1, hookId: 'hook-plan', motherMatches: [], readiness: 'needs-mother' }
    ]
  }
];

let batchList = demoBatches.slice();
let activeBatchId = batchList[0].id;

function getActiveBatch() {
  return batchList.find(b => b.id === activeBatchId) || batchList[0];
}

function getBatchMotherById(id) {
  return batchMotherLibrary.find(m => m.id === id);
}

function getHookById(id) {
  return batchHooks.find(h => h.id === id);
}

const batchStatusLabel = {
  'pending': '待确认',
  'generating': '生成中',
  'done': '已完成'
};

const readyLabel = {
  'ready': '可直接生成',
  'needs-mother': '缺母文章'
};

const queueStatusLabel = {
  'waiting': '等待',
  'running': '运行中',
  'done': '已完成',
  'rework': '需返工'
};

const queueStatusClass = {
  'waiting': 'badge-yellow',
  'running': 'badge-green',
  'done': 'badge-gray',
  'rework': 'badge-red'
};

/* ====== Batch Queue State ====== */

let batchQueues = {};
let batchQueueRunning = false;
let batchQueueTimer = null;

function getBatchQueue(batchId) {
  if (!batchQueues[batchId]) batchQueues[batchId] = [];
  return batchQueues[batchId];
}

function setBatchQueue(batchId, items) {
  batchQueues[batchId] = items;
}

function clearBatchQueue(batchId) {
  delete batchQueues[batchId];
}

function generateQueueForBatch(batch) {
  const items = [];
  batch.keywords.forEach(kw => {
    if (kw.readiness !== 'ready' || kw.count <= 0) return;
    for (let i = 0; i < kw.count; i++) {
      items.push({
        id: 'q-' + kw.id + '-' + i,
        keywordId: kw.id,
        title: '【' + kw.keyword + '】第' + (i+1) + '篇',
        status: 'waiting',
        outputFile: batch.outputDir + kw.keyword.replace(/[\/\s]/g, '_') + '_' + (i+1) + '.md'
      });
    }
  });
  return items;
}

const batchQueueDemoTitles = {
  'q-b1kw1-0': '友邦2026分红实现率全解析：百年品牌的分红到底靠不靠谱',
  'q-b1kw1-1': '友邦分红实现率连续5年超100%，但这份计划书里藏着一个风险',
  'q-b1kw2-0': '保诚分红实现率低于预期？先别急，看这三个数字再决定',
  'q-b1kw3-0': '安盛盛利2分红实现率测算：6.8%演示是怎么来的',
  'q-b1kw4-0': '分红实现率低于100%会亏钱吗？三个角度让你彻底明白',
  'q-b1kw4-1': '分红实现率没达到100%，你的保单价值会缩水多少？',
  'q-b1kw4-2': '分红实现率波动的真相：不是所有产品都能用同一套标准看',
  'q-b2kw1-0': '每年从港险提5万够养老吗？用567提领算一笔真实账',
  'q-b2kw2-0': '225提领和567提领到底有什么区别？一张表看懂',
  'q-b2kw3-0': '星河尊享2提领能力测评：防守型标杆的提领真实表现',
  'q-b2kw4-0': '提领断单风险最大的产品是哪个？这份横评告诉你答案',
  'q-b3kw1-0': '友邦环宇盈活vs安盛盛利2，同一笔钱到底选哪个',
  'q-b3kw1-1': '环宇盈活和盛利2的提领对决：第20年差距有多大',
  'q-b3kw2-0': '星河尊享2 vs 盛利2，提领到底谁更强？',
  'q-b3kw3-0': '2026港险提领能力排行榜：这几家产品断层领先',
  'q-b3kw3-1': '港险提领大横评：五大产品225/258/567/51010全面对比',
  'q-b3kw4-0': '保证收益最高的港险产品竞猜：不是你想的那家'
};

const markdownPreviews = {
  aiaLiving: `# 友邦环宇盈活

页面类型：产品母页
状态：正式页
最后更新：2026-05-04

## 1. 页面用途

记住，这不是那种看两眼就完事的"短摘要"，它是环宇盈活的**"终极情报主档案"**。

**核心边界**：别把它当成"全能型产品"来写。它本质上就是**"用低提领灵活性，换中长期静态收益的极致表现"**。如果客户有频繁或大额早期提领需求，这不是最优选——星河尊享2和盛利2会把环宇盈活按在地上。

## 2. 怎么一句话看懂它？（定调判断）

**港险里"中长期静态收益的冠军型选手"。** 第30年即达6.5%演示上限，友邦百年品牌+分红实现率稳健是第一信任背书。但提领后表现被星河尊享2反超，567极致提领下甚至可能断单。

## 6. 关键事实与数据（出稿前的硬核弹药）

**【5年缴静态收益】**

- 第7年预期回本（比盈御3快1年）。
- 第10年IRR约3.47%。
- 第20年IRR约5.67%。
- 第30年达6.5%演示上限（比市场同类产品快10年+）。

**结论：千万别把它写成"确定性高收益"，它的核心竞争力是"中长期静态收益的爆发力"，不是"提领后的稳定性"。**

**【提领表现】**

- 566/5108提领：前10年现金价值稍优，第20年起被星河尊享2全面反超。
- 567极致提领：出现断单情况。
- **关键机制缺陷**：提领时先从复归红利和终期红利同时取钱，复归取完后才开始动保证部分。这意味着终期账户被更早消耗，影响后期增值。

## 8. 这产品有什么硬伤？（三个专属缺陷）

- **缺陷一：提领后表现明显弱于星河尊享2。** 566/5108提领下第20年起被反超；567极致提领下可能断单。如果客户核心需求是"提领养老"，星河尊享2更合适。
- **缺陷二：提领逻辑更耗终期账户。** 环宇盈活先从复归红利和终期红利同时取钱，复归取完后才开始动保证部分。这意味着终期账户被更早消耗，影响后期增值。
- **缺陷三：前期爆发力不如盛利2。** 虽然第30年达6.5%上限很快，但前10-20年的预期收益不如盛利2激进。

## 10. 横评证据与对照结论

| 横评页 | 对比口径 | 环宇盈活赢在哪里 | 环宇盈活输在哪里 |
|---|---|---|---|
| 港险提领功能横评 | 51010提领 | — | 第30年累计领取后账户约25万美元，盛利2和盈聚天下2均约40万美元。 |
| 四大外资保司王牌产品横评 | 四大外资5年缴王牌 | 货币转换和保司分红稳定性叙事强；第30年达6.5%演示上限速度快。 | 566提领后剩余价值不如盛利2；保证回本慢于宏挚家传承。 |
| 2年缴储蓄险横评 | 超长期收益 | 有来源称第23年起环宇盈活在总收益上开始反超盛利2。 | 前23年预期收益落后于盛利2。 |

## 12. 图片弹药库

![环宇盈活5年缴静态收益IRR表](https://mmbiz.qpic.cn/sz_mmbiz_png/Uo8SjRYXVdLLgOm6Jbvk0kibmib4v0lUibNqMf4lxtY8JpIzibibM2ic2efkuxjQRyg1ic2w1PhpSZh4fjHWqAj7YlwJw/640?wx_fmt=png&from=appmsg)
<!-- 插图建议：环宇盈活5年缴静态收益IRR表。第7年预期回本，第10年IRR约3.47%，第20年IRR约5.67%，第30年达6.5%演示上限。适合展示"中长期静态收益冠军"定位。 -->

![环宇盈活 vs 星河尊享2提领对比表](https://mmbiz.qpic.cn/sz_mmbiz_png/Uo8SjRYXVdLLgOm6Jbvk0kibmib4v0lUibNGDObibqQBXCxM2VBr12YRAOaw4A6yveU6rDYmnUDKTjOKdSYmCsYoPA/640?wx_fmt=png&from=appmsg)
<!-- 插图建议：566/5108提领下前10年环宇盈活稍优，但第20年起被星河尊享2全面反超；567极致提领下环宇盈活出现断单。 -->

![四大外资王牌产品566提领对比表](https://mmbiz.qpic.cn/mmbiz_png/O1oFUTFpDP05pwfQqOvvyuYJFfaeaN5iaEyKs1Ct5zJ3A2kFKfNkB6BkVfU29u741sFYrkG2Kq7b4NAcpjnAYXicqgnMeOZezzPNWz2reN0os/640?wx_fmt=png&from=appmsg)
<!-- 插图建议：盛利2前中后期断层领先，第20年剩余现价比环宇盈活高出近20万；适合展示环宇盈活的提领短板。 -->

![复归红利占比对比图](https://mmbiz.qpic.cn/sz_mmbiz_png/vv6EicHFdTb1SwElfNEw1piaaxezCYTKyGGMmN62GTj48KlTPqaWt56PwBcT7Jycleech5H0AwxMpgFwGEbOKvKYJLGToGh3RIekbgOFlwBOc/640?wx_fmt=png&from=appmsg)
<!-- 插图建议：环宇盈活复归红利占比仅8%，行业垫底；复归红利占比直接决定提领后的保单稳定性。 -->
`,
  withdrawalCompare: `# 港险提领功能横评

页面类型：横评聚合页（非单品母页）
状态：正式页
最后更新：2026-05-06

## 页面用途

本页是横评聚合页，非单品母页。它承接“提领密码”这类功能型横评：同样一张储蓄分红险，不同产品在开启提领后，剩余账户价值会拉开多大差距。

**核心价值**：提领横评能逼出单品母页里最有价值的专属缺陷。一个产品不提领时看起来很强，开启提领后可能立刻露馅。

## 一句话定调

不提领时，很多港险长期收益差距不大；一旦开启提领密码，产品设计差距会被迅速放大。**星河尊享2强在 225 快领，盛利2强在 258 和 567，盛利2与盈聚天下2在 51010 高额延迟提领里并列强势。**

## 赛道定义

| 缴费期 | 提领密码 | 含义 |
|---|---|---|
| 2 年缴 | 225 | 2 年缴，第 2 年起，每年提总保费 5% |
| 2 年缴 | 258 | 2 年缴，第 5 年起，每年提总保费 8% |
| 5 年缴 | 567 | 5 年缴，第 6 年起，每年提总保费 7% |
| 5 年缴 | 51010 | 5 年缴，第 10 年起，每年提总保费 10% |

## 结论矩阵

| 提领口径 | 当前领先者 | 横评结论 | 适配人群 |
|---|---|---|---|
| 225 极致快领 | 永明星河尊享2 | 仅星河尊享2、宏挚家传承、匠心传承2、富饶万家能做到 225 终身提取；星河尊享2账户剩余价值明显领先。 | 刚缴完就需要现金流、接近退休或已有明确短期支出的人。 |
| 258 延迟三年高比例领取 | 安盛盛利2 | 能在保单有效期内持续不断单完成 258 的只有盛利2；富卫盈聚天下2局部表现也强。 | 5 年内准备退休、愿意晚三年领但想提高领取比例的人。 |
| 567 主流稳健提领 | 安盛盛利2 | 5 年缴主流提领测试里，盛利2在大多数年份提领后账户价值最高。 | 追求长期养老现金流、希望领钱后账户还继续长的人。 |
| 51010 延迟高额养老 | 安盛盛利2、富卫盈聚天下2 | 第 30 年盛利2与盈聚天下2账户价值都约 40 万美元；友邦环宇盈活约25万美元。 | 提前 10-15 年规划高品质退休的人。 |

## 关键数字

### 567：盛利2是 5 年缴主流提领强者

测试口径：45 岁女士，每年 5 万美元，连续交 5 年。

- 第 20 年：累计领取 26.2 万美元后，盛利2账户还有 34 万美元；盈聚天下2 31 万美元；星河尊享2 27 万美元。
- 第 30 年：累计领取 43 万美元后，盛利2账户还有 43 万美元；宏挚家传承只有 26 万美元。

### 51010：盛利2和盈聚天下2并驾齐驱

测试口径：45 岁男士，每年 5 万美元，连续交 5 年。

- 到第 30 年，累计领取 52.5 万美元后，盛利2和盈聚天下2账户均约 40 万美元；星河尊享2约 37 万美元；友邦环宇盈活约 25 万美元。

## 争议与分歧

- **提领密码不是提款圣经，而是能力证明**。567、258 这些数字不代表"合同写了一定能提"，只代表"如果分红实现率 100% 达成，按这个比例提取可以一直提下去"。
- **三账户提领顺序决定保单生死**：港险一般有 3 个账户（保证收益、复归红利、终期红利）。过早提取较大会掏空复归账户，导致后续增长受损。
- **提领后的胜负比静态收益更有杀伤力**：不领取时长期持有各家收益差距可能不大；开启提领后，账户价值差距会明显放大。

## 图片弹药库

![四大外资保司566提领后剩余现金价值与IRR对比表](https://mmbiz.qpic.cn/mmbiz_png/O1oFUTFpDP05pwfQqOvvyuYJFfaeaN5iaEyKs1Ct5zJ3A2kFKfNkB6BkVfU29u741sFYrkG2Kq7b4NAcpjnAYXicqgnMeOZezzPNWz2reN0os/640?wx_fmt=png)
<!-- 插图建议：关键数据：盛利2第20年剩余81.5万，环宇盈活61.4万。讲“四大外资566提领，盛利2断层领先”时插入。 -->

![567提领后各保司账户价值对比表](https://mmbiz.qpic.cn/mmbiz_png/PXKyvKHFU40wYH0uMwL0wic5GKW6Y4hIQz4SBlfJh2NS3N3pujTY4usCdsym16dzckf3KXia83pkVqcYeHndaVrt95RNiaE75GVsKsvNAJrw5I/640?wx_fmt=png)
<!-- 插图建议：关键数据：第20年盛利2账户34万、盈聚天下2 31万、星河尊享2 27万；第30年盛利2 43万。 -->

![环宇盈活 vs 星河尊享2提领对比表](https://mmbiz.qpic.cn/sz_mmbiz_png/Uo8SjRYXVdLLgOm6Jbvk0kibmib4v0lUibNGDObibqQBXCxM2VBr12YRAOaw4A6yveU6rDYmnUDKTjOKdSYmCsYoPA/640?wx_fmt=png&from=appmsg)
<!-- 插图建议：星河在提领后账户价值明显领先环宇盈活。写“星河尊享2 vs 环宇盈活提领能力差异”时插入。 -->

![提领密码总览表](https://mmbiz.qpic.cn/sz_mmbiz_png/mm0SFeTD9mEW4vbMymVsqoXM4m6llyb5Gew2XJg7HMc6dIicVMxWx3kEOydnSGYshaLSWP94SYTzhGRHEw5fnV3aRXjoe5WNt4Xyb7tAlia6s/640?wx_fmt=png&from=appmsg)
<!-- 插图建议：225/258/567/51010四种密码的特点、适合人群、推荐产品。适合文章开篇或结尾总结。 -->
`,
  sunLifeGalaxy: `# 永明星河尊享2

页面类型：产品母页
状态：正式页
最后更新：2026-05-08

## 1. 页面用途

记住，这不是那种看两眼就完事的"短摘要"，它是星河尊享2的**"终极情报主档案"**。

**核心边界**：别把它当成"高收益冲刺型"产品来吹。它本质上就是**"用一部分预期高回报的可能性，置换更高的保证性和确定性"**。

## 2. 怎么一句话看懂它？（定调判断）

**香港储蓄险里典型的"防守型标杆"。** 保证IRR约1%全港领先，复归红利占比22.76%让提领稳如磐石，但前中期预期收益不如盛利2激进。

## 3. 写作抓手层（Agent 先读这个）

**3.1 先抓误会，不要一上来就讲它保底高**

- **误会一：提领王，就等于任何口径下都最强。** 星河强的是提领确定性，不是所有提领口径都赢。
- **误会二：稳，就等于无聊、等于收益低。** 星河真正厉害的地方，是把"安全垫"和"现金流"做成了一体。
- **误会三：高保证，谁买都不会错。** 如果客户核心目标是前 10-20 年效率，必须承认盛利2更有冲劲。

## 7. 关键事实与数据（出稿前的硬核弹药）

**【保证底座：全港第一梯队】**

- 5年缴：13年保证回本，7年预期回本。
- 保证IRR从第10年起一路领先竞品，后期可达1%。
- 保单第10年，保证现金价值就能比盛利2多出约20万（25万美元×2年示例）。

**【复归红利占比：22.76%】**

- 复归红利一经派发即保证，是提领时优先动用的部分。
- 占比越高，提领确定性越强，对保单后续增长消耗越小。
- 相比之下，盛利2复归红利仅约14.12%。

## 10. 怎么跟别人比？（横评坐标系）

- **对决 安盛盛利2**：星河是防守（保证+确定性），盛利是进攻（预期+提领）。225快领星河更强，258/567提领盛利更强。
- **对决 富卫盈聚天下2**：盈聚天下2提领功能丰富（236/567），但保证底座和复归红利确定性不如星河。
- **对决 纯高保证产品**：星河在保证和预期之间做了较好平衡。

## 11. 横评证据与对照结论

| 横评页 | 对比口径 | 星河尊享2赢在哪里 | 星河尊享2输在哪里 |
|---|---|---|---|
| 2年缴储蓄险横评 | 保证现金价值 | 9款2年缴对比里，星河保证回本第10年，第20年保证现金价值112,400美元，全场最高。 | 无 |
| 2年缴储蓄险横评 | 盛利2 vs 星河尊享2 | 保证现金价值从头领先，第13年保证回本。 | 前30年预期现金价值落后盛利2约13万美元。 |
| 港险提领功能横评 | 225极致快领 | 2年缴第2年起每年提5%，交完即领；225终身提取能力全场最强。 | 258/567/51010等延迟高比例提领口径下，盛利2更强。 |

## 13. 图片弹药库

![9款2年缴产品保证现金价值对比表](https://mmbiz.qpic.cn/sz_mmbiz_png/O1oFUTFpDP2NW80KChvn96I9ku9dyaFJC6URA1Fia8Eoxkx6B7ZMbCUNKfFtpywYENc7x1A5r5FrYHe0H58TD5EkKv00eDZ0icLruMtgm0qbY/640?wx_fmt=png)
<!-- 插图建议：星河尊享2保证回本13年全场最快，长期保证IRR达1%远超其他产品。适合论证“保证底座全港最强”。 -->

![盛利2 vs 星河尊享2保证现金价值对比](https://mmbiz.qpic.cn/mmbiz_png/O1oFUTFpDP25eGuMcrZVmCIiceba7zETWj9zGWcHuBQicb8doyIiaMJ8FpFr5UTrBn8oSXz9joDllSxAUaZjqQXzLVTf3OpXFI0DHVvic8GQzhE/640?wx_fmt=png&from=appmsg)
<!-- 插图建议：星河保证现金价值从头领先到尾，第10年差出约20万美金；适合写“防守vs进攻”坐标系。 -->

![567提领全生命周期案例](https://mmbiz.qpic.cn/sz_mmbiz_png/Uo8SjRYXVdJ9CibE1hV7I9Vj0SwYrsmo1s7chAMugDnzA8vR7oJ9iarCFuicE6F2pK5icqmONQdZVmHJ7reJCOx5ibQ/640?wx_fmt=png)
<!-- 插图建议：5万×5年缴，第6年起每年提1.75万，覆盖教育金、养老金和传承。适合写“现金流底仓”。 -->

![盛利2 vs 星河尊享2提领后剩余现价对比](https://mmbiz.qpic.cn/sz_mmbiz_png/O1oFUTFpDP1qm7ByHEIz5QyKIbBOyudeY5tFgsRTze6hHMRegHuJeIuNI7SZjBwN8mgLNmPxic4yYbicBZfTicwpEFeA9uNMHhuJNlp48r4VbM/640?wx_fmt=png&from=appmsg)
<!-- 插图建议：268提领下盛利2剩余现金价值始终领先星河尊享2，用来说明“提领王不是所有口径都赢”。 -->
`
};

const knowledgeBaseDir = 'KnowledgeBase/hk-savings-insurance-wiki/';

const jobs = [
  {
    id: '260706_友邦环宇盈活提领',
    title: '友邦环宇盈活提领',
    topic: '友邦环宇盈活提领能力怎么样',
    folder: 'WritingSpace/260706_友邦环宇盈活提领/',
    purpose: '想写给已经拿到计划书、关心未来养老提领的客户，让他意识到环宇盈活不是不能买，而是不能把它当高频提领工具。',
    status: 'waiting',
    stage: 'decision',
    updated: '10:20',
    category: '提领现金流台',
    archived: false,
    materials: [
      {
        id: 'm1',
        type: 'local',
        title: '友邦环宇盈活',
        path: 'wiki/产品母页/友邦环宇盈活.md',
        reason: '这篇母页直接给出环宇盈活的核心边界：长期静态收益强，但提领后表现弱，适合作为主事实来源。',
        points: ['长期静态持有是主场，不适合高频提领', '567 极致提领下可能断单', '提领时会更早消耗终期账户'],
        visuals: ['环宇盈活 vs 星河尊享2 提领对比表', '四大外资 566 提领对比表'],
        visualAssets: [
          {
            title: '环宇盈活 vs 星河尊享2 提领对比表',
            src: 'https://mmbiz.qpic.cn/sz_mmbiz_png/Uo8SjRYXVdLLgOm6Jbvk0kibmib4v0lUibNGDObibqQBXCxM2VBr12YRAOaw4A6yveU6rDYmnUDKTjOKdSYmCsYoPA/640?wx_fmt=png&from=appmsg',
            note: '566/5108 提领下前 10 年环宇稍优，第 20 年起被星河反超；567 极致提领下环宇可能断单。',
            tag: '提领对比'
          },
          {
            title: '四大外资 566 提领对比表',
            src: 'https://mmbiz.qpic.cn/mmbiz_png/O1oFUTFpDP05pwfQqOvvyuYJFfaeaN5iaEyKs1Ct5zJ3A2kFKfNkB6BkVfU29u741sFYrkG2Kq7b4NAcpjnAYXicqgnMeOZezzPNWz2reN0os/640?wx_fmt=png&from=appmsg',
            note: '盛利2前中后期断层领先，第 20 年剩余现价比环宇高近 20 万。',
            tag: '横评表'
          }
        ],
        usage: 'must',
        previewMarkdown: markdownPreviews.aiaLiving
      },
      {
        id: 'm2',
        type: 'local',
        title: '港险提领功能横评',
        path: 'wiki/产品母页/港险提领功能横评.md',
        reason: '它把 225、258、567、51010 四种提领口径放在一起，能支撑“同样提领，产品差距很大”的核心冲突。',
        points: ['225 星河尊享2领先', '258/567 盛利2更强', '51010 下环宇盈活表现弱于盛利2'],
        visuals: ['567 提领后各保司账户价值表', '提领密码结论矩阵'],
        visualAssets: [
          {
            title: '567 提领后各保司账户价值表',
            src: 'https://mmbiz.qpic.cn/mmbiz_png/PXKyvKHFU40wYH0uMwL0wic5GKW6Y4hIQz4SBlfJh2NS3N3pujTY4usCdsym16dzckf3KXia83pkVqcYeHndaVrt95RNiaE75GVsKsvNAJrw5I/640?wx_fmt=png',
            note: '第 20 年盛利2账户 34 万、盈聚天下2 31 万、星河尊享2 27 万；适合支撑 567 主流提领口径。',
            tag: '567'
          },
          {
            title: '提领密码结论矩阵',
            src: 'https://mmbiz.qpic.cn/sz_mmbiz_png/mm0SFeTD9mEW4vbMymVsqoXM4m6llyb5Gew2XJg7HMc6dIicVMxWx3kEOydnSGYshaLSWP94SYTzhGRHEw5fnV3aRXjoe5WNt4Xyb7tAlia6s/640?wx_fmt=png&from=appmsg',
            note: '把 225、258、567、51010 四种提领密码放在一张图里，适合开头建立横评坐标。',
            tag: '结论矩阵'
          }
        ],
        usage: 'must',
        previewMarkdown: markdownPreviews.withdrawalCompare
      },
      {
        id: 'm3',
        type: 'local',
        title: '永明星河尊享2',
        path: 'wiki/产品母页/永明星河尊享2.md',
        reason: '星河是环宇盈活在提领场景里的关键对照组，能说明“静态收益强”和“提领稳”不是一回事。',
        points: ['保证底座更厚', '复归红利占比 22.76%', '225 快领更适合接近退休的人'],
        visuals: ['盛利2 vs 星河尊享2 提领对比表'],
        visualAssets: [
          {
            title: '盛利2 vs 星河尊享2 提领对比表',
            src: 'https://mmbiz.qpic.cn/sz_mmbiz_png/O1oFUTFpDP1qm7ByHEIz5QyKIbBOyudeY5tFgsRTze6hHMRegHuJeIuNI7SZjBwN8mgLNmPxic4yYbicBZfTicwpEFeA9uNMHhuJNlp48r4VbM/640?wx_fmt=png&from=appmsg',
            note: '268 提领下盛利2剩余现金价值始终领先星河，用来说明“提领王不是所有口径都赢”。',
            tag: '提领横评'
          },
          {
            title: '盛利2 vs 星河尊享2 保证现金价值对比',
            src: 'https://mmbiz.qpic.cn/mmbiz_png/O1oFUTFpDP25eGuMcrZVmCIiceba7zETWj9zGWcHuBQicb8doyIiaMJ8FpFr5UTrBn8oSXz9joDllSxAUaZjqQXzLVTf3OpXFI0DHVvic8GQzhE/640?wx_fmt=png&from=appmsg',
            note: '星河保证现金价值从头领先到尾，适合补充“防守型标杆”的判断。',
            tag: '保证底座'
          }
        ],
        usage: 'reference',
        previewMarkdown: markdownPreviews.sunLifeGalaxy
      }
    ],
    urlMaterials: [
      {
        id: 'url_001',
        type: 'url',
        title: '临时 URL 素材：客户发来的计划书解读',
        url: 'https://mp.weixin.qq.com/s/WritingMoneyDemo001',
        path: 'WritingSpace/260706_友邦环宇盈活提领/materials/url_001.md',
        reason: '用户手动添加的临时素材，已模拟解析正文、图片 OCR 和表格摘要，只服务当前文章项目。',
        points: ['可作为本篇的计划书语境', '需要人工确认数据口径', '不作为长期知识库事实'],
        visuals: ['计划书截图 OCR 表格'],
        usage: 'reference',
        parseStatus: 'ready',
        addedAt: '10:12'
      }
    ],
    templates: [
      {
        id: 't1',
        title: '条款黑盒写作框架',
        path: 'wiki/创作框架/条款黑盒写作框架.md',
        reason: '这个选题核心不是介绍环宇盈活，而是拆提领顺序和账户消耗机制，适合把销售说法和真实执行规则分开。',
        selected: true
      },
      {
        id: 't2',
        title: '产品写作框架',
        path: 'wiki/创作框架/产品写作框架.md',
        reason: '如果要做单品测评，可以用产品框架，但要避免写成普通产品介绍，必须把提领短板放在主线。',
        selected: false
      },
      {
        id: 't3',
        title: '产品横评写作框架',
        path: 'wiki/创作框架/产品横评写作框架.md',
        reason: '如果文章想同时截流星河、盛利、环宇三款提领对比词，可以改用横评框架，但会牺牲单品聚焦度。',
        selected: false
      }
    ],
    plan: {
      titleDirection: '友邦环宇盈活提领，最容易误判的不是收益，而是账户怎么被掏空',
      core: '环宇盈活的主场是长期静态复利，不是提领现金流。它赢在不动，输在持续大额提领。',
      outline: [
        '开头先拆误会：看到 6.5% 演示，不等于提领也强',
        '承认环宇盈活真实优势：品牌、静态收益、分红实现率',
        '重点拆提领机制：复归红利和终期账户如何被消耗',
        '对比星河尊享2 / 盛利2：不同提领口径下谁更合适',
        '分人群判断：谁适合环宇，谁应该换产品',
        '结尾破窗：同样提领，启动年份和比例不同，20 年后差距可能很大'
      ],
      close: '把问题落到读者自己的计划书：什么时候开始领、每年领多少、分红打折后是否还能撑住。引导用户发计划书做 20 分钟提领压力测试。'
    }
  },
  {
    id: '260706_保费融资利率压力测试',
    title: '保费融资利率压力测试',
    topic: '保费融资利率上升后还划算吗',
    folder: 'WritingSpace/260706_保费融资利率压力测试/',
    purpose: '写给被 10%+ 自有资金回报吸引，但没有真正算过贷款利率和分红下修的人。',
    status: 'running',
    stage: 'decision',
    updated: '10:18',
    category: '保费融资台',
    archived: false,
    materials: [
      {
        id: 'fm1',
        type: 'local',
        title: '保诚世誉财富 × 集友银行保费融资',
        path: 'wiki/产品母页/保诚世誉财富 × 集友银行保费融资.md',
        reason: '这套方案提供了完整的利率、杠杆、压力测试数据，是写融资风险的主素材。',
        points: ['P-1.4% 是当前快照，不是锁定承诺', '利率和分红会同时影响息差', '资产证明和流动性门槛必须写清'],
        visuals: ['融资后 IRR 测算表', '分红 85% 压力测试表'],
        usage: 'must'
      }
    ],
    urlMaterials: [],
    templates: [
      {
        id: 'ft1',
        title: '保费融资写作框架',
        path: 'wiki/创作框架/保费融资写作框架.md',
        reason: '这个选题要拆利率、分红、现金流和退出机制，必须用保费融资框架做压力测试。',
        selected: true
      }
    ],
    plan: {
      titleDirection: '保费融资不是白捡钱，利率一动，10% 年化可能就变味',
      core: '融资保单的核心不是收益数字，而是息差、杠杆和现金流能不能在坏场景里撑住。',
      outline: ['先承认高收益吸引力', '拆贷款利率', '拆分红实现率', '做组合压力测试', '划适配门槛'],
      close: '落到客户自己的资产流动性和银行条件。'
    }
  },
  {
    id: '260705_安盛盛利2保证回本',
    title: '安盛盛利2保证回本',
    topic: '安盛盛利2保证回本18年合理吗',
    folder: 'WritingSpace/260705_安盛盛利2保证回本/',
    purpose: '写给被 258 提领吸引，但忽略保证现金价值和回本期的人。',
    status: 'done',
    stage: 'done',
    updated: '昨天',
    category: '产品测评台',
    archived: false,
    materials: [],
    urlMaterials: [],
    templates: [],
    plan: {
      titleDirection: '安盛盛利2不是不能买，但保证回本18年这件事必须先看懂',
      core: '盛利2用低保证换高预期，适合能接受长期波动的人。',
      outline: [],
      close: '引导计划书审计。'
    }
  }
];

const demoMoreJobs = [
  ['260706_盛利2提领密码', '盛利2提领密码', '盛利2 258 提领到底是不是提款承诺', 'waiting', 'decision', '提领现金流台'],
  ['260706_星河尊享2养老现金流', '星河尊享2养老现金流', '星河尊享2 225 快领适合谁', 'running', 'decision', '提领现金流台'],
  ['260706_财富盈活vs环宇盈活', '财富盈活 vs 环宇盈活', '财富盈活是不是环宇盈活升级版', 'waiting', 'decision', '提领现金流台'],
  ['260706_51010高品质养老', '51010 高品质养老', '51010 提领到底谁更稳', 'done', 'done', '提领现金流台'],
  ['260706_世誉财富集友融资', '世誉财富集友融资', '自付 4 万撬 50 万保单能不能做', 'waiting', 'decision', '保费融资台'],
  ['260706_国寿丰饶广发融资', '国寿丰饶广发融资', '国寿丰饶传承 3 融资压力测试', 'running', 'decision', '保费融资台'],
  ['260706_中银薪火传承融资', '中银薪火传承融资', '中银薪火传承融资折扣是否值得', 'waiting', 'decision', '保费融资台'],
  ['260706_融资保单退出窗口', '融资保单退出窗口', '保费融资第几年退出最舒服', 'done', 'done', '保费融资台'],
  ['260706_保诚分红实现率', '保诚分红实现率', '保诚总现金价值履行比例是不是美颜', 'waiting', 'decision', '分红实现率台'],
  ['260706_友邦分红实现率', '友邦分红实现率', '友邦分红实现率到底稳不稳', 'running', 'decision', '分红实现率台'],
  ['260706_安盛分红异常值', '安盛分红异常值', '安盛分红实现率最低 28% 怎么看', 'waiting', 'decision', '分红实现率台'],
  ['260706_永明分红稳定性', '永明分红稳定性', '永明平均实现率不高但波动更小吗', 'done', 'done', '分红实现率台'],
  ['260706_宏挚传承无忧选', '宏挚传承无忧选', '无忧选是不是比普通提领更安全', 'running', 'decision', '产品测评台'],
  ['260706_鑫安逸锁息价值', '鑫安逸锁息价值', '30 年保证复利 3.5% 值不值得', 'waiting', 'decision', '产品测评台'],
  ['260706_盈聚天下2短缴速领', '盈聚天下2短缴速领', '富卫盈聚天下2短缴提领适合谁', 'waiting', 'decision', '产品测评台'],
  ['260706_周大福匠心飞越', '匠心飞越提领', '周大福匠心飞越 116 提领有什么代价', 'done', 'done', '产品测评台'],
  ['260705_CRS2港险税务', 'CRS2 港险税务', 'CRS2.0 后香港保险到底要不要交税', 'waiting', 'decision', '外围选题台'],
  ['260705_提前还贷还是港险', '提前还贷还是港险', '手里 300 万先还贷还是配置港险', 'running', 'decision', '外围选题台'],
  ['260705_港险开户收紧', '港险开户收紧', '香港开户收紧会不会影响投保', 'waiting', 'decision', '外围选题台'],
  ['260705_大额保单核保', '大额保单核保', '高净值客户买大额保单会卡在哪里', 'done', 'done', '外围选题台']
];

demoMoreJobs.forEach(([id, title, topic, status, stage, category]) => {
  jobs.push({
    id,
    title,
    topic,
    folder: `WritingSpace/${id}/`,
    purpose: `围绕「${topic}」做一篇可进入公众号发布流程的静态 Demo 文章项目。`,
    status,
    stage,
    updated: status === 'done' ? '昨天' : status === 'running' ? '运行中' : '待确认',
    category,
    archived: false,
    materials: [],
    urlMaterials: [],
    templates: [],
    plan: {
      titleDirection: `${title}：先看清楚这一层，再谈值不值得`,
      core: '等待素材推荐和模板选择后固化核心判断。',
      outline: ['收集素材', '选择模板', '确认方案', '生成写作包'],
      close: '等待写作方案生成。'
    }
  });
});

let activeJobId = jobs[0].id;
let activeStage = 'decision';
let activeMode = 'mother';
let activeDetail = { type: 'job', id: jobs[0].id };
let showArchived = true;
let toastTimer = null;
let purposeConfirmHandlers = {};
const collapsedGroups = new Set();

function $(selector) {
  return document.querySelector(selector);
}

function getActiveJob() {
  return jobs.find(job => job.id === activeJobId) || jobs[0];
}

function statusLabel(status) {
  return {
    waiting: '等待确认',
    running: '运行中',
    done: '已完成',
    failed: '失败',
    archived: '已归档',
    rework: '需返工'
  }[status] || status;
}

function sidebarStatusLabel(status) {
  return {
    waiting: '待确认',
    running: '运行中',
    done: '完成',
    failed: '失败',
    archived: '归档',
    rework: '返工'
  }[status] || status;
}



function normalizeStage(stage, mode = activeMode) {
  if (mode === 'batch') {
    return ['batch-config', 'batch-done'].includes(stage) ? stage : 'batch-config';
  }
  return ['materials', 'templates', 'plan', 'decision'].includes(stage) ? 'decision' : stage;
}

function stageLabel(stage) {
  const mode = ['batch-config', 'batch-done'].includes(stage) ? 'batch' : 'mother';
  const list = mode === 'batch' ? batchStages : stages;
  return (list.find(s => s.id === normalizeStage(stage, mode)) || list[0]).name;
}

function getCurrentStages() {
  return activeMode === 'batch' ? batchStages : stages;
}

function usageLabel(usage) {
  return { must: '必用', reference: '参考', skip: '不用' }[usage] || usage;
}

function parseStatusLabel(status) {
  return { parsing: '解析中', ready: '已解析', failed: '解析失败' }[status] || '已解析';
}

function getLocalMaterials(job) {
  return Array.isArray(job.materials) ? job.materials : [];
}

function getUrlMaterials(job) {
  if (!Array.isArray(job.urlMaterials)) job.urlMaterials = [];
  return job.urlMaterials;
}

function getAllMaterials(job) {
  return [...getLocalMaterials(job), ...getUrlMaterials(job)];
}

function findMaterial(job, materialId) {
  return getAllMaterials(job).find(material => material.id === materialId);
}

function getJobStats(job) {
  const localMaterials = getLocalMaterials(job);
  const urlMaterials = getUrlMaterials(job);
  const allMaterials = [...localMaterials, ...urlMaterials];
  return {
    total: allMaterials.length,
    localTotal: localMaterials.length,
    urlTotal: urlMaterials.length,
    must: allMaterials.filter(m => m.usage === 'must').length,
    reference: allMaterials.filter(m => m.usage === 'reference').length,
    skip: allMaterials.filter(m => m.usage === 'skip').length,
    templates: job.templates.length
  };
}

function showToast(message) {
  const toast = $('#toast');
  toast.textContent = message;
  toast.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('show'), 1800);
}

function openPurposeEditor() {
  const job = getActiveJob();
  const modal = $('#purposeModal');
  modal.dataset.jobId = job.id;
  modal.dataset.original = job.purpose;
  $('#purposeEditor').value = job.purpose;
  updatePurposeEditorMeta();
  modal.classList.add('show');
  modal.setAttribute('aria-hidden', 'false');
  setTimeout(() => $('#purposeEditor').focus(), 0);
}

function closePurposeEditor() {
  $('#purposeModal').classList.remove('show');
  $('#purposeModal').setAttribute('aria-hidden', 'true');
  closePurposeConfirm();
}

function hasPurposeDraftChanged() {
  return $('#purposeEditor').value !== ($('#purposeModal').dataset.original || '');
}

function updatePurposeEditorMeta() {
  const changed = hasPurposeDraftChanged();
  $('#purposeDirtyState').textContent = changed ? '已修改，关闭前需要确认' : '未修改';
  $('#purposeCharCount').textContent = $('#purposeEditor').value.length;
  $('#purposeDirtyState').parentElement.classList.toggle('dirty', changed);
}

function askPurposeClose(intent) {
  if (!hasPurposeDraftChanged()) {
    closePurposeEditor();
    if (intent === 'save') showToast('写作目的未修改');
    return;
  }
  if (intent === 'save') {
    openPurposeConfirm({
      title: '确认保存写作目的？',
      desc: '保存后会更新主页面、项目详情和写作包里的写作目的。',
      primary: '保存并关闭',
      secondary: '继续编辑',
      onPrimary: savePurposeDraft,
      onSecondary: closePurposeConfirm
    });
    return;
  }
  openPurposeConfirm({
    title: '写作目的已修改',
    desc: '要保存这次修改吗？如果不保存，这次编辑内容会被丢弃。',
    primary: '保存并关闭',
    secondary: '不保存关闭',
    tertiary: '继续编辑',
    onPrimary: savePurposeDraft,
    onSecondary: discardPurposeDraft,
    onTertiary: closePurposeConfirm
  });
}

function savePurposeDraft() {
  const draft = $('#purposeEditor').value.trim();
  if (!draft) {
    closePurposeConfirm();
    showToast('写作目的不能为空');
    $('#purposeEditor').focus();
    return;
  }
  const jobId = $('#purposeModal').dataset.jobId;
  const job = jobs.find(item => item.id === jobId) || getActiveJob();
  job.purpose = draft;
  job.updated = '刚刚';
  closePurposeEditor();
  renderAll();
  showToast('写作目的已保存');
}

function discardPurposeDraft() {
  closePurposeEditor();
  showToast('已放弃本次修改');
}

function openPurposeConfirm(config) {
  purposeConfirmHandlers = config;
  $('#purposeConfirmTitle').textContent = config.title;
  $('#purposeConfirmDesc').textContent = config.desc;
  $('#purposeConfirmPrimary').textContent = config.primary;
  $('#purposeConfirmSecondary').textContent = config.secondary;
  $('#purposeConfirmTertiary').textContent = config.tertiary || '';
  $('#purposeConfirmTertiary').style.display = config.tertiary ? '' : 'none';
  $('#purposeConfirmSecondary').classList.toggle('danger', config.secondaryDanger !== false);
  $('#purposeConfirmModal').classList.add('show');
  $('#purposeConfirmModal').setAttribute('aria-hidden', 'false');
}

function closePurposeConfirm() {
  $('#purposeConfirmModal').classList.remove('show');
  $('#purposeConfirmModal').setAttribute('aria-hidden', 'true');
  purposeConfirmHandlers = {};
}

function renderAll() {
  renderModeSwitch();
  renderNavMeta();
  updateSidebar();
  if (activeMode === 'batch') {
    document.body.classList.add('batch-mode');
    renderBatchList();
    renderBatchView();
  } else {
    document.body.classList.remove('batch-mode');
    renderJobs();
    renderHero();
    renderSteps();
    renderWorkspace();
  }
}

function renderHero() {
  const job = getActiveJob();
  $('#jobFolder').textContent = job.folder;
  $('#jobTitle').textContent = job.title;
  $('#jobPurpose').textContent = job.purpose;
}

function renderJobs() {
  const q = $('#jobSearch').value.trim().toLowerCase();
  const filtered = jobs.filter(job => {
    if (!showArchived && job.archived) return false;
    const hay = [job.title, job.topic, job.folder, job.purpose, statusLabel(job.status), stageLabel(job.stage)].join(' ').toLowerCase();
    return !q || hay.includes(q);
  });
  const groups = {};
  filtered.forEach(job => {
    const key = job.category || '未分类';
    if (!groups[key]) groups[key] = [];
    groups[key].push(job);
  });
  const order = ['提领现金流台', '保费融资台', '分红实现率台', '产品测评台', '外围选题台', '未分类'];
  $('#jobList').innerHTML = order.filter(name => groups[name]?.length).map(name => {
    const collapsed = collapsedGroups.has(name);
    return `
      <div class="job-group ${collapsed ? 'collapsed' : ''}" data-group="${name}">
        <div class="job-group-head" data-toggle-group="${name}">
          <span class="job-group-name"><span class="job-group-arrow">▾</span>${name}</span>
          <span class="job-group-count">${groups[name].length}</span>
        </div>
        <div class="job-group-body">
          ${groups[name].map(job => `
            <div class="job-row ${job.id === activeJobId ? 'active' : ''}" data-job-id="${job.id}" title="${job.topic}">
              <span class="status-dot ${job.status}"></span>
              <div class="job-title">${job.title}</div>
              <span class="job-status-text ${job.status}">${sidebarStatusLabel(job.status)}</span>
            </div>
          `).join('')}
        </div>
      </div>
    `;
  }).join('') || '<div class="empty-block">没有匹配的文章项目</div>';
}

function renderSteps() {
  const job = getActiveJob();
  const list = getCurrentStages();
  const progressStage = activeMode === 'batch' ? activeStage : job.stage;
  const jobStage = normalizeStage(progressStage);
  const viewStage = normalizeStage(activeStage);
  const activeIndex = Math.max(0, list.findIndex(s => s.id === jobStage));
  $('#stepTrack').innerHTML = list.map((stage, index) => `
    <div class="step-item has-tip ${stage.id === viewStage ? 'active' : ''} ${index < activeIndex ? 'done' : ''}" data-stage="${stage.id}" data-tip="${escapeAttr(stage.desc)}" title="${escapeAttr(stage.desc)}">
      <div class="step-name"><span class="step-num">${index + 1}</span><span>${stage.name}</span></div>
      <div class="step-desc">${stage.desc}</div>
    </div>
  `).join('');
}

function renderWorkspace() {
  const stage = normalizeStage(activeStage);
  if (stage === 'decision') return renderDecision();
  if (stage === 'package') return renderPackage();
  return renderDone();
}

function workspaceHead(title, desc, actions = '') {
  return `
    <div class="workspace-head">
      <div>
        <div class="workspace-title has-tip" data-tip="${escapeAttr(desc)}" title="${escapeAttr(desc)}">${title}</div>
      </div>
      <div class="workspace-actions">${actions}</div>
    </div>
  `;
}

function renderUsageButtons(material) {
  return `
    <div class="segmented" data-material-id="${escapeAttr(material.id)}">
      <button class="seg ${material.usage === 'must' ? 'active must' : ''}" data-usage="must">必用</button>
      <button class="seg ${material.usage === 'reference' ? 'active ref' : ''}" data-usage="reference">参考</button>
      <button class="seg ${material.usage === 'skip' ? 'active skip' : ''}" data-usage="skip">不用</button>
    </div>
  `;
}

function renderPointList(material) {
  const points = Array.isArray(material.points) ? material.points : [];
  if (!points.length) return '<div class="muted-line">等待可用观点</div>';
  return points.slice(0, 3).map(point => `<div class="point">${escapeHtml(point)}</div>`).join('');
}

function renderParseStatus(material) {
  const status = material.parseStatus || 'ready';
  const spinner = status === 'parsing' ? '<span class="tiny-spinner"></span>' : '';
  return `<span class="status-chip ${status}">${spinner}${parseStatusLabel(status)}</span>`;
}

function materialSectionHead(title, desc, actions = '') {
  return `
    <div class="material-section-head">
      <div class="material-section-title has-tip" data-tip="${escapeAttr(desc)}" title="${escapeAttr(desc)}">${title}</div>
      <div class="workspace-actions">${actions}</div>
    </div>
  `;
}

function renderMaterialSections(job) {
  const localMaterials = getLocalMaterials(job);
  const urlMaterials = getUrlMaterials(job);
  return `
    <div class="material-sections">
      <section class="material-section">
        ${materialSectionHead(
          '知识库推荐素材',
          '只从本地知识库里推荐素材；重跑推荐不会影响下面的 URL 临时素材。',
          '<button class="mini-btn" data-action="rerun-materials">重跑素材推荐</button>'
        )}
        <div class="material-scroll">
          <div class="material-grid">
            <div class="material-table-head">
              <div>素材</div><div>推荐语</div><div>可用观点</div><div>使用状态</div>
            </div>
            ${localMaterials.map(material => `
              <div class="material-row ${activeDetail.type === 'material' && activeDetail.id === material.id ? 'active' : ''}" data-detail-type="material" data-detail-id="${escapeAttr(material.id)}">
                <div>
                  <div class="mat-title">${escapeHtml(material.title)}</div>
                  <div class="mat-path">${escapeHtml(material.path)}</div>
                  <div class="mat-badges"><span class="badge badge-blue">本地 Markdown</span></div>
                </div>
                <div class="mat-text">${escapeHtml(material.reason)}</div>
                <div class="mat-points">${renderPointList(material)}</div>
                ${renderUsageButtons(material)}
              </div>
            `).join('') || '<div class="empty-block material-empty">当前项目还没有知识库推荐素材。点击“重跑素材推荐”模拟重新推荐。</div>'}
          </div>
        </div>
      </section>

      <section class="material-section url-section">
        ${materialSectionHead(
          'URL 临时素材',
          '用于放你手动补充的公众号 URL。真实版本会解析正文、图片 OCR 和表格摘要；这里不会自动进入正式 Wiki。',
          '<button class="mini-btn" data-action="add-url">添加 URL</button>'
        )}
        <div class="material-scroll">
          <div class="material-grid url-material-grid">
            <div class="material-table-head">
              <div>URL 素材</div><div>解析状态</div><div>OCR / 可用观点</div><div>使用状态</div>
            </div>
            ${urlMaterials.map(material => `
              <div class="material-row url-material-row ${activeDetail.type === 'material' && activeDetail.id === material.id ? 'active' : ''}" data-detail-type="material" data-detail-id="${escapeAttr(material.id)}">
                <div>
                  <div class="mat-title">${escapeHtml(material.title)}</div>
                  <div class="mat-path">${escapeHtml(material.url || material.path)}</div>
                  <div class="mat-path">${escapeHtml(material.path)}</div>
                </div>
                <div>
                  ${renderParseStatus(material)}
                  <div class="mat-mini">${escapeHtml(material.addedAt || '刚刚')}</div>
                </div>
                <div class="mat-points">${renderPointList(material)}</div>
                ${renderUsageButtons(material)}
              </div>
            `).join('') || '<div class="empty-block material-empty">还没有 URL 临时素材。点“添加 URL”输入公众号链接，解析后出现在这里。</div>'}
          </div>
        </div>
      </section>
    </div>
  `;
}

function renderTemplateSection(job) {
  return `
    <section class="material-section decision-template-section">
      ${materialSectionHead(
        '模板推荐',
        'AI 从 wiki/创作框架/*.md 里预选模板；如果不合适，可以回到 Obsidian 改模板后重跑。',
        '<button class="mini-btn" data-action="rerun-templates">重跑模板推荐</button>'
      )}
      <div class="template-grid decision-template-grid">
        ${job.templates.map(template => `
          <div class="template-card ${template.selected ? 'active' : ''}" data-template-id="${template.id}" data-detail-type="template" data-detail-id="${template.id}">
            <div class="template-title">${template.title}</div>
            <div class="template-path">${template.path}</div>
            <div class="template-reason">${template.reason}</div>
            <div class="job-meta"><span class="badge ${template.selected ? 'badge-blue' : 'badge-gray'}">${template.selected ? '已选中' : '候选模板'}</span></div>
          </div>
        `).join('') || '<div class="empty-block">当前项目还没有模板推荐。</div>'}
      </div>
    </section>
  `;
}

function renderPlanSection(job) {
  const selectedTemplate = job.templates.find(t => t.selected);
  return `
    <section class="material-section decision-plan-section">
      ${materialSectionHead(
        'AI 初版写作方案',
        '这不是正式正文，而是 Agent 基于素材和模板先给出的写作决策草案；你确认后再生成写作包。',
        '<button class="mini-btn" data-action="rerun-plan">重跑方案</button>'
      )}
      <div class="agent-plan">
        <div class="agent-plan-note">Agent 预决策 · 非正文</div>
        <p>我会把这篇文章先写成一篇“买前误判拆解”，不急着介绍产品参数，而是先把读者真正容易看错的地方拎出来：他以为自己在比较收益，其实是在比较未来能不能稳定提领。</p>
        <p>这版方案建议沿用「${selectedTemplate ? selectedTemplate.title : '待选模板'}」的思路，但不把结构锁死。Agent 可以根据素材临场调整标题、开头、观点顺序、案例切入和结尾引流，只要最后仍然围绕这个核心判断：</p>
        <blockquote>${job.plan.core}</blockquote>
        <div class="agent-plan-flow">
          ${job.plan.outline.map((item, index) => `<div class="agent-step"><span>${index + 1}</span>${item}</div>`).join('') || '<div class="agent-step"><span>1</span>等待素材和模板后，Agent 自行生成文章推进路径。</div>'}
        </div>
        <p>结尾不预设成固定话术，只给方向：${job.plan.close}</p>
      </div>
    </section>
  `;
}

function renderDecisionSummary(job) {
  const stats = getJobStats(job);
  const selectedTemplate = job.templates.find(t => t.selected);
  return `
    <div class="decision-summary">
      <div class="decision-summary-item">
        <span>素材</span>
        <b>${stats.total}</b>
        <em>${stats.must} 必用 · ${stats.reference} 参考</em>
      </div>
      <div class="decision-summary-item">
        <span>模板</span>
        <b>${selectedTemplate ? selectedTemplate.title : '未选择'}</b>
        <em>${job.templates.length} 个候选</em>
      </div>
      <div class="decision-summary-item">
        <span>方案</span>
        <b>${job.plan.titleDirection}</b>
        <em>确认后固化进写作包</em>
      </div>
    </div>
  `;
}

function renderDecision() {
  const job = getActiveJob();
  $('#workspaceCard').innerHTML = `
    ${workspaceHead(
      '写作决策',
      'AI 一次性预跑知识库素材、URL 临时素材、模板推荐和初版写作方案；你在这一页完成审美判断和取舍。',
      '<button class="mini-btn" data-action="rerun-decision">重跑预决策</button><button class="mini-btn primary" data-action="confirm-decision">确认决策，生成写作包</button>'
    )}
    ${renderDecisionSummary(job)}
    ${renderMaterialSections(job)}
    ${renderTemplateSection(job)}
    ${renderPlanSection(job)}
  `;
}

function renderMaterials() {
  const job = getActiveJob();
  $('#workspaceCard').innerHTML = `
    ${workspaceHead(
      '素材篮',
      '上半区是 AI 从本地知识库推荐的 Markdown；下半区是你手动添加的 URL 临时素材，两者互不覆盖。',
      ''
    )}
    ${renderMaterialSections(job)}
  `;
}

function renderTemplates() {
  const job = getActiveJob();
  $('#workspaceCard').innerHTML = `
    ${workspaceHead(
      '模板推荐',
      '当前默认从 wiki/创作框架/*.md 推荐。每个模板就是一个 Markdown，你可以在 Obsidian 里改完再重跑。',
      '<button class="mini-btn" data-action="rerun-templates">重跑模板推荐</button><button class="mini-btn primary" data-action="go-plan">生成写作方案</button>'
    )}
    <div class="template-grid">
      ${job.templates.map(template => `
        <div class="template-card ${template.selected ? 'active' : ''}" data-template-id="${template.id}" data-detail-type="template" data-detail-id="${template.id}">
          <div class="template-title">${template.title}</div>
          <div class="template-path">${template.path}</div>
          <div class="template-reason">${template.reason}</div>
          <div class="job-meta"><span class="badge ${template.selected ? 'badge-blue' : 'badge-gray'}">${template.selected ? '已选中' : '候选模板'}</span></div>
        </div>
      `).join('')}
    </div>
  `;
}

function renderPlan() {
  const job = getActiveJob();
  const selectedTemplate = job.templates.find(t => t.selected);
  $('#workspaceCard').innerHTML = `
    ${workspaceHead(
      '写作方案确认',
      '这里不是正式写作，只是让 Agent 自由提出方案；它可以增删结构、改标题方向、调整观点和结尾。',
      '<button class="mini-btn" data-action="back-materials">返回素材</button><button class="mini-btn" data-action="rerun-plan">重跑方案</button><button class="mini-btn primary" data-action="confirm-plan">确认方案</button>'
    )}
    <div class="agent-plan">
      <div class="agent-plan-note">Agent 自由方案 · 非正文</div>
      <p>我会把这篇文章先写成一篇“买前误判拆解”，不急着介绍产品参数，而是先把读者真正容易看错的地方拎出来：他以为自己在比较收益，其实是在比较未来能不能稳定提领。</p>
      <p>这版方案建议沿用「${selectedTemplate ? selectedTemplate.title : '待选模板'}」的思路，但不把结构锁死。Agent 可以根据素材临场调整标题、开头、观点顺序、案例切入和结尾引流，只要最后仍然围绕这个核心判断：</p>
      <blockquote>${job.plan.core}</blockquote>
      <div class="agent-plan-flow">
        ${job.plan.outline.map((item, index) => `<div class="agent-step"><span>${index + 1}</span>${item}</div>`).join('') || '<div class="agent-step"><span>1</span>等待素材和模板后，Agent 自行生成文章推进路径。</div>'}
      </div>
      <p>结尾不预设成固定话术，只给方向：${job.plan.close}</p>
    </div>
  `;
}

function renderPackage() {
  const job = getActiveJob();
  const selectedTemplate = job.templates.find(t => t.selected);
  const localMaterialLines = getLocalMaterials(job).map(m => `- [${usageLabel(m.usage)}] ${m.path}
  - 推荐语：${m.reason}
  - 可用观点：${m.points.join('；')}`).join('\n') || '无';
  const urlMaterialLines = getUrlMaterials(job).map(m => `- [${usageLabel(m.usage)}] ${m.path}
  - 原始 URL：${m.url || '未记录'}
  - 解析状态：${parseStatusLabel(m.parseStatus)}
  - 推荐语：${m.reason}
  - 可用观点：${m.points.join('；')}`).join('\n') || '无';
  const pack = `# WritingMoney 写作包

## 选题
${job.topic}

## 写作目的
${job.purpose}

## 工作目录
${job.folder}

## 知识库目录
${knowledgeBaseDir}

## 知识库推荐素材
${localMaterialLines}

## URL 临时素材
${urlMaterialLines}

## 选中模板
${selectedTemplate ? selectedTemplate.path : '未选择'}

## 写作方案
- 标题方向：${job.plan.titleDirection}
- 核心判断：${job.plan.core}
- 结尾引流：${job.plan.close}

## 输出要求
- 输出到 output/
- 同步复制到 ${job.folder}output.md
- 保留本次 decision / package / output 全流程记录
`;
  $('#workspaceCard').innerHTML = `
    ${workspaceHead(
      '写作包预览',
      '真实版本会把这个固定输入包传给 claude -p。静态 Demo 只展示它长什么样。',
      '<button class="mini-btn" data-action="copy-package">复制写作包</button><button class="mini-btn dark" data-action="simulate-finish">模拟完成写作</button>'
    )}
    <div class="note-box">这一步已经把“写作决策”固化成 Context：素材三态、模板选择、AI 初版方案和最终输入包都会落盘。</div>
    <div class="package-preview"><div class="code-card">${escapeHtml(pack)}</div></div>
  `;
}





function renderDone() {
  const job = getActiveJob();
  $('#workspaceCard').innerHTML = `
    ${workspaceHead('成稿已完成', '真实版本会写入 output/ 并复制到工作目录 output.md。这里展示静态完成态。')}
    <div class="empty-block">output/${job.id}.md 已生成，等待回到 Wiki Viewer 编辑和发布。</div>
  `;
}

function setDrawer(kicker, title, body) {
  $('#drawerKicker').textContent = kicker;
  $('#drawerTitle').textContent = title;
  $('#drawerBody').innerHTML = body;
  document.body.classList.add('drawer-open');
}

function closeDrawer() {
  document.body.classList.remove('drawer-open');
}

function renderInlineMarkdown(value) {
  return escapeHtml(value)
    .replace(/\[\[([^\]|]+)\|([^\]]+)\]\]/g, '<span class="wiki-link">$2</span>')
    .replace(/\[\[([^\]]+)\]\]/g, '<span class="wiki-link">$1</span>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
}

function isMarkdownBoundary(line) {
  const text = line.trim();
  return !text ||
    /^(#{1,6}\s|[-*]\s|\d+\.\s|>\s|!\[|\|)/.test(text) ||
    /^---+$/.test(text) ||
    /^<!--/.test(text);
}

function renderMarkdownTable(lines) {
  const rows = lines.map(line => line.trim().replace(/^\||\|$/g, '').split('|').map(cell => cell.trim()));
  const hasDivider = rows.length > 1 && rows[1].every(cell => /^:?-{3,}:?$/.test(cell));
  const head = rows[0] || [];
  const body = hasDivider ? rows.slice(2) : rows.slice(1);
  return `
    <div class="md-table-wrap">
      <table>
        <thead><tr>${head.map(cell => `<th>${renderInlineMarkdown(cell)}</th>`).join('')}</tr></thead>
        <tbody>
          ${body.map(row => `<tr>${row.map(cell => `<td>${renderInlineMarkdown(cell)}</td>`).join('')}</tr>`).join('')}
        </tbody>
      </table>
    </div>
  `;
}

function renderMarkdown(markdown) {
  if (!markdown) return '';
  const lines = markdown.trim().split(/\r?\n/);
  const output = [];
  let i = 0;

  while (i < lines.length) {
    const rawLine = lines[i];
    const line = rawLine.trim();

    if (!line) {
      i += 1;
      continue;
    }

    if (/^---+$/.test(line)) {
      output.push('<hr>');
      i += 1;
      continue;
    }

    const comment = line.match(/^<!--\s*(.*?)\s*-->$/);
    if (comment) {
      const note = comment[1].replace(/^插图建议：?/, '');
      output.push(`<div class="md-note"><span>插图建议</span><p>${renderInlineMarkdown(note)}</p></div>`);
      i += 1;
      continue;
    }

    const image = line.match(/^!\[(.*?)\]\((.*?)\)$/);
    if (image) {
      const alt = image[1] || 'Markdown 图片';
      const src = image[2];
      output.push(`
        <figure class="md-figure">
          <img src="${escapeAttr(src)}" alt="${escapeAttr(alt)}" loading="lazy" referrerpolicy="no-referrer" onerror="this.closest('figure').classList.add('image-error')">
          <figcaption>${renderInlineMarkdown(alt)}</figcaption>
        </figure>
      `);
      i += 1;
      continue;
    }

    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      const level = Math.min(heading[1].length + 1, 5);
      output.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`);
      i += 1;
      continue;
    }

    if (line.startsWith('|')) {
      const tableLines = [];
      while (i < lines.length && lines[i].trim().startsWith('|')) {
        tableLines.push(lines[i]);
        i += 1;
      }
      output.push(renderMarkdownTable(tableLines));
      continue;
    }

    if (line.startsWith('>')) {
      const quoteLines = [];
      while (i < lines.length && lines[i].trim().startsWith('>')) {
        quoteLines.push(lines[i].trim().replace(/^>\s?/, ''));
        i += 1;
      }
      output.push(`<blockquote>${quoteLines.map(renderInlineMarkdown).join('<br>')}</blockquote>`);
      continue;
    }

    if (/^[-*]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^[-*]\s+/.test(lines[i].trim())) {
        items.push(lines[i].trim().replace(/^[-*]\s+/, ''));
        i += 1;
      }
      output.push(`<ul>${items.map(item => `<li>${renderInlineMarkdown(item)}</li>`).join('')}</ul>`);
      continue;
    }

    if (/^\d+\.\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\d+\.\s+/.test(lines[i].trim())) {
        items.push(lines[i].trim().replace(/^\d+\.\s+/, ''));
        i += 1;
      }
      output.push(`<ol>${items.map(item => `<li>${renderInlineMarkdown(item)}</li>`).join('')}</ol>`);
      continue;
    }

    const paragraphLines = [line];
    i += 1;
    while (i < lines.length && !isMarkdownBoundary(lines[i])) {
      paragraphLines.push(lines[i].trim());
      i += 1;
    }
    output.push(`<p>${renderInlineMarkdown(paragraphLines.join(' '))}</p>`);
  }

  return output.join('');
}

function renderFallbackArticle(material) {
  return `
    <h4>${escapeHtml(material.title)}</h4>
    <p>${escapeHtml(material.reason)}</p>
    <ul>
      ${material.points.map(point => `<li>${escapeHtml(point)}</li>`).join('')}
    </ul>
    <p>这里模拟未来点开素材后的文章页：正文 Markdown、图片 OCR、表格摘要都会在这个抽屉里预览，不挤占主工作区。</p>
    <div class="mock-image-grid">
      ${material.visuals.map(v => `<div class="mock-image-card">${escapeHtml(v)}</div>`).join('')}
    </div>
  `;
}

function extractMarkdownFigures(markdown) {
  if (!markdown) return [];
  const lines = markdown.trim().split(/\r?\n/);
  const figures = [];

  lines.forEach((line, index) => {
    const image = line.trim().match(/^!\[(.*?)\]\((.*?)\)$/);
    if (!image) return;
    const nextLine = (lines[index + 1] || '').trim();
    const comment = nextLine.match(/^<!--\s*(.*?)\s*-->$/);
    figures.push({
      title: image[1] || 'Markdown 图片',
      src: image[2],
      note: comment ? comment[1].replace(/^插图建议：?/, '') : '',
      tag: 'Markdown'
    });
  });

  return figures;
}

function normalizeVisualText(value) {
  return String(value || '').replace(/[\s｜|/、，,。:：-]/g, '').toLowerCase();
}

function getVisualAssets(material) {
  if (Array.isArray(material.visualAssets) && material.visualAssets.length) {
    return material.visualAssets;
  }

  const labels = Array.isArray(material.visuals) ? material.visuals : [];
  const figures = extractMarkdownFigures(material.previewMarkdown);
  if (!figures.length) {
    return labels.map(label => ({ title: label, note: '真实版本会显示解析后的原图、OCR 或表格缩略图。' }));
  }

  if (!labels.length) return figures.slice(0, 6);

  return labels.map(label => {
    const labelKey = normalizeVisualText(label);
    const matched = figures.find(figure => {
      const figureKey = normalizeVisualText(figure.title);
      return figureKey.includes(labelKey) || labelKey.includes(figureKey);
    });
    return matched ? { ...matched, title: label } : { title: label, note: '这张图在正文预览里可继续向下查看。' };
  });
}

function renderVisualGallery(material) {
  const assets = getVisualAssets(material);
  if (!assets.length) return '<div class="empty-block">当前素材还没有可用图片或表格。</div>';

  return `
    <div class="visual-gallery">
      ${assets.map(asset => `
        <article class="visual-card ${asset.src ? '' : 'visual-card-empty'}">
          ${asset.src ? `
            <div class="visual-thumb-wrap">
              <img class="visual-thumb" src="${escapeAttr(asset.src)}" alt="${escapeAttr(asset.title)}" loading="lazy" referrerpolicy="no-referrer" onload="queueVisualMasonry()" onerror="this.closest('.visual-card').classList.add('image-error'); queueVisualMasonry()">
            </div>
          ` : '<div class="visual-placeholder">等待解析缩略图</div>'}
          <div class="visual-meta">
            <div class="visual-title">${escapeHtml(asset.title)}</div>
            ${asset.note ? `<div class="visual-note">${escapeHtml(asset.note)}</div>` : ''}
            ${asset.tag ? `<span class="visual-tag">${escapeHtml(asset.tag)}</span>` : ''}
          </div>
        </article>
      `).join('')}
    </div>
  `;
}

function layoutVisualMasonry() {
  document.querySelectorAll('.visual-gallery').forEach(gallery => {
    const cards = Array.from(gallery.querySelectorAll('.visual-card'));
    if (!cards.length) return;

    const gap = 10;
    const galleryWidth = gallery.clientWidth;
    const cols = galleryWidth >= 760 && cards.length >= 3 ? 3 : galleryWidth >= 420 && cards.length >= 2 ? 2 : 1;
    const colWidth = Math.floor((galleryWidth - gap * (cols - 1)) / cols);
    const heights = Array(cols).fill(0);

    gallery.classList.add('masonry-ready');
    gallery.dataset.cols = String(cols);
    cards.forEach(card => {
      card.style.width = `${colWidth}px`;
      card.style.transform = 'translate(0, 0)';
    });

    cards.forEach(card => {
      const targetHeight = Math.min(...heights);
      const column = heights.indexOf(targetHeight);
      const x = column * (colWidth + gap);
      const y = heights[column];
      card.style.transform = `translate(${x}px, ${y}px)`;
      heights[column] += card.offsetHeight + gap;
    });

    gallery.style.height = `${Math.max(...heights) - gap}px`;
  });
}

function queueVisualMasonry() {
  requestAnimationFrame(() => {
    layoutVisualMasonry();
    setTimeout(layoutVisualMasonry, 80);
  });
}

function openMaterialDrawer(materialId) {
  const job = getActiveJob();
  const material = findMaterial(job, materialId) || getAllMaterials(job)[0];
  if (!material) return openJobDrawer(job);
  activeDetail = { type: 'material', id: material.id };
  const previewTitle = material.type === 'url' ? '临时 URL 文章预览' : '真实 Markdown 渲染预览';
  const typeLabel = material.type === 'url' ? 'URL 临时素材' : '本地 Markdown';
  const typeClass = material.type === 'url' ? 'badge-yellow' : 'badge-blue';
  const articleBody = material.previewMarkdown ? renderMarkdown(material.previewMarkdown) : renderFallbackArticle(material);
  const sourceBlock = material.type === 'url' ? `
      <div class="detail-card">
        <div class="detail-card-title">URL 解析</div>
        <div class="kv"><span>原始 URL</span><span class="kv-url">${escapeHtml(material.url || '未记录')}</span></div>
        <div class="kv"><span>解析状态</span><span>${parseStatusLabel(material.parseStatus)}</span></div>
        <div class="kv"><span>临时落盘</span><span class="kv-url">${escapeHtml(material.path)}</span></div>
      </div>
    ` : '';
  setDrawer('素材预览', material.title, `
      <div class="detail-title-row">
        <div>
          <div class="detail-title">${escapeHtml(material.title)}</div>
          <div class="detail-sub">${escapeHtml(material.path)}</div>
        </div>
        <span class="badge ${typeClass}">${typeLabel}</span>
      </div>
      ${sourceBlock}
      <div class="detail-card">
        <div class="detail-card-title">推荐语</div>
        <div class="card-v">${escapeHtml(material.reason)}</div>
      </div>
      <div class="detail-card">
        <div class="detail-card-title">可用观点</div>
        <div class="detail-list">${material.points.map(point => `<div class="detail-row">${escapeHtml(point)}</div>`).join('')}</div>
      </div>
      <div class="detail-card">
        <div class="detail-card-title">可用图片 / 表格</div>
        ${renderVisualGallery(material)}
      </div>
      <div class="detail-card">
        <div class="detail-card-title">当前状态</div>
        <div class="kv"><span>使用方式</span><span>${usageLabel(material.usage)}</span></div>
        <div class="kv"><span>偏好记录</span><span>会写入 materials.selected.json</span></div>
      </div>
      <div class="article-preview">
        <div class="article-preview-head">
          <div class="article-preview-title">${previewTitle}</div>
          <div class="article-preview-meta">${escapeHtml(material.path)}</div>
          ${material.previewMarkdown ? '<div class="article-preview-tags"><span>Rendered</span><span>图片弹药库</span><span>表格</span></div>' : ''}
        </div>
        <div class="article-md">${articleBody}</div>
      </div>
    `);
  queueVisualMasonry();
}

function openTemplateDrawer(templateId) {
  const job = getActiveJob();
  const template = job.templates.find(t => t.id === templateId) || job.templates[0];
  if (!template) return openJobDrawer(job);
  activeDetail = { type: 'template', id: template.id };
  setDrawer('模板详情', template.title, `
      <div class="detail-title-row">
        <div>
          <div class="detail-title">${template.title}</div>
          <div class="detail-sub">${template.path}</div>
        </div>
        <span class="badge ${template.selected ? 'badge-blue' : 'badge-gray'}">${template.selected ? '已选中' : '候选'}</span>
      </div>
      <div class="detail-card">
        <div class="detail-card-title">推荐理由</div>
        <div class="card-v">${template.reason}</div>
      </div>
      <div class="detail-card">
        <div class="detail-card-title">Demo 规则</div>
        <div class="detail-row">模板 Markdown 现阶段来自 wiki/创作框架/*.md。</div>
        <div class="detail-row">用户可以在 Obsidian 里修改模板，再回到系统重跑推荐。</div>
      </div>
      <div class="article-preview">
        <div class="article-preview-head">
          <div class="article-preview-title">模板 Markdown 预览</div>
          <div class="article-preview-meta">${template.path}</div>
        </div>
        <div class="article-md">
          <h4># ${template.title}</h4>
          <p>## 适用场景</p>
          <p>${template.reason}</p>
          <p>## 写作动作</p>
          <ul>
            <li>先拆读者误判，再给产品真实边界。</li>
            <li>观点必须落到计划书、提领比例、现金流压力测试。</li>
            <li>结尾回到客户自己的方案，而不是泛泛讲产品好坏。</li>
          </ul>
        </div>
      </div>
    `);
}

function openJobDrawer(job = getActiveJob()) {
  const selectedTemplate = job.templates.find(t => t.selected);
  const stats = getJobStats(job);
  activeDetail = { type: 'job', id: job.id };
  setDrawer('项目详情', job.title, `
    <div class="detail-title-row">
      <div>
        <div class="detail-title">${job.title}</div>
        <div class="detail-sub">${job.folder}</div>
      </div>
      <span class="badge ${job.status === 'waiting' ? 'badge-yellow' : job.status === 'running' ? 'badge-green' : 'badge-gray'}">${statusLabel(job.status)}</span>
    </div>
    <div class="detail-card">
      <div class="detail-card-title">Job 摘要</div>
      <div class="kv"><span>当前阶段</span><span>${stageLabel(job.stage)}</span></div>
      <div class="kv"><span>素材数量</span><span>${stats.total}</span></div>
      <div class="kv"><span>知识库 / URL</span><span>${stats.localTotal} / ${stats.urlTotal}</span></div>
      <div class="kv"><span>必用 / 参考 / 不用</span><span>${stats.must} / ${stats.reference} / ${stats.skip}</span></div>
      <div class="kv"><span>选中模板</span><span>${selectedTemplate ? selectedTemplate.title : '未选择'}</span></div>
      <div class="kv"><span>更新时间</span><span>${job.updated}</span></div>
    </div>
    <div class="detail-card">
      <div class="detail-card-title">选题</div>
      <div class="card-v">${job.topic}</div>
    </div>
    <div class="detail-card">
      <div class="detail-card-title">写作目的</div>
      <div class="card-v">${job.purpose}</div>
    </div>
    <div class="detail-card">
      <div class="detail-card-title">目录</div>
      <div class="kv"><span>工作目录</span><span>${job.folder}</span></div>
      <div class="kv"><span>知识库目录</span><span>${knowledgeBaseDir}</span></div>
    </div>
    <div class="detail-card">
      <div class="detail-card-title">工作目录将记录</div>
      <div class="chip-row">
        <span class="kw-tag blue">job.json</span>
        <span class="kw-tag blue">materials JSON</span>
        <span class="kw-tag blue">template JSON</span>
        <span class="kw-tag green">writing_plan.md</span>
        <span class="kw-tag green">writing_package.md</span>
      </div>
    </div>
  `);
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/"/g, '&quot;');
}

function setStage(stage) {
  activeStage = normalizeStage(stage);
  activeDetail = activeMode === 'batch'
    ? { type: 'batch', id: activeBatchId }
    : { type: 'job', id: activeJobId };
  closeDrawer();
  renderAll();
}

function setJob(id) {
  activeJobId = id;
  const job = getActiveJob();
  activeStage = normalizeStage(job.stage);
  activeDetail = { type: 'job', id };
  document.body.classList.remove('sidebar-open');
  closeDrawer();
  renderAll();
}

function advanceJob(job) {
  const idx = stages.findIndex(s => s.id === normalizeStage(job.stage));
  const next = stages[Math.min(idx + 1, stages.length - 1)];
  job.stage = next.id;
  job.status = next.id === 'done' ? 'done' : 'waiting';
  activeStage = next.id;
  job.updated = '刚刚';
}

document.addEventListener('click', event => {
  const groupHead = event.target.closest('[data-toggle-group]');
  if (groupHead) {
    const group = groupHead.dataset.toggleGroup;
    if (collapsedGroups.has(group)) collapsedGroups.delete(group);
    else collapsedGroups.add(group);
    renderJobs();
    return;
  }
  const batchRow = event.target.closest('[data-batch-id]');
  if (batchRow) {
    activeBatchId = batchRow.dataset.batchId;
    activeDetail = { type: 'batch', id: activeBatchId };
    activeStage = 'batch-config';
    document.body.classList.remove('sidebar-open');
    closeDrawer();
    renderAll();
    return;
  }
  const jobRow = event.target.closest('.job-row');
  if (jobRow) {
    setJob(jobRow.dataset.jobId);
    return;
  }
  const stageBtn = event.target.closest('[data-stage]');
  if (stageBtn && stageBtn.classList.contains('step-item')) {
    setStage(stageBtn.dataset.stage);
    return;
  }
  const templateCard = event.target.closest('.template-card');
  if (templateCard) {
    const job = getActiveJob();
    job.templates.forEach(t => t.selected = t.id === templateCard.dataset.templateId);
    activeDetail = { type: 'template', id: templateCard.dataset.templateId };
    showToast('已选择模板');
    renderAll();
    openTemplateDrawer(templateCard.dataset.templateId);
    return;
  }
  const detailRow = event.target.closest('[data-detail-type]');
  if (detailRow && !event.target.closest('.segmented')) {
    activeDetail = { type: detailRow.dataset.detailType, id: detailRow.dataset.detailId };
    renderAll();
    if (activeDetail.type === 'material') openMaterialDrawer(activeDetail.id);
    if (activeDetail.type === 'template') openTemplateDrawer(activeDetail.id);
    return;
  }
  const seg = event.target.closest('.seg');
  if (seg) {
    const id = seg.closest('.segmented').dataset.materialId;
    const material = findMaterial(getActiveJob(), id);
    if (!material) return;
    material.usage = seg.dataset.usage;
    activeDetail = { type: 'material', id };
    showToast(`已标记为：${usageLabel(material.usage)}`);
    renderAll();
    if (document.body.classList.contains('drawer-open')) openMaterialDrawer(id);
    return;
  }
  const keywordEditBtn = event.target.closest('[data-keyword-edit]');
  if (keywordEditBtn) {
    openBatchKeywordEditor(keywordEditBtn.dataset.keywordEdit);
    return;
  }
  const motherEditBtn = event.target.closest('[data-mother-edit]');
  if (motherEditBtn) {
    openBatchMotherEditor(motherEditBtn.dataset.motherEdit);
    return;
  }
  const stepperBtn = event.target.closest('.stepper-btn');
  if (stepperBtn) {
    const stepper = stepperBtn.closest('.stepper');
    const kwId = stepper.dataset.keywordId;
    const batch = getActiveBatch();
    const kw = batch.keywords.find(k => k.id === kwId);
    if (!kw) return;
    const direction = stepperBtn.dataset.step;
    kw.count = direction === 'inc' ? kw.count + 1 : Math.max(0, kw.count - 1);
    clearBatchQueue(batch.id);
    renderBatchConfig();
    return;
  }
  const reworkBtn = event.target.closest('[data-action="queue-rework-back"]');
  if (reworkBtn) {
    askQueueReworkBack(reworkBtn.dataset.keywordId);
    return;
  }
  const action = event.target.closest('[data-action]')?.dataset.action;
  if (action) handleAction(action);
});

function handleAction(action) {
  const job = getActiveJob();
  let drawerToOpen = null;
  if (action === 'advance') {
    advanceJob(job);
    showToast('已推进到下一阶段');
  }
  if (action === 'simulate-running') {
    job.status = 'running';
    showToast('已切换为运行中状态');
  }
  if (action === 'edit-purpose') {
    openPurposeEditor();
    return;
  }
  if (action === 'open-job-detail') {
    drawerToOpen = { type: 'job', id: job.id };
  }
  if (action === 'add-url') {
    openUrlModal();
    return;
  }
  if (action === 'rerun-materials') showToast('已模拟重跑知识库推荐，URL 临时素材保留不变');
  if (action === 'rerun-templates') showToast('静态 Demo：模板推荐已模拟重跑');
  if (action === 'rerun-decision') showToast('已模拟重跑写作决策：素材、模板和方案会一起刷新，URL 临时素材保留不变');
  if (action === 'go-plan') activeStage = 'decision';
  if (action === 'back-materials') activeStage = 'decision';
  if (action === 'rerun-plan') showToast('静态 Demo：写作方案已模拟重跑');
  if (action === 'confirm-plan' || action === 'confirm-decision') {
    activeStage = 'package';
    job.stage = 'package';
    job.status = 'waiting';
    showToast('写作决策已确认，进入写作包');
  }
  if (action === 'copy-package') showToast('静态 Demo：写作包已复制');
  if (action === 'simulate-finish') {
    job.status = 'done';
    job.stage = 'done';
    activeStage = 'done';
    showToast('已模拟完成写作');
  }
  if (action === 'back-to-mother') {
    askBatchBackToMother();
    return;
  }
  if (action === 'back-batch-config') {
    activeStage = 'batch-config';
  }
  if (action === 'confirm-batch-queue') {
    const batch = getActiveBatch();
    const items = generateQueueForBatch(batch);
    items.forEach(q => {
      if (batchQueueDemoTitles[q.id]) q.title = batchQueueDemoTitles[q.id];
    });
    setBatchQueue(batch.id, items);
    activeStage = 'batch-done';
    batch.updatedAt = '刚刚';
    showToast('已生成成稿队列');
  }
  if (action === 'simulate-queue-run') {
    simulateQueueRun();
    return;
  }
  if (action === 'queue-view') {
    showToast('静态 Demo：查看成稿详情');
    return;
  }
  if (action === 'batch-needs-mother') {
    askBatchBackToMother();
    return;
  }

  renderAll();
  if (drawerToOpen?.type === 'job') openJobDrawer(job);
  if (drawerToOpen?.type === 'material') openMaterialDrawer(drawerToOpen.id);
}

$('#jobSearch').addEventListener('input', renderJobs);
$('#collapseArchivedBtn').addEventListener('click', () => {
  showArchived = !showArchived;
  $('#collapseArchivedBtn').classList.toggle('active', showArchived);
  renderJobs();
});

$('#newJobBtn').addEventListener('click', () => $('#newJobModal').classList.add('show'));
$('#closeNewJob').addEventListener('click', closeNewJobModal);
$('#cancelNewJob').addEventListener('click', closeNewJobModal);
$('#newJobModal').addEventListener('click', event => {
  if (event.target.id === 'newJobModal') closeNewJobModal();
});
$('#confirmNewJob').addEventListener('click', () => {
  const topic = $('#topicInput').value.trim();
  const purpose = $('#purposeInput').value.trim();
  if (!topic || !purpose) {
    showToast('请填写选题和写作目的');
    return;
  }
  const id = `260706_${topic.slice(0, 14).replace(/\s+/g, '')}`;
  jobs.unshift({
    id,
    title: topic.slice(0, 16),
    topic,
    folder: `WritingSpace/${id}/`,
    purpose,
    status: 'running',
    stage: 'decision',
    updated: '刚刚',
    category: '未分类',
    archived: false,
    materials: [],
    templates: [],
    plan: {
      titleDirection: '等待素材推荐后生成',
      core: '等待素材推荐后生成',
      outline: ['等待 Agent 读取素材', '等待模板推荐', '等待方案确认'],
      close: '等待写作方案生成。'
    }
  });
  activeJobId = id;
  activeStage = 'decision';
  activeDetail = { type: 'job', id };
  closeNewJobModal();
  showToast('已创建静态文章项目');
  renderAll();
});

function closeNewJobModal() {
  $('#newJobModal').classList.remove('show');
}

function openUrlModal() {
  const job = getActiveJob();
  const nextIndex = String(getUrlMaterials(job).length + 1).padStart(3, '0');
  $('#urlModal').dataset.jobId = job.id;
  $('#urlInput').value = `https://mp.weixin.qq.com/s/WritingMoneyDemo${nextIndex}`;
  $('#urlNoteInput').value = '';
  $('#urlModal').classList.add('show');
  $('#urlModal').setAttribute('aria-hidden', 'false');
  setTimeout(() => $('#urlInput').focus(), 0);
}

function closeUrlModal() {
  $('#urlModal').classList.remove('show');
  $('#urlModal').setAttribute('aria-hidden', 'true');
}

function createUrlMaterial() {
  const rawUrl = $('#urlInput').value.trim();
  const note = $('#urlNoteInput').value.trim();
  if (!rawUrl) {
    showToast('请先填写 URL');
    $('#urlInput').focus();
    return;
  }
  try {
    const parsed = new URL(rawUrl);
    if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error('unsupported protocol');
  } catch (error) {
    showToast('URL 格式不对，请检查链接');
    $('#urlInput').focus();
    return;
  }

  const jobId = $('#urlModal').dataset.jobId;
  const job = jobs.find(item => item.id === jobId) || getActiveJob();
  const urlMaterials = getUrlMaterials(job);
  const nextIndex = String(urlMaterials.length + 1).padStart(3, '0');
  const id = `url_${Date.now().toString(36)}`;
  const material = {
    id,
    type: 'url',
    title: '解析中：公众号 URL 临时素材',
    url: rawUrl,
    path: `${job.folder}materials/url_${nextIndex}.md`,
    reason: '正在解析正文、图片 OCR 和表格摘要。解析完成前也可以先标记使用状态。',
    points: ['正文提取中', '图片 OCR 排队中', '表格结构识别中'],
    visuals: ['图片 OCR 解析中'],
    usage: 'reference',
    parseStatus: 'parsing',
    addedAt: '刚刚'
  };

  urlMaterials.push(material);
  job.updated = '刚刚';
  activeDetail = { type: 'material', id };
  closeUrlModal();
  renderAll();
  showToast('URL 已进入临时素材，正在解析');
  setTimeout(() => finishUrlParsing(job.id, id, note), 1400);
}

function finishUrlParsing(jobId, materialId, note) {
  const job = jobs.find(item => item.id === jobId);
  if (!job) return;
  const material = findMaterial(job, materialId);
  if (!material || material.parseStatus !== 'parsing') return;
  const noteTitle = note ? note.slice(0, 28) : '公众号文章解析结果';
  material.title = `临时 URL 素材：${noteTitle}`;
  material.reason = '已模拟解析正文、图片 OCR 和表格摘要；它只服务当前文章项目，重跑知识库推荐不会覆盖它。';
  material.points = ['提取到一段可用的客户问题语境', '图片 OCR 里识别出计划书关键数字', '表格摘要可用于核对提领口径'];
  material.visuals = ['正文配图 OCR 摘要', '计划书数字表格', '原文观点截图索引'];
  material.parseStatus = 'ready';
  material.addedAt = '刚刚';
  job.updated = '刚刚';
  renderAll();
  showToast('URL 临时素材解析完成');
}

$('#purposeEditor').addEventListener('input', updatePurposeEditorMeta);
$('#savePurposeEdit').addEventListener('click', () => askPurposeClose('save'));
$('#cancelPurposeEdit').addEventListener('click', () => askPurposeClose('cancel'));
$('#closePurposeEditor').addEventListener('click', () => askPurposeClose('cancel'));
$('#purposeConfirmPrimary').addEventListener('click', () => purposeConfirmHandlers.onPrimary?.());
$('#purposeConfirmSecondary').addEventListener('click', () => purposeConfirmHandlers.onSecondary?.());
$('#purposeConfirmTertiary').addEventListener('click', () => purposeConfirmHandlers.onTertiary?.());

$('#confirmUrlModal').addEventListener('click', createUrlMaterial);
$('#closeUrlModal').addEventListener('click', closeUrlModal);
$('#cancelUrlModal').addEventListener('click', closeUrlModal);

$('#mobileMenuBtn').addEventListener('click', () => document.body.classList.add('sidebar-open'));
document.addEventListener('click', event => {
  const target = event.target instanceof Element ? event.target : null;
  if (target?.closest('#mobileMenuBtn')) {
    document.body.classList.add('sidebar-open');
  }
});
$('#mobileOverlay').addEventListener('click', () => document.body.classList.remove('sidebar-open'));
$('#drawerClose').addEventListener('click', closeDrawer);
$('#drawerMask').addEventListener('click', closeDrawer);
document.querySelectorAll('.mode-btn').forEach(btn => {
  btn.addEventListener('click', () => setMode(btn.dataset.mode));
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') {
    if ($('#purposeConfirmModal').classList.contains('show')) {
      closePurposeConfirm();
      return;
    }
    if ($('#backToMotherModal').classList.contains('show')) {
      cancelBackToMother();
      return;
    }
    if ($('#newBatchModal').classList.contains('show')) {
      closeNewBatchModal();
      return;
    }
    if ($('#batchKeywordModal').classList.contains('show')) {
      closeBatchKeywordModal();
      return;
    }
    if ($('#batchMotherModal').classList.contains('show')) {
      closeBatchMotherModal();
      return;
    }
    if ($('#purposeModal').classList.contains('show')) {
      askPurposeClose('cancel');
      return;
    }
    if ($('#urlModal').classList.contains('show')) {
      closeUrlModal();
      return;
    }
    closeDrawer();
    closeNewJobModal();
    document.body.classList.remove('sidebar-open');
  }
});

window.addEventListener('resize', queueVisualMasonry);


function setMode(mode) {
  activeMode = mode;
  if (mode === 'batch') {
    activeStage = 'batch-config';
    activeDetail = { type: 'batch', id: activeBatchId };
  } else {
    activeStage = 'decision';
    activeDetail = { type: 'job', id: activeJobId };
  }
  closeDrawer();
  renderAll();
}
function renderModeSwitch() {
  document.querySelectorAll('.mode-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.mode === activeMode);
  });
}

function renderNavMeta() {
  var label1 = $('#navMetaLabel1');
  var label2 = $('#navMetaLabel2');
  var countEl = $('#jobCountTop');
  var waitingEl = $('#waitingCountTop');
  if (activeMode === 'batch') {
    if (label1) label1.textContent = '批次';
    if (label2) label2.textContent = '待确认';
    var batchTotal = batchList.length;
    var batchPending = batchList.filter(function(b) { return b.status === 'pending'; }).length;
    if (countEl) countEl.textContent = batchTotal;
    if (waitingEl) waitingEl.textContent = batchPending;
  } else {
    if (label1) label1.textContent = '项目';
    if (label2) label2.textContent = '待确认';
    var total = jobs.filter(function(j) { return !j.archived; }).length;
    var waiting = jobs.filter(function(j) { return j.status === 'waiting'; }).length;
    if (countEl) countEl.textContent = total;
    if (waitingEl) waitingEl.textContent = waiting;
  }
}

function updateSidebar() {
  var isBatch = activeMode === 'batch';
  var title = $('#sidebarTitle');
  var desc = $('#sidebarDesc');
  var jobSearch = $('#jobSearch');
  var batchSearch = $('#batchSearch');
  var newJobBtn = $('#newJobBtn');
  var newBatchBtn = $('#newBatchBtn');
  var archiveBtn = $('#collapseArchivedBtn');
  if (title) title.textContent = isBatch ? '成稿批次' : '文章项目';
  if (desc) desc.textContent = isBatch ? '批次管理：按关键词×母文章批量成稿。' : '多篇文章可以并行推进，绿点代表等你确认下一步。';
  if (jobSearch) jobSearch.classList.toggle('hidden', isBatch);
  if (batchSearch) batchSearch.classList.toggle('hidden', !isBatch);
  if (newJobBtn) newJobBtn.classList.toggle('hidden', isBatch);
  if (newBatchBtn) newBatchBtn.classList.toggle('hidden', !isBatch);
  if (archiveBtn) archiveBtn.classList.toggle('hidden', isBatch);
}

renderAll();

/* ====== Batch Mode Rendering ====== */

function renderBatchView() {
  const batch = getActiveBatch();
  if (!batch) return;
  const batchStage = activeStage === 'batch-done' ? 'batch-done' : 'batch-config';

  $('#jobFolder').textContent = batch.outputDir;
  $('#jobTitle').textContent = batch.name;
  $('#jobPurpose').textContent = batch.brief;

  $('#stepTrack').innerHTML = batchStages.map((s, i) => {
    const isActive = s.id === batchStage;
    const isDone = ['batch-config', 'batch-done'].indexOf(batchStage) > i;
    return '<div class="step-item ' + (isActive ? 'active' : '') + (isDone ? ' done' : '') + '" data-stage="' + s.id + '">' +
      '<span class="step-num">' + (i + 1) + '</span>' +
      '<span class="step-name">' + s.name + '</span></div>';
  }).join('');

  if (batchStage === 'batch-config') {
    renderBatchConfig();
  } else {
    renderBatchQueue();
  }
}

function renderBatchConfig() {
  const batch = getActiveBatch();
  const ws = $('#workspaceCard');
  const totalKeywords = batch.keywords.length;
  const totalPlanned = batch.keywords.reduce(function(s, k) { return s + k.count; }, 0);
  const readyCount = batch.keywords.filter(function(k) { return k.readiness === 'ready'; }).length;
  const needsMotherCount = batch.keywords.filter(function(k) { return k.readiness === 'needs-mother'; }).length;
  const readyPlanned = batch.keywords.filter(function(k) { return k.readiness === 'ready' && k.count > 0; }).reduce(function(s, k) { return s + k.count; }, 0);

  ws.innerHTML =
    '<div class="workspace-head">' +
      '<div><div class="workspace-title">批次配置</div>' +
        '<div class="workspace-desc batch-workspace-desc">' + escapeHtml(batch.name) + '</div></div>' +
      '<div class="workspace-actions">' +
        (needsMotherCount > 0 ? '<button class="mini-btn" data-action="batch-needs-mother">缺母文章去铸造</button>' : '') +
        '<button class="mini-btn primary" data-action="confirm-batch-queue">确认批次，生成队列</button></div></div>' +

    '<div class="batch-section">' +
      '<div class="batch-section-head"><span class="batch-section-title">批次概览</span></div>' +
      '<div class="batch-strategy-grid">' +
        '<div class="batch-strategy-item"><span>关键词数</span><b>' + totalKeywords + '</b></div>' +
        '<div class="batch-strategy-item"><span>计划成稿数</span><b>' + totalPlanned + '</b></div>' +
        '<div class="batch-strategy-item"><span>可直接生成关键词</span><b>' + readyCount + '</b></div>' +
        '<div class="batch-strategy-item"><span>缺母文章</span><b>' + needsMotherCount + '</b></div>' +
        '<div class="batch-strategy-item"><span>本次可生成篇数</span><b>' + readyPlanned + '</b></div>' +
        '<div class="batch-strategy-item wide"><span>本批次写作要求</span>' +
          '<b class="batch-brief-text">' + escapeHtml(batch.brief) + '</b></div></div></div>' +

    '<div class="batch-section">' +
      '<div class="batch-section-head"><span class="batch-section-title">关键词队列</span>' +
        '<span class="batch-section-sub">' + totalKeywords + ' 个关键词 \u00b7 ' + totalPlanned + ' 篇</span></div>' +
      '<div class="batch-kw-head">' +
        '<div>关键词 / 匹配母文章 / 写作目的</div><div>热点信号</div><div>生成篇数</div></div>';

  batch.keywords.forEach(function(kw) {
    const signal = batchSignalConfig[kw.signal] || batchSignalConfig.medium;
    const motherNames = kw.motherMatches.map(function(m) {
      const mother = getBatchMotherById(m.motherId);
      return mother ? mother.title : '';
    }).filter(Boolean);
    const hook = getHookById(kw.hookId);
    const kwRow = document.createElement('div');
    kwRow.className = 'batch-kw-row';
    kwRow.innerHTML =
      '<div class="batch-kw-cell">' +
        '<div class="kw-main-line">' +
          '<span class="kw-keyword">' + escapeHtml(kw.keyword) + '</span>' +
          '<button class="kw-edit-btn ' + (kw.purpose ? 'active' : '') + '" data-keyword-edit="' + kw.id + '" title="编辑写作目的">\u270e</button></div>' +
        (kw.purpose ? '<div class="kw-purpose-preview">' + escapeHtml(kw.purpose) + '</div>' : '') +
        '<div class="kw-purpose-preview kw-meta-line">' +
          '<span class="kw-slot">母文章：</span>' +
          (motherNames.length > 0 ? motherNames.map(function(n) { return '<span class="kw-tag blue">' + escapeHtml(n) + '</span>'; }).join(' ') : '<span class="kw-tag red">缺母文章</span>') +
          '<button class="text-btn kw-mother-edit-btn" data-mother-edit="' + kw.id + '" title="调整母文章匹配">调整</button>' +
          '<span class="kw-slot kw-hook-sep">钩子：</span>' +
          '<span class="kw-hook">' + (hook ? hook.title : '未设置') + '</span>' +
          '<span class="kw-slot kw-hook-sep">状态：</span>' +
          '<span class="kw-tag ' + (kw.readiness === 'ready' ? 'green' : 'red') + '">' + readyLabel[kw.readiness] + '</span></div></div>' +
      '<div class="batch-kw-cell">' +
        '<span class="status-chip ' + signal.class + '">' + signal.label + '</span>' +
        '<div class="kw-purpose-preview kw-signal-reason">' + escapeHtml(signal.reason) + '</div></div>' +
      '<div class="batch-kw-cell kw-stepper">' +
        '<div class="stepper" data-keyword-id="' + kw.id + '">' +
          '<button class="stepper-btn" data-step="dec">\u2212</button>' +
          '<div class="stepper-value">' + kw.count + '</div>' +
          '<button class="stepper-btn" data-step="inc">+</button></div></div>';
    ws.appendChild(kwRow);
  });

  const outputSection = document.createElement('div');
  outputSection.className = 'batch-section';
  outputSection.innerHTML =
    '<div class="batch-section-head"><span class="batch-section-title">输出目录</span></div>' +
    '<div class="batch-output-row"><span class="mat-path">' + escapeHtml(batch.outputDir) + '</span></div>';
  ws.appendChild(outputSection);
}

function renderBatchQueue() {
  const batch = getActiveBatch();
  const ws = $('#workspaceCard');
  const queue = getBatchQueue(batch.id);
  var stats = { done: 0, rework: 0, running: 0, waiting: 0, planned: queue.length };
  queue.forEach(function(q) { stats[q.status]++; });

  ws.innerHTML =
    '<div class="workspace-head">' +
      '<div><div class="workspace-title">成稿队列</div>' +
        '<div class="workspace-desc batch-workspace-desc">' + escapeHtml(batch.name) + '</div></div>' +
      '<div class="workspace-actions">' +
        '<button class="mini-btn" data-action="back-batch-config">返回配置</button>' +
        '<button class="mini-btn primary" data-action="simulate-queue-run">模拟运行</button></div></div>' +

    '<div class="batch-section">' +
      '<div class="batch-overview">' +
        '<div class="batch-overview-item planned"><span>计划生成</span><b>' + stats.planned + '</b></div>' +
        '<div class="batch-overview-item waiting"><span>等待</span><b>' + stats.waiting + '</b></div>' +
        '<div class="batch-overview-item running"><span>运行中</span><b>' + stats.running + '</b></div>' +
        '<div class="batch-overview-item done"><span>已完成</span><b>' + stats.done + '</b></div>' +
        '<div class="batch-overview-item rework"><span>需返工</span><b>' + stats.rework + '</b></div></div></div>' +

    '<div class="batch-section">' +
      '<div class="batch-section-head"><span class="batch-section-title">发布交接</span></div>' +
      '<div class="batch-output-row batch-handoff-row">' +
        '<span class="mat-path">' + escapeHtml(batch.outputDir) + '</span>' +
        '<span class="batch-handoff-note">目录交接 \u00b7 接口未连接</span></div></div>' +

    '<div class="batch-section">' +
      '<div class="batch-queue-head">' +
        '<div>预生成标题</div><div>状态</div><div>输出文件</div><div>操作</div></div>';

  if (queue.length === 0) {
    ws.innerHTML += '<div class="empty-block">当前批次无成稿任务，请返回配置页调整关键词生成篇数。</div>';
  } else {
    queue.forEach(function(q) {
      const kw = batch.keywords.find(function(k) { return k.id === q.keywordId; });
      const motherNames = kw ? kw.motherMatches.map(function(m) { var mo = getBatchMotherById(m.motherId); return mo ? mo.title : ''; }).filter(Boolean) : [];
      const hook = kw ? getHookById(kw.hookId) : null;
      var row = document.createElement('div');
      row.className = 'batch-queue-row';
      var motherHtml = motherNames.length > 0 ? motherNames.map(function(n) { return '<span class="kw-tag blue">' + escapeHtml(n) + '</span>'; }).join(' ') : '';
      var hookHtml = hook ? '<span class="kw-slot kw-hook-sep">钩子：</span><span class="kw-hook">' + escapeHtml(hook.title) + '</span>' : '';
      row.innerHTML =
        '<div><div class="batch-queue-title">' + escapeHtml(q.title) + '</div>' +
          '<div class="kw-purpose-preview kw-queue-meta">' +
            '<span class="kw-slot">关键词：</span><span class="kw-hook">' + escapeHtml(kw ? kw.keyword : '') + '</span>' +
            motherHtml + hookHtml + '</div></div>' +
        '<div class="batch-queue-status"><span class="status-chip ' + queueStatusClass[q.status] + '">' + queueStatusLabel[q.status] + '</span></div>' +
        '<div class="batch-queue-action batch-queue-file"><span class="mat-path batch-queue-filepath">' + escapeHtml(q.outputFile) + '</span></div>' +
        '<div class="batch-queue-action">' +
          (q.status === 'rework' ? '<button class="mini-btn primary" data-action="queue-rework-back" data-keyword-id="' + q.keywordId + '">返回母文章</button>' : '<button class="mini-btn" data-action="queue-view">查看</button>') +
        '</div>';
      ws.appendChild(row);
    });
  }
  ws.innerHTML += '</div>';
}

function renderBatchList() {
  const query = ($('#batchSearch') ? $('#batchSearch').value : '').trim().toLowerCase();
  const filtered = batchList.filter(function(b) {
    if (!query) return true;
    return b.name.toLowerCase().indexOf(query) >= 0 ||
      b.source.toLowerCase().indexOf(query) >= 0 ||
      b.status.toLowerCase().indexOf(query) >= 0 ||
      b.keywords.some(function(k) { return k.keyword.toLowerCase().indexOf(query) >= 0; });
  });

  var groups = [
    { label: '今天', filter: function(b) { return b.createdAt.indexOf('今天') >= 0 && b.status !== 'done'; } },
    { label: '本周', filter: function(b) { return b.createdAt.indexOf('今天') < 0 && b.status !== 'done'; } },
    { label: '已完成', filter: function(b) { return b.status === 'done'; } }
  ];

  var listHtml = '';
  groups.forEach(function(g) {
    var items = filtered.filter(g.filter);
    if (items.length === 0) return;
    listHtml +=
      '<div class="job-group">' +
        '<div class="job-group-head"><div class="job-group-name">' +
          '<span class="job-group-arrow">▾</span><span>' + g.label + '</span></div>' +
          '<span class="job-group-count">' + items.length + '</span></div>' +
        '<div class="job-group-body">' +
          items.map(function(b) {
            var isActive = b.id === activeBatchId;
            return '<div class="job-row ' + (isActive ? 'active' : '') + '" data-batch-id="' + b.id + '">' +
              '<div class="job-title-row"><div class="job-title">' + escapeHtml(b.name) + '</div></div>' +
              '<span class="job-status-text ' + b.status + '">' + batchStatusLabel[b.status] + '</span></div>';
          }).join('') +
        '</div></div>';
  });

  $('#jobList').innerHTML = listHtml || '<div class="empty-block">无匹配批次</div>';
}

/* ====== New Batch Modal ====== */

function openNewBatchModal() {
  $('#newBatchModal').classList.add('show');
  $('#newBatchModal').setAttribute('aria-hidden', 'false');
  setTimeout(function() { $('#batchNameInput').focus(); }, 0);
}

function closeNewBatchModal() {
  $('#newBatchModal').classList.remove('show');
  $('#newBatchModal').setAttribute('aria-hidden', 'true');
}

function createNewBatch() {
  var name = $('#batchNameInput').value.trim();
  var brief = $('#batchBriefInput').value.trim();
  var keywordsRaw = $('#batchKeywordsInput').value.trim();
  var outputDir = $('#batchOutputDirInput').value.trim();

  if (!name) { showToast('请填写批次名称'); $('#batchNameInput').focus(); return; }
  if (!keywordsRaw) { showToast('请至少填写一个关键词'); $('#batchKeywordsInput').focus(); return; }

  var keywords = keywordsRaw.split('\n').filter(function(k) { return k.trim(); }).map(function(k) { return k.trim(); });
  var dir = outputDir || 'WritingSpace/BatchQueue/' + Date.now().toString(36) + '/';
  var now = new Date();
  var timeStr = now.getHours().toString().padStart(2, '0') + ':' + now.getMinutes().toString().padStart(2, '0');
  var id = 'batch-' + Date.now().toString(36);
  var isHotBatch = /热点|火爆|多写|加量|重点覆盖/.test(brief);

  var keywordTasks = keywords.map(function(k, i) {
    var matched = matchMothersForKeyword(k);
    var signal = isHotBatch ? 'hot' : 'medium';
    var hookIds = ['hook-pressure', 'hook-plan', 'hook-resource', 'hook-cross', 'hook-discount'];
    return {
      id: 'kw-' + id + '-' + i,
      keyword: k,
      purpose: '',
      signal: signal,
      signalReason: signal === 'hot' ? '热点关键词，建议多篇覆盖' : '稳定搜索量',
      count: signal === 'hot' ? 2 : 1,
      recommendedCount: signal === 'hot' ? 2 : 1,
      hookId: hookIds[i % 5],
      motherMatches: matched.map(function(m, mi) {
        return { motherId: m.id, role: '智能匹配', confidence: 0.82 - mi * 0.03 };
      }),
      readiness: matched.length > 0 ? 'ready' : 'needs-mother'
    };
  });

  var batch = {
    id: id, name: name, source: '手动创建',
    brief: brief || '批量成稿：' + keywords.slice(0, 3).join('、') + (keywords.length > 3 ? '等' : ''),
    status: 'pending',
    createdAt: '今天 ' + timeStr, updatedAt: '今天 ' + timeStr,
    outputDir: dir, publishHandoff: false,
    keywords: keywordTasks
  };

  batchList.unshift(batch);
  activeBatchId = id;
  activeStage = 'batch-config';
  clearBatchQueue(id);
  $('#batchSearch').value = '';
  closeNewBatchModal();
  $('#batchNameInput').value = '';
  $('#batchBriefInput').value = '';
  $('#batchKeywordsInput').value = '';
  $('#batchOutputDirInput').value = '';
  showToast('批次已创建，智能匹配完成');
  renderAll();
}

/* ====== Batch Keyword Editor ====== */

var batchKeywordEditorId = null;
var batchKeywordSavedKeyword = '';
var batchKeywordSavedPurpose = '';

function openBatchKeywordEditor(kwId) {
  var batch = getActiveBatch();
  var kw = batch.keywords.find(function(k) { return k.id === kwId; });
  if (!kw) return;
  batchKeywordEditorId = kwId;
  batchKeywordSavedKeyword = kw.keyword;
  batchKeywordSavedPurpose = kw.purpose;
  $('#batchKeywordModal').dataset.batchId = batch.id;
  $('#batchKeywordInput').value = kw.keyword;
  $('#batchKeywordPurposeInput').value = kw.purpose;
  $('#batchKeywordDirtyState').textContent = '未修改';
  $('#batchKeywordDirtyState').parentElement.classList.remove('dirty');
  $('#batchKeywordCharCount').textContent = kw.purpose.length;
  $('#batchKeywordModal').classList.add('show');
  $('#batchKeywordModal').setAttribute('aria-hidden', 'false');
}

function closeBatchKeywordModal() {
  $('#batchKeywordModal').classList.remove('show');
  $('#batchKeywordModal').setAttribute('aria-hidden', 'true');
}

function saveBatchKeyword() {
  var batchId = $('#batchKeywordModal').dataset.batchId;
  var batch = batchList.find(function(b) { return b.id === batchId; }) || getActiveBatch();
  var kw = batch.keywords.find(function(k) { return k.id === batchKeywordEditorId; });
  if (!kw) return;
  var newKeyword = $('#batchKeywordInput').value.trim();
  var newPurpose = $('#batchKeywordPurposeInput').value.trim();
  if (!newKeyword) {
    showToast('关键词不能为空');
    $('#batchKeywordInput').focus();
    return;
  }
  var keywordChanged = newKeyword !== kw.keyword;
  kw.keyword = newKeyword;
  kw.purpose = newPurpose;
  if (keywordChanged) {
    kw.motherMatches = [];
    kw.readiness = 'needs-mother';
  }
  closeBatchKeywordModal();
  clearBatchQueue(batch.id);
  if (keywordChanged) {
    showToast('关键词已修改，母文章匹配已清空，请重新调整母文章');
  } else {
    showToast('写作目的已保存，旧队列已清除');
  }
  renderAll();
}

/* ====== Batch Mother Article Editor ====== */

var batchMotherEditorKwId = null;

function openBatchMotherEditor(kwId) {
  var batch = getActiveBatch();
  var kw = batch.keywords.find(function(k) { return k.id === kwId; });
  if (!kw) return;
  batchMotherEditorKwId = kwId;
  $('#batchMotherTitle').textContent = '调整母文章：' + kw.keyword;
  var existingIds = kw.motherMatches.map(function(m) { return m.motherId; });
  var listHtml = batchMotherLibrary.map(function(m) {
    var checked = existingIds.indexOf(m.id) >= 0 ? ' checked' : '';
    var existingMatch = kw.motherMatches.find(function(match) { return match.motherId === m.id; });
    var roleText = existingMatch ? '当前角色：' + escapeHtml(existingMatch.role) : '未匹配';
    return '<label class="mother-match-item">' +
      '<input type="checkbox" data-mother-id="' + m.id + '"' + checked + '>' +
      '<div class="mother-match-item-body">' +
        '<div class="mother-match-item-title">' + escapeHtml(m.title) + '</div>' +
        '<div class="mother-match-item-path">' + escapeHtml(m.path) + '</div>' +
        '<div class="mother-match-item-role">' + roleText + '</div>' +
      '</div>' +
    '</label>';
  }).join('');
  $('#batchMotherList').innerHTML = listHtml;
  $('#batchMotherModal').classList.add('show');
  $('#batchMotherModal').setAttribute('aria-hidden', 'false');
}

function closeBatchMotherModal() {
  $('#batchMotherModal').classList.remove('show');
  $('#batchMotherModal').setAttribute('aria-hidden', 'true');
}

function saveBatchMother() {
  var batch = getActiveBatch();
  var kw = batch.keywords.find(function(k) { return k.id === batchMotherEditorKwId; });
  if (!kw) return;
  var checkedIds = Array.from($('#batchMotherList').querySelectorAll('input[type="checkbox"]:checked')).map(function(cb) { return cb.dataset.motherId; });
  var newMatches = checkedIds.map(function(mid) {
    var existing = kw.motherMatches.find(function(m) { return m.motherId === mid; });
    if (existing) return existing;
    return { motherId: mid, role: '手动选择', confidence: 0.8 };
  });
  kw.motherMatches = newMatches;
  kw.readiness = newMatches.length > 0 ? 'ready' : 'needs-mother';
  closeBatchMotherModal();
  clearBatchQueue(batch.id);
  if (activeStage === 'batch-done') activeStage = 'batch-config';
  showToast('母文章匹配已更新，旧队列已清除');
  renderAll();
}

/* ====== Batch Queue Simulation ====== */

function simulateQueueRun() {
  if (batchQueueRunning) return;
  var batch = getActiveBatch();
  var queue = generateQueueForBatch(batch);
  if (queue.length === 0) {
    showToast('当前没有可生成任务，请先返回配置');
    return;
  }
  batchQueueRunning = true;
  batch.status = 'generating';
  batch.updatedAt = '刚刚';

  queue.forEach(function(q) {
    if (batchQueueDemoTitles[q.id]) q.title = batchQueueDemoTitles[q.id];
  });
  setBatchQueue(batch.id, queue);

  renderAll();
  showToast('开始模拟批量成稿\u2026');

  var idx = 0;
  batchQueueTimer = setInterval(function() {
    if (idx < queue.length) {
      queue[idx].status = 'running';
      renderAll();
      setTimeout(function() {
        if (queue[idx]) {
          queue[idx].status = idx === 0 ? 'rework' : 'done';
          renderAll();
        }
        idx++;
      }, 800);
    } else {
      clearInterval(batchQueueTimer);
      batchQueueTimer = null;
      batchQueueRunning = false;
      batch.status = 'done';
      batch.updatedAt = '刚刚';
      renderAll();
      showToast('批量成稿模拟完成');
    }
  }, 1500);
}

/* ====== Back to Mother from Batch ====== */

function askBatchBackToMother() {
  var batch = getActiveBatch();
  var needsMotherKws = batch.keywords.filter(function(k) { return k.readiness === 'needs-mother'; });
  if (needsMotherKws.length === 0) { showToast('当前批次没有缺母文章的关键词'); return; }
  var msg = '以下 ' + needsMotherKws.length + ' 个关键词缺少匹配的母文章：' + needsMotherKws.map(function(k) { return k.keyword; }).join('、') + '。确认切换到母文章模式去铸造吗？';
  $('#backToMotherTitle').textContent = '缺母文章去铸造';
  $('#backToMotherModalDesc').textContent = msg;
  $('#backToMotherModal').classList.add('show');
  $('#backToMotherModal').setAttribute('aria-hidden', 'false');
}

function askQueueReworkBack(keywordId) {
  var batch = getActiveBatch();
  var kw = batch.keywords.find(function(k) { return k.id === keywordId; });
  if (!kw) return;
  var motherNames = kw.motherMatches.map(function(m) {
    var mo = getBatchMotherById(m.motherId);
    return mo ? mo.title : '';
  }).filter(Boolean);
  var motherText = motherNames.length > 0 ? motherNames.join('、') : '暂无匹配母文章';
  var msg = '这篇成稿需要回到母文章铸造检查/迭代其母文章。关键词：「' + kw.keyword + '」，已匹配母文章：' + motherText + '。确认切换到母文章模式吗？';
  $('#backToMotherTitle').textContent = '返回母文章铸造';
  $('#backToMotherModalDesc').textContent = msg;
  $('#backToMotherModal').classList.add('show');
  $('#backToMotherModal').setAttribute('aria-hidden', 'false');
}

function confirmBackToMother() {
  $('#backToMotherModal').classList.remove('show');
  $('#backToMotherModal').setAttribute('aria-hidden', 'true');
  setMode('mother');
  showToast('已切换到母文章模式，请先铸造母文章');
}

function cancelBackToMother() {
  $('#backToMotherModal').classList.remove('show');
  $('#backToMotherModal').setAttribute('aria-hidden', 'true');
}

/* ====== Batch Event Listeners ====== */

$('#newBatchBtn').addEventListener('click', openNewBatchModal);
$('#closeNewBatchModal').addEventListener('click', closeNewBatchModal);
$('#cancelNewBatch').addEventListener('click', closeNewBatchModal);
$('#newBatchModal').addEventListener('click', function(event) {
  if (event.target.id === 'newBatchModal') closeNewBatchModal();
});
$('#confirmNewBatch').addEventListener('click', createNewBatch);

$('#batchKeywordInput').addEventListener('input', function() {
  var kw = $('#batchKeywordInput').value.trim();
  var purpose = $('#batchKeywordPurposeInput').value.trim();
  var dirty = kw !== batchKeywordSavedKeyword || purpose !== batchKeywordSavedPurpose;
  $('#batchKeywordDirtyState').textContent = dirty ? '已修改' : '未修改';
  $('#batchKeywordDirtyState').parentElement.classList.toggle('dirty', dirty);
  $('#batchKeywordCharCount').textContent = purpose.length;
});
$('#batchKeywordPurposeInput').addEventListener('input', function() {
  var kw = $('#batchKeywordInput').value.trim();
  var purpose = $('#batchKeywordPurposeInput').value.trim();
  var dirty = kw !== batchKeywordSavedKeyword || purpose !== batchKeywordSavedPurpose;
  $('#batchKeywordDirtyState').textContent = dirty ? '已修改' : '未修改';
  $('#batchKeywordDirtyState').parentElement.classList.toggle('dirty', dirty);
  $('#batchKeywordCharCount').textContent = purpose.length;
});
$('#saveBatchKeyword').addEventListener('click', saveBatchKeyword);
$('#cancelBatchKeyword').addEventListener('click', closeBatchKeywordModal);
$('#closeBatchKeywordEditor').addEventListener('click', closeBatchKeywordModal);
$('#batchKeywordModal').addEventListener('click', function(event) {
  if (event.target.id === 'batchKeywordModal') closeBatchKeywordModal();
});

$('#confirmBackToMother').addEventListener('click', confirmBackToMother);
$('#cancelBackToMother').addEventListener('click', cancelBackToMother);

$('#closeBatchMotherModal').addEventListener('click', closeBatchMotherModal);
$('#cancelBatchMother').addEventListener('click', closeBatchMotherModal);
$('#saveBatchMother').addEventListener('click', saveBatchMother);
$('#batchMotherModal').addEventListener('click', function(event) {
  if (event.target.id === 'batchMotherModal') closeBatchMotherModal();
});

$('#batchSearch').addEventListener('input', renderBatchList);
