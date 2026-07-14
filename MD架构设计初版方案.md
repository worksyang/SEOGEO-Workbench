### USER
咱们先不聊复杂的，咱们就只说一个HTML里面有小红书、有抖音、有视频号、有GEO、有这个微信公众号。我都是基于搜索。
  
你就说我们这些字段，文章的Markdown文件，排序的一些情况，时间的一些情况，怎么存呢？就每个平台假设HTML页面都是不一样的，但是它们可以互相切换。然后它们中间是不是有一些link的页面之类的？我有点懵

我们目前的文章都是遵循这样的储存方式。/Users/works14/.claude/监控/wechat-ybxhyyh-top3/微信搜索结果/260610_高端提领王｜友邦「财富盈活」深度解析（提领完结篇）.md

就是日期名称加上名字，因为我们这个未来系统Agent也可以自己去访问，Agent就可以直接去搜索名称，就很方便。


### ASSISTANT
你的直觉非常敏锐，坚持用“扁平 Markdown + 语义化文件名”是对 Agent 最友好的设计，千万别丢掉。

你现在的困惑在于：**“单平台时按名字存文件挺好用，但现在有 5 个平台，页面又不一样，到底怎么存才能不乱，还能互相跳转？”**

解决理念很简单：**物理文件保持扁平直存，但给每个文件套一个“身份挂牌（Frontmatter）”，平台隔离和互相跳转全靠挂牌，不靠文件夹。**

下面分三步把这套极简存储理念说清楚：

### 1. 物理存储：扁平直存，只加平台前缀防重名

所有平台的文章/笔记/视频，抓取成 Markdown 后，全部平铺存到一个大池子里。文件名保持你现在的习惯，只加一个平台前缀防重名。

- `/微信搜索结果/260610_高端提领王｜友邦「财富盈活」深度解析.md` （你的现有格式）
- `/内容总库/微信_260610_高端提领王｜友邦「财富盈活」深度解析.md`
- `/内容总库/小红书_260612_友邦盈活提领避坑指南.md`
- `/内容总库/抖音_260615_揭秘港险提领套路.md`

**为什么不分平台建文件夹？** 因为同一个事件（比如友邦提领），小红书和微信可能各有一篇。如果分文件夹存，Agent 要对比时得跨文件夹找；平铺在一起，Agent 用关键词一搜，全平台的证据全出来了。

### 2. 身份挂牌：所有隔离与计算，全看 Markdown 头部

这是解决你所有“懵”的核心。在每一个 Markdown 文件的最顶部，加上一段 YAML 格式的“身份挂牌”。

比如小红书的一篇笔记 Markdown：
```markdown
---
platform: xiaohongshu
domain: 跨境财富
entity: [友邦环宇盈活]
intent: 提领
content_id: xhs_8732
linked_wechat_id: wx_9981   # <--- 关键：如果它和微信某篇讲的是同一个点，挂上微信的ID
steady_read: 1284
wow_change: +24%
---

# 友邦盈活提领避坑指南
（正文内容...）
```

**这套挂牌怎么解决你说的三个问题？**
- **算分隔离：** 系统算小红书的 P99 时，直接筛选 `platform: xiaohongshu` 的文件，绝对不会碰到微信的 10w+。
- **页面切换：** HTML 页面上，不管是哪个平台，只要读到 `platform` 字段，就切到对应平台的展示样式（小红书看互动，微信看阅读）。
- **互相跳转：** 在 HTML 的列表页，如果系统发现这篇笔记的挂牌里有 `linked_wechat_id`，就在旁边亮起一个小按钮“查看微信同类文章”，一点就跳过去。如果没有，就不亮。**所谓跨平台关联，本质就是文件挂牌里的 ID 互相指向。**

### 3. 跨平台关联的“链接页面”到底是个啥？

你担心的“中间是不是有一些 link 的页面”，其实根本不需要单独建一个复杂的中间页。

**理念是：需求单元（实体+意图）就是那个隐形的“链接页面”。**

假设你在 HTML 界面上点开了一个“需求视角”的看板，看到 `实体: 友邦环宇盈活 + 意图: 提领`。
系统底层做的事就是：去池子里扫所有 Markdown 的挂牌，把所有挂着 `entity: [友邦环宇盈活]` 且 `intent: 提领` 的文件全捞出来。
然后在这个页面上，分几个 Tab 展示：
- 【微信】Tab：显示 `wx_9981.md`（阅读量 939）
- 【小红书】Tab：显示 `xhs_8732.md`（收藏 482，评论求购买 15 条）
- 【GEO】Tab：显示 AI 引用了哪篇文章

你在这个页面上，不仅能看到各平台的原始数据，还能看到它们之间的强弱对比。这就是你梦寐以求的“跨平台套利信号看板”。

**总结一句：** 存储就是一堆平铺的 Markdown，互不干涉；但靠文件头部的 `platform`、`entity`、`intent` 和 `linked_id` 这几个字段，系统就能在底层把它们织成一张网。HTML 只负责读这张网，按需展示。Agent 也只需读文件名和挂牌，就能理解一切。

### USER
你这个命名为什么不是分文件加存放，而是直接要放个叫内容总库的东西？这放进去不乱吗？你这个命名为什么不是分文件加存放，而是直接要放个叫内容总库的东西？这放进去不乱吗？这是第一个点。第二个点是假设两个文章重名，查到的日期也一样，那这两个文件会不会有问题？

### ASSISTANT
你这两个问题问得非常精准，直接戳中了文件系统设计的要害。我分开回答，先说为什么“不要分文件夹”，再说“重名冲突”怎么彻底解决。

---

## 一、为什么放一个“总库”里，而不是分文件夹？

你担心放一起会乱，这是人之常情。但在数据和 Agent 的世界里，**“分文件夹”才是真正的乱，放一起反而是最干净的秩序。**

原因有三个：

1. **Agent 的搜索逻辑是“全盘扫描”，不是“按路径点选”。**
   当 Agent 想找“友邦提领”的资料时，如果分了文件夹，它得先猜：这篇在微信文件夹还是小红书文件夹？如果猜错就找不到。放在同一个总库里，Agent 只需要搜关键词 `友邦 AND 提领`，所有平台的证据一次性全出来。路径越浅，搜索越快，漏检率越低。

2. **分文件夹会制造“信息孤岛”，阻碍跨平台对比。**
   你最想要的跨平台套利信号（小红书火了微信没写），如果数据分在两个文件夹，系统每次对比都得跨目录读取、拼合。放在一个目录里，它们天然在一起，按文件头的标签（`platform` 字段）过滤即可，计算效率极高。

3. **文件夹路径是脆弱的，文件名和标签才是稳定的。**
   你未来可能把“视频号”改成“微视”，文件夹改名会导致所有旧脚本里的路径全部报错。但如果你只用一个扁平的目录，文件头打个 `platform: wechat` 的标签，未来改平台名，只是改个标签值，文件物理位置纹丝不动。

**所以放总库里，靠文件名前缀和头部标签区分，不仅不乱，反而是最抗混乱的工程实践。**（如果文件实在太多，最多按“年月”建子文件夹，如 `2026_06/`，绝对不要按“平台”建文件夹）。

---

## 二、日期和名字都一样，文件冲突怎么办？

这是个必然发生的物理冲突，绝对不能靠“加 (1)、(2)”来覆盖。

**彻底的解法是：在文件名末尾加上一段“平台_唯一ID”作为物理防撞锁。**

因为不同平台的底层 ID 体系完全不同，哪怕标题和日期 100% 相同，它们的底层 ID 绝不可能相同。

文件命名最终长这样：

```text
260610_高端提领王｜友邦「财富盈活」深度解析_wx_749d.md
260610_高端提领王｜友邦「财富盈活」深度解析_mp_8821.md
260610_高端提领王｜友邦「财富盈活」深度解析_xhs_5512.md
```

- `260610`：日期，给人看的。
- `中间中文`：标题，给 Agent 搜索和给你看的。
- `wx_749d`：平台前缀 + 该平台底层唯一 ID 的后四位，给操作系统防撞用的。

**这样设计的好处：**
1. **永不冲突：** 哪怕同一秒、同名的文章在微信和小红书同时被抓到，因为后缀的 ID 不同，物理文件绝不会互相覆盖。
2. **溯源极快：** 你在系统里看到这个文件名，一眼就知道它来自哪个平台，甚至能顺藤摸瓜找到原始链接。
3. **Agent 不会懵：** Agent 读到 `_wx_` 就知道用微信的阅读量算法去解析它，读到 `_xhs_` 就知道去解析评论互动。

一句话总结：**物理上全部平铺在一个库（甚至只按月份分个子目录），靠文件名末尾的“平台_ID”防撞，靠文件头部的标签做逻辑隔离和跨平台关联。这是兼顾人类直觉、Agent 效率和系统健壮性的最优解。**

### USER
好的，那你把 Markdown 文件的标准给我起草一个新的标准吧。

### ASSISTANT
这份标准直接发给开发 Agent 即可作为“最高存储契约”。

---

> 💡 **核心设计理念：** 文件名负责“给人看和给 Agent 搜”，文件头负责“给系统算和给平台隔”。两者分离，物理永不冲突，逻辑天然互联。

### 📄 Markdown 文件命名与存储规范 v1.0

#### 一、 物理存储路径
**禁止按平台建文件夹，全部平铺存储**（如果文件量过大，最多按 `年月` 建子目录，如 `content/2026_07/`）。
这保证了 Agent 可以在一个目录下一次性扫出所有平台的证据，不用跨路径猜。

#### 二、 文件名防撞规范
格式：`YYMMDD_文章或笔记标题_平台缩写_平台唯一ID后4位.md`

- **日期**：抓取或发布日期（YYMMDD），用于时间排序。
- **标题**：原始标题，去除特殊非法字符（如 `/ \ : * ? " < > |`），保留可读性。
- **平台缩写**：`wx`（微信搜一搜）、`mp`（公众号采集）、`xhs`（小红书）、`dy`（抖音）、`geo`（AI引用源）。
- **唯一 ID**：该平台原始数据的主键后 4 位（如微信文章的 mid，小红书笔记的 note_id）。**这是物理防撞锁，哪怕同一天同名的文章，也绝不会互相覆盖。**

**示例：**
- `260610_高端提领王｜友邦财富盈活深度解析_wx_749d.md`
- `260612_友邦盈活提领避坑指南_xhs_5512.md`
- `260615_揭秘港险提领套路_dy_88a1.md`

#### 三、 Frontmatter 头部规范（YAML 格式）
每个 Markdown 文件的最顶部，必须包含以下字段。这是系统进行板块隔离、跨平台跳转和算法计算的唯一依据。

```yaml
---
# 1. 身份与溯源
content_id: "wx_749d"              # 全局唯一内容ID（等于文件名后缀，便于反查）
source_url: "https://..."          # 原始链接（用于前端点击跳转原文）
captured_at: "2026-06-10T08:00:00" # 抓取时间
published_at: "2026-06-09T22:00:00"# 文章/笔记的原始发布时间

# 2. 平台与板块隔离
platform: "wechat"                 # 平台标识：wechat, mp, xiaohongshu, douyin, geo
domain: "跨境财富"                  # 业务板块：跨境财富, 科技前沿（用于P99计算隔离）

# 3. 跨平台映射核心
entity: ["友邦环宇盈活"]            # 实体集合（产品/公司/品类），支持多个，走全局实体树
intent: "提领"                     # 意图（痛点），走受控意图字典枚举，意图为空则为母词
linked_ids: ["xhs_5512", "mp_8821"]# 关联内容ID列表（如果该文与其他平台内容讲同一件事，在此挂号）

# 4. 原始事实指标（各平台专属，系统不互相污染）
# 微信专属
read_count: 9843
like_count: 210
read_delta_15d: 939
steady_read: 55

# 小红书专属（如果本文件是小红书，则填这块，其他平台留空或不写）
# xhs_collects: 482
# xhs_comments: 51
# xhs_comment_intent: "求购买"     # 评论区高频意图聚类结果
---
```

#### 四、 字段使用边界（给开发者的死命令）

1. **算分隔离只看 `domain` 和 `platform`：**
   - 算微信 P99 阅读基准时，WHERE 条件必须是 `domain='跨境财富' AND platform='wechat'`。
   - 绝对禁止拿小红书的点赞数和微信的阅读量塞进同一个公式。
2. **跨平台跳转只看 `linked_ids`：**
   - 前端要实现“跨平台对比看板”，就是通过 `linked_ids` 找到对应 `content_id` 的文件，拉取展示。找不到就不亮按钮，绝不靠标题去模糊匹配。
3. **Agent 检索只认文件名和 `entity/intent`：**
   - Agent 搜资料，直接 grep 文件名（标题），或者系统按 `entity` 过滤出文件列表。不需要解析正文去提取实体，降低 90% 的 Token 消耗。

#### 五、 正文规范
- Frontmatter 下方空一行，直接放纯正文（Markdown 格式）。
- 图片如果需要本地化，放在同目录的 `assets/` 文件夹下，正文用相对路径引用（如

### USER
我打断你一下，WX 和 MP 本质上你觉得需要区分吗？这里是我非常纠结的。现在 WX 的文章我是并没有收录任何一篇非搜索结果页当中的文章的。但我确实有一个公众号监控系统。那这里面就会有冲突，冲突在于我很不希望我的搜索关键词系统里面混入非搜索结果公众号文章。因为我的 Agent 会每天分析我搜索流需要写哪些内容，如果一旦混进来，Agent 分析不知道会不会乱。你来考量一下我这两个特性，在这个点上跟我先简单聊起来。方案我们一会再说。

