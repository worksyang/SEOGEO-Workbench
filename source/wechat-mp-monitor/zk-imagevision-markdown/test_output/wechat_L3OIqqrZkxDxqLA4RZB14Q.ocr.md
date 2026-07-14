# 装了这个AI热点Skill之后，你再也不需要自己去刷AI新闻了。

昨天，我把我的AIHOT，也就是AI热点监控网站向大家免费开放了。

得到了很多很多朋友的喜欢。

也万万没想到，第一天的访问用户就突破了10万UV，浏览的PV量更是超过了60万，而且没啥差评，也没出现啥BUG，大家反馈都还挺喜欢，我终于长长的松了一口气。

然后昨天，我收到的最多的两个需求，第一个就是深色看的太难受了，能不能增加浅色模式，这个确实是我自己的疏忽，昨天早上花了一个多小时紧急做完了，昨天中午已经上线了。

![图片](https://mmbiz.qpic.cn/sz_mmbiz_png/2jjfQoZLoqXVFRWpaftiaUyNFT7VBS6da9azkdbwwVW3cwPBlXf4nL1TZJZw3GREdibKLZz90u0KkWP2kGiapSI1mbRFDAiaJtyJ0fHCNziciaJKc/640?wx_fmt=png&from=appmsg)

<!-- OCR内容：
# AIHOT

## 精选
AI 自动挑选的高价值内容

搜索标题/摘要...
搜索

### 5月8日

**02:11**

**Perplexity @perplexity_ai**
Personal Computer现已通过全新的Perplexity Mac应用向所有用户开放。
Personal Computer是Perplexity Computer的进阶版本。
它可在任何Mac设备上运行,能跨本地文件、原生Mac应用、网络以及Perplexity安全服务器执行任务。

[图片]

智能体 产品更新

推荐理由:Perplexity 把计算机使用能力做进了Mac 原生应用,能跨本地文件、桌面程序和网络运行任务,对Mac用户而言AI助手真正开始接管操作系统层级的工作。

**02:11**

**Claude @claudeai**
Claude for Excel、PowerPoint 和 Word 现已全面可用,Claude for Outlook 进入公开测试版。
当Claude 在您的微软应用间切换时,会完整携带对话的全部上下文。

[图片]

Anthropic Microsoft 产品更新

---

**侧边栏导航:**

*   精选
*   全部 AI 动态
*   AI 日报
*   公众号爆文
*   关于
*   反馈
*   登录

-->



然后另一个需求，就是能不能增加skill/API/RSS。

![图片](https://mmbiz.qpic.cn/sz_mmbiz_png/2jjfQoZLoqWtBBeTW0tNeOuleQYBlfSn3zevZfAZ5A9icJcGyNVic0tl4eyllgEib8zM7mwAasCgaBpoKicOVhZCeqCBPMzJRlYAhWsZh3jgCdw/640?wx_fmt=png&from=appmsg)

<!-- OCR内容：
小卡 智能体

**用户反馈 #172**
匿名访客 · 5/7 23:52
卡佬,可不可以提供一个对外访问的api,或者其他什么方式可以保存或者收藏一些新闻,我想把一些有趣有用的新闻或者推文保存到我的obsidian或者我的个人知识库

**用户反馈 #146**
匿名访客 · 5/7 15:43
可不可以加一个接口,可以让我直接通过接口获取网站里公开的数据、信息,方便我继续基于您的数据做一些工作(会注明来源)。
(P.S.感谢卡神的分享,永远支持卡神!!卡神配享太庙!!!)

-->



也收到了阮总的督促，那必须得干了。

![图片](https://mmbiz.qpic.cn/sz_mmbiz_png/2jjfQoZLoqXdkEIHnb8PsV6GgklU1zT2nFcd8f3I0nNwlcWd4jxm0x7PpicKicS2WFJUA8Sl7QH2ibkpbUqLeibd41DWChH5r5Nq9qF0vuDoAok/640?wx_fmt=png&from=appmsg)

<!-- OCR内容：
来源内容: 这个封装了我3年自媒体经验的AI热点网站,今天向所有人免费开放。

阮良 留言12次 关注3年
别做反爬,我让我的Agent每天来你的网站趴一下就完事儿了
昨天 10:20:10 浙江
👍 60 💬 ...

数字生命卡兹克 作者
需求收到了,这两天我把Agent接入做好
昨天 12:54:24
👍 34 💬 ...

共4条回复

-->



这毕竟是个AI时代，只有网站这一种形式，确实还是太笨拙了，所以晚上下班回家，决定继续打开AI开始Coding，把大家提的需求都补上。

于是，在一个通宵之后，我终于全部开发完了。

今天，我觉得也可以给所有的Agent用户，开放我的AIHOT了。

而且同样老规矩，所有人都免费使用。

网址在此：https://aihot.virxact.com

你进入AIHOT主站，点击左边的Agent接入，就可以看到了。

![图片](https://mmbiz.qpic.cn/mmbiz_png/2jjfQoZLoqXPh2whOBXgSgnicZkpZSZViblgjHW0GNCZAxiaePRBeUO2lbktnfUfjjbmhyzwtb91nHCkrdD8LicagnBdbr8KPeogfIoq0Upb4G0/640?wx_fmt=png&from=appmsg)

<!-- OCR内容：
# 把 AI HOT 接进你的工作流 (测试版)

让 Claude Code、RSS reader、任意 Agent 直接读到 AI HOT 的全部 AI 动态和每日精编日报。匿名免费、无需 token。

## Skill, RSS, REST API

### Skill
任意 Agent · SKILL.md 标准

### RSS
任意 RSS reader · 零配置订阅

### REST API
开发者 / 自定义集成 · OpenAPI 3.1

---

## 一份 Skill, 任何 Agent 都能用

SKILL.md 标准格式, 跨 Claude Code · Codex CLI · Cursor · Gemini CLI · GitHub Copilot · OpenCode · Cline · Windsurf 等任意 Agent 平台都能装。装好后用最自然的中文一句话拿到 AI HOT 数据, 不需要 API Key, 不需要配 MCP server.

### 安装

在你的 Agent 里直接发这句话, Agent 会自己装到对应目录, 不用你操心路径:

帮我安装这个 skill: `https://aihot.virxact.com/aihot-skill/`

本 Skill 同时同步到了卡兹克的 Skills 合集 KKKKhazix/khazix-skills (和 hv-analysis/khazix-writer/neat-freak 等其他 Skill 一起)。

### 触发示例

“今天 AI 圈有什么新东西”

---

**侧边栏导航:**

*   精选
*   全部 AI 动态
*   AI 日报
*   公众号爆文
*   Agent 接入
*   关于
*   反馈

-->



目前开放了3种接入方式：

Skill、RSS、API，分别对应三种不同的需求。

目前把我觉得能对外开放的数据，也做了最大程度的开放。

重点说说AIHOT Skill，这应该是呼声最高的，也是AI时代最重要的东西。

Skills我就不详细再去过多解释了，这玩意就是给Agent使用的技能包，如果有不懂的，可以去我的公众号搜索Skills，我写过非常多了。

你可以用任意的Agent，比如Claude Code、Codex、OpenCode、OpenClaw、Hermers等等支持Skill协议的来进行安装。

而这个AIHOT.skill的作用呢，也特别简单，让你的Agent可以直接读我的AIHOT网站的部分数据，从而来实现嵌入到你的工作流中。

![图片](https://mmbiz.qpic.cn/sz_mmbiz_png/2jjfQoZLoqVLrXmhEo80Utic50hLCtUzWj8SWJRdqxyPKW2cvwOpHEYUJuOdWTUy5u5wibfsygT6kMSj2fycohkYPO6lKibiaBvSRGI6TCWRLWM/640?wx_fmt=png&from=appmsg)

<!-- OCR内容：
# Skill

任意 Agent · SKILL.md 标准

---

# RSS

任意 RSS reader · 零配置订阅

---

# REST API

开发者 · 自定义集成 · OpenAPI 3.1

---

SKILL · 任意 AGENT · SKILL.MD 标准

## 一份 Skill, 任何 Agent 都能用

遵循 Agent Skills 开放标准, 跨 Claude Code · Codex CLI · Cursor · Gemini CLI · GitHub Copilot · OpenCode · Cline · Windsurf 等任意支持 SKILL.md 的 Agent 平台都能装。装好后用最自然的中文一句话拿到 AI HOT 数据, 不需要 API Key、不需要配 MCP server。

## 安装

在你的 Agent 里直接发这句话, Agent 会自己装到对应目录, 不用你操心路径:

帮我安装这个 skill: https://aihot.virxact.com/aihot-skill/

本 Skill 同时同步到了卡兹克的 Skills 合集 KKKKhazix/khazix-skills (和 hv-analysis/khazix-writer/neat-freak 等其他 Skill一起)。

## 触发示例

"今天 AI 圈有什么新东西"

“看一下今天的 AI 日报"

-->



安装也特别简单，一句话就可以，我直接放在了我的服务器上，所以也无需魔法：

```
`帮我安装这个 skill：https://aihot.virxact.com/aihot-skill/`
```

当然，这个skill我也按老规矩，传到了我自己Skills集合的Github上，想用github源的可以：

https://github.com/KKKKhazix/khazix-skills

![图片](https://mmbiz.qpic.cn/mmbiz_png/2jjfQoZLoqWzA5CISIVWF8lRXxWjujgfFgTd5U1Z6qa9LZoyEOupM02tAKdn4fUCrTyAe2yWNPAwQZbuuknxClES57o16rLycy5icjdlGRuM/640?wx_fmt=png&from=appmsg)

<!-- OCR内容：
# KKKKhazix / khazix-skills

## 文件列表

- **aihot**
  - chore(aihot): sync SKILL.md (items cache 60s→300s) - 14 minutes ago
- hv-analysis
  - Add hv-analysis skill for horizontal-vertical deep research - last month
- khazix-writer
  - Replace kaizike-writer with updated khazix-writer skill - last month
- neat-freak
  - Fix Codex skills path in neat-freak references and update ... - last week
- prompts
  - Add 横纵分析法 deep research prompt and update README - last month
- .gitignore
  - Untrack internal CLAUDE.md from public repo - last week
- LICENSE
  - Update README: professional skill description, installation... - last month
- README.en.md
  - feat(aihot): add aihot skill (iter-2 100% pass) - 29 minutes ago
- README.md
  - feat(aihot): add aihot skill (iter-2 100% pass) - 29 minutes ago

## 关于

数字生命卡兹克开源的AI Skills 合集

- Readme
- MIT license
- Activity
- 8.3k stars
- 43 watching
- 1.2k forks

## Releases

- 3 tags
- Create a new release

## Packages

- No packages published
- Publish your first package

-->



AIHOT Skill装上之后，你就不用再打开浏览器，不用再去刷网站，甚至不用再去想今天AI圈到底发生了什么。

你就跟你的Agent说一句话就行了。

我再花点篇幅，说说它到底能干什么。

第一个，AI日报。

AIHOT每天北京时间早上八点，会自动生成一份当天的AI日报。这个日报是我的系统从几百个信源里抓取、筛选、去重、打分、分类之后，最后精选出来的。

比如你早上起来，打开Claude Code或者OpenClaw，跟它说一句“给我一份今天的AI日报”，它就会自动触发AIHOT Skill，把当天的AI日报拉下来，整理成一份中文简报，直接摆在你面前。

![图片](https://mmbiz.qpic.cn/mmbiz_png/2jjfQoZLoqVcUoWBdMgQEGxgOe63icpkOVibc539T8PiaXVtMeSfHasYTwNglHdicDyg1q5sU8ktkaHmpB4nmTJP5PTjoUTyVd8LWOsB0A0DYia4/640?wx_fmt=png&from=appmsg)

<!-- OCR内容：
sandbox · opus.4.7[1m] · 5% · $0.71
►► bypass permissions on (shift+tab to cycle)

-->



日报分五个版块，模型发布/更新、产品发布/更新、行业动态、论文研究、技巧与观点。每条都有中文标题、一句话摘要、信息来源、还有原文链接，你感兴趣的点进去就能看原文。

30秒，你就知道昨天整个AI行业发生了什么。

而且你不光能看今天的日报，你还可以看前几天的。

比如你可以跟Agent说看一下5月6号的AI日报，它就直接给你拉那天的，这个其实我自己很喜欢的场景，我觉得非常适用于周末的。

就是周末我相信很多人肯定不咋看AI新闻或者消息，都是周日晚上或者周一早上再统一看下，这时候，你就可以周一早上说一句“给我总结一下最近三天的AI日报”，他就刷刷刷全出来了，还挺快的。

![图片](https://mmbiz.qpic.cn/sz_mmbiz_png/2jjfQoZLoqXqmkqOhqF42vicWH6SuE5tXBAGWDzrKR9esJ9LFUWRTtmmq3r0uoyRHWo1fJ4kibDK1fgPJaLuO523g94WfP2Qn1gicA7AMeCYgE/640?wx_fmt=png&from=appmsg)

<!-- OCR内容：
# AI HOT 三日要闻 · 2026-05-05 ~ 05-07

| 主题 | 内容 | 日期 |
|---|---|---|
| 模型发布/更新 | 1. GPT-5.5 Instant 成为 ChatGPT 默认模型，更准更少幻觉，个性化控制更强。 | 5/6 |
|  | 2. Grok 4.3 上线 XAI API，1M token 上下文，输入$1.25 / 输出 $2.5。Artificial Analysis 工具调用与指令遵循榜首。 | 5/6 |
|  | 3. Gemma 4 系列发布 MTP 草稿模型，推理 3 倍加速无损。 | 5/6 |
|  | 4. 商汤 SenseNova-U1 开源 8 步蒸馏 LORA。推理时间 100 NFE → 8 NFE，H100 上从 23 秒降到 2 秒，已支持 ComfyUI。 | 5/7 |
|  | 5. 字节豆包 Doubao-Seed-2.0-lite 升级为全模态理解模型。原生支持视频/图像/音频/文本联合推理，多项基准超 Gemini-3.1-Pro。 | 5/7 |
| 产品发布/更新 |  |  |

-->



第二个能力是精选模式。

精选和日报的区别是，日报是按版块打包好的成品，像一份报纸，并且有自己的最大条数限制，精选是AI从所有的信息中挑出来的值得关注的信息，同时以原始的时间流呈现，像一个Feed。

精选模式，也是整个AIHOT最核心的模式，当你不明确的说压迫日报或者全部之类的话的时候，都会默认以精选的数据源来进行回答。

这块其实就看你自己的需求，如果只是每天早上快速扫一眼大事就看日报，想要不漏掉任何高质量条目就看精选就行了。

![图片](https://mmbiz.qpic.cn/mmbiz_png/2jjfQoZLoqXA5CP6b5VcILdkpCkxO5mZww2gmyvKNdJK5wr86tch06cssPKlqutZk6x5RQ8OSlo9BuDVYQ5iczeavfTGdictZXzSAz4ukC2fo/640?wx_fmt=png&from=appmsg)

<!-- OCR内容：
**5月8日**

**04:36**

**Luma @LumaLabsAI**
你已有标语。现在将其变为广告。
输入你的标语。定义美学风格。Luma Agents 将据此构建广告。
赋予它生命 → http://lumalabs.ai/app

[视频播放器截图]

**智能体** **产品更新** **视频**

推荐理由: Luma这个Agent把广告制作从找团队、写脚本变成输入口号直接出片,小品牌和营销人应该会狂点头。

**04:30**

**Simon Willison 博客**
**GitHub Repo Stats**
作者开发了一个名为“GitHub Repo Stats”的在线工具,用于解决GitHub移动端网站不显示仓库提交次数的问题。用户只需输入GitHub仓库的URL或“foo/bar”格式的仓库ID,该工具便会通过REST或GraphQL API获取并展示仓库的关键统计数据,其中首要指标就是提交总数。工具已提供实际示例,如查看simonw/datasette和simonw/llm这两个仓库的详细数据。

**GitHub** **产品更新** **部署/工程**

推荐理由: Simon 这个 GitHub Repo Stats 工具虽小,但直接解决了移动端看不了commit 数这个真实痛点,做开源评估的开发者可以立刻用起来。

**04:10**

**OpenAI @OpenAI**
Codex现可直接在macOS和Windows的Chrome中运行。
它在处理Chrome中的应用和网站时表现更佳,并能在后台跨标签页并行工作,而不会占用浏览器控制权。
要开始使用,请在Codex应用中安装Chrome插件。

---

**主要视觉信息:**

图片展示了一个信息流界面，包含三条动态更新，分别来自 Luma、Simon Willison 和 OpenAI。每条动态都包含发布者信息、时间戳、内容描述以及推荐理由。

*   第一条动态（04:36）是关于 Luma 的一个广告生成工具，提供了一个链接和一段视频截图。
*   第二条动态（04:30）是关于 Simon Willison 开发的一个名为“GitHub Repo Stats”的在线工具，用于查看 GitHub 仓库的提交统计数据。
*   第三条动态（04:10）是关于 OpenAI 的 Codex 工具，宣布其可以在 macOS 和 Windows 的 Chrome 浏览器中运行，并提升了处理网页应用和后台任务的性能。

每条动态旁边都有“精选”标签和点赞、评论、收藏等互动按钮。

-->



第三个能力，按时间窗口或分类查。

有时候你觉得日报或者精选的信息量还不够，就想看某一个方向的全部的动态。

那再往上，其实还有全量的AI相关的所有信息，这些只不过为了保护注意力，他们可能没有被精选选中而已，但是不代表他们没有价值。

![图片](https://mmbiz.qpic.cn/sz_mmbiz_png/2jjfQoZLoqVYafic8wibjoafWoX44WerRe3BGoJ2CUiaZb8LlYXl1fHVRIkRYhlOutsdLDYdFvY1BK2aJojKFpEhzN3qCKSyibohRLoRl9y8EtA/640?wx_fmt=png&from=appmsg)

<!-- OCR内容：
- **04:31**
    - **Chubby @kimmonismus**
        - **AlphaEvolve优化TPU设计,展现AI实际应用潜力**
            - 作者认为关注AI在实际应用中的优势更令人兴奋,并以Google的AlphaEvolve为例。AlphaEvolve是一个基于Gemini的编码智能体,自2025年起被用于优化下一代TPU的设计。它在两天内就发现了更高效的缓存替换策略,而此前这需要人类团队数月的密集工作。这类实例印证了AI能力呈指数级增长的观点,表明AI已在硬件、软件等各个领域推动进步。
        - **Improving Al infrastructure**
        - **Google 大佬观点 部署/工程**

- **04:30**
    - **Simon Willison 博客**
        - **GitHub Repo Stats**
            - 作者开发了一个名为“GitHub Repo Stats”的在线工具,用于解决GitHub移动端网站不显示仓库提交次数的问题。用户只需输入GitHub仓库的URL或“foo/bar”格式的仓库ID,该工具便会通过REST或GraphQL API获取并展示仓库的关键统计数据,其中首要指标就是提交总数。工具已提供实际示例,如查看simonw/datasette和simonw/llm这两个仓库的详细数据。
        - **GitHub 产品更新 部署/工程**
        - **推荐理由:** Simon 这个 GitHub Repo Stats 工具虽小,但直接解决了移动端看不了 commit 数这个真实痛点,做开源评估的开发者可以立刻用起来。

- **04:30**
    - **Simon Willison 博客**
        - **大文字工具**
            - Simon Willison 为配合其“氛围编码”macOS演示工具,快速开发了一个名为“Big Words”的网页工具。该工具通过URL参数(如文本、渐变、大小)动态生成可自定义的幻灯片页面,解决了演示工具仅能嵌入URL内容的需求。用户可双击页面访问设置表单,自由调整文本内容、颜色、背景(支持纯色或渐变)、字体、字重、大小等多种视觉选项。所有设置均可生成一个特定的URL...
        - **开源/仓库 部署/工程**

-->



这时候，你可以Agent说，比如"看看全部消息，列出所有的新模型发布"，它就给你拉AI模型所有的相关条目了，你说"看看最近3天所有的AI产品发布"，它就给你筛所有产品方向的。

类别跟日报一样，支持这五个分类，模型发布、产品发布、行业动态、论文研究、技巧与观点。

当然，你也可以指定时间范围，比如你也可以说，过去24小时AI行业有啥大新闻。

![图片](https://mmbiz.qpic.cn/mmbiz_png/2jjfQoZLoqWRB82hmJBiayeKzb2mMdG9qTmDPLeUkmdJUvgjO6jF5WvONeKOdgzhSianibwpKG5HrCVN2ibo5w1VauBwQqTk0uPQaQvaicJHicfgs/640?wx_fmt=png&from=appmsg)

<!-- OCR内容：
过去24小时AI行业有啥大新闻

Skill (aihot)
Successfully loaded skill

Running 2 shell commands...
$ UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
since=$(date -u -v-1d +%Y-%m-%dT%H:%M:%SZ)
echo "since=$since"
curl -sH "User-Agent: $UA" "https://aihot.virxact.com/api/public/items?mode=selected&since=$since&take=100" \
|...
* Flowing... (27s ↑ 1.2k tokens)

sandbox opus.4.7[1m] 4% $0.64
►► bypass permissions on (shift+tab to cycle)

-->



1分钟左右，你就可以得到非常详细且准确的信息了。

当然，默认也会使用精选的信息进行回答，保护你我的注意力。

![图片](https://mmbiz.qpic.cn/mmbiz_png/2jjfQoZLoqWqDfUBebA6eXOXsAk3f49x3qMibSR3TIcHPcFUECxbfD2QKpmVeWzcOFYRqdS2xczQ85GsMHKpiaBvDBX8hAVOZdyYg4QCbQ8ibk/640?wx_fmt=png&from=appmsg)

<!-- OCR内容：
## 过去24小时AI行业大新闻 (2026-05-07 ~ 2026-05-08 北京时间)

### 模型发布/更新

1.  **OpenAI 新一代语音模型上线 API** - OpenAI 官网 (5/7 18:00)
    *   实时语音模型支持推理、翻译、语音转录，为开发者构建自然语音应用提供新工具。
    *   链接：`https://openai.com/index/advancing-voice-intelligence-with-new-models-in-the-api`

2.  **蚂蚁百灵 Ling-2.6-1T 万亿参数旗舰指令模型上线 OpenRouter** - 蚂蚁百灵 (5/7 23:06)
    *   主打“快速思考”，AIME26 / SWE-bench Verified 顶尖性能，同时成本降低约 75%，面向编程、复杂推理、大规模 Agent 工作流。
    *   链接：`https://x.com/AntLingAGI/status/2052404630488023536`

### 产品发布/更新

3.  **Claude for Excel/PowerPoint/Word 全面 GA, Outlook 公测** - Claude Blog (今天凌晨 02:41)
    *   跨四大微软应用保持上下文连续：Outlook 分类邮件起草回复、Excel 改数据后 PowerPoint 图表与 Word 同步更新；企业可通过 M365 管理中心统一部署。Microsoft 365 Copilot 用户可直接调用 Claude。
    *   链接：`https://claude.com/blog/collaborate-with-claude-across-excel-powerpoint-word-and-outlook`

4.  **ChatGPT 开始测试广告** - OpenAI 官网 (5/7 08:00)
    *   OpenAI 首次在 ChatGPT 内测广告以支撑免费服务，强调广告会有明确标识、不影响回答独立性，配套隐私保护。商业模式重大转向。
    *   链接：`https://openai.com/index/testing-ads-in-chatgpt`

5.  **Gemini 3.1 Flash Lite 上线 OpenRouter** - OpenRouter (5/8 01:43)
    *   多模态（文本/图像/视频/音频/PDF→文本），100 万上下文，可选思考层级。输入 $0.25/M tokens、输出 $1.50/M。
    *   链接：`https://x.com/OpenRouter/status/2052444130287210984`

6.  **Perplexity 推出 Mac 版 Personal Computer 应用** - Perplexity (5/8 01:48)
    *   面向所有用户开放，可跨本地文件、原生 Mac 应用、网络与 Perplexity 安全服务器执行任务。
    *   链接：`https://x.com/perplexity_ai/status/2052445405754040816`

7.  **ChatGPT 上线 Trusted Contact 功能** - OpenAI 官网 (5/7 08:00)
    *   检测到严重自残风险时，主动通知用户预先设定的可信联系人，是一项可选安全功能。
    *   链接：`https://openai.com/index/introducing-trusted-contact-in-chatgpt`

8.  **Google 翻译耳机实时同传，支持 70+ 语言**

-->



时间窗口最长支持7天，因为再往前的数据量就太大了，同时我也是为了保护一下我这个脆弱的土豆服务器，真的怕量一大，直接给我干崩了。。。

第四个能力，按关键词查。

这块我把搜索也做了进去，可以支持一些基本的搜索，比如最近XXX产品更新了哪些新功能、OpenAI发布了哪些新模型之类的。

这块正好也可以给大家看一下有skill和没skill在信息时效性上的对比。

比如我问，OpenAI 最近发了什么。

装了skill的情况下，会非常的详细且全面，凌晨刚刚发布的语音模型也抓进去了。

![图片](https://mmbiz.qpic.cn/mmbiz_png/2jjfQoZLoqVzTr90fZiceJicdzoLSibrezyuY43IsdMcDFtDCDicCz9sM8N8JDpBIJZcvN8R3JVCZEAFaxuAfibibaS4OSPXun0CBxFDo9Kz9vyyI/640?wx_fmt=png&from=appmsg)

<!-- OCR内容：
response.md
**OpenAI 最近发了什么**(时间窗 2026-05-05 ~ 2026-05-07,按发布时间倒序)

## 模型发布/更新
1. **GPT-Realtime-2 系列三款实时语音模型上线 API** OpenAI 官网(5/7 18:00)
GPT-Realtime-2 具备 GPT-5级推理能力,让语音助手能在对话中实时思考;GPT-Realtime-Translate 支持70+种输入语言到 13 种输出语言的实时翻译;GPT-Realtime-Whisper 提供流式语音转文本。
128K 上下文窗口,可调推理强度。
https://openai.com/index/advancing-voice-intelligence-with-new-models-in-the-api
2. **GPT-5.5 Instant 成为 ChatGPT 默认模型** OpenAI 官网 (5/5 18:00)
更智能、更准确,幻觉显著减少,个性化控制更强。这是ChatGPT 默认对话模型的一次小版本升级。
https://openai.com/index/gpt-5-5-instant

## 产品发布/更新
3. **ChatGPT 上线 Excel 和 Google Sheets 插件**X: ChatGPT 官号(5/6 05:28)
能直接在表格里分析杂乱数据、写公式、改单元格,并解释操作过程。由GPT-5.5驱动。
https://x.com/ChatGPTapp/status/2051776032127238266
4. **ChatGPT 推出"可信联系人"安全功能** OpenAI 官网(5/7 08:00)
当系统检测到用户存在严重自残风险时,会主动通知预先设定的可信联系人——通过社交支持给心理危机用户多一道兜底。
https://openai.com/index/introducing-trusted-contact-in-chatgpt
5. **ChatGPT 开始测试广告 + 推出广告自助平台** OpenAI 官网 (5/5~5/7)
两步走:先在 ChatGPT 里小范围测带标识的广告(强调不影响回答独立性),同时上线自助广告管理平台测试版,支持CPC竞价。这是ChatGPT 商业化的一个明确信号。
https://openai.com/index/testing-ads-in-chatgpt
https://openai.com/index/new-ways-to-buy-chatgpt-ads
6. **Agents SDK TypeScript 版更新** X: OpenAI Developers (5/6 02:05)

-->



并且都有源地址，你可以随意进行跳转。

如果没装，就是这样的。

![图片](https://mmbiz.qpic.cn/sz_mmbiz_png/2jjfQoZLoqWFibU1BlbBQkKScFSCeDE0Cz3E6NaasSiaQYibQjUib9YibIu6OYNErianNyAmxibpLUG1iaUvOgWtMUkunHx7QNFoYdbdGXytiaia4tHXA/640?wx_fmt=png&from=appmsg)

<!-- OCR内容：
response.md

# OpenAI 最近一周动作

- **5/5 GPT-5.5 Instant**: 替代 5.3 成 ChatGPT 默认模型,高风险领域(法律/医疗/金融)幻觉率降 52.5%,延迟不变
- **个性化升级**: 可调用历史对话、文件、Gmail 给个性化回答(Plus/Pro 先开)
- **5/6 ChatGPT Futures: Class of 2026** 项目发布
- **4/30 Advanced Account Security**: 抗钓鱼登录 + 自动排除训练

Sources:
- [GPT-5.5 Instant](https://openai.com/index/gpt-5-5-instant/)
- [TechCrunch](https://techcrunch.com/2026/05/05/openai-releases-gpt-5-5-instant-a-new-default-model-for-chatgpt/)
- [OpenAI News](https://openai.com/news/)

Download

-->



还是有一点奇怪和信息缺失的。

大概就是这样。

然后在输出格式上，我只做了最基础的md，因为我觉得我就直接提供一个数据源得了，反正skills大家能随便改的，你们想输出成啥样的，或者嵌入到什么工作流中，就交给大家自己后续优化吧～

希望AIHOT这个skill，能让你的Agent多了一双眼睛，帮你盯着整个AI行业的新闻。

然后说说RSS。

这个其实是给那些不用Agent、但是日常用RSS阅读器的朋友准备的。很多技术圈的老哥习惯用Feedly、Inoreader、NetNewsWire这些工具来管理信息流，RSS对他们来说是最自然的接入方式。

我开放了三个Feed，精选动态、全部AI动态、AI日报，你挑你需要的订阅就行，Feed地址在Agent接入页面都能看到，一键复制。

![图片](https://mmbiz.qpic.cn/mmbiz_png/2jjfQoZLoqW2vNjPN45jdguDLU1gZeIUibrXqdpNMqL4IEXyU3STLj0kicIb7q2ToRBdZWMSQbVdcKQibhJF5kFJeMAUSoWZoVN9HapHHGZ12Y/640?wx_fmt=png&from=appmsg)

<!-- OCR内容：
# Skill

* 任意 Agent
* SKILL.md 标准

# RSS

* 任意 RSS reader
* 零配置订阅

# REST API

* 开发者
* 自定义集成
* OpenAPI 3.1

---

# RSS · 任意 READER 订阅

## 三个 feed, 按需订阅

所有主流 RSS reader (Feedly / Inoreader / NetNewsWire / Reeder / Reader.app / Tiny Tiny RSS...) 都能直接订阅。复制下面的 URL 粘到 reader 里即可。

### 三个 feed

#### AI HOT - 精选

* 每日精编候选池, 最新 50 条
* 推荐订阅 - 大多数人想要的 就是这个

`https://aihot.virxact.com/feed.xml`

#### AI HOT - 全部 AI 动态

* 抓取的全部 AI 行业内容流, 最新 50 条
* 信息密度高 / 想自己筛选的人

`https://aihot.virxact.com/feed/all.xml`

#### AI HOT 日报

* 每天 08:00 北京时间发布的精编日报, 最新 30 期
* 每天一条, 最适合阅读类 RSS reader

`https://aihot.virxact.com/feed/daily.xml`

-->



然后就是API了，有很多朋友说想集成到自己公司的一些内部系统里或者其他的一些工具，想了想，也还是给大家开放了。

如果你自己在做AI相关的产品，想把AIHOT的数据接到你自己的系统里，就可以直接调API。

![图片](https://mmbiz.qpic.cn/sz_mmbiz_png/2jjfQoZLoqXNkjzibIkicEDxuudm98ibzPcLvXpjXqmMKsrxFgXzBzJbeuGsXUkR4DGL8ITx23W6cqhTTOdamzpOQhyvqKWGJ0fZYUAexhORVE/640?wx_fmt=png&from=appmsg)

<!-- OCR内容：
bash
# 先决条件:必须带 User-Agent(默认 curl UA 会被 403)
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

-->



完整的OpenAPI规范文档我也尽可能让AI写的详细了，但是这块我提前说一个风险，因为我确实没有自己开放过API，也确实看不太懂，这块完全是完全我说了自己需求和一些风控问题之后，Agent自己处理的，所以API这块我心理真的没啥底。

如果大家接入过程有BUG，在查了文档之后，是非常规情况下的报错，可以再提交反馈页面报一下，我让Agent来去修= =

三种接入方式，覆盖三种不同的人群。

总之，我希望的就是这玩意，能对大家有用，也希望不管你用什么姿势获取信息，AIHOT都能对你真的有一点点帮助。

也希望，我能给这个互联网。

留下一点点，自己的印记。

******以上，既然看到这里了，如果觉得不错，随手点个赞、在看、转发三连吧，如果想第一时间收到推送，也可以给我个星标⭐～谢谢你看我的文章，我们，下次再见。******

>/ 作者：卡兹克

>/ 投稿或爆料，请联系邮箱：wzglyay@virxact.com