### ASSISTANT
必须严格区分，你的直觉非常准。这两个系统在业务上绝对不能混，一旦混了 Agent 必定产生幻觉。

**WX（搜一搜）和 MP（公众号采集）的本质区别不在于“文章从哪来”，而在于“这篇文章证明了什么”。**

- **WX 证明的是【需求与供给的交汇】**：它出现在搜索结果里，说明有真实用户在搜这个词（需求），且微信算法认为它能满足这个需求（供给匹配）。这是 Agent 做选题的**核心判断依据**。
- **MP 证明的是【创作者的日常表达】**：它只是某个号今天发了篇文章。可能是一篇软文，可能是一篇毫无搜索价值的行业感悟。它没有经过搜索流量的验证。

如果你把 MP 采集的文章混进 WX 的池子里，Agent 看到某关键词下有一篇文章，会误以为“这个需求很热，连非搜索渠道都在讨论”，从而错误地判定为热点选题。实际上可能只是某个代理人花钱投的软文。

**所以，底层逻辑是：**
1. **物理存储上，可以放一起**（用 `wx_` 和 `mp_` 区分文件名防撞）。
2. **业务计算上，绝对隔离**。Agent 每天分析“搜索流选题”时，系统的过滤条件必须死死锁住 `platform = wx`，把 MP 数据完全屏蔽在外。
3. **MP 的价值在于“资产沉淀”**，它给 Wiki Viewer 提供母文章素材，但它绝不参与搜索热度（P99、阅读增量）的计算，更不能作为 Agent 做选题的供需依据。

一句话：**WX 是雷达（找需求），MP 是军火库（找素材）。可以放在同一个基地，但绝不能让军火库的盘点数据干扰雷达的敌情扫描。** 你觉得这个区分逻辑清楚吗？

### USER
可是那意味着，比如说今天有篇文章叫《香港保险分红实现率》，假设同一天我们爬也爬到它了，搜索引擎也抓到了，那么它有 WX 和 MP 两篇文章是一模一样的，你觉得合理吗？怎么感觉有点怪怪的？

### ASSISTANT
你的直觉完全正确，这确实非常怪，而且绝对不能允许出现两篇一模一样的 Markdown 文件。

这在系统设计上叫**“同一实体，多重身份”**，绝对不能通过创建两份物理副本去解决，那会直接导致熵增爆炸（你改了一份，另一份没改，Agent 读到两份矛盾的信息就彻底懵了）。

**正确的理念是：单源存储，多标签挂载。**

1.  **物理文件只存一份：** 文章落盘时，系统先做一次“内容指纹识别”（比如 SimHash 或 URL + 标题去重）。发现微信搜一搜（WX）和公众号采集（MP）抓到了同一篇文章，**只存一个 Markdown 文件**。
2.  **给文件头挂“双重身份”标签：** 
    - 文件名用更通用的日期+标题，不加平台前缀。
    - 在 **Frontmatter** 里，`platform` 字段直接变成一个数组：`platform: [wx, mp]`。
    - 同时，两个来源的事实指标分离记录：
      `wx_steady_read: 55` （搜一搜带来的阅读数据）
      `mp_read_count: 9843` （公众号文章本身的阅读数据）

**Agent 和系统怎么用它？**
- **Agent 看 App 页面时：** 页面后端会说：“在这个关键词下，搜一搜的结果里有一篇热门文章，同时它也是公众号监控里的存量精品。”—— 这反而成了最强的信号。
- **Agent 做选题时：** 当它需要分析“微信搜索流量”时，系统只筛选 `platform` 包含 `wx` 的文件。因为文件是唯一的，它读到的就是正确的数据。当它需要分析“公众号存量资产”时，系统筛选 `platform` 包含 `mp` 的文件，同样是这份唯一的数据，但读的是 `mp_read_count` 字段。

**这样就彻底解决了你的担忧：**
- **不混乱：** 不会有两个一模一样的文件把 Agent 搞晕。
- **不污染：** 搜一搜的选题分析，绝不会混入那些压根没上过搜索结果的公众号文章（因为那些文章没有 `wx` 标签）。
- **有惊喜：** 系统能自动发现“这篇文章既在搜索里爆了，又在我的公众号监控里”，这是最有价值的资产。

### USER
这里还有一个问题就是阅读量，因为我们现在微信搜一搜的系统，Markdown 文件是无限熵增的。比如说今天爬到一个关键词有 10 个文章，明天这 10 个文章依旧在，依旧存一遍。这样的话，这些文章的阅读量变化、点赞量变化、评论区变化，它都能够知得到。但是你现在那个头部的标签里面能加这些字段吗？

### ASSISTANT
绝对不能把这些字段“写死”在 Markdown 文件头里。

如果每抓一次就改一次文件头里的阅读量、点赞数，**几千个 Markdown 文件每天都会因为数据变化而产生 Git 提交或文件系统变动**，这会直接导致你的知识库、Wiki 和 Agent 索引彻底崩溃，因为对 Agent 来说，文件内容变了意味着“知识更新了”，但实际变的只是毫无意义的阅读数字。

**正确的理念是：Markdown 是“静态资产”，只存事实；动态指标是“观测日志”，必须外挂。**

你需要把目前的“阅读量/点赞变化”逻辑，从 Markdown 文件里剥离出来，存到**轻量级索引层**里，让它通过 `content_id` 去关联。

### 具体方案：增加一个“配套 Todo 小文件”或“统一观测日志”

**方案 A（零侵入，极简）：给每个 Markdown 配一个同名 `.json` 观测日志**

保持 Markdown 文件绝对干净，正文和资产信息完全不动。在它旁边，放一个同名的小文件，只存被抓取时的“时间点快照”。

- **Markdown 文件：** `260610_高端提领王｜友邦财富盈活_wx_749d.md`
  - 头部只写：`content_id: wx_749d`、`entity: 友邦环宇盈活`、`intent: 提领`等**永不变化**的静态属性。
- **观测日志：** `260610_高端提领王｜友邦财富盈活_wx_749d.obs.json`
  - 存所有动态变化：
    ```json
    {
      "content_id": "wx_749d",
      "observations": [
        { "captured_at": "2026-06-10T08:00", "read_count": 8500, "like_count": 120, "rank": 3 },
        { "captured_at": "2026-06-11T08:00", "read_count": 9843, "like_count": 210, "rank": 1 },
        { "captured_at": "2026-06-12T08:00", "read_count": 10500, "like_count": 255, "rank": 2 }
      ]
    }
    ```

**这样做的好处：**
1.  **Markdown 文件永不增熵：** 无论你抓多少次，Markdown 文件本身纹丝不动，Agent 和 Wiki 不会因为阅读量变化而误判“知识更新”。
2.  **无限追踪变化：** 那个 `.obs.json` 文件可以无限往里面追加新的抓取记录，轻松算出阅读增量、排名变化等所有动态指标。
3.  **系统计算极快：** 算“友邦环宇盈活”的 15 天阅读增量时，只需要扫描所有 `*_wx_*.obs.json` 文件，过滤出 `entity` 匹配的，然后做差值计算。完全不用打开厚重的 Markdown 正文。

**方案 B（如果你未来想做更复杂的分析）：统一观测日志库**

如果不想散落一堆小文件，就在 `content/` 目录下建一个 `_observations.jsonl` 大文件，每一行是一次抓取记录，通过 `content_id` 关联回 Markdown 文件。

---

**总结一句话：** Markdown 文件从今天起，禁止写入任何动态变化的数据。所有时序变化（阅读、点赞、排名）全部外挂到轻量级的 JSON 观测日志里。Markdown 是永恒不变的资产，JSON 日志是随时间流动的快照。

### USER
从数据库查询的角度，什么样的方式最干净？就 Markdown 不影响的情况下，是不是建一个数据库来去记录这些快照会比较好呢？时间信息和阅读量等一些数据，一些变化数据。

### ASSISTANT
**是的，建一个轻量级 SQLite 数据库是绝对最优解，比 JSON 文件干净一个数量级。**

你之前用 JSON 文件做快照，是因为系统还小。现在跨平台、多板块、Agent 要高频查询，JSON 文件有两个致命问题：

1. **查询效率极低**：Agent 想知道“友邦环宇盈活过去 7 天排名变化”，得用脚本遍历几百个 JSON 文件，再解析、再排序，不仅慢，而且每次写脚本都是重复造轮子。
2. **数据一致性问题**：JSON 文件写入一半系统崩溃了，文件就坏了；多个脚本同时写同一个 JSON，数据会互相覆盖。

**SQLite 为什么干净？**

因为它是**单文件、零配置、嵌入式**的数据库，和你的 Markdown 文件平起平坐，不需要装任何服务，但拥有完整的关系型查询能力。

就像你现在的项目里用 SQLite 存人工配置和运行状态一样，这次只多建一张专门的“观测快照表”。

---

## 具体方案：一张 `observations` 表，轻到极致

表结构就一张，字段极少，但能覆盖你所有查询需求：

```sql
CREATE TABLE IF NOT EXISTS observations (
    content_id TEXT,          -- 对应 Markdown 文件的唯一 ID（wx_749d）
    captured_at TEXT,         -- 抓取时间，格式 "2026-06-12T08:00:00"
    platform TEXT,            -- wx / xhs / mp / dy / geo
    keyword TEXT,             -- 命中关键词
    rank INTEGER,             -- 排名（第几名）
    read_count INTEGER,       -- 阅读量（微信）/ 点赞量（小红书）
    like_count INTEGER,       -- 点赞数
    comment_count INTEGER,    -- 评论数
    share_count INTEGER,      -- 分享数
    source_url TEXT,          -- 原始链接
    UNIQUE(content_id, captured_at, keyword)  -- 同一文章同一时间同一关键词只存一条
);
```

**就这么一张表，没有任何外键、没有复杂关联，但能解决你所有问题。**

---

## 为什么比 JSON 文件干净？

| 场景 | JSON 方式 | SQLite 方式 |
|---|---|---|
| Agent 查“友邦环宇盈活 15 天阅读增量” | 遍历几百个文件，逐个解析，做差值 | 一条 SQL：`SELECT MAX(read_count)-MIN(read_count) FROM observations WHERE content_id IN (SELECT content_id FROM observations WHERE keyword='友邦环宇盈活') AND captured_at BETWEEN ... AND ...` |
| 系统算“微信港险板块 P99 阅读基准” | 先找到所有微信文章，再读每个文章的 JSON 文件取最新 read_count | 一条 SQL：`SELECT read_count FROM observations WHERE platform='wx' AND domain='跨境财富' ORDER BY read_count DESC LIMIT 1 OFFSET (SELECT COUNT(*)*0.99 FROM ...)` |
| 页面展示排名快照变化 | 找到所有快照 JSON，按时间排序渲染 | 一条 SQL：`SELECT * FROM observations WHERE keyword='友邦环宇盈活' ORDER BY captured_at DESC`，直接返回给前端渲染 |
| 数据一致性 | 文件写入中崩溃，文件损坏 | SQLite 事务保证原子性，要么全写要么全不写，不会产生脏数据 |
| 并发写入 | 多个抓取脚本同时写一个 JSON，互相覆盖 | SQLite 支持多读单写，锁机制自动排队 |

---

## 唯一索引防重复，抓多少次都不脏

你担心的“同一天抓到同一篇文章，数据膨胀”问题，用 `UNIQUE(content_id, captured_at, keyword)` 就解决了。

- 今天抓到 `wx_749d` 在“友邦财富盈活”排第 3，阅读量 9843，写入一条。
- 明天同一篇文章仍在榜，阅读量 10500，再写入一条。
- 同一天两次抓取，INSERT 时会自动忽略重复记录（或更新为最新值），不会产生脏数据。

**15 天后，一个关键词下最多 150 条记录（10 篇文章 × 15 天），数据量极小，查询毫秒级。**

---

## 和 Markdown 文件的关联方式

Markdown 文件只存 `content_id`（在 Frontmatter 里），这是**唯一关联键**。

- 系统从 SQLite 查到 `content_id = wx_749d` 的最新阅读量是 10500，Agent 需要看正文时，直接去 Markdown 文件里找 `wx_749d` 开头的文件。
- Markdown 文件里永远不写动态数据，只写静态属性和正文。

**一句话：Markdown 管“内容”，SQLite 管“变化”，两者通过 `content_id` 关联，永远不互相污染。**

### USER
这里还有个问题就是快照是有排名信息的，比如说这个文章在某一个关键词下，某一次取，它排在这个关键词的第几名。那这个是不是也要存到数据库里面？

### ASSISTANT
**必须存，而且这是这张表最核心的字段之一。**

你之前算“经典文章”（在第4-10名稳定存活）和“时效文章”（冲进前三），全靠这个排名信息。

其实在我上一条给你的建表语句里，已经包含了 `rank` 这个字段，我们再把它拆细一点看它怎么完美解决你的问题。

### 1. 怎么存？

非常简单，每次抓取时，直接把该文章在该关键词下的排名作为一个整数存进去：

```sql
INSERT INTO observations (content_id, captured_at, platform, keyword, rank, read_count) 
VALUES ('wx_749d', '2026-06-12T08:00:00', 'wx', '友邦财富盈活', 3, 9843);
```

如果这篇文章同时命中了两个关键词（比如“友邦盈活”和“香港储蓄险”），那就存两行，各自记录在那个词下的排名。

### 2. 存进去能干出什么“天才”的事？

有了这个字段，Agent 和系统算你之前定的那些复杂规则，就是一条 SQL 的事：

- **找“经典文章”（第4-10名存活3天以上）：**
  `SELECT content_id FROM observations WHERE rank BETWEEN 4 AND 10 GROUP BY content_id HAVING COUNT(DISTINCT date(captured_at)) >= 3`
- **找“时效爆发”（刚冲进前三）：**
  `SELECT content_id FROM observations WHERE rank <= 3 AND captured_at >= date('now', '-3 days')`
- **算某篇文章在15天内的“平均排名”：**
  `SELECT AVG(rank) FROM observations WHERE content_id = 'wx_749d'`

### 3. 最关键的一点：防数据污染

注意我上条建议的表结构里有一个唯一索引：`UNIQUE(content_id, captured_at, keyword)`。

这意味着：
- 6月12日早上8点，系统抓到 `wx_749d` 在“友邦财富盈活”排第 3 名，存入数据库。
- 6月12日下午2点，系统又抓了一次，它掉到了第 4 名。
- 因为是“同一天同一文章同一关键词”，数据库会直接**覆盖或忽略**下午这次抓取（取决于你用 `INSERT OR REPLACE` 还是 `INSERT OR IGNORE`）。

**为什么要这样？** 因为你算的是“日级”的稳定性和趋势，一天之内排名来回横跳（早上第3下午第5晚上又回到第2）属于噪音。每天只留一条记录（建议留每天最后一次抓取的快照），算出来的“经典存活”和“时效动能”才最干净，不会被日内波动搞乱。

### USER
还有个动态的部分就是评论区，你比如抖音和小红书都是有评论区的。那假设这些内容的评论区被读者删了，但是我们前面抓到一个他没删的状态，第二次取的时候没取到了，但上下文其实还有一些关系，能看得出来没抓错的，那这怎么办呀？这种存法的话存数据库吗？还是存在 Markdown 里评论区？因为你存在数据库的话，我怕 AI Agent 在分析的时候，万一假设它读了文章，没有反查数据库，那它可能就不知道这个内容的评论情况了。不过呢，这东西存在数据库好像更合理，那怎么办呢？

### ASSISTANT
你的担忧非常精准：**如果把评论区塞进数据库，Agent 读文章时如果不去查库，就会对这篇文章的“真实热度”一无所知；但如果把几百条评论全塞进 Markdown 正文，又会把正文撑爆，导致 Agent 读一篇短文消耗几万 Token，极其浪费算力。**

特别是你提到的“被读者删了但上下文有关联”的情况，纯靠零散字段根本描述不清楚。

**最干净的理念是：正文（Markdown）保持绝对纯净，但在它的文件头钉上一张“评论动态体检表”，再把完整的评论尸体存进数据库。**

具体分三步来做，既兼顾 Agent 的阅读直觉，又保证数据的法律级留痕：

### 1. Markdown 头部：钉一张“评论状态铭牌”

在每一个 Markdown 的 Frontmatter 里，专门开辟一个评论区区块。Agent 只要打开文章，扫一眼头部就知道这片评论区发生过什么，不需要去查库。

```yaml
---
content_id: "xhs_5512"
platform: "xiaohongshu"
# ... (其他静态属性)

# --- 评论区动态铭牌 ---
comment_summary:
  status: "部分删失"             # 正常 / 部分删失 / 全部清空
  total_seen: 51                 # 历史累计抓取到的评论条数
  current_alive: 42              # 当前还活着的评论条数
  deleted_count: 9               # 消失的评论数
  deleted_samples:               # 被删评论的代表性原话（最多存3条，给Agent看线索）
    - "求私信，想买"
    - "听说提领时有坑？"
    - "对比过保诚了，还是这个香"
  latest_intent: "求购买"         # 评论区最新聚类意图（提领/对比/求购买/避坑）
---
```

**为什么这样设计？**
- Agent 读文章时，一眼就能看到：“哦，这篇笔记有 51 条评论，但删了 9 条，而且删掉的评论里居然有‘求私信想买’，说明这文章触发了极强的真实购买欲，但可能被博主或平台清理了。”
- 这种“残缺的线索”对 Agent 判断选题价值极大，而且完全不需要去数据库里翻找，省了无数 Token。

### 2. 数据库：存完整的“评论尸体”和“生死时间”

Markdown 头部只存摘要，真正的评论明细（哪怕被删了）必须存进 SQLite 数据库，作为永久留痕。

新建一张 `comments` 表：

```sql
CREATE TABLE IF NOT EXISTS comments (
    comment_id TEXT,             -- 评论的唯一ID
    content_id TEXT,             -- 关联的文章/笔记ID
    captured_at TEXT,            -- 抓取到这条评论的时间
    author TEXT,                 -- 评论者
    text TEXT,                   -- 评论原话
    is_alive INTEGER DEFAULT 1,  -- 1=还活着, 0=已被删除/隐藏
    first_seen_at TEXT,          -- 首次发现这条评论的时间
    last_seen_at TEXT            -- 最后一次确认它还存在的时间
);
```

**怎么处理“被删了但上下文有关联”？**
- 假设“求私信”这条评论，6月10日抓到，6月12日再抓没抓到。
- 系统不会直接把它删掉，而是执行一条更新语句：`UPDATE comments SET is_alive = 0 WHERE comment_id = 'xxx'`。
- 这样它就成了一具“尸体”。系统依然可以通过 `content_id` 把它和文章关联起来，上下文关系（谁回复了谁）依然保留在原话里。

### 3. 前端展示：像考古一样展示评论区

当你在 HTML 页面或 Agent 在详细分析某篇文章时：
- **存活评论**：正常黑色显示。
- **删失评论**：灰色斜体加个删除线（如：~~求私信，想买~~），并在旁边标注“该评论于 06-12 后不可见”。

### 总结这套打法的核心优势

1. **Markdown 不膨胀**：正文依然只有几百字，评论摘要只在头部占十几行，Agent 读起来毫无压力。
2. **Agent 不犯傻**：Agent 哪怕不查数据库，也能从头部的 `deleted_samples` 知道这篇文章曾经有过极度强烈的购买意向信号。
3. **防反噬**：无论小红书或抖音怎么删评、限流，你的数据库里完整保留了“案发现场”，随时可以拉出来做深度语义复盘。

### USER
好的，那我这边还有一个问题就是，豆包的 SEO 搜索结果，它是会有一个 Markdown 回答的，那个 Markdown 回答我们是也单独存成一个 Markdown 文件，还是存到数据库里，当成一个 JSON 的字段比较好？这是第一个。第二一个是任何搜索结果，它都应着，应，应当是有一张表的，有排序，然后还有下拉词有关联词，然后每个平台还有一些搜索的额外的内容，有些是有些搜索平台现在已经有 AI 总结了。你比如说微信搜一搜也有了，小红书搜一搜也有了，抖音搜一搜部分也有了。那这个的话，你来帮我思考一下，这些东西是存进数据库，对吧？因为我们的系统最终都要把这些数据还原出来的。

### ASSISTANT
结论先行：**AI回答直接存进 Markdown 文件，当作“一种特殊的母文章资产”管理；而搜索结果的榜单、下拉词、AI总结框这些“快照事实”，全部以结构化形式存入 SQLite 数据库。**

下面把这两个问题拆开说透。

---

### 一、豆包/GEO 的 AI 回答 Markdown 怎么存？

**必须存成独立的 Markdown 文件。**

你想想，GEOProMax 的核心目的是什么？是观察“AI 如何回答用户问题，并引用了谁”。这段 AI 的回答，本质上就是一篇由机器生成的“百科母文章”。它有长达几千字的逻辑结构，而且你需要让 Agent 去阅读、分析它的口径。

如果你把它塞进数据库的 JSON 字段里，Agent 每次想看这段回答，都得先写 SQL 把这一大坨文本查出来，再喂给大模型，这极其反人类，也违反了“Markdown 是唯一正文源”的契约。

**正确做法：**
1. **存 Markdown**：文件名如 `260612_香港储蓄险有什么缺点_geo_8a1f.md`。
2. **头部挂牌**：
```yaml
---
content_id: "geo_8a1f"
platform: "geo"
domain: "跨境财富"
query: "香港储蓄险有什么缺点"   # 触发这个回答的原问题
ai_model: "豆包"
captured_at: "2026-06-12T08:00:00"
---
（这里放豆包返回的完整 Markdown 回答正文）
```
3. **引用源存哪？** 引用源（第几名引用了哪个链接）是动态变化的快照，这些不要写进 Markdown，存进下面要说的数据库表里。

---

### 二、搜索结果的榜单、下拉词、AI总结框怎么存？

你提到的“下拉词、排名表、各平台的AI总结框”，它们有一个共同特征：**它们是“某一次搜索”在“某一刻”的附属产物，而不是内容本身。**

如果把下拉词塞进文章的 Markdown 头部，会导致同一篇文章因为被不同词搜到，而被迫挂载一堆无关的下拉词，直接乱套。

**正确理念：建立“快照事实表”，与文章表分离。**

在你的 SQLite 数据库里，除了之前定的 `observations`（文章观测表）和 `comments`（评论表），再加两张表：`search_snapshots`（搜索快照表）和 `search_features`（搜索特征表）。

#### 1. `search_snapshots` 表：存搜索榜单
每次你在微信或小红书搜一个词，都会生成一个“搜索结果页”。这个页面本身的元数据存这里。

```sql
CREATE TABLE IF NOT EXISTS search_snapshots (
    snapshot_id TEXT,            -- 这次搜索的唯一ID (如: snap_2026061208_xhs_001)
    captured_at TEXT,            -- 搜索时间
    platform TEXT,               -- wx / xhs / dy / geo
    keyword TEXT,                -- 搜索词
    content_id TEXT,             -- 榜上第几名对应的那篇Markdown文章的ID
    rank INTEGER                 -- 排第几名
);
```
*注：这其实就是你之前抓取逻辑的核心转化。一篇“搜索结果文章”被抓下来，生成一个 Markdown 文件，同时在 `search_snapshots` 里记一笔：“在某时某刻搜某词，它排第几”。*

#### 2. `search_features` 表：存下拉词、相关词、AI总结框
这些是搜索页上的“边角料特征”，它们和某一次搜索行为绑定，但不和某篇文章绑定。

```sql
CREATE TABLE IF NOT EXISTS search_features (
    snapshot_id TEXT,            -- 关联刚才那次搜索的唯一ID
    feature_type TEXT,           -- 类型：suggestion (下拉词) / related (相关词) / ai_summary (平台AI总结)
    feature_text TEXT,           -- 具体内容（比如下拉词是"友邦提领坑"，AI总结是"根据搜索结果..."）
    position INTEGER             -- 如果是下拉词排第几，如果是AI总结在第几位
);
```

### 这样存的好处是什么？（解决你的还原焦虑）

你担心“系统最终都要把这些数据还原出来”，用这套架构，还原一次完整的“历史搜索现场”只需两步：

1. **前端要还原页面**：选一个时间和关键词。系统先查 `search_snapshots` 拿到当时排前10的文章 ID 列表。然后去 `search_features` 查当时的下拉词。用文章 ID 去文件系统拉取 Markdown 渲染正文。完美复刻当时的搜索页。
2. **Agent 要做选题分析**：Agent 不需要去解析复杂的 HTML 页面。它直接读数据库：“系统，给我查一下，昨天小红书搜‘友邦提领’时，AI总结框里提到了什么？下拉词新增了什么？”系统返回纯文本，Agent 瞬间秒懂，直接写出选题报告。

**总结：**
- **文本资产（正文、回答） -> 存 Markdown。**
- **时序事实（排名、阅读量、评论存亡、下拉词、AI总结框） -> 存 SQLite。**
- 两者通过 `content_id` 和 `snapshot_id` 像齿轮一样咬合，永远不互相污染，随时能完美还原现场。

### USER
好的，那我们聊了这么多，你要不把我们最终的版本，就是这个通用化的存储方式，文本资产、时序事实、搜索结果、Markdown、回答结果之类的。变为一个一整套的规范和标准吧。

### ASSISTANT
<details type="reasoning" done="true" duration="15">
<summary>Thought for 15 seconds</summary>
&gt; 1.  **理解目标**：用户希望基于之前所有的讨论，制定一份关于整个内容监控系统（覆盖微信、小红书、抖音、GEO、公众号采集）的全面且最终的存储规范与标准。核心目标是实现通用化、跨平台兼容、Agent 友好且逻辑隔离。
&gt; 
&gt; 2.  **核心原则回顾**：
&gt;     *   **物理平铺，逻辑隔离**：不按平台建文件夹，全部平铺存储在 Markdown 文件中。
&gt;     *   **静态资产与动态观测分离**：Markdown 文件仅用于正文和静态属性。动态数据（阅读量、点赞、排名、评论）存入 SQLite。
&gt;     *   **双层身份防撞**：文件名使用日期+标题+平台+短 ID。
&gt;     *   **跨平台映射**：利用 YAML Frontmatter 中的 `entity` 和 `intent` 映射到全局树。
&gt;     *   **评论动态处理**：在 Markdown Frontmatter 中存摘要，在 SQLite 中存明细“尸体”。
&gt;     *   **GEO/AI 回答**：将其视为特殊类型的 Markdown 资产处理。
&gt;     *   **搜索页特征**：下拉词、AI 总结等作为特征存入 SQLite，与搜索快照绑定。
&gt; 
&gt; 3.  **规范结构**：
&gt;     *   **架构理念**：高层级原则。
&gt;     *   **第一部分：物理存储与文件命名规范**：如何存储 Markdown 文件。
&gt;     *   **第二部分：Markdown 静态资产契约**：YAML Frontmatter 结构。
&gt;     *   **第三部分：SQLite 动态时序数据库规范**：表结构。
&gt;     *   **第四部分：跨平台与跨系统联动机制**：数据如何通过 ID 关联。
&gt; 
&gt; 4.  **起草内容（为了清晰度和“Agent 友好”语调进行迭代优化）**：
&gt; 
&gt;     *   *命名规范*：`YYMMDD_标题_平台缩写_短ID.md`。前缀：wx、mp、xhs、dy、geo。
&gt;     *   *Frontmatter YAML*：
&gt;         *   身份与溯源：`content_id`、`source_url`、`captured_at`、`published_at`。
&gt;         *   隔离：`platform`（如果是 WX+MP 重复则用数组）、`domain`。
&gt;         *   映射：`entity`（列表）、`intent`、`linked_ids`（列表）。
&gt;         *   评论铭牌：`comment_summary` 块（状态、总数、存活、删除数、样本、最新意图）。
&gt;     *   *SQLite 表*：
&gt;         *   `observations`：content_id、captured_at、keyword、rank、read_count、like_count 等。（用于排名/阅读量时间线）。
&gt;         *   `comments`：comment_id、content_id、text、is_alive、first_seen_at 等。（用于评论追踪）。
&gt;         *   `search_snapshots`：snapshot_id、captured_at、platform、keyword、content_id、rank。（用于特定时间的搜索结果页面）。
&gt;         *   `search_features`：snapshot_id、feature_type（下拉词/相关词/AI总结）、feature_text、position。
&gt; 
&gt; 5.  **根据用户需求进行核对**：
&gt;     *   是否涵盖文本资产？是的（Markdown）。
&gt;     *   是否涵盖时序事实？是的（`observations`）。
&gt;     *   是否涵盖搜索结果？是的（`search_snapshots` 和 `search_features`）。
&gt;     *   是否涵盖 Markdown 回答？是的（GEO 作为 Markdown 资产处理）。
&gt;     *   是否通用化/跨平台？是的（适用于 wx、xhs、dy、mp、geo）。
&gt;     *   是否对 Agent 友好？是的，明确的 Frontmatter 以及纯 Markdown 正文。
&gt; 
&gt; 6.  **最终打磨**：使其读起来像一份严格的工程规范。使用清晰、明确的语言。强调 Markdown 文件的不可变属性。
</details>
这份规范是基于我们讨论的所有核心逻辑（实体意图解耦、动态静态分离、跨平台防撞、Agent友好优先）凝结而成的最终版架构契约。你可以直接把它作为开发文档的下发标准。

---

# 📄 跨平台内容监控与存储架构规范 (v1.0)

## 核心设计理念
1. **静态资产与动态观测分离**：Markdown 是永不增熵的静态资产；SQLite 是记录时序变化的动态日志。两者通过 `content_id` 齿轮咬合。
2. **物理平铺，逻辑隔离**：文件不按平台分文件夹，全平铺在总库；隔离全靠 Frontmatter 头部的 `domain` 和 `platform` 标签。
3. **跨平台映射不靠标题**：不同平台的内容，通过 `entity`（实体）和 `intent`（意图）两张全局表映射，通过 `linked_ids` 互相跳转。

---

## 第一部分：物理存储与命名规范

### 1. 存储路径
- **禁止按平台建文件夹**。所有平台的文章/笔记/回答全部平铺存储。
- 如果文件量过大，最多按 `年月` 建子目录：`/内容总库/2026_07/`。

### 2. 文件命名防撞锁
格式：`YYMMDD_标题_平台缩写_短ID.md`
- **日期**：发布或抓取日期（YYMMDD）。
- **标题**：去除特殊非法字符后的原始标题。
- **平台缩写**：`wx`（微信搜一搜）、`mp`（公众号采集）、`xhs`（小红书）、`dy`（抖音）、`geo`（AI引用源）。
- **短ID**：该平台原始数据主键的后4位（物理防撞，杜绝同名覆盖）。
- **示例**：`260612_友邦盈活提领避坑指南_xhs_5512.md`

---

## 第二部分：Markdown 静态资产契约 (Frontmatter)

每个 Markdown 文件最顶部必须包含以下 YAML 格式头部。**正文在此头部下方空一行开始，正文内禁止写入任何动态变化的数值（如阅读量、排名）。**

```yaml
---
# === 1. 身份与溯源 (必填) ===
content_id: "xhs_5512"              # 全局唯一ID，与文件名后缀一致
source_url: "https://..."           # 原始链接
captured_at: "2026-06-12T08:00:00"  # 首次抓取时间
published_at: "2026-06-11T22:00:00" # 内容原始发布时间

# === 2. 平台与板块隔离 (必填) ===
platform: "xiaohongshu"             # wechat / mp / xiaohongshu / douyin / geo
domain: "跨境财富"                  # 跨境财富 / 科技前沿 (用于P99计算硬隔离)

# === 3. 跨平台映射核心 (必填) ===
entity: ["友邦环宇盈活"]            # 实体集合，走全局实体树
intent: "提领"                     # 意图，走受控意图字典
linked_ids: ["wx_9981", "mp_8821"] # 关联同事件的其他平台内容ID (无则留空数组)

# === 4. 评论区铭牌 (条件必填，仅限有评论的xhs/dy等) ===
comment_summary:
  status: "部分删失"               # 正常 / 部分删失 / 全部清空
  total_seen: 51                   # 历史累计抓取评论数
  current_alive: 42                # 当前存活数
  deleted_count: 9                 # 消失数
  deleted_samples:                 # 最多3条被删评论原话，供Agent做意图研判
    - "求私信，想买"
  latest_intent: "求购买"           # 评论区最新聚类意图

# === 5. GEO专属字段 (仅限platform=geo) ===
query: "香港储蓄险有什么缺点"        # 触发此AI回答的原始问题
ai_model: "豆包"                   # 生成此回答的AI模型
---
```

---

## 第三部分：SQLite 动态时序数据库规范

所有随时间变化的数据（排名、阅读量、评论尸体、下拉词、AI总结框）全部存入 SQLite。共四张核心表：

### 1. `observations` (文章观测快照表)
记录文章/笔记在每次抓取时的指标变化。
```sql
CREATE TABLE IF NOT EXISTS observations (
    content_id TEXT,         -- 关联Markdown的content_id
    captured_at TEXT,        -- 抓取时间
    platform TEXT,           -- 平台
    keyword TEXT,            -- 命中的搜索关键词
    rank INTEGER,            -- 在该关键词下的排名
    read_count INTEGER,      -- 阅读量(wx/mp) / 点赞量(xhs/dy)
    like_count INTEGER,      -- 点赞数
    comment_count INTEGER,   -- 评论数
    share_count INTEGER,     -- 分享数
    UNIQUE(content_id, captured_at, keyword) -- 唯一索引：同天同文同词仅留一条
);
```

### 2. `comments` (评论尸体存档表)
记录每一条评论的生死状态，防平台删评导致线索丢失。
```sql
CREATE TABLE IF NOT EXISTS comments (
    comment_id TEXT PRIMARY KEY, -- 评论唯一ID
    content_id TEXT,             -- 关联的文章ID
    author TEXT,                 -- 评论者
    text TEXT,                   -- 评论原话
    is_alive INTEGER DEFAULT 1,  -- 1=存活, 0=已被删除/隐藏
    first_seen_at TEXT,          -- 首次发现时间
    last_seen_at TEXT            -- 最后一次确认存活时间
);
```

### 3. `search_snapshots` (搜索榜单表)
记录“某时某刻搜某词，榜单上是谁”。
```sql
CREATE TABLE IF NOT EXISTS search_snapshots (
    snapshot_id TEXT,        -- 搜索行为唯一ID
    captured_at TEXT,        -- 搜索时间
    platform TEXT,           -- 平台
    keyword TEXT,            -- 搜索词
    content_id TEXT,         -- 榜上对应的Markdown文章ID
    rank INTEGER             -- 排名
);
```

### 4. `search_features` (搜索特征表)
记录搜索页的附属特征（下拉词、相关词、各平台自带的AI总结框）。
```sql
CREATE TABLE IF NOT EXISTS search_features (
    snapshot_id TEXT,        -- 关联搜索榜单表
    feature_type TEXT,       -- 类型：suggestion(下拉词) / related(相关词) / ai_summary(平台AI总结)
    feature_text TEXT,       -- 具体文本内容
    position INTEGER         -- 所在位置/序号
);
```

---

## 第四部分：系统协同与防污染红线

1. **算分隔离红线**：计算任何 P99 基准线或热度分时，SQL 的 WHERE 条件必须同时包含 `domain` 和 `platform`。绝不允许小红书的点赞数与微信的阅读量进入同一公式，绝不允许科技板块的爆文污染港险板块基准。
2. **WX与MP双重身份**：若公众号采集（MP）与搜一搜（WX）抓到同一篇文章，物理上只生成一个 Markdown 文件，其 `platform` 字段变为数组（如 `[wx, mp]`）。系统读取时按需过滤，Agent 分析搜索流时只认包含 `wx` 的文件。
3. **Markdown 不可变性**：除了修改头部 Frontmatter 的极少量静态/摘要字段，正文区域一旦落盘，任何脚本不得因为阅读量变化而覆写文件。
4. **Agent 摄取协议**：Agent 做每日选题播报时，无需全盘扫描。优先读取 SQLite 中的异动指标，再凭 `content_id` 定点打开少数几个 Markdown 文件查看正文和评论铭牌，将上下文消耗降至最低。

### USER
是这样的，我们刚才聊的呢，并没有涉及到真实的实际的情况。假设你是一个顶级的架构师，非常懂得设计一个Markdown加数据库的资产管理系统。可以实现快查询、存储有序。字段有标准未来可拓展，我把所有实际的情况的一些字段发给你看一下，好吗？然后你再来评估一下，可以吗？就是我们的Markdown体系、数据库体系怎么设计？
  
# 系统数据字段全景
> 范围：微信搜一搜关键词监控、公众号监控、小红书关键词监控、GEOProMax、Wiki Viewer（母文章库）、WritingMoney、老写作与发布系统  
> 状态：基于实际代码/数据库/JSON 文件的事实陈述  
> 时间：2026-07-14
---
## 一、微信搜一搜关键词监控系统
**目录**`/Users/works14/.claude/监控/wechat-ybxhyyh-top3`  
**事实源**：微信搜一搜搜索结果页，每次搜索保存一个 Markdown 文件  
**核心特点**：**时间切片**——同一个关键词每次搜索都保存一份完整 Markdown，多次抓取形成排名变化序列
### 1.1 原始数据形态：搜索 Markdown
搜索结果保存为 `Markdown` 文件，格式固定：
```
# <关键词>
- 时间：<YYYY-MM-DD HH-MM-SS>
#### 搜索下拉词
- <下拉词1>
- <下拉词2>
#### 相关搜索
- <相关词1>
- <相关词2>
#### 文章列表
01. <文章标题>
    文章简介：<摘要>
    公众号：<公众号名>
    时间：<发布时间>
02. ...
```
- 每抓一次存一份，按关键词目录组织，路径如 `微信搜索结果/批量抓取/<批次>/<关键词>/<时间戳>_<关键词>.md`
- 同一关键词可能积累数十个时间切片文件
### 1.2 数据库：SQLite（配置层）+ 标准化 JSON（事实层）
**配置层 SQLite**`data/state/app.db`
| 表 | 说明 |
|---|---|
| `keyword_registry` | 关键词注册表：keyword_id, keyword_text, status, group_id, topic, keyword_bucket, refresh_frequency_days, commercial_value_score, lifecycle_stage, observation_started_at/deadline_at, auto_archive_locked, archive_reason_code 等 |
| `keyword_groups` | 关键词分组：group_id, label, display_order |
| `keyword_discovery_probes` | 关键词发现探针（从搜索下拉词/相关词挖掘候选词）：probe_id, probe_text, status, source_brief_id, etc. |
| `keyword_discovery_candidates` | 发现候选词：candidate_id, candidate_text, normalized_text, status, validation_score, business_value_score, promoted_keyword_id 等 |
| `keyword_discovery_evidence` | 发现证据链：evidence_id, candidate_id, probe_id, snapshot_id, evidence_date, term_type, position |
**标准化 JSON 目录**`normalized/`
| 文件 | 说明 | 关键字段 |
|---|---|---|
| `keywords.json` | 关键词元数据 | keyword_id, keyword_text, is_active, first_seen_at, last_seen_at, snapshot_count |
| `snapshots.json` | 抓取快照 | snapshot_id, keyword_id, captured_at, snapshot_date, trigger_type(scheduled/manual), is_primary, result_count, source_file_path |
| `snapshot_terms.json` | 搜索建议词/相关词 | term_id, snapshot_id, term_type(suggestion/related), position, term_text |
| `ranking_hits.json` | **核心事实表**——排名命中 | hit_id, snapshot_id, rank, article_id, account_id, title_raw, summary_raw, account_name_raw, published_at_raw, url_raw |
| `articles.json` | 文章元数据 | article_id, normalized_url, raw_url, title, account_id, published_at, summary, first_seen_at, last_seen_at, content_status(available/missing), content_file_path |
| `accounts.json` | 公众号 | account_id, canonical_name, first_seen_at, last_seen_at |
| `article_metric_observations.json` | 阅读量观测 | observation_id, article_id, account_id, observed_at, read_count, like_count, friends_follow_count, original_article_count, match_method, extract_confidence |
| `keyword_read_deltas.json` | 关键词阅读增量估算 | keyword_id, keyword, window_start/end, window_days, method, steady_read_median, read_delta_estimated, trend_signal, trend_label, confidence_score, confidence_level, daily_read_delta_points[] |
| `monitor-data.json` | **前端聚合**（派生层） | generated_at, window_days, keywords[].keyword, keyword_id, topic, keyword_bucket, score, today_best, coverage_days, tracked_accounts, article_count, latest_run, runs[], history_best[15], history_hits[15], day_scores[15], accounts[] |
### 1.3 派生指标
- **rank_weight(rank)**：第1名=2.0, 第2名=1.5, 第3名=1.0, 第n≥4=round(10×0.72^(n-4), 2)
- **阅读增量估算**：基于命中文章在多次抓取间阅读变化，通过 `schedule_adjusted_read_rate_v3` 模型，结合文章年龄、排名权重、缺失日插值等
- **账号分**：15天窗口观察覆盖、稳定文章、持续经营、内容矩阵、战场广度
- **时效分**：3天窗口观察Top3、新文章冲榜、新进Top3、连续冲榜、上升动能
- **当天分**：1天窗口观察今日Top3、关键词数、文章数、排名质量
---
## 二、公众号监控与 Markdown 入库系统
**目录**`/Users/works14/Documents/zkcode/250626_mpGUI`  
**事实源**：通过 WeRSS API 获取指定公众号的文章列表和全文  
**与微信搜一搜关系**：**无数据耦合**，即使文章从未进入搜一搜结果，只要属于被监控公众号即可被抓取。两边可能抓到同一文章，但不会自动合并。
### 2.1 原始数据形态：公众号文章 → Markdown
- 从 WeRSS 获取文章原文 URL，调用 `SomeURL2MD` 模块转成 Markdown
- 输出到 `/Users/works14/Documents/output_md/` 下按分类命名的子目录
- 分类目录`热门产品/z产品对比/z香港vs内地/港险优惠/美联储降息/保司盘点/什么是香港保险/香港储蓄险/z非热门产品/其他/新加坡保险/候选区/排除流/`
- Markdown 文件格式：标题+链接+正文（含图片 via 微信图床）
- 可选的 OCR 注释和图片处理`SomeURL2MD/` 模块提供）
### 2.2 数据库：SQLite
**文件**`.web_console/app.db`
| 表 | 字段 | 说明 |
|---|---|---|
| `settings` | key, value, updated_at | 系统设置（如WeRSS登录信息） |
| `account_flags` | mp_id, mp_name, monitor_enabled, run_enabled, category_name, updated_at | 公众号标记：是否监控、是否进入工作流、所属分类 |
| `categories` | name, created_at | 分类定义 |
### 2.3 工作流状态
- `rejected_articles.csv`：拒绝文章列表（标题去重 + 人工拒绝）
- 通过 `article_workflow.py` 编排：拉取 → 去重 → AI筛选 → Markdown转换 → 落盘
- AI筛选使用 `AIClassifier` 判断文章是否值得入库
### 2.4 变化字段
公众号监控系统的**变化字段很少**，主要是：
- 每次抓取的时间不同
- 文章可能被 AI 分类到不同文件夹
- 标题去重避免重复入库
- 没有阅读量、排名等动态数据
---
## 三、小红书关键词监控系统
**目录**`/Users/works14/Documents/zkcode/取数/xhs-keyword-monitor`  
**事实源**：TikHub 小红书实时 API  
**架构**：微信搜一搜监控系统的复刻版，主对象变更为小红书笔记/博主
### 3.1 原始数据形态
- 原始 API 返回保存在 `data/raw/tikhub/xhs/`
- 标准化 JSON 保存在 `normalized/`
### 3.2 标准化 JSON 文件
| 文件 | 数据量 | 关键字段 |
|---|---|---|
| `keywords.json` | 151 条 | keyword_id, keyword_text, is_active, platform, source_provider |
| `snapshots.json` | 906 条 | snapshot_id, keyword_id, captured_at, trigger_type, result_count, source_name |
| `ranking_hits.json` | 17950 条 | hit_id, snapshot_id, rank, article_id, account_id, title_raw, summary_raw, account_name_raw, published_at_raw, url_raw, source |
| `articles.json` | 7441 条 | article_id, title, summary, url, account_id, published_at, type, cover_url, content_path |
| `accounts.json` | 4201 条 | account_id, canonical_name, headimg_url, description, fans, total_works, likes, collects, follows, ip_location, verify_info, platform_payload |
| `note_metric_observations.json` | 17950 条 | observation_id, article_id, snapshot_id, captured_at, liked_count, collected_count, comment_count, shared_count, read_count |
| `snapshot_terms.json` | 0 条（暂无数据） | 小红书的搜索建议词/相关词 |
| `monitor-data.json` | 聚合数据 | generated_at, window_days, platform, keywords[] |
### 3.3 与微信搜一搜的关键区别
| 维度 | 微信搜一搜 | 小红书 |
|---|---|---|
| 互动指标 | read_count, like_count, friends_follow_count | liked_count, collected_count, comment_count, shared_count |
| 账号字段 | 有限（公众号名） | 丰富（fans, total_works, likes, collects, follows, ip_location, verify_info, 平台payload） |
| 搜索建议词 | 有数据（snapshot_terms有值） | 暂无数据 |
| 热度代理 | 用阅读量 | 用公开互动数据（点赞/收藏/评论/分享）生成热度、峰值与趋势 |
| 成本控制 | — | 搜索首抓与笔记/博主详情分离，详情按需拉取并缓存 |
| 内容封面 | 有 cover_url（微信图床） | 有 cover_url |
| 笔记类型 | — | 图文/视频类型区分 |
---
## 四、GEOProMax：AI 引用源观察系统
**目录**`/Users/works14/Documents/zkcode/GEOProMax`  
**事实源**：豆包（Doubao）AI 对话平台的回答，通过 RedFox 采集器抓取  
**核心特点**：观察"一个问题在某次采集时，AI 如何回答、引用了哪些来源、引用位次如何变化"
### 4.1 数据流
```
原始 JSON（RedFox采集）
  ↓ 拆解JSON
统一临时数据
  ├─→ 保存AI回答Markdown → data/answers/<平台>/<问题>/<时间戳>.md
  └─→ 写入SQLite 八表 → data/index/geopromax.sqlite
```
### 4.2 Markdown 存储格式
**AI 回答正文**`data/answers/<平台>/<问题>/<时间戳>_<平台>_<模式>.md`
- 纯正文章 Markdown，含 AI 回答全文、标题、分段
- 目前已采集 1164 个问题，覆盖 6 个批次
**来源正文**：保存在 `data/answers/` 下的 sources 子目录（从 GEO 架构来看`sources` 的 Markdown 路径设计为 `<平台>/<20位ID>.md`）
### 4.3 SQLite 八表结构
**文件**`data/index/geopromax.sqlite`（当前已积累大量数据：6 batches, 1164 answers, 7811 sources, 20028 source_relations, 9135 source_metrics）
| 表 | 字段 | 说明 |
|---|---|---|
| `batches` | id, raw_file, app, channel(mobile/web/api), mode(quick/pro), new_context, status, started_at/finished_at, duration_seconds, total/succeeded/failed | 一个采集批次对应一个原始 JSON 文件，当前 app=豆包，channel=mobile |
| `answers` | id, batch_id, question, status, mode, new_context, started_at/finished_at, duration_seconds, share_link, markdown_path, error | 一次提问行为，markdown_path 指向 AI 回答正文 |
| `tools` | id, answer_id, position, type, content | 回答内有序工具链（搜索工具等） |
| `tool_search_keywords` | tool_id, position, keyword | 搜索工具使用的关键词序列 |
| `sources` | id(sha256前20位), url, title, summary, platform, favicon, published_at, author, author_profile_link, cover_image, markdown_path, content_hash | **文章/视频的稳定主体**，平台+URL 唯一 |
| `source_relations` | id, answer_id, tool_id, source_id, type(search_result/text_reference/image_reference/related_video), position, anchor_index, anchor_text, image_url, error | **回答与来源的事件关系**，也是双向索引核心 |
| `suggested_questions` | answer_id, position, question | AI 回答的推荐追问 |
| `source_metrics` | source_id, answer_id, read_count, like_count, comment_count, favorite_count, share_count | **来源在某次回答采集时的动态快照**，以 (source_id, answer_id) 唯一，可按时间观察变化 |
### 4.4 平台归一化
`data/platforms.json` 维护了 50+ 个平台的标准名和别名映射：
| 平台 | 别名示例 |
|---|---|
| 会计学堂 | acc5.com |
| 抖音 | douyin.com |
| 网易 | 163.com, 手机网易网, 网易新闻客户端 |
| 雪球 | xueqiu.com |
| 新浪财经 | cj.sina.cn, finance.sina.com.cn |
| 微博 | weibo.cn, weibo.com |
| 今日头条 | toutiao.com |
| 搜狐 | sohu.com |
| 东方财富财富号 | caifuhao.eastmoney.com |
| 香港保险业监管局 | ia.org.hk |
| 永明金融（香港） | sunlife.com.hk |
| 周大福人寿（香港） | ctflife.com.hk |
| 保诚香港 | prudential.com.hk |
| 安盛香港 | axa.com.hk |
| 友邦保险（香港） | aia.com.hk |
| 等 50+ 个 | |
### 4.5 GEO 特有的变化字段
GEO 的 **变化字段特别多**，因为豆包平台收录很细节：
- 同一个问题在不同时间采集，AI 回答可能完全不同（answer hash 变化）
- 引用源列表会变化：新增来源、来源消失、位次升降
- 同一来源在不同采集中的互动指标变化（read_count, like_count, comment_count, favorite_count, share_count）
- 平台分布变化：抖音、会计学堂、网易等不同网站的出现频率
- 作者/创作者维度：同一个来源的作者信息
- 引用位次矩阵：随时间变化的引用排名
---
## 五、Wiki Viewer：母文章库
**目录**`/Users/works14/Documents/output_md/wiki-viewer/`  
**数据源目录**`/Users/works14/Documents/output_md/`  
**核心特点**：**Markdown 是唯一数据源**，无快照、无缓存、无备份，实时读写文件
### 5.1 目录结构
```
/Users/works14/Documents/output_md/
├── wiki/                      # 母文章知识库（核心）
│   ├── 产品母页/              # 产品维度的母文章（安盛盛利2、宏利宏挚传承等）
│   ├── 保司母页/              # 保险公司维度的母文章
│   ├── 底层认知/              # 基础概念类母文章
│   ├── 创作框架/              # 写作框架类母文章
│   ├── 案例故事/              # 案例类母文章
│   ├── 案例库/                # 案例库索引
│   ├── 知识库规则/             # 知识库维护规则
│   └── index.md               # 知识库总索引
├── 热门产品/                  # 公众号入库的Markdown
├── z产品对比/
├── z香港vs内地/
├── 港险优惠/
├── 美联储降息/
├── 保司盘点/
├── 什么是香港保险/
├── 香港储蓄险/
├── z非热门产品/
├── 其他/
├── 新加坡保险/
├── 候选区/
├── 排除流/
└── 微信搜索结果/              # 搜一搜的原始Markdown副本
```
### 5.2 母文章格式
母文章是**标准 Markdown + Wiki 双链**，例如 `wiki/产品母页/安盛盛利2.md` 包含：
- 产品事实（收益率、提领能力、分红实现率）
- 优势与专属短板
- 适配/错配信号
- 时间线和来源依据
- 通过 `[[双链语法]]` 引用其他页面
### 5.3 辅助数据
- `ocr-db.json`：图片 OCR 索引数据库
- `wiki-image-probe.json`：图片远程可用性探测结果
- 无传统数据库，所有内容即文件
### 5.4 与公众号监控的关系
公众号监控系统运行的最终产出就是**把抓取到的文章转为 Markdown 存入 `output_md/` 目录**，Wiki Viewer 提供阅读和编辑这些 Markdown 的能力。两者共享同一套文件系统。
---
## 六、WritingMoney：母文章铸造与批量成稿
**目录**`/Users/works14/Documents/output_md/wiki-viewer/WritingMoney/`  
**状态**：当前是静态可交互 Demo（HTML/CSS/JS），尚未真实连接LLM或监控系统
### 6.1 母文章铸造模式
- 一个项目对应一篇可长期维护的母文章
- 输入：选题、写作目的、URL 临时素材、本地知识库推荐 Markdown
- 素材标记：必用/参考/不用
- 工作目录规划使用 `job.json` 保存阶段、状态、模板和输出路径
### 6.2 批量成稿模式
- 一个项目对应一次成稿批次
- 主视角：关键词 → 匹配母文章 → 生成篇数
- 状态`ready` 可进入队列`needs-mother` 返回母文章铸造
- 输出：批次目录、成稿 Markdown`batch_manifest.json`
### 6.3 数据字段（规划中）
| 实体 | 字段 |
|---|---|
| 批次 | batch_name, source, writing_requirements, keywords[], output_dir |
| 关键词匹配 | keyword, matched_mother_articles[], recommended_hooks, suggested_article_count |
| 生产条件 | ready, needs-mother |
| 成稿队列 | 关键词×篇数，状态(等待/运行中/完成/返工) |
| 输出 | batch_manifest.json |
---
## 七、老写作与微信公众号发布系统
**目录**`/Users/works14/Documents/zkcode/YZKcode/1126WritePublish`  
**状态**：已实际运行过，写作和发布两部分串联
### 7.1 Write：文章生成
**数据流**：输入 Markdown/TXT → AI 改写 → 敏感词替换 → 输出 Markdown
**配置**：
- AI 模型配置`Write/ai_services/openaiapi.json`
- 敏感词表`Write/sensitive_words.txt`
- Prompt 模板`prompt/` 目录
**运行状态**：通过 `main.py` 的 `ContentManager` 类，记录每篇文章的处理状态、错误记录、重试次数
### 7.2 Publish：微信公众号发布
**账号管理**`Publish/config/accounts.json`——10 个公众号账号
| 字段 | 说明 |
|---|---|
| id | 账号编号 |
| name | 公众号名称（保智库pro、小美之家点评、保智库国际希希 等） |
| profile_dir | Chrome 浏览器配置目录 |
| cookie_file | Cookie 文件路径 |
| token_file | Token 文件路径 |
| status | 启用状态 |
**发布流程**：Markdown → HTML(微信编辑器格式) → 封面上传 → 原创声明 → 发布/群发
**调度系统**`publish_scheduler.py` 支持多账号轮换、发布间隔、每日轮次、进度汇总
---
## 八、跨系统字段对比总表
### 8.1 核心实体对比
| 实体 | 微信搜一搜 | 小红书 | GEOProMax | 公众号监控 | Wiki |
|---|---|---|---|---|---|
| 搜索对象 | 关键词 | 关键词 | 问题 | 公众号 | — |
| 排名对象 | 文章 | 笔记 | 引用源 | — | — |
| 账号/作者 | 公众号名 | 博主（含粉丝/IP/认证） | 作者（含主页链接） | 公众号名 | — |
| 内容载体 | Markdown 搜索结果 | 标准化JSON+API原始 | AI回答Markdown+SQLite | Markdown 正文 | Markdown 母文章 |
| 时间切片 | 多次抓取 | 多次抓取 | 多次采集 | 单次抓取 | 无（单版本） |
| 互动指标 | 阅读/点赞/朋友在看 | 点赞/收藏/评论/分享 | 阅读/点赞/评论/收藏/分享 | 无 | 无 |
| 搜索建议词 | 有（下拉词+相关词） | 暂无数据 | 推荐追问 | — | — |
### 8.2 变化维度对比
| 系统 | 变化维度 | 变化频率 | 变化类型 |
|---|---|---|---|
| **微信搜一搜** | 排名变化、阅读量变化、搜狗下拉词变化、相关搜索变化、新文章入榜、文章消失 | 每日多次 | 排名位次、阅读增量、趋势信号 |
| **小红书** | 排名变化、互动数据变化、新笔记入榜 | 每日多次 | 排名位次、互动增量、趋势 |
| **GEOProMax** | AI回答变化、引用源变化、引用位次变化、平台分布变化、互动指标变化 | 每批次 | AI回答内容、引用列表、位次矩阵、平台分布、作者出现 |
| **公众号监控** | 只有抓取时间不同 | 不定期 | 无动态变化字段 |
| **Wiki** | 人工编辑 | 按需 | 内容版本变化 |
### 8.3 存储格式对比
| 系统 | 主存储 | 辅助存储 | 格式 |
|---|---|---|---|
| 微信搜一搜 | 标准化 JSON | SQLite（配置） | JSON + Markdown（原始搜索结果） |
| 小红书 | 标准化 JSON | SQLite（配置，建设中） | JSON + 原始API返回 |
| GEOProMax | SQLite 八表 | Markdown（AI回答正文） | SQLite + Markdown |
| 公众号监控 | SQLite | Markdown（文章正文） | SQLite + Markdown |
| Wiki | 文件系统 | JSON（OCR索引） | Markdown |
| WritingMoney | 文件系统(规划) | JSON（job manifest） | JSON + Markdown |
| 老发布系统 | 文件系统 | JSON（账号配置） | 文件系统 |
---
## 九、当前边界与已知问题
1. **无统一文章主键**：微信搜一搜、小红书、GEO、公众号监控各自独立生成 article_id，同一篇文章在不同系统有不同 ID
2. **无自动流转**：监控系统发现的选题/热文不会自动投递到 WritingMoney
3. **公众号与搜一搜无耦合**：同一篇文章可能同时出现在两个系统，但无自动合并逻辑
4. **GEO Markdown 目录为空**`Markdown/` 目录下无实际数据，仅有数据架构设计
5. **小红书 snapshot_terms 为空**：搜索建议词/相关词未采集过
6. **WritingMoney 还是 Demo**：尚未连接 LLM、监控系统或 Wiki 后端
7. **老发布系统账号配置**：10个账号的 Cookie 和 Token 以文件形式存储，无统一密钥管理
8. **GEO 平台归一化**：50+ 平台通过别名映射，但回写 SQLite 和 Markdown 路径时仍保留原始平台名
  
# 内容监控、母文章、写作与发布系统总览
> 本文只整理当前代码中已经存在的系统、功能、数据来源和目录边界，帮助 Agent 快速理解整套项目。现阶段各系统独立开发、独立存储，没有统一数据库或自动流转；共同产物主要是关键词、选题、问题和 Markdown。
## 一、整体链路
整套体系可以按职责理解为：**发现选题与内容 → 沉淀 Markdown/母文章 → 承接写作 → 批量发布**。
| 环节 | 系统 | 当前职责 |
|---|---|---|
| 搜索观察 | 微信搜一搜关键词监控、小红书关键词监控 | 持续抓取关键词搜索结果，观察内容、账号、排名和时间变化 |
| 账号采集 | 公众号监控系统 | 监控指定公众号，抓取账号文章并转为 Markdown |
| GEO 观察 | GEOProMax | 观察 AI 回答、引用源、平台、作者和引用位次变化 |
| 内容资产 | Wiki Viewer | 阅读、编辑和整理本地 Markdown，维护母文章及图片 |
| 写作生产 | WritingMoney | 创建母文章，或用已有母文章承接关键词批量成稿 |
| 发布执行 | 老写作与发布系统 | AI 改写、Markdown 合成、敏感词处理和微信公众号多账号发布 |
## 二、微信搜一搜关键词监控系统
**目录：** `/Users/works14/.claude/监控/wechat-ybxhyyh-top3`
这是以微信搜一搜结果页为事实源的关键词监控系统。系统记录“某个时间搜索某个关键词时，第几名出现了哪篇文章、属于哪个公众号”，并把多次抓取保存为可回放的排名快照。它只覆盖进入搜索结果的文章，不等于指定公众号的全部文章。
| 功能面 | 当前内容 |
|---|---|
| 关键词管理 | 关键词增删改、启停、分组、置顶、刷新策略、备注和候选词发现 |
| 分类逻辑 | `topic` 表示产品/主题归并`keyword_bucket` 表示搜索意图分类；人工配置与抓取事实分开保存 |
| 监控主页 | 展示关键词列表、最新快照、Top10 文章、账号、覆盖时间、趋势和数据质量 |
| 排名事实 | 保存抓取时间、关键词、文章、账号、排名、发布时间、链接、下拉词和相关词 |
| 常态阅读 | 根据文章在不同时间的阅读观测，估算关键词对应内容的稳定阅读水平 |
| 阅读增量 | 计算 15 日阅读增量、近期趋势、实测占比、模型补值和置信度；不等同微信真实搜索量 |
| 账号分 | 主要观察滚动窗口内的历史/近期覆盖、稳定文章、持续经营、内容矩阵和战场广度 |
| 时效分 | 主要观察最近 3 天 Top3、新文章冲榜、新进 Top3、连续冲榜和上升动能 |
| 当天分 | 只观察当天 Top3、关键词数、文章数、主题、排名质量和相对昨日变化 |
| 文章查看 | 文章列表、命中详情、正文 Markdown、封面、阅读互动观测和来源路径 |
主要位置`app/ingest/` 负责原始文件解析、标准化和指标构建`app/services/` 负责业务聚合`app/web/` 与 `app/static/` 负责页面/API`normalized/` 保存关键词、快照、文章、账号、排名命中和阅读观测等标准化 JSON`data/` 与 SQLite 保存人工配置和运行状态。
## 三、公众号监控与 Markdown 入库系统
**目录：** `/Users/works14/Documents/zkcode/250626_mpGUI`
这是指定公众号的独立采集系统，网页形态为“公众号抓取控制台”。它连接 WeRSS 获取公众号和文章，再通过本地工作流筛选、分类、去重并转换为 Markdown。它与微信关键词监控没有数据耦合：文章即使从未进入搜一搜结果，只要属于被监控公众号，也可以被抓取。
| 功能面 | 当前内容 |
|---|---|
| 公众号管理 | 获取公众号列表，设置选中、刷新、是否进入工作流及所属分类 |
| 微信登录 | 检查登录状态、生成二维码并完成扫码登录 |
| 抓取任务 | 创建、取消和查看后台任务，展示进度、日志与结果统计 |
| 抓取范围 | 支持近 N 天文章处理，也保留指定公众号历史抓取脚本与 SOP |
| 去重与拒绝 | 扫描已有 Markdown、拒绝记录和标题，避免重复入库 |
| AI 筛选 | 使用可配置模型判断文章是否进入本地知识库 |
| Markdown 转换 | 使用微信原文 URL 获取全文并转为 Markdown，不依赖文章摘要 |
| 图片处理 | `SomeURL2MD/` 提供图片下载、二维码识别、OCR 注释等能力 |
| 状态存储 | `.web_console/app.db` 保存设置、公众号标记、分类与任务状态 |
主要位置`client/` 是 Vue 前端`server/` 是 FastAPI 后端`workflow_service.py` 编排登录、刷新、拉取和工作流`article_workflow.py` 负责筛选、去重、转换与落盘`werss_client.py` 封装 WeRSS。
## 四、小红书关键词监控系统
**目录：** `/Users/works14/Documents/zkcode/取数/xhs-keyword-monitor`
这是微信关键词监控架构在小红书上的复刻版。主对象由公众号文章/账号变为小红书笔记/博主，当前正式数据源为 TikHub 小红书实时 API；上层继续使用关键词、快照、排名、账号透视和三榜结构。
| 功能面 | 当前内容 |
|---|---|
| 关键词管理 | 关键词 CRUD、启停、分类、分组、置顶和单词/批量刷新 |
| 搜索快照 | 保存关键词、抓取时间、笔记排名、发布时间、作者和内容 ID |
| 笔记字段 | 标题、摘要、图文/视频类型、封面、原文链接和详情缓存 |
| 互动观测 | 点赞、收藏、评论、分享及不同时间的互动变化 |
| 博主资料 | 昵称、头像、粉丝、关注、IP、认证、作品数和已发布笔记 |
| 账号分 | 15 天窗口观察覆盖、稳定笔记、持续经营、互动质量和战场广度 |
| 时效分 | 3 天窗口观察 Top3、新笔记、新进榜、连续冲榜和互动动能 |
| 当天分 | 1 天窗口观察今日 Top3、关键词、笔记、主题、排名和互动增长 |
| 热度代理 | 用公开互动数据生成稳定热度、峰值与趋势；小红书没有微信式阅读指标 |
| 成本控制 | 搜索首抓与笔记/博主详情分离，详情在打开时按需拉取并缓存 |
入口为 `run.pyapp/ingest/` 负责 TikHub/历史 RedFox 适配与指标构建`data/raw/tikhub/xhs/` 保存原始返回`normalized/` 保存标准化事实与 `monitor-data.jsonapp/web/` 提供页面和 API。
## 五、Wiki Viewer：Markdown 与母文章编辑器
**目录：** `/Users/works14/Documents/output_md/wiki-viewer`
Wiki Viewer 是本地 Markdown 知识库的定制阅读与编辑界面，形态接近轻量 Obsidian。它主要用于维护母文章、写作模板和其他知识资产，不负责关键词抓取或公众号发布。
| 功能面 | 当前内容 |
|---|---|
| 文件导航 | 懒加载目录树、Markdown 文件列表和数量 |
| 搜索阅读 | 全库文件名搜索、Markdown 渲染和 Wiki 双链跳转 |
| 编辑保存 | 全文编辑、局部段落编辑、快捷键保存并写回原 Markdown |
| 图片查看 | 正文图片灯箱、原图和图片 URL 查看 |
| 单文删图 | 删除当前文章中的图片、OCR/插图建议注释及相邻分隔内容 |
| 全库删图 | 查询同一图片的全部引用并批量删除 |
| 图片索引 | 统计图片引用次数、引用文件和重复关系 |
| OCR 辅助 | 建立与回填 OCR 数据库、检查远程图片；部分 AI 洗图/重绘按钮仍为待开发交互 |
主要位置`server.py` 提供目录、搜索、读取、保存、图片扫描和删除 API`wiki.html` 与 `app.js` 是前端`ocr-db.json` 是 OCR 索引。Markdown 文件本身是主要数据源。
## 六、WritingMoney：母文章铸造与批量成稿
**目录：** `/Users/works14/Documents/output_md/wiki-viewer/WritingMoney`
WritingMoney 当前是静态可交互 Demo，目标是承接关键词、选题和已有母文章。它分为两个模式，共享母文章库，但项目列表和生产流程相互独立。
### 1. 母文章铸造
一个项目对应一篇可长期维护的母文章。现有流程包括：输入选题和写作目的、添加 URL 临时素材、从本地知识库推荐 Markdown、把素材标为必用/参考/不用、选择写作模板、确认写作方案、生成写作包，未来再交给 Agent 生成母文章。规划中的工作目录使用 `job.json` 保存阶段、状态、模板和输出路径。
### 2. 批量成稿
一个项目对应一次成稿批次，主视角是关键词。一个关键词可匹配零篇、一篇或多篇母文章，一篇母文章也可服务多个关键词。
| 功能面 | 当前设计 |
|---|---|
| 批次输入 | 批次名称、来源、自然语言写作要求、关键词列表和输出目录 |
| 智能匹配 | 为关键词匹配母文章、推荐钩子和建议生成篇数 |
| 生产条件 | `ready` 可进入队列`needs-mother` 返回母文章铸造 |
| 人工确认 | 用户可改关键词、写作目的、篇数、钩子和母文章匹配 |
| 成稿队列 | 按“关键词 × 篇数”展开，展示等待、运行中、完成和返工状态 |
| 输出边界 | 计划输出批次目录、成稿 Markdown 和 `batch_manifest.json`，不管理发布账号 |
目前 `index.htmlstyles.cssapp.js` 主要实现 Demo 数据和交互，尚未真实连接 LLM、监控系统、Wiki Viewer 后端或发布系统。
## 七、GEOProMax：AI 引用源观察系统
**目录：** `/Users/works14/Documents/zkcode/GEOProMax`
GEOProMax 观察“一个问题在某次采集时，AI 如何回答、引用了哪些来源、引用位次如何变化”。它与传统关键词搜索监控的事实源不同，但同样以问题/关键词和时间快照为主轴。
| 功能面 | 当前内容 |
|---|---|
| 问题库 | 按问题和类目组织监控对象 |
| 回答快照 | 保存采集时间、AI 回答 Markdown、answer hash 和状态 |
| 引用源 | 提取引用标题、链接、域名、平台、作者和引用位次 |
| 时间变化 | 展示同一问题不同时间的回答及引用位次矩阵 |
| 问题视角 | 查看回答原文、搜索词、建议追问、引用榜和平台分布 |
| 引用源视角 | 先看平台，再看作者、被引用内容和覆盖问题 |
| RedFox 接入 | 当前支持单问题手动刷新，并把结果写入 `Markdown/` |
`official_app.py` 是当前工程化入口`geo_demo.py` 是 Demo 数据入口`RedFoxAPI/redfox_markdown.py` 负责调用与落盘`Markdown/` 保存问题快照、历史和分类数据。
## 八、老写作与微信公众号发布系统
**目录：** `/Users/works14/Documents/zkcode/YZKcode/1126WritePublish`
该系统由 `Write/` 和 `Publish/` 两部分组成，保存了过去从素材改写到微信公众号发布的完整流程，可作为 WritingMoney 和未来发布层的事实参考。
### Write：文章生成
| 功能面 | 当前内容 |
|---|---|
| 输入 | 扫描 Markdown/TXT；有 TXT 时读取标题，无 TXT 时可用文件名回退 |
| AI 改写 | 选择 Prompt、模型平台和模型，支持串行或并发调用 |
| Prompt 记录 | 保存每篇任务实际发送的完整提示词 |
| 输出检查 | 检查空响应、传输异常和指定作废关键词 |
| 敏感词 | 根据词表替换敏感词；部分 Prompt 模式可跳过 |
| Markdown | 清理模型输出，拼接可配置的头尾模板并保存 |
| 任务控制 | 重试、错误记录、成功/失败统计和连续失败保护 |
### Publish：微信公众号发布
| 功能面 | 当前内容 |
|---|---|
| 多账号 | 管理账号目录、Cookie、启停状态和登录资料 |
| 登录 | 新增、删除、重命名账号，扫码登录并检查 Cookie |
| 内容转换 | 将 Markdown 转为微信公众号编辑器可使用的 HTML |
| 页面自动化 | 新建文章、填写标题正文、上传封面、处理原创声明 |
| 发布模式 | 普通发布、立即群发、定时群发和混合调度 |
| 账号轮询 | 多账号顺序轮换，账号不可用时切换或等待 |
| 批量调度 | 文章分组、发布间隔、每日轮次和进度汇总 |
| 发布后处理 | 移动已处理文件并记录成功/失败结果 |
根目录 `run.py` 把写作和发布串联起来`Write/main.py` 是内容生成主流程`Publish/publish_scheduler.py` 是批量调度核心`Publish/wechat_publisher.pybrowser_manager.pymd_wxhtml.py` 分别负责微信后台操作、浏览器和 Markdown 转换。
## 九、当前边界与潜在连接点
1. 微信关键词监控、公众号监控、小红书关键词监控和 GEOProMax 的事实源、存储、任务和页面均独立，当前没有统一文章主键或共享数据库。
2. 微信关键词系统表示“搜索结果中出现的文章”；公众号监控表示“指定账号发布的文章集合”。两边可能抓到同一文章，但目前不会自动合并。
3. 监控系统尚未自动向 WritingMoney 投递选题；WritingMoney 目前通过 Demo 数据和手工关键词模拟输入。
4. Wiki Viewer 负责 Markdown/母文章资产，WritingMoney 负责写作决策和生产批次，老 Publish 负责账号、排期与微信后台操作，职责不同。
5. 当前最通用的跨系统载体是 Markdown。未来若建立连接，除正文外还需要传递来源系统、稳定 ID、关键词/问题、标题、原文 URL、作者/账号、发布时间、分类、本地 Markdown 路径和采集时间。

### ASSISTANT
<details type="reasoning" done="true" duration="41">
<summary>Thought for 41 seconds</summary>
&gt; 1. **理解目标：** 用户希望获得一套顶级的架构标准/规范，用于设计一套基于 Markdown + SQLite 的资产管理系统，该系统需综合其现有系统的实际现状（微信搜一搜、公众号采集、小红书、GEOProMax、Wiki、WritingMoney、发布系统）。核心要求：快查询、存储有序、标准化的可拓展字段、未来可拓展性，以及严格遵守其提供的实际数据字段。
&gt; 
&gt; 2. **分析用户的实际数据（“现状”）：**
&gt;     *   **微信搜一搜：** 在基于关键词的目录下存储原始 Markdown 快照。使用 SQLite 存配置，JSON 存事实（快照、排名命中、文章、指标）。指标包括 read_count、like_count、friends_follow_count。拥有派生得分（账号分、时效分、当天分）。
&gt;     *   **公众号采集：** 使用 SQLite 存配置/标记。基于 AI 筛选将全文 Markdown 输出到分类目录中。无阅读量/排名等动态数据。
&gt;     *   **小红书：** 微信搜一搜监控系统的克隆。使用 JSON 存事实。指标包括 liked_count、collected_count、comment_count、shared_count、read_count（有时有）。账号（博主）信息丰富（粉丝、IP 等）。
&gt;     *   **GEOProMax：** AI 引用观察。使用 SQLite（8 张表）存批次、回答、工具、来源、关系、指标、问题。将 AI 回答的 Markdown 存储在按平台/问题划分的目录中。拥有 source_metrics 表（随时间变化的 read/like/comment/fav/share）。
&gt;     *   **Wiki：** 带有双链的纯 Markdown 文件。OCR JSON 索引。
&gt;     *   **WritingMoney：** 基于文件的 `job.json`。关键词到母文章的映射。
&gt;     *   **发布系统：** 基于文件的配置（accounts.json）。基于队列的执行。
&gt; 
&gt; 3. **识别核心问题与脱节之处：**
&gt;     *   无统一主键（系统之间无法关联同一篇文章）。
&gt;     *   存储碎片化（部分用 SQLite 存配置，部分用 JSON 存事实，部分用 Markdown）。
&gt;     *   重复存储（微信搜一搜和公众号采集抓取了同一篇文章却互不知道）。
&gt;     *   基于关键词的 Markdown 快照（微信搜一搜针对每次搜索生成一个巨大的 Markdown 文件）对比单篇文章的 Markdown（公众号采集、小红书、GEO）。这是一个根本性的差异，需要统一。
&gt; 
&gt; 4. **制定“未来状态”架构（Markdown + SQLite + 标准化目录）：**
&gt;     *   **理念 1：“资产”对比“观测事实”对比“事件”**
&gt;         *   资产 = 内容本身（Markdown 正文文件）。
&gt;         *   观测事实 = 动态指标（排名、阅读量、点赞数、评论）。
&gt;         *   事件 = 搜索快照（谁在什么时候搜了什么词，以及那时弹出了什么内容）。
&gt;     *   **理念 2：统一的文件存储（扁平化 + 日期/类型 + 唯一 ID）**
&gt;         *   停止在每次微信搜索时将 10 篇文章打包进一个 Markdown 的做法。解耦它。1 篇文章 = 1 个 Markdown 文件。
&gt;         *   命名规范：`YYMMDD_平台缩写_文章ID.md`。
&gt;     *   **理念 3：统一的 SQLite 数据库（“中枢”）**
&gt;         *   一个中央 SQLite 数据库，取代碎片化的 JSON 和分散的 SQLite 文件。
&gt;         *   表结构需要覆盖所有系统：内容、账号、搜索快照、排名命中、观测事实、评论、GEO 引用、批次任务。
&gt; 
&gt; 5. **起草架构标准（分步进行）：**
&gt; 
&gt;     *   **阶段 1：全局文件存储规范（Markdown 层）**
&gt;         *   *规则：* 1 篇文章/笔记/AI 回答 = 1 个 Markdown 文件。
&gt;         *   *路径：* `/content_pool/{YYYY_MM}/`（平铺，无平台文件夹）。
&gt;         *   *命名：* `{YYMMDD}_{platform_short}_{short_id}.md`
&gt;         *   *Frontmatter（头部 YAML）：* 必须包含 `content_id`、`platform`、`source_url`、`published_at`、`captured_at`、`domain`、`entity`、`intent`。
&gt; 
&gt;     *   **阶段 2：统一的 SQLite 数据库规范（事实与动态层）**
&gt;         *   *数据库名：* `system_brain.db`
&gt;         *   *表 1：`contents`（全局文章表）* -&gt; `content_id`、`platform`、`title`、`md_path`、`url`、`author_id`、`published_at`、`domain`。
&gt;         *   *表 2：`authors`（全局账号/创作者表）* -&gt; `author_id`、`platform`、`name`、`fans`、`ip_location`。
&gt;         *   *表 3：`search_snapshots`（搜索行为表）* -&gt; `snapshot_id`、`captured_at`、`platform`、`keyword`、`trigger_type`。（替代旧的微信搜索 Markdown）。
&gt;         *   *表 4：`ranking_hits`（排名命中表）* -&gt; `snapshot_id`、`content_id`、`rank`。（将搜索与文章关联）。
&gt;         *   *表 5：`observations`（动态指标观测表）* -&gt; `obs_id`、`content_id`、`captured_at`、`read_count`、`like_count`、`collected_count`、`comment_count`、`shared_count`。（吸收微信 JSON、小红书 JSON 和 GEO 指标）。
&gt;         *   *表 6：`comments`（评论存档表）* -&gt; `comment_id`、`content_id`、`text`、`is_alive`。
&gt;         *   *表 7：`geo_relations`（GEO 引用关系表）* -&gt; `answer_id`（链接到 content_id）、`source_id`（链接到 content_id）、`position`。
&gt;         *   *表 8：`job_manifests`（生产任务表）* -&gt; `job_id`、`type`、`status`、`output_path`。
&gt; 
&gt;     *   **阶段 3：处理具体系统差异（“如何做”）**
&gt;         *   *微信搜一搜：* 停止将搜索结果 Markdown 存为单一文件。解析该 Markdown，为前 10 篇文章各创建独立的 Markdown 文件，插入 `contents`、`search_snapshots`、`ranking_hits` 和 `observations` 记录。
&gt;         *   *公众号采集：* 将其 Markdown 直接输出至 `content_pool`。如有抓取指标，则记录至 `observations`。
&gt;         *   *小红书：* 与微信搜一搜相同，但 `observations` 保存点赞/收藏数据。评论存入 `comments` 表。
&gt;         *   *GEO：* AI 回答的 Markdown 存入 `content_pool`。被引用的来源也作为内容存入。通过 `geo_relations` 表建立关联。
&gt;         *   *Wiki/母文章：* 存储于 `/wiki/`，但带有 frontmatter，以便 WritingMoney 可以通过 `entity` 进行查询匹配。
&gt; 
&gt;     *   **阶段 4：Frontmatter 模板（具体的 YAML）**
&gt;         *   创建一个通用模板以及平台专属区块。
&gt; 
&gt; 6. **优化与格式化输出：**
&gt;     *   使用专业、权威的语气（“作为你的顶级架构师...”）。
&gt;     *   使用清晰的标题、表格和 SQL 代码块进行结构化。
&gt;     *   强调*为什么*要做出这些更改（例如，“将微信搜索 Markdown 解耦为单独的文章文件”）。
&gt;     *   确保用户实际字段中的所有具体字段都映射到新架构中。
&gt; 
&gt; 7. **起草过程中的自我纠错/审查：**
&gt;     *   *问题：* 微信搜一搜目前针对每次搜索存储一个巨大的 Markdown 文件。
&gt;     *   *纠正：* 架构必须明确说明：在摄入时“解耦”该文件。将 10 篇文章存为 10 个单独的 Markdown 文件，并使用一张搜索快照表将它们绑定。
&gt;     *   *问题：* WX 和 MP 冲突。
&gt;     *   *纠正：* 使用基于 URL 或标题+作者的哈希生成 `content_id`。如果哈希匹配，则更新现有内容，而不是创建新内容。`platform` 字段可以是一个数组，或由单独的“content_platform_links”表来处理。为简单起见，坚持使用单独的链接表或逗号分隔的数组，但 SQLite 更倾向于规范化。建议使用单一的 `content_id`，并让 `observations` 记录来源平台。事实上，在 `contents` 表中保留一个 `source_platforms` 字段（例如 &quot;wx,mp&quot;）或专门建一个映射表。让我们使用 `contents` 表中的 `origin` 字段，但要允许多次抓取。最佳方案：`content_id` 是全局的。`observations` 表有 `platform` 字段，指示该快照来自哪里。
&gt; 
&gt; 8. **最终润色：** 确保术语与用户的领域对齐（母文章、微信搜一搜、GEOProMax 等）。使 SQL DDL 保持稳健性。准备输出。
</details>
作为你的架构师，看完你这套包含 7 大系统、横跨 4 个平台、几十张表和 JSON 的真实全貌后，我的第一判断是：**你现在的系统正处于“诸侯割据”状态。每个系统都在用自己的方式存文件和建表，字段定义完全不统一。**

如果不做底层大一统，未来你每加一个平台（比如抖音），或者让 Agent 跨系统调数据，都会是一场灾难。

基于你提供的实际字段，我为你设计了这套 **「全域内容资产与时序观测规范 v2.0」**。这套规范的核心是：**一套统一的 SQLite 数据库 + 一套扁平的 Markdown 文件库，彻底吃掉你现有的 7 个孤岛。**

---

# 🏛️ 全域内容资产与时序观测规范 (v2.0)

## 核心设计理念：资产与观测分离
1. **静态资产（Markdown）**：文章、笔记、AI回答、母文章，全部是单文件存储，存正文和静态标签，永不增熵。
2. **时序事实（SQLite）**：搜索行为、排名、阅读量、点赞数、引用位次，全部是带时间戳的流水账记录。
3. **大一统主键**：全系统不再按 `wx_id` 或 `xhs_id` 混存，一律用 `content_id` 统一主键。微信、小红书、GEO 共用一张表。

---

## 第一部分：Markdown 静态资产存储规范

### 1. 物理路径与命名
**废弃**：按关键词、批次、公众号分类建文件夹的旧习惯。
**新规**：所有内容资产（包括微信文章、小红书笔记、GEO回答、公众号采集、母文章）全部平铺在总库中。
- 路径：`/Users/works14/Documents/output_md/content_pool/`
- 命名：`YYMMDD_标题_来源平台短ID.md`（例：`260714_友邦盈活提领避坑_xhs_5512.md`）
- 如果文件量极大，可按月建子目录：`content_pool/2026_07/`。

### 2. 统一 Frontmatter (头部契约)
所有 Markdown 必须挂载此头部，这是跨系统、跨平台查询的唯一依据。

```yaml
---
# === 1. 全局身份 (必填) ===
content_id: "xhs_5512"               # 全局唯一ID (平台缩写_原ID后4位)
origin_platforms: ["xiaohongshu"]    # 来源平台：多个平台抓到同一篇则变数组如 [wx, mp]
source_url: "https://..."
captured_at: "2026-07-14T08:00:00"   # 首次入库时间
published_at: "2026-07-13T22:00:00"  # 原始发布时间

# === 2. 业务映射 (必填) ===
domain: "跨境财富"                    # 业务板块：跨境财富 / 科技前沿
entity: ["友邦环宇盈活"]              # 实体集合 (对应全局实体树)
intent: "提领"                       # 意图枚举
linked_ids: ["wx_9981"]              # 关联同事件的其他平台文章ID

# === 3. 来源平台专属属性 (按需填写) ===
# 微信/公众号 专属
wx_account_name: "保智库"            # 公众号名
# 小红书 专属
xhs_author_name: "港险小助手"
xhs_note_type: "图文"                # 图文/视频
# GEO 专属
geo_query: "香港储蓄险有什么缺点"     # 触发此回答的原问题
geo_ai_model: "豆包"                 # AI模型
---
（正文在此处开始，禁止写入任何动态数据）
```

---

## 第二部分：SQLite 时序事实数据库规范

废弃原来散落在各系统的 JSON 文件和零散 SQLite 配置库，新建一个全局统一数据库：`/Users/works14/Documents/output_md/system_brain.db`。

里面共 **6 张核心表**，吃掉你全景图中所有的动态字段：

### 1. `contents` 表 (全局内容主表)
取代原有的 `articles.json`、`accounts.json` 中的静态部分。
```sql
CREATE TABLE IF NOT EXISTS contents (
    content_id TEXT PRIMARY KEY,      -- 全局唯一ID
    title TEXT,                       -- 标题
    md_path TEXT,                     -- 对应Markdown文件的路径
    author_name TEXT,                 -- 作者/公众号名/博主名
    author_id TEXT,                   -- 全局创作者ID (对应矩阵号树)
    content_type TEXT,                -- article/note/geo_answer/mother_article
    summary TEXT,                     -- 摘要
    is_mother_article INTEGER DEFAULT 0, -- 是否为Wiki母文章(1=是)
    first_seen_at TEXT
);
```

### 2. `search_snapshots` 表 (搜索行为与特征表)
吃掉原有的 `snapshots.json`、`snapshot_terms.json`、微信搜索 Markdown 里的下拉词、GEO 的 `batches`。
```sql
CREATE TABLE IF NOT EXISTS search_snapshots (
    snapshot_id TEXT PRIMARY KEY,     -- 搜索行为唯一ID
    captured_at TEXT,                 -- 搜索时间
    platform TEXT,                    -- wx / xhs / geo / douyin
    keyword TEXT,                     -- 搜索词或GEO问题
    result_count INTEGER,             -- 结果总数
    -- 搜索特征列 (JSON格式存储，避免表结构爆炸)
    features TEXT                     -- 例: {"suggestions":["下拉词1"],"ai_summary":"平台AI总结文本"}
);
```
*亮点：把微信的 `snapshot_terms.json` 和小红书暂无的下拉词，统一用 JSON 存进 `features` 字段，完美兼容未来扩展。*

### 3. `ranking_hits` 表 (排名命中表)
吃掉原有的 `ranking_hits.json` 和 GEO 的 `source_relations` 表（回答引用关系本质也是排名）。
```sql
CREATE TABLE IF NOT EXISTS ranking_hits (
    hit_id TEXT PRIMARY KEY,
    snapshot_id TEXT,                 -- 关联哪次搜索
    content_id TEXT,                  -- 关联哪篇文章/笔记
    rank INTEGER,                     -- 排名位次 (第几名/引用第几位)
    hit_type TEXT,                    -- search_result / geo_reference / related_video
    UNIQUE(snapshot_id, content_id)
);
```

### 4. `observations` 表 (时序互动指标表)
吃掉原有的 `article_metric_observations.json`、`note_metric_observations.json` 和 GEO 的 `source_metrics` 表。
```sql
CREATE TABLE IF NOT EXISTS observations (
    obs_id TEXT PRIMARY KEY,
    content_id TEXT,                  -- 关联哪篇文章
    snapshot_id TEXT,                 -- 在哪次搜索时观测到的
    observed_at TEXT,                 -- 观测时间
    platform TEXT,                    -- wx / xhs / geo
    -- 核心动态指标 (全平台统一字段名)
    read_count INTEGER,               -- 阅读(WX/GEO) / 播放(DY)
    like_count INTEGER,               -- 点赞
    comment_count INTEGER,            -- 评论
    share_count INTEGER,              -- 分享
    collect_count INTEGER,            -- 收藏(XHS) / 在看(WX)
    UNIQUE(content_id, observed_at)
);
```
*亮点：无论微信的阅读量还是小红书的点赞量，全部拍平到这 5 个统一字段里。算 P99 时，用 `platform` 过滤即可。*

### 5. `comments` 表 (评论尸体表)
保留小红书/抖音的评论生死留痕。
```sql
CREATE TABLE IF NOT EXISTS comments (
    comment_id TEXT PRIMARY KEY,
    content_id TEXT,                  -- 关联哪篇笔记
    author TEXT,
    text TEXT,                        -- 评论原话
    is_alive INTEGER DEFAULT 1,       -- 1=存活, 0=删失
    first_seen_at TEXT,
    last_seen_at TEXT
);
```

### 6. `production_manifests` 表 (写作与发布任务表)
吃掉 WritingMoney 的 `job.json` 和老发布系统的 `accounts.json` 调度状态。
```sql
CREATE TABLE IF NOT EXISTS production_manifests (
    job_id TEXT PRIMARY KEY,
    job_type TEXT,                    -- mother_crafting / batch_generation / publishing
    status TEXT,                      -- created / matching / generating / published / failed
    source_content_ids TEXT,          -- 用了哪些母文章 (JSON数组)
    output_content_id TEXT,           -- 生成的成稿 content_id
    publish_account TEXT,             -- 发布到哪个公众号
    created_at TEXT,
    updated_at TEXT
);
```

---

## 第三部分：旧系统改造与迁移红线

基于上述规范，你现有的系统需要做以下“不归路”改造：

### 1. 微信搜一搜监控的重构（最痛但最值）
**废弃**：把 10 篇文章打包写进一个按时间命名的 Markdown 文件的做法。
**新规**：每次抓取微信搜一搜，解析结果页。把 Top10 的文章**各自拆成独立的 Markdown 文件存入总库。同时，原来的搜索页快照（下拉词、相关词）存入 SQLite 的 `search_snapshots` 表，排名关系存入 `ranking_hits` 表。微信的原始搜索 Markdown 只作为留档备份，不再参与计算和展示。

### 2. 公众号监控的并行并入
公众号监控系统抓到文章后，**直接生成单篇 Markdown 文件**存入同一个总库。
如果同一篇文章既被公众号监控抓到，又出现在了微信搜一搜的结果里，系统通过 `normalized_url` 或 `content_hash` 识别到重复。此时，**不生成新的 Markdown 文件**，而是更新原文件的 `origin_platforms` 为 `["wx", "mp"]`。SQLite 中只需新增一条 `observations` 记录即可。

### 3. GEOProMax 的无缝融入
GEO 的 AI 回答本身是一篇高质量的 Markdown，直接按规范存入总库，打上 `content_type: geo_answer` 的标签。它引用的那些外部文章（来源），同样生成一条 `contents` 记录（如果有正文就存 Markdown，没有就只存摘要和 URL）。GEO 系统原来的 8 张 SQLite 表，全部被 v2.0 规范里的 6 张通用表接管，原表废弃。

### 4. 小红书与抖音的极简接入
由于底层已经统一了 `observations`（点赞、收藏、评论统一字段）和 `comments`（评论尸体表），小红书和未来的抖音只需要写一个“平台适配器”：抓取数据 -> 翻译成统一的 `content_id` 和指标格式 -> 吐进 SQLite 和文件库即可。核心业务代码一行都不用改。

### 5. WritingMoney 与老发布系统的收编
WritingMoney 和老发布系统不再自己维护零散的 JSON 配置。所有的成稿、批次状态、发布账号分配，全部记录在 `production_manifests` 表中。写作系统只认 `mother_id`，发布系统只认 `draft_id`，彻底解耦。

---

## 第四部分：字段映射对照表（给开发 Agent 的迁移指南）

为了防止开发 Agent 迁移时把字段搞错，这里明确新旧字段的映射关系：

| 新统一表字段 | 原微信搜一搜字段 | 原小红书字段 | 原 GEO 字段 |
|---|---|---|---|
| `content_id` | `article_id` (加wx_前缀) | `article_id` (加xhs_前缀) | `source_id` (加geo_前缀) |
| `search_snapshots.features` | `snapshot_terms.json` | (暂无) | `tools` 表 |
| `ranking_hits.rank` | `ranking_hits.json:rank` | `ranking_hits.json:rank` | `source_relations:position` |
| `observations.read_count` | `article_metric_observations:read_count`| (无) | `source_metrics:read_count` |
| `observations.like_count` | `article_metric_observations:like_count` | `note_metric_observations:liked_count` | `source_metrics:like_count` |
| `observations.collect_count`| `friends_follow_count` | `collected_count` | `favorite_count` |

> 💡 **避坑指南**：微信的“朋友在看”本质上是一种弱收藏，在底层直接映射为 `collect_count`。小红书的收藏也是 `collect_count`。这样跨平台算“互动热度”时，系统只需 `SELECT like_count + collect_count + comment_count FROM observations WHERE platform=...` 就能一把算出，不需要写恶心的 if-else 兼容代码。

---

## 终局推演：Agent 如何在这个架构下工作？

假设未来某天早上，你的 Agent 醒来要写选题播报，它的动作是这样的：

1. **查异动**：Agent 向 `system_brain.db` 发一条 SQL：
   ```sql
   SELECT keyword, platform, MAX(read_count) FROM observations 
   WHERE observed_at > date('now', '-3 days') GROUP BY keyword ORDER BY MAX(read_count) DESC LIMIT 10;
   ```
   *瞬间拿到最近3天互动量飙升的关键词。*
2. **找缺口（套利信号）**：Agent 发现“友邦盈活提领”在小红书热度极高，但在微信的 `observations` 里增量很低。
3. **抓证据**：Agent 通过 `content_id` 去 `content_pool/` 里拉取那篇小红书爆款笔记的 Markdown。看到头部 `comment_summary` 里写着 `deleted_samples: ["求私信，想买"]`。
4. **定选题**：Agent 确认这是一个极强的跨平台需求缺口，直接在 `production_manifests` 表里创建一条记录：`job_type=mother_crafting, keyword=友邦盈活提领`。
5. **出报告**：Agent 输出：“今日发现小红书关于‘友邦盈活提领’需求爆发，且有大量求购评论被删。微信侧尚无跟进，建议以此为核心撰写一篇提领避坑指南，已为您创建母文章铸造任务。”

**全程零 HTML 解析，零跨目录找文件，Token 消耗极低，判断极精准。这就是大一统架构的威力。**