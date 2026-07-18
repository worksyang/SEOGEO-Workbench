(() => {
  'use strict';

  const statusMeta = {
    draft: { label: '待启动', tone: 'muted' },
    analyzing: { label: 'Agent 分析中', tone: 'blue' },
    generating: { label: '生成中', tone: 'green' },
    partial: { label: '部分完成', tone: 'orange' },
    completed: { label: '已完成', tone: 'gray' },
    paused: { label: '已暂停', tone: 'muted' },
    archived: { label: '已归档', tone: 'muted' },
    queued: { label: '排队中', tone: 'muted' },
    routing: { label: '路由中', tone: 'blue' },
    ready: { label: '待生成', tone: 'blue' },
    running: { label: '生成中', tone: 'green' },
    done: { label: '已完成', tone: 'gray' },
    failed: { label: '失败', tone: 'red' },
    blocked: { label: '阻塞', tone: 'red' },
    waiting: { label: '等待生成', tone: 'muted' }
  };

  const frameworkNames = {
    product: '产品写作框架',
    financing: '保费融资写作框架',
    dividend: '分红实现率写作框架',
    comparison: '产品横评写作框架',
    blackbox: '条款黑盒写作框架',
    insurer: '保司写作框架',
    general: '通用问题写作框架'
  };

  const anglePool = [
    ['产品误判与风险测评', '先承认吸引力，再拆开最容易误听成承诺的数字。'],
    ['家庭现金流适配判断', '把产品放回资金期限、提领节奏和家庭账本。'],
    ['关键规则与隐藏条件', '从客户看不见的条款、银行或执行条件切入。'],
    ['横向取舍与选择边界', '用两个相邻选项制造坐标，说明谁适合什么需求。'],
    ['热点问题的买前审计', '承接热点关键词，但不暴露搜索动作，回到买前判断。']
  ];

  const demoDateLabel = '2026-07-17';
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  function escapeHtml(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function safeMediaUrl(value) {
    const url = String(value || '').trim();
    if (!url || /^(javascript|vbscript):/i.test(url)) return '';
    if (/^data:/i.test(url) && !/^data:image\//i.test(url)) return '';
    return url;
  }

  function configureMarked() {
    if (!window.marked) return;
    window.marked.setOptions({ breaks: true, gfm: true });
    window.marked.use({
      renderer: {
        image(token, titleArg, textArg) {
          const objectMode = token && typeof token === 'object';
          const href = safeMediaUrl(objectMode ? token.href : token);
          const title = objectMode ? token.title : titleArg;
          const text = objectMode ? token.text : textArg;
          if (!href) return '<div class="markdown-image-failed">图片链接不可用</div>';
          return `
            <figure class="markdown-image">
              <img src="${escapeHtml(href)}" alt="${escapeHtml(text || 'Markdown 图片')}" loading="lazy" data-action="open-lightbox" data-image-src="${escapeHtml(href)}" data-image-title="${escapeHtml(text || title || 'Markdown 图片')}">
              <span class="markdown-image-fallback">图片加载失败，可在 Markdown 源码中修改链接。</span>
              ${(text || title) ? `<figcaption>${escapeHtml(text || title)}</figcaption>` : ''}
            </figure>
          `;
        }
      }
    });
  }

  function sanitizeMarkdownHtml(html) {
    const template = document.createElement('template');
    template.innerHTML = html;
    template.content.querySelectorAll('script, iframe, object, embed, style, link, meta, form').forEach(node => node.remove());
    template.content.querySelectorAll('*').forEach(node => {
      Array.from(node.attributes).forEach(attribute => {
        const name = attribute.name.toLowerCase();
        const value = attribute.value.trim();
        if (name.startsWith('on')) node.removeAttribute(attribute.name);
        if ((name === 'href' || name === 'src') && /^(javascript|vbscript):/i.test(value)) node.removeAttribute(attribute.name);
      });
      if (node.tagName === 'A') {
        node.setAttribute('target', '_blank');
        node.setAttribute('rel', 'noreferrer');
      }
    });
    return template.innerHTML;
  }

  function fallbackMarkdown(markdown) {
    const escaped = escapeHtml(markdown);
    return escaped
      .replace(/^### (.+)$/gm, '<h3>$1</h3>')
      .replace(/^## (.+)$/gm, '<h2>$1</h2>')
      .replace(/^# (.+)$/gm, '<h1>$1</h1>')
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/^&gt; (.+)$/gm, '<blockquote><p>$1</p></blockquote>')
      .split(/\n{2,}/)
      .map(block => /^<(h[1-3]|blockquote)/.test(block) ? block : `<p>${block.replace(/\n/g, '<br>')}</p>`)
      .join('');
  }

  function renderMarkdown(markdown) {
    let prepared = String(markdown || '');
    const noteSlots = [];
    prepared = prepared.replace(/<!--\s*(OCR内容|插图建议)\s*[:：]?\s*([\s\S]*?)-->/g, (_, kind, body) => {
      const index = noteSlots.push({ kind, body: body.trim() }) - 1;
      return `<div data-markdown-note="${index}"></div>`;
    });
    prepared = prepared.replace(/\[\[([^\]]+)\]\]/g, (_, link) => {
      const parts = link.split('|');
      return `<span class="wiki-link">[[${escapeHtml((parts[1] || parts[0]).trim())}]]</span>`;
    });
    const raw = window.marked ? window.marked.parse(prepared) : fallbackMarkdown(prepared);
    const template = document.createElement('template');
    template.innerHTML = sanitizeMarkdownHtml(raw);
    noteSlots.forEach((slot, index) => {
      const target = template.content.querySelector(`[data-markdown-note="${index}"]`);
      if (!target) return;
      const aside = document.createElement('aside');
      aside.className = `markdown-note ${slot.kind === '插图建议' ? 'suggestion' : 'ocr'}`;
      aside.innerHTML = `<b>${slot.kind}</b><div>${slot.kind === 'OCR内容' && window.marked ? sanitizeMarkdownHtml(window.marked.parse(slot.body)) : escapeHtml(slot.body).replace(/\n/g, '<br>')}</div>`;
      target.replaceWith(aside);
    });
    return template.innerHTML;
  }

  function slugify(value) {
    return String(value || '')
      .replace(/[\\/:*?"<>|]/g, '')
      .replace(/\s+/g, '_')
      .slice(0, 28) || '未命名';
  }

  function sourceMarkdownFor(path, task, index) {
    const fileName = path.split('/').pop() || path;
    if (fileName === '保诚.md') {
      return `# 保诚保险保司母页

> **Demo Wiki 资产**：用于展示 Agent 选材预览，不代表真实条款、产品承诺或最新经营数据。

## 页面用途

这份母页负责回答三个问题：

1. 写保诚相关内容时，哪些判断属于**保司层面**；
2. 哪些内容必须回到具体产品、计划书或官方披露；
3. 不能把旧产品表现直接外推成新产品承诺。

## 写作时可直接取用的判断

| 判断层 | 可写内容 | 必须补证据 |
| --- | --- | --- |
| 保司层 | 品牌历史、业务定位、产品线结构 | 最新官方资料 |
| 产品层 | 保证与非保证利益的结构 | 对应版本计划书 |
| 客户层 | 现金流目标与风险承受能力 | 客户自己的方案 |

![分红实现率阅读框架](assets/dividend-reading-guide.svg)

## 关键门禁

- 不把“过往表现”写成“未来一定如此”；
- 不混用不同年代、不同产品、不同红利口径；
- 如果关键词是“分红实现率”，必须同时调用 [[分红实现率判断模型]]；
- 结论要回到读者的持有期限、提领需求和风险边界。

<!-- 插图建议:
保留上方四象限图，帮助用户从口径、时间、产品和目标四个维度阅读分红实现率。
-->

## 可引用结论

> 单一百分比只能作为线索，不能单独完成产品判断。先核对口径，再看持续性，最后回到具体产品与家庭目标。`;
    }

    if (fileName.includes('分红实现率判断模型')) {
      return `# 分红实现率判断模型

> **Demo 认知资产**：给 Agent 一个稳定的判断顺序，避免文章只围绕“高于或低于 100%”下结论。

## 一、先确认数字在比较什么

同样叫“实现率”，可能对应不同红利类型、不同保单批次与不同计算方式。**口径不一致，数字就不能直接横比。**

## 二、再看时间序列

| 观察方式 | 容易产生的误判 | 更好的问题 |
| --- | --- | --- |
| 只看单年 | 把一次波动当成长期能力 | 多年趋势是否持续偏离 |
| 只看平均值 | 掩盖极端年份 | 最差年份对目标影响多大 |
| 只看保司 | 忽略具体产品差异 | 当前产品与历史样本是否同类 |

![四维阅读框架](assets/dividend-reading-guide.svg)

## 三、最后回到客户目标

- 如果客户依赖固定年份提领，波动的影响会更直接；
- 如果资金期限足够长，短期波动未必等于方案失败；
- 如果结论会影响投保，必须回到具体计划书重新测算。

## Agent 使用规则

1. 先解释数字的口径；
2. 再解释它能说明什么、不能说明什么；
3. 最后给出客户可执行的核对清单；
4. 禁止把历史数据写成未来承诺。`;
    }

    if (fileName.includes('保费融资') && path.includes('产品母页')) {
      return `# 保诚世誉财富 × 集友银行保费融资

> **Demo 产品母页**：仅用于验证批量成稿的选材、Markdown 渲染和编辑流程。

## 这份资产解决什么问题

它不负责告诉读者“融资一定更划算”，而是把产品、贷款与家庭现金流拆成三个相互独立的判断对象。

![保费融资判断路径](assets/premium-financing-map.svg)

## 需要分别核对的事实

| 模块 | 需要读取 | 写作边界 |
| --- | --- | --- |
| 保险计划 | 保证 / 非保证利益、退保价值、提领安排 | 以对应版本计划书为准 |
| 融资安排 | 利率、期限、续贷、抵押与追缴条件 | 以银行最终批核为准 |
| 家庭现金流 | 首期资金、利息来源、备用资金 | 不用演示收益替代压力测试 |

## 最容易被误解的地方

- 把“融资后投入金额变大”误写成“收益一定变高”；
- 忽略利率、汇率与续贷条件会变化；
- 只展示顺风情景，不展示提前退出与现金流紧张情景。

## 可引用结论

> 保费融资首先是负债安排，其次才是资产配置工具。能否承受坏情景，比演示中的漂亮数字更重要。`;
    }

    if (fileName.includes('保费融资写作框架')) {
      return `# 保费融资写作框架

## 路由目的

当关键词包含“融资、贷款、杠杆、首期保费”时，文章必须同时处理**吸引力、成本和坏情景**，不能只写放大效果。

## 推荐叙事顺序

1. 承认融资方案为什么吸引人；
2. 拆开保险账户与银行贷款；
3. 把利率、汇率、续贷和退保放进压力测试；
4. 给出适合与不适合的家庭画像；
5. 用低门槛问题收口，而不是催促成交。

## 思维门禁

- 演示收益不等于承诺；
- 银行批核条件不等于长期固定条件；
- 杠杆不创造确定性，只改变收益与风险的分布；
- 没有家庭现金流数据时，不给个性化结论。

![保费融资判断路径](assets/premium-financing-map.svg)

## 输出要求

正文至少出现一次坏情景推演，并明确“需以保险计划书、银行批核与个人情况为准”。`;
    }

    if (fileName.includes('条款黑盒')) {
      return `# 条款黑盒写作框架

## 核心任务

把用户只看到结果、看不到条件的机制拆开。重点不是罗列条款，而是回答：**什么情况下结果会变化？**

## 写作路径

- 先写用户最关心的结果；
- 再找决定结果的时间点、条件与例外；
- 把看不见的执行条件翻译成家庭现金流语言；
- 给出核对清单，避免“听懂了概念，却不会判断”。

## 禁区

不把演示表当条款，不把个案当通用结论，不跳过版本差异。`;
    }

    return `# ${fileName.replace(/\.md$/, '')}

> **Demo Wiki 资产**：这是“${task.keyword}”任务选中的第 ${index + 1} 份素材，可在右侧直接预览和编辑。

## 资产角色

${index === 0 ? '提供与关键词对象直接相关的产品或主题事实。' : index === 1 ? '提供底层判断、风险边界与写作门禁。' : '补充对比坐标、案例或客户需求视角。'}

## 可复用内容

- 先回答读者真正要解决的问题；
- 关键判断必须能回到来源；
- 不把漂亮数字、单一案例或历史表现写成承诺；
- 结论要落回家庭现金流与适配边界。

## 与本任务的关系

关键词：**${task.keyword}**

主框架：**${frameworkNames[task.framework] || task.framework}**

Agent 选择理由：${index === 0 ? '这是最接近关键词对象的主要事实源。' : '它能防止成稿退化成单纯的产品介绍。'}`;
  }

  function buildSourceDocuments(task) {
    return task.sourceRefs.map((path, index) => ({
      id: `${task.id}-source-${index + 1}`,
      path,
      title: (path.split('/').pop() || path).replace(/\.md$/, ''),
      role: index === 0 ? '主要事实与产品抓手' : index === 1 ? '底层判断与规则边界' : '补充对比或案例',
      reason: index === 0 ? '与关键词对象直接相关' : '用于避免文章只停留在产品介绍',
      markdown: sourceMarkdownFor(path, task, index)
    }));
  }

  function articleMarkdown(task, index, base, finalArticle) {
    const title = index === 0 ? `${task.keyword}：买前最容易误判的那个变量` : `${task.keyword}：${base[0]}`;
    if (!finalArticle) {
      return `# ${title}

> 当前为 Agent 已生成的文章计划，正文尚在自动生产中。

## 核心角度

${base[1]}

## 素材组合

${task.sourceRefs.map(ref => `- [[${(ref.split('/').pop() || ref).replace(/\.md$/, '')}]]`).join('\n')}

## 拟定结构

1. 用读者熟悉的吸引力进入；
2. 拆开最容易被忽略的变量；
3. 用压力测试或对比坐标完成判断；
4. 给出适合与不适合的边界；
5. 留下一个可继续沟通的具体问题。`;
    }

    if (task.framework === 'financing') {
      return `# ${title}

> 很多人第一次看到保费融资，先看到的是“用较少现金撬动更大保单”。但真正决定它值不值得做的，往往不是演示收益，而是家庭能不能长期扛住负债。

![保费融资判断路径](assets/premium-financing-map.svg)

## 先把两本账分开

第一本是保险账户：保证利益、非保证利益、退保价值与提领安排。第二本是银行账户：贷款本金、利息、期限、续贷与抵押条件。两本账可以互相影响，却不能混成一个“看起来更高的回报率”。

如果只看顺风情景，融资当然容易显得漂亮；但家庭真正需要回答的是：**利率上升、分红偏低或临时需要退出时，现金流还能不能接得住？**

## 三个必须提前问的问题

1. 首期资金和后续利息来自哪里，会不会挤压应急资金？
2. 如果融资条件变化，家庭是否有能力补充现金或降低杠杆？
3. 如果保险账户短期表现不及演示，是否仍能按原计划持有？

## 什么家庭更适合继续研究

通常不是“最想放大收益”的家庭，而是资产负债表清晰、现金流稳定、能理解长期持有与坏情景成本的家庭。相反，如果首期保费已经接近流动资金上限，或者需要依赖短期收入支付利息，就不该先被漂亮数字推动。

## 最后的判断

保费融资不是一个单独产品，而是保险计划、银行贷款和家庭现金流的组合。**先证明最坏情况扛得住，再讨论杠杆有没有意义。**

如果你正在看一份融资建议书，最值得先核对的不是最高演示值，而是利率上升一到两个档位后，每年真实需要拿出多少钱。

*本文为 Demo 成稿，不构成保险、贷款或投资建议；具体以正式计划书、银行批核和个人情况为准。*`;
    }

    if (task.framework === 'dividend') {
      return `# ${title}

> “分红实现率低于 100%，是不是就代表产品亏了？”这个问题看似只需要一个数字，实际上至少要拆开口径、时间、产品和家庭目标四层。

![分红实现率阅读框架](assets/dividend-reading-guide.svg)

## 100% 不是一条简单的及格线

实现率反映的是某一类非保证利益相对演示或目标的表现。不同产品、不同保单批次、不同红利类型可能使用不同口径，所以数字不能脱离说明直接横向比较。

## 单年低于 100% 说明什么

它说明当年实际派发与对应演示之间存在偏离，但不自动等于保单整体亏损，也不能单独证明未来一定更差。更有价值的观察是：偏离持续了多久、幅度多大、发生在哪类产品，以及是否影响你的提领目标。

## 真正要核对的四件事

1. 这个百分比对应哪一种红利；
2. 是单年波动，还是多年持续偏离；
3. 历史样本与当前产品是否属于同一类；
4. 即使表现偏弱，是否会破坏你的现金流计划。

## 买前与持有中的用法不同

买前，它更像保司历史管理能力的一条线索；持有中，它更像需要继续追踪的体检指标。无论哪种场景，都不该替代具体计划书和家庭目标。

## 结论

看到实现率，先别急着问“高不高”，先问“这个数字在比较什么”。把口径和时间序列弄清楚，才有资格进一步讨论产品是否适合。

*本文为 Demo 成稿，不构成产品承诺；实际判断需结合官方披露、具体计划书与个人情况。*`;
    }

    return `# ${title}

> 搜索“${task.keyword}”的人，通常不是缺一份产品介绍，而是缺一个能把吸引力、条件和家庭目标放在一起的判断框架。

## 为什么这个问题容易被误判

大家最先看到的往往是一个结果：更高的演示、更灵活的提领，或一个看起来很有吸引力的案例。但结果背后还有版本、时间点、执行条件和个人现金流，少看任何一层，都可能把“可以做到”误听成“一定做到”。

## 先核对三件事

1. 资料是否对应你正在看的产品与版本；
2. 关键数字是保证还是非保证；
3. 计划是否经得起收入变化、提前退出或提领节奏改变。

## 把产品放回家庭账本

真正的适配，不是产品看起来多强，而是它在你的资金期限、备用金和目标里是否仍然成立。先从坏情景倒推，往往比从最高演示顺推更接近真实答案。

## 结论

“${task.keyword}”值得研究，但不值得只凭一个数字下结论。把资料、条件和家庭目标对齐，才算完成买前判断。

*本文为 Demo 成稿，不构成任何产品或投资建议。*`;
  }

  function buildVariants(task) {
    const previous = new Map((task.variants || []).map(variant => [variant.id, variant]));
    return Array.from({ length: task.count }, (_, index) => {
      const base = anglePool[index % anglePool.length];
      const id = `${task.id}-article-${index + 1}`;
      const old = previous.get(id);
      const status = index < task.articlesDone ? 'done' : task.status === 'running' && index === task.articlesDone ? 'running' : 'waiting';
      const finalArticle = status === 'done';
      return {
        id,
        label: `${index === 0 ? '主稿' : `变体 ${index + 1}`} · ${base[0]}`,
        angle: base[0],
        brief: base[1],
        status,
        title: index === 0 ? `${task.keyword}：买前最容易误判的那个变量` : `${task.keyword}：${base[0]}`,
        markdown: old && old.userEdited ? old.markdown : articleMarkdown(task, index, base, finalArticle),
        userEdited: Boolean(old && old.userEdited)
      };
    });
  }

  function makeTask(input) {
    const task = {
      id: input.id,
      keyword: input.keyword,
      signal: input.signal || 'stable',
      signalText: input.signalText || '稳定搜索',
      count: input.count || 1,
      mode: input.mode || 'llm',
      status: input.status || 'queued',
      confidence: input.confidence ?? 0.86,
      framework: input.framework || 'product',
      sourceCount: input.sourceCount || 2,
      articlesDone: input.articlesDone || 0,
      title: input.title || input.keyword,
      sourceRefs: input.sourceRefs || [],
      traceStage: input.traceStage || '等待 Agent 分析',
      warning: input.warning || '',
      revisionNote: input.revisionNote || '',
      revisionCount: input.revisionCount || 1,
      sessionId: `claude-session-${input.id}`,
      routeVersion: input.routeVersion || 1,
      createdAt: '2026-07-17T09:20:00+08:00'
    };
    task.sourceDocuments = buildSourceDocuments(task);
    task.variants = buildVariants(task);
    return task;
  }

  configureMarked();

  const packages = [
    {
      id: 'pkg-hot-0717',
      name: '7 月 17 日高价值关键词生产',
      source: '微信关键词监控',
      sourceTone: 'purple',
      brief: '热点词优先快速覆盖，复杂机制型关键词自动切换深度 Agent；提示不阻断生成。',
      createdAt: '今天 09:20',
      updatedAt: '刚刚',
      outputDir: 'output/20260717_0920_永明心核传承_盛利2提领/',
      defaultMode: 'llm',
      status: 'partial',
      archived: false,
      tasks: [
        makeTask({
          id: 'kw-yongming',
          keyword: '永明心核传承',
          signal: 'rising',
          signalText: '持续上升',
          count: 3,
          mode: 'llm',
          status: 'done',
          confidence: 0.94,
          framework: 'product',
          sourceCount: 3,
          articlesDone: 3,
          sourceRefs: ['wiki/产品母页/永明星河尊享2.md', 'wiki/保司母页/永明.md', 'wiki/底层认知/港险产品-用户需求反查表.md'],
          traceStage: '已完成写作、保存 Markdown 并留下复盘证据'
        }),
        makeTask({
          id: 'kw-shengli',
          keyword: '盛利2提领',
          signal: 'hot',
          signalText: '高热度',
          count: 2,
          mode: 'agent',
          status: 'running',
          confidence: 0.89,
          framework: 'blackbox',
          sourceCount: 3,
          articlesDone: 1,
          sourceRefs: ['wiki/产品母页/安盛盛利2.md', 'wiki/底层认知/港险基础认知与计划书阅读.md', 'wiki/创作框架/条款黑盒写作框架.md'],
          traceStage: 'Claude Code -p 正在核对提领时点并生成第 2 篇'
        }),
        makeTask({
          id: 'kw-dividend',
          keyword: '保诚分红实现率',
          signal: 'stable',
          signalText: '稳定搜索',
          count: 1,
          mode: 'agent',
          status: 'done',
          confidence: 0.78,
          framework: 'dividend',
          sourceCount: 2,
          articlesDone: 1,
          sourceRefs: ['wiki/保司母页/保诚.md', 'wiki/底层认知/分红实现率判断模型.md'],
          traceStage: '已按口径差异提示完成成稿，提示随证据包保存',
          warning: '新旧产品口径可能存在差异；Agent 已在正文中降级表达并保留来源提示。'
        }),
        makeTask({
          id: 'kw-finance',
          keyword: '港险保费融资',
          signal: 'rising',
          signalText: '上升中',
          count: 1,
          mode: 'llm',
          status: 'ready',
          confidence: 0.97,
          framework: 'financing',
          sourceCount: 2,
          articlesDone: 0,
          sourceRefs: ['wiki/产品母页/保诚世誉财富 × 集友银行保费融资.md', 'wiki/创作框架/保费融资写作框架.md'],
          traceStage: 'RoutePlan 与写作包已编译，等待自动生成'
        })
      ]
    },
    {
      id: 'pkg-deep-0716',
      name: '复杂机制深度稿试跑',
      source: '手动创建',
      sourceTone: 'blue',
      brief: '复杂机制型关键词使用深度 Agent，保留关键决策对话、素材版本和最终 Markdown。',
      createdAt: '昨天 16:40',
      updatedAt: '昨天 18:12',
      outputDir: 'output/20260716_1640_保费融资_分红实现率/',
      defaultMode: 'agent',
      status: 'completed',
      archived: false,
      tasks: [
        makeTask({
          id: 'kw-premium-finance',
          keyword: '保诚世誉财富保费融资',
          signal: 'hot',
          signalText: '高热度',
          count: 2,
          mode: 'agent',
          status: 'done',
          confidence: 0.97,
          framework: 'financing',
          sourceCount: 2,
          articlesDone: 2,
          sourceRefs: ['wiki/产品母页/保诚世誉财富 × 集友银行保费融资.md', 'wiki/创作框架/保费融资写作框架.md'],
          traceStage: '已完成 2 篇成稿与 2 轮 Session 修改',
          revisionCount: 2
        }),
        makeTask({
          id: 'kw-compare',
          keyword: '分红实现率低于 100% 会亏钱吗',
          signal: 'rising',
          signalText: '上升中',
          count: 1,
          mode: 'agent',
          status: 'done',
          confidence: 0.84,
          framework: 'dividend',
          sourceCount: 3,
          articlesDone: 1,
          sourceRefs: ['wiki/底层认知/分红实现率判断模型.md', 'wiki/创作框架/分红实现率写作框架.md', 'wiki/底层认知/港险基础认知与计划书阅读.md'],
          traceStage: '成稿已完成，等待未来效果 API 回收',
          warning: '历史同类文章阅读较高但咨询偏低；本次成稿已强化结尾的具体核对问题。'
        })
      ]
    },
    {
      id: 'pkg-archive-0708',
      name: '7 月 8 日长尾补位',
      source: '微信关键词监控',
      sourceTone: 'purple',
      brief: '长尾词自动覆盖，结果已归档，等待后续效果回收。',
      createdAt: '7 月 8 日',
      updatedAt: '7 月 9 日',
      outputDir: 'output/20260708_1015_港险长尾补位/',
      defaultMode: 'llm',
      status: 'archived',
      archived: true,
      tasks: [
        makeTask({
          id: 'kw-long-tail',
          keyword: '每年提 5 万港险够养老吗',
          signal: 'stable',
          signalText: '稳定搜索',
          count: 2,
          mode: 'llm',
          status: 'done',
          confidence: 0.92,
          framework: 'general',
          sourceCount: 3,
          articlesDone: 2,
          sourceRefs: ['wiki/产品母页/港险提领功能横评.md', 'wiki/底层认知/港险基础认知与计划书阅读.md', 'wiki/案例故事/赵女士40岁养老金方案.md'],
          traceStage: '已归档'
        })
      ]
    }
  ];

  const state = {
    selectedPackageId: 'pkg-hot-0717',
    selectedTaskId: 'kw-yongming',
    packageFilter: 'active',
    packageQuery: '',
    taskFilter: 'all',
    taskSearch: '',
    detailTab: 'articles',
    reader: null,
    revisionTaskId: '',
    simulationTimers: []
  };

  function selectedPackage() {
    return packages.find(item => item.id === state.selectedPackageId) || packages[0];
  }

  function selectedTask() {
    const pkg = selectedPackage();
    return pkg.tasks.find(item => item.id === state.selectedTaskId) || pkg.tasks[0];
  }

  function findTaskWithPackage(taskId) {
    for (const pkg of packages) {
      const task = pkg.tasks.find(item => item.id === taskId);
      if (task) return { pkg, task };
    }
    return null;
  }

  function statusBadge(status, compact = false) {
    const meta = statusMeta[status] || { label: status, tone: 'muted' };
    return `<span class="status-badge ${meta.tone}${compact ? ' compact' : ''}"><i></i>${escapeHtml(meta.label)}</span>`;
  }

  function signalBadge(task) {
    const signalTone = task.signal === 'hot' ? 'hot' : task.signal === 'rising' ? 'rising' : 'stable';
    return `<span class="signal-badge ${signalTone}"><i></i>${escapeHtml(task.signalText)}</span>`;
  }

  function modeBadge(mode) {
    return mode === 'agent'
      ? '<span class="mode-badge agent"><b>✦</b> 深度 Agent</span>'
      : '<span class="mode-badge llm"><b>↯</b> 快速 LLM</span>';
  }

  function frameworkLabel(key) {
    return frameworkNames[key] || key;
  }

  function countStats(pkg) {
    const total = pkg.tasks.reduce((sum, task) => sum + task.count, 0);
    const completed = pkg.tasks.reduce((sum, task) => sum + task.articlesDone, 0);
    const warnings = pkg.tasks.filter(task => Boolean(task.warning)).length;
    const running = pkg.tasks.filter(task => ['running', 'routing', 'analyzing'].includes(task.status)).length;
    const failed = pkg.tasks.filter(task => ['failed', 'blocked'].includes(task.status)).length;
    return { total, completed, warnings, running, failed };
  }

  function packageMatchesFilter(pkg) {
    if (state.packageFilter === 'archived') return pkg.archived;
    if (pkg.archived) return false;
    if (state.packageFilter === 'warning') return pkg.tasks.some(task => Boolean(task.warning));
    if (state.packageFilter === 'done') return pkg.status === 'completed';
    return pkg.status !== 'completed';
  }

  function renderPackages() {
    const query = state.packageQuery;
    const list = packages.filter(pkg => {
      const haystack = `${pkg.name} ${pkg.source} ${pkg.tasks.map(task => task.keyword).join(' ')}`.toLowerCase();
      return packageMatchesFilter(pkg) && (!query || haystack.includes(query.toLowerCase()));
    });
    $('#packageList').innerHTML = list.length ? list.map(pkg => {
      const stats = countStats(pkg);
      const active = pkg.id === state.selectedPackageId;
      return `
        <button class="package-item${active ? ' active' : ''}" type="button" data-package-id="${escapeHtml(pkg.id)}">
          <div class="package-item-top">
            <span class="package-source ${pkg.sourceTone}">${escapeHtml(pkg.source)}</span>
            ${pkg.archived ? '<span class="archive-mark">归档</span>' : ''}
          </div>
          <div class="package-item-name">${escapeHtml(pkg.name)}</div>
          <div class="package-item-meta">
            <span>${pkg.tasks.length} 个关键词 · ${stats.total} 篇</span>
            ${stats.warnings ? `<span class="review-count">${stats.warnings} 条提示</span>` : `<span>${stats.completed}/${stats.total} 完成</span>`}
          </div>
          <div class="package-progress"><span style="width:${stats.total ? Math.round(stats.completed / stats.total * 100) : 0}%"></span></div>
        </button>
      `;
    }).join('') : '<div class="empty-sidebar">没有符合条件的任务包</div>';

    $('#navPackageCount').textContent = packages.filter(pkg => !pkg.archived).length;
    $('#navWarningCount').textContent = packages.reduce((sum, pkg) => sum + countStats(pkg).warnings, 0);
  }

  function renderHero(pkg) {
    $('#topbarPackageName').textContent = pkg.name;
    $('#packageHero').innerHTML = `
      <div class="hero-copy">
        <div class="hero-eyebrow">${escapeHtml(pkg.source)} <span>·</span> 创建于 ${escapeHtml(pkg.createdAt)}</div>
        <div class="hero-title-line">
          <h1>${escapeHtml(pkg.name)}</h1>
          ${statusBadge(pkg.status)}
        </div>
        <p class="hero-brief">${escapeHtml(pkg.brief)}</p>
        <div class="hero-meta">
          <span class="path-pill">⌁ ${escapeHtml(pkg.outputDir)}</span>
          <span>执行方式：<b>Claude Code -p 全自动</b></span>
          <span>默认模式：<b>${pkg.defaultMode === 'agent' ? '深度 Agent' : '快速 LLM'}</b></span>
        </div>
      </div>
      <div class="hero-actions">
        <button class="ghost-btn" data-action="copy-package">复制摘要</button>
        <button class="ghost-btn" data-action="rerun-package">重新生成</button>
        <button class="primary-btn" data-action="run-auto" ${pkg.archived ? 'disabled' : ''}>${pkg.status === 'generating' ? '查看生成中' : '启动全部待生成'}</button>
        <button class="more-btn" data-action="toggle-package-menu" aria-label="更多">⋯</button>
      </div>
    `;
  }

  function renderMetrics(pkg) {
    const stats = countStats(pkg);
    const agentCount = pkg.tasks.filter(task => task.mode === 'agent').length;
    const llmCount = pkg.tasks.filter(task => task.mode === 'llm').length;
    $('#metricGrid').innerHTML = `
      <div class="metric-card">
        <span class="metric-label">关键词任务</span>
        <strong>${pkg.tasks.length}</strong>
        <small>全部已解析</small>
      </div>
      <div class="metric-card accent-blue">
        <span class="metric-label">计划生成</span>
        <strong>${stats.total}</strong>
        <small>${llmCount} 个快速 LLM · ${agentCount} 个深度 Agent</small>
      </div>
      <div class="metric-card accent-orange">
        <span class="metric-label">非阻断提示</span>
        <strong>${stats.warnings}</strong>
        <small>${stats.warnings ? '随任务证据保存，生产继续' : '当前没有提示'}</small>
      </div>
      <div class="metric-card accent-green">
        <span class="metric-label">完成进度</span>
        <strong>${stats.completed}<em>/${stats.total}</em></strong>
        <small>${stats.total ? Math.round(stats.completed / stats.total * 100) : 0}% 已生成</small>
      </div>
    `;
  }

  function renderControlStrip(pkg) {
    const stats = countStats(pkg);
    $('#controlStrip').innerHTML = `
      <div class="control-status">
        <span class="control-live"></span>
        <div><b>${pkg.status === 'generating' ? '自动任务正在运行' : '全自动生产台已就绪'}</b><small>最近更新 ${escapeHtml(pkg.updatedAt)} · Demo 模拟</small></div>
      </div>
      <div class="control-actions">
        <button class="control-btn" data-action="pause-package">${pkg.status === 'paused' ? '继续任务' : '暂停任务'}</button>
        <button class="control-btn" data-action="retry-failed" ${stats.failed ? '' : 'disabled'}>重试失败 <span>${stats.failed}</span></button>
        <button class="control-btn" data-action="archive-package">${pkg.archived ? '取消归档' : '归档任务包'}</button>
      </div>
    `;
  }

  function visibleTasks(pkg) {
    return pkg.tasks.filter(task => {
      const textHit = !state.taskSearch || task.keyword.toLowerCase().includes(state.taskSearch.toLowerCase());
      let filterHit = true;
      if (state.taskFilter === 'warning') filterHit = Boolean(task.warning);
      if (state.taskFilter === 'running') filterHit = ['running', 'routing'].includes(task.status);
      if (state.taskFilter === 'done') filterHit = task.status === 'done';
      if (state.taskFilter === 'blocked') filterHit = ['blocked', 'failed'].includes(task.status);
      return textHit && filterHit;
    });
  }

  function renderQueue(pkg) {
    const tasks = visibleTasks(pkg);
    $('#taskQueue').innerHTML = tasks.length ? tasks.map(task => {
      const progress = task.count ? Math.round(task.articlesDone / task.count * 100) : 0;
      const routeTone = task.warning ? 'warn' : ['blocked', 'failed'].includes(task.status) ? 'danger' : 'ok';
      return `
        <article class="task-row${task.id === state.selectedTaskId ? ' selected' : ''}" data-task-id="${escapeHtml(task.id)}">
          <div class="task-primary">
            <div class="task-title-line">
              <span class="task-signal-dot ${task.signal}"></span>
              <strong>${escapeHtml(task.keyword)}</strong>
            </div>
            <div class="task-subline">
              ${signalBadge(task)}
              <span class="source-count">⌁ ${task.sourceDocuments.length} 份 Wiki 资产</span>
            </div>
          </div>
          <div class="count-control" data-stop-row-click="true">
            <button type="button" data-action="dec-count" aria-label="减少篇数">−</button>
            <b>${task.count}</b>
            <button type="button" data-action="inc-count" aria-label="增加篇数">＋</button>
          </div>
          <div class="route-cell">
            <span class="route-framework">${escapeHtml(frameworkLabel(task.framework))}</span>
            <span class="route-sub ${routeTone}">${task.warning ? '提示 · ' + escapeHtml(task.warning) : 'RoutePlan v' + task.routeVersion + ' · 信心 ' + Math.round(task.confidence * 100) + '%'}</span>
          </div>
          <div>${modeBadge(task.mode)}</div>
          <div class="progress-cell">
            <div class="task-progress"><span style="width:${progress}%"></span></div>
            <div class="progress-meta"><span>${task.articlesDone}/${task.count} 篇</span>${statusBadge(task.status, true)}</div>
          </div>
          <div class="task-actions" data-stop-row-click="true">
            <button class="row-action" type="button" data-action="open-detail">详情</button>
          </div>
        </article>
      `;
    }).join('') : '<div class="empty-queue">当前筛选没有任务</div>';
  }

  function renderAll() {
    const pkg = selectedPackage();
    if (!pkg) return;
    renderPackages();
    renderHero(pkg);
    renderMetrics(pkg);
    renderControlStrip(pkg);
    renderQueue(pkg);
    if ($('#detailDrawer').classList.contains('open')) renderDrawer();
  }

  function routePlan(task) {
    const framework = frameworkLabel(task.framework);
    return {
      schema_version: 'route-plan.v2-demo',
      route_plan_id: `rp_${task.id}_v${task.routeVersion}`,
      keyword_task_id: task.id,
      keyword: task.keyword,
      keyword_signal: { type: task.signal, label: task.signalText },
      reader_problem: `读者已经对“${task.keyword}”产生兴趣，但还没有完成买前判断。`,
      framework: {
        name: framework,
        reason: `关键词语义与${framework}的适用边界一致。`,
        confidence: task.confidence
      },
      sources: task.sourceDocuments.map(doc => ({
        source_ref: doc.path,
        role: doc.role,
        reason: doc.reason
      })),
      mental_gates: [
        { name: '静态演示的幻觉', role: '防止把漂亮数字直接写成承诺' },
        { name: '事实边界', role: '要求每个关键判断回到 Wiki 事实' }
      ],
      narrative: {
        opening: '先给出一个读者意外的变量，再进入事实拆解。',
        main_path: ['承认吸引力', '拆出关键变量', '做压力测试', '划定适配门槛'],
        breakthrough_path: '把问题留在读者自己的方案与家庭账本上'
      },
      article_variants: task.variants.map(variant => ({
        variant_id: variant.id,
        angle: variant.angle,
        title_direction: variant.title,
        differentiation: variant.brief
      })),
      generation: {
        recommended_mode: task.mode === 'agent' ? 'deep_agent' : 'fast_llm',
        execution_policy: 'fully_automatic',
        session_id: task.sessionId
      },
      gates: {
        fact_sufficiency: task.warning ? 'warn_non_blocking' : 'pass',
        route_conflict: 'pass',
        variant_diversity: task.count > 1 ? 'pass' : 'not_applicable',
        warnings: task.warning ? [task.warning] : []
      },
      protocol_versions: {
        identity: 'output_md/AGENTS.md@2026-05-21',
        execution_rules: '知识库规则/01-Agent创作执行流程@v7.4',
        framework: `${framework}@demo`
      }
    };
  }

  function renderDrawer() {
    const task = selectedTask();
    if (!task || !$('#detailDrawer').classList.contains('open')) return;
    $('#drawerKicker').textContent = `关键词任务 · RoutePlan v${task.routeVersion}`;
    $('#drawerTitle').textContent = task.keyword;
    $('#drawerSubtitle').innerHTML = `${signalBadge(task)} <span class="drawer-sub-sep">·</span> ${modeBadge(task.mode)} <span class="drawer-sub-sep">·</span> ${statusBadge(task.status, true)}`;
    const activeTab = state.reader ? state.reader.originTab : state.detailTab;
    $$('.drawer-tab').forEach(tab => tab.classList.toggle('active', tab.dataset.detailTab === activeTab));

    if (state.reader) {
      $('#drawerBody').innerHTML = renderMarkdownReader(task);
    } else {
      const renderers = {
        articles: renderArticlesTab,
        route: renderRouteTab,
        trace: renderTraceTab,
        review: renderReviewTab
      };
      $('#drawerBody').innerHTML = renderers[state.detailTab](task);
    }
    $('#drawerFoot').innerHTML = renderDrawerFoot(task);
  }

  function renderRouteTab(task) {
    const plan = routePlan(task);
    return `
      <div class="detail-summary-row">
        <div class="confidence-card">
          <span>Agent 路由信心</span>
          <strong>${Math.round(task.confidence * 100)}%</strong>
          <div class="confidence-bar"><i style="width:${task.confidence * 100}%"></i></div>
        </div>
        <div class="confidence-card source-card">
          <span>事实来源 · 点击预览全文</span>
          <strong>${task.sourceDocuments.length}<small>份 Wiki 资产</small></strong>
          <div class="source-preview-list">
            ${task.sourceDocuments.map((doc, index) => `
              <button type="button" data-action="preview-source" data-source-index="${index}" title="${escapeHtml(doc.path)}">
                <span>⌁ ${escapeHtml(doc.title)}</span><small>预览 Markdown</small>
              </button>
            `).join('')}
          </div>
        </div>
      </div>
      ${task.warning ? `<div class="warning-panel"><b>Agent 提示 · 不阻断</b><span>${escapeHtml(task.warning)}</span></div>` : ''}
      <div class="route-block">
        <div class="route-block-head"><span>Agent 已完成的门禁路由</span><button class="text-action" data-action="show-json">查看原始 JSON</button></div>
        <div class="route-flow">
          <div class="route-node"><span class="route-node-index">01</span><div><b>判断读者问题</b><small>${escapeHtml(plan.reader_problem)}</small></div><span class="check-mark">✓</span></div>
          <div class="route-node"><span class="route-node-index">02</span><div><b>选择主创作框架</b><small>${escapeHtml(plan.framework.name)} · ${escapeHtml(plan.framework.reason)}</small></div><span class="check-mark">✓</span></div>
          <div class="route-node"><span class="route-node-index">03</span><div><b>匹配并阅读全文</b><small>${task.sourceDocuments.map(doc => escapeHtml(doc.title)).join(' · ')}</small></div><span class="check-mark">✓</span></div>
          <div class="route-node"><span class="route-node-index">04</span><div><b>设置思维门禁</b><small>${plan.mental_gates.map(item => escapeHtml(item.name)).join(' · ')}</small></div><span class="check-mark">✓</span></div>
          <div class="route-node"><span class="route-node-index">05</span><div><b>规划文章变体</b><small>${task.count} 篇 · 每篇角度独立，避免重复改写</small></div><span class="check-mark">✓</span></div>
        </div>
      </div>
      <div class="detail-card">
        <div class="detail-card-head"><b>写作方向</b><span class="version-tag">RoutePlan v${task.routeVersion}</span></div>
        <div class="copy-pair"><span>开头</span><strong>${escapeHtml(plan.narrative.opening)}</strong></div>
        <div class="copy-pair"><span>正文路径</span><strong>${plan.narrative.main_path.map(escapeHtml).join(' → ')}</strong></div>
        <div class="copy-pair"><span>结尾破窗</span><strong>${escapeHtml(plan.narrative.breakthrough_path)}</strong></div>
      </div>
      <div class="detail-card compact-card">
        <div class="detail-card-head"><b>规则版本</b><button class="text-action" data-action="show-trace">查看 Agent 对话</button></div>
        <div class="rule-chips"><span>AGENTS.md · 2026-05-21</span><span>执行流程 v7.4</span><span>${escapeHtml(frameworkLabel(task.framework))}</span></div>
      </div>
    `;
  }

  function renderArticlesTab(task) {
    return `
      <div class="tab-intro"><b>${task.count} 篇文章计划</b><span>点击可在侧边栏内预览完整 Markdown，并切换到源码编辑。</span></div>
      ${task.warning ? `<div class="warning-panel compact-warning"><b>成稿提示</b><span>${escapeHtml(task.warning)}</span></div>` : ''}
      <div class="variant-list">
        ${task.variants.map((variant, index) => `
          <article class="variant-card">
            <div class="variant-index">${String(index + 1).padStart(2, '0')}</div>
            <div class="variant-main">
              <div class="variant-title-line"><strong>${escapeHtml(variant.label)}</strong>${statusBadge(variant.status, true)}</div>
              <div class="variant-title">${escapeHtml(variant.title)}</div>
              <p>${escapeHtml(variant.brief)}</p>
              <div class="variant-meta"><span>关键词：${escapeHtml(task.keyword)}</span><span>素材组合：${task.sourceDocuments.length} 份</span><span>${task.mode === 'agent' ? 'Claude Code -p Session' : 'Agent 编译写作包'}</span></div>
            </div>
            <button class="row-action" type="button" data-action="view-article" data-variant-id="${escapeHtml(variant.id)}">${variant.status === 'done' ? '查看成稿' : '预览计划'}</button>
          </article>
        `).join('')}
      </div>
    `;
  }

  function agentMessages(task) {
    const sources = task.sourceDocuments.map(doc => `- \`${doc.path}\`：${doc.role}`).join('\n');
    const messages = [
      {
        role: 'user',
        label: '你给 Claude Code -p 的综合 Prompt',
        time: '09:20',
        markdown: `请围绕关键词 **${task.keyword}** 全自动完成一轮生产：

1. 读取知识库入口与写作规则；
2. 从 Wiki 选择 2–3 份真正需要阅读全文的资产；
3. 生成 RoutePlan 与 ${task.count} 篇差异化文章计划；
4. ${task.mode === 'agent' ? '继续在当前 Session 深度写作' : '把 RoutePlan 编译成完整写作包，再交给 LLM 成稿'}；
5. 保存 Markdown、RoutePlan、关键对话摘要和复盘证据。

出现口径差异时不要停下来等待：请降低表述强度、保留提示并继续。`
      },
      {
        role: 'assistant',
        label: 'Claude Code -p · 任务理解',
        time: '09:20',
        markdown: `### 我先收口任务

“${task.keyword}”不是单纯的产品介绍词，读者已经接近买前判断。我会把**吸引力、隐藏条件和家庭现金流**放在同一条叙事线上，而不是直接复述产品卖点。`
      },
      {
        role: 'assistant',
        label: 'Claude Code -p · 选材决定',
        time: '09:21',
        markdown: `### 已选中 ${task.sourceDocuments.length} 份 Wiki 资产

${sources}

第一份承担主要事实，第二份负责思维门禁；如果有第三份，只用于补充对比或案例，不平均拼接素材。`
      },
      {
        role: 'assistant',
        label: 'Claude Code -p · 路由决定',
        time: '09:22',
        markdown: `### RoutePlan v${task.routeVersion}

- **主框架**：${frameworkLabel(task.framework)}
- **路由信心**：${Math.round(task.confidence * 100)}%
- **正文路径**：承认吸引力 → 拆出关键变量 → 压力测试 → 适配边界
- **生成方式**：${task.mode === 'agent' ? '深度 Agent 继续写作' : 'Agent 编译写作包，LLM 一次成稿'}

${task.warning ? `> 非阻断提示：${task.warning}` : '> 当前素材足以支撑成稿，没有阻断项。'}`
      },
      {
        role: 'assistant',
        label: 'Claude Code -p · 阶段结果',
        time: task.status === 'done' ? '09:28' : '进行中',
        markdown: `### ${task.traceStage}

当前已完成 **${task.articlesDone}/${task.count}** 篇。每篇文章都保留对应的 Markdown、素材路径和 RoutePlan 版本，后续可按关键词表现回查这次选材和叙事决策。`
      }
    ];
    if (task.revisionNote) {
      messages.push({
        role: 'user',
        label: '追加修改要求',
        time: '最近',
        markdown: task.revisionNote
      });
    }
    return messages;
  }

  function renderTraceTab(task) {
    return `
      <div class="trace-head"><div><b>Agent 关键对话</b><span>${escapeHtml(task.sessionId)}</span></div><span class="trace-live"><i></i> Demo Session</span></div>
      <div class="trace-note">只展示用户 Prompt、关键判断、选材理由和阶段结果；不展示隐藏思维链，也不逐条模拟 Edit 或工具日志。</div>
      <div class="agent-conversation">
        ${agentMessages(task).map(message => `
          <article class="agent-message ${message.role}">
            <div class="agent-avatar">${message.role === 'user' ? '你' : 'C'}</div>
            <div class="agent-message-main">
              <div class="agent-message-head"><b>${escapeHtml(message.label)}</b><span>${escapeHtml(message.time)}</span></div>
              <div class="agent-message-bubble markdown-prose">${renderMarkdown(message.markdown)}</div>
            </div>
          </article>
        `).join('')}
      </div>
    `;
  }

  function renderReviewTab(task) {
    return `
      <div class="review-hero"><span class="review-label">效果 API 状态</span><strong>等待接入</strong><p>Demo 只展示复盘结构，不伪造真实阅读、收录或咨询数据。</p></div>
      <div class="review-grid">
        <div><span>关键词收录</span><b>待回收</b></div>
        <div><span>阅读量</span><b>—</b></div>
        <div><span>收藏 / 转发</span><b>—</b></div>
        <div><span>咨询 / 加好友</span><b>—</b></div>
      </div>
      <div class="detail-card">
        <div class="detail-card-head"><b>未来复盘问题</b></div>
        <ul class="review-list">
          <li>关键词是否真的有交易距离，而不只是热度高？</li>
          <li>框架路由和素材组合是否与最终表现匹配？</li>
          <li>文章热门但转化低时，是否是破窗和钩子的问题？</li>
          <li>本次成功或失败是否值得沉淀为候选规则？</li>
        </ul>
      </div>
      <div class="detail-card">
        <div class="detail-card-head"><b>证据文件</b></div>
        <div class="file-list"><span>route_plan.v${task.routeVersion}.json</span><span>writing_package.json</span><span>agent_dialogue.md</span><span>article_markdown/</span><span>retrospective.json</span></div>
      </div>
    `;
  }

  function readerItem(task) {
    if (!state.reader) return null;
    if (state.reader.kind === 'source') return task.sourceDocuments[Number(state.reader.id)] || null;
    return task.variants.find(variant => variant.id === state.reader.id) || null;
  }

  function renderMarkdownReader(task) {
    const item = readerItem(task);
    if (!item) return '<div class="empty-queue">Markdown 不存在</div>';
    const isSource = state.reader.kind === 'source';
    const path = isSource ? item.path : `${selectedPackage().outputDir}${slugify(item.title)}.md`;
    const backLabel = state.reader.originTab === 'route' ? '返回路由方案' : '返回文章计划';
    if (state.reader.editing) {
      return `
        <section class="markdown-reader editing">
          <div class="markdown-reader-toolbar">
            <button class="reader-back" type="button" data-action="close-reader">← ${backLabel}</button>
            <div class="reader-actions">
              <button class="ghost-btn" type="button" data-action="cancel-markdown-edit">取消</button>
              <button class="primary-btn" type="button" data-action="save-markdown-edit">保存 Demo 修改</button>
            </div>
          </div>
          <div class="markdown-reader-meta">
            <span class="reader-type">${isSource ? 'Wiki 素材' : '最终成稿'}</span>
            <b>${escapeHtml(item.title)}</b>
            <code>${escapeHtml(path)}</code>
          </div>
          <textarea id="markdownEditor" class="markdown-editor" spellcheck="false">${escapeHtml(state.reader.draft)}</textarea>
          <div class="markdown-editor-tip">编辑图片时直接修改 <code>![说明](图片路径)</code>；保存只作用于当前静态 Demo 会话。</div>
        </section>
      `;
    }
    return `
      <section class="markdown-reader">
        <div class="markdown-reader-toolbar">
          <button class="reader-back" type="button" data-action="close-reader">← ${backLabel}</button>
          <div class="reader-actions">
            <button class="ghost-btn" type="button" data-action="copy-markdown">复制 Markdown</button>
            <button class="primary-btn" type="button" data-action="edit-markdown">编辑源码</button>
          </div>
        </div>
        <div class="markdown-reader-meta">
          <span class="reader-type">${isSource ? 'Wiki 素材' : item.status === 'done' ? '最终成稿' : '文章计划'}</span>
          <b>${escapeHtml(item.title)}</b>
          <code>${escapeHtml(path)}</code>
          ${item.userEdited ? '<small>已在 Demo 会话修改</small>' : ''}
        </div>
        <article class="markdown-prose markdown-document">${renderMarkdown(item.markdown)}</article>
      </section>
    `;
  }

  function renderDrawerFoot(task) {
    if (state.reader) {
      return `
        <div class="drawer-foot-info"><span>Markdown 预览</span><b>图片可点开；源码可在侧边栏内编辑</b></div>
        <div class="drawer-foot-actions"><button class="ghost-btn" data-action="request-revision">让 Agent 继续修改</button></div>
      `;
    }
    return `
      <div class="drawer-foot-info"><span>Session</span><b>${escapeHtml(task.sessionId)}</b></div>
      <div class="drawer-foot-actions">
        <button class="ghost-btn" data-action="request-revision">让 Agent 继续修改</button>
        <button class="primary-btn" data-action="run-task">${task.status === 'done' ? '重新生成此任务' : '运行此任务'}</button>
      </div>
    `;
  }

  function openDetail(taskId, tab = 'articles') {
    state.selectedTaskId = taskId;
    state.detailTab = tab;
    state.reader = null;
    $('#detailDrawer').classList.add('open');
    $('#detailDrawer').setAttribute('aria-hidden', 'false');
    renderAll();
  }

  function closeDetail() {
    state.reader = null;
    $('#detailDrawer').classList.remove('open');
    $('#detailDrawer').setAttribute('aria-hidden', 'true');
  }

  function openMarkdownReader(kind, id, originTab) {
    state.reader = { kind, id: String(id), originTab, editing: false, draft: '' };
    renderDrawer();
    $('#drawerBody').scrollTop = 0;
  }

  function showToast(message) {
    const toast = $('#toast');
    toast.textContent = message;
    toast.classList.add('show');
    clearTimeout(showToast.timer);
    showToast.timer = setTimeout(() => toast.classList.remove('show'), 2600);
  }

  function formatPackageStem(keywords) {
    const date = demoDateLabel.replace(/-/g, '');
    const time = '1430';
    return `${date}_${time}_${keywords.slice(0, 3).map(slugify).join('_')}`;
  }

  function formatFolderName(keywords) {
    return `output/${formatPackageStem(keywords)}/`;
  }

  function parseKeywords(value) {
    return Array.from(new Set(String(value || '').split(/[,，;；\n]+/).map(item => item.trim()).filter(Boolean)));
  }

  function makeNewTask(keyword, index, mode) {
    const lower = keyword.toLowerCase();
    const framework = /融资|杠杆|贷款/.test(keyword) ? 'financing'
      : /分红|实现率|履约/.test(keyword) ? 'dividend'
      : /提领|条款|费用|核保|规则/.test(keyword) ? 'blackbox'
      : /对比|vs|哪家|哪个好/.test(lower) ? 'comparison'
      : 'product';
    const warning = /分红实现率|是不是|靠谱吗|缺点/.test(keyword) ? 'Demo：这是证据敏感关键词，Agent 会降级表述并保留来源提示，但不会停下来等待。' : '';
    return makeTask({
      id: `kw-new-${Date.now()}-${index}`,
      keyword,
      signal: index === 0 ? 'rising' : 'stable',
      signalText: index === 0 ? '上升中' : '待监测',
      count: 1,
      mode,
      status: 'ready',
      confidence: warning ? 0.76 : 0.88,
      framework,
      sourceCount: 2,
      articlesDone: 0,
      sourceRefs: [`wiki/产品母页/${keyword}.md`, `wiki/创作框架/${frameworkLabel(framework)}.md`],
      traceStage: warning ? '已带提示形成计划，等待自动生成' : 'RoutePlan 已完成，等待自动生成',
      warning
    });
  }

  function updateTaskCount(task, delta) {
    task.count = Math.max(1, Math.min(8, task.count + delta));
    task.variants = buildVariants(task);
    showToast(`“${task.keyword}”计划生成 ${task.count} 篇，Agent 已重新规划差异化角度。`);
    renderAll();
  }

  function runTask(task) {
    const located = findTaskWithPackage(task.id);
    if (!located) return;
    if (located.pkg.archived) {
      showToast('归档任务包不能直接运行，请先取消归档。');
      return;
    }
    task.status = 'running';
    task.articlesDone = 0;
    task.traceStage = task.mode === 'agent' ? 'Claude Code -p 正在当前 Session 中继续写作' : 'Agent 已编译写作包，LLM 正在生成';
    task.variants = buildVariants(task);
    located.pkg.status = 'generating';
    located.pkg.updatedAt = '刚刚';
    state.reader = null;
    renderAll();
    state.simulationTimers.push(setTimeout(() => {
      task.status = 'done';
      task.articlesDone = task.count;
      task.traceStage = task.mode === 'agent' ? '已完成写作、保存 Markdown 与关键对话摘要' : '已完成写作包生成并保存 Markdown';
      task.variants = buildVariants(task);
      const stats = countStats(located.pkg);
      located.pkg.status = stats.completed === stats.total ? 'completed' : 'partial';
      located.pkg.updatedAt = '刚刚';
      showToast(`“${task.keyword}”已完成 ${task.count} 篇（Demo 模拟）。`);
      renderAll();
    }, 1400));
  }

  function runAutoTasks() {
    const pkg = selectedPackage();
    if (pkg.archived) {
      showToast('归档任务包不能直接启动，请先取消归档。');
      return;
    }
    const candidates = pkg.tasks.filter(task => ['ready', 'queued', 'routing'].includes(task.status));
    if (!candidates.length) {
      showToast('当前没有待生成任务；可对已完成任务单独重新生成。');
      return;
    }
    candidates.forEach((task, index) => setTimeout(() => runTask(task), index * 250));
    showToast(`已启动 ${candidates.length} 个任务；提示不会阻断生产。`);
  }

  function requestRevision(task) {
    state.revisionTaskId = task.id;
    $('#revisionInput').value = '';
    $('#revisionModal').classList.add('open');
    $('#revisionModal').setAttribute('aria-hidden', 'false');
    setTimeout(() => $('#revisionInput').focus(), 0);
  }

  function submitRevision() {
    const located = findTaskWithPackage(state.revisionTaskId);
    const request = $('#revisionInput').value.trim();
    if (!located || !request) {
      showToast('请先写下你希望 Agent 调整的内容。');
      return;
    }
    const { task } = located;
    task.routeVersion += 1;
    task.revisionCount += 1;
    task.revisionNote = request;
    task.status = 'running';
    task.traceStage = '已收到追加要求，正在同一 Session 自动修改';
    closeRevisionModal();
    openDetail(task.id, 'articles');
    showToast(`已发送到 ${task.sessionId}，Agent 将自动修改并继续生成。`);
    state.simulationTimers.push(setTimeout(() => runTask(task), 300));
  }

  function closeRevisionModal() {
    $('#revisionModal').classList.remove('open');
    $('#revisionModal').setAttribute('aria-hidden', 'true');
  }

  function openPackageModal() {
    $('#packageModal').classList.add('open');
    $('#packageModal').setAttribute('aria-hidden', 'false');
    $('#newPackageKeywords').focus();
  }

  function closePackageModal() {
    $('#packageModal').classList.remove('open');
    $('#packageModal').setAttribute('aria-hidden', 'true');
  }

  function createPackage(event) {
    event.preventDefault();
    const keywords = parseKeywords($('#newPackageKeywords').value);
    if (!keywords.length) {
      showToast('请至少输入一个关键词。');
      $('#newPackageKeywords').focus();
      return;
    }
    const mode = $('#newPackageMode').value;
    const name = $('#newPackageName').value.trim() || formatPackageStem(keywords);
    const outputDir = $('#newPackageOutput').value.trim() || formatFolderName(keywords);
    const packageId = `pkg-new-${Date.now()}`;
    const pkg = {
      id: packageId,
      name,
      source: '手动创建',
      sourceTone: 'blue',
      brief: $('#newPackageBrief').value.trim() || 'Claude Code -p 全自动完成选材、路由、写作与证据保存。',
      createdAt: '刚刚',
      updatedAt: '刚刚',
      outputDir,
      defaultMode: mode,
      status: 'analyzing',
      archived: false,
      tasks: keywords.map((keyword, index) => makeNewTask(keyword, index, mode))
    };
    packages.unshift(pkg);
    state.selectedPackageId = packageId;
    state.selectedTaskId = pkg.tasks[0].id;
    state.packageFilter = 'active';
    $$('.filter-chip').forEach(chip => chip.classList.toggle('active', chip.dataset.packageFilter === 'active'));
    closePackageModal();
    $('#newPackageForm').reset();
    renderAll();
    showToast(`已创建任务包，${keywords.length} 个关键词进入全自动生产队列（Demo 模拟）。`);
  }

  function showJson() {
    const task = selectedTask();
    const json = JSON.stringify(routePlan(task), null, 2);
    state.reader = null;
    $('#drawerBody').innerHTML = `
      <div class="json-head"><div><b>route_plan.v${task.routeVersion}.json</b><span>Agent 路由结果 · 只读演示</span></div><button class="text-action" data-action="copy-json">复制 JSON</button></div>
      <pre class="json-view">${escapeHtml(json)}</pre>
    `;
    state.detailTab = 'route';
    $$('.drawer-tab').forEach(tab => tab.classList.toggle('active', tab.dataset.detailTab === 'route'));
  }

  function copyText(text, message) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(() => showToast(message)).catch(() => showToast('Demo 已生成可复制内容。'));
    } else {
      showToast(message);
    }
  }

  function packageSummary(pkg) {
    const stats = countStats(pkg);
    return JSON.stringify({
      package: pkg.name,
      source: pkg.source,
      output_dir: pkg.outputDir,
      execution_policy: 'fully_automatic',
      keywords: pkg.tasks.map(task => ({
        keyword: task.keyword,
        count: task.count,
        status: task.status,
        warning: task.warning || null,
        mode: task.mode,
        route_plan_version: task.routeVersion
      })),
      stats
    }, null, 2);
  }

  function openImageLightbox(src, title) {
    const safe = safeMediaUrl(src);
    if (!safe) return;
    $('#lightboxImage').src = safe;
    $('#lightboxImage').alt = title || 'Markdown 图片';
    $('#lightboxImageTitle').textContent = title || 'Markdown 图片';
    $('#lightboxImagePath').textContent = safe;
    $('#imageLightbox').classList.add('open');
    $('#imageLightbox').setAttribute('aria-hidden', 'false');
  }

  function closeImageLightbox() {
    $('#imageLightbox').classList.remove('open');
    $('#imageLightbox').setAttribute('aria-hidden', 'true');
  }

  function handleClick(event) {
    const target = event.target.closest('[data-action], [data-package-id], [data-task-id], [data-detail-tab], [data-package-filter]');
    if (!target) return;

    if (target.dataset.packageId) {
      state.selectedPackageId = target.dataset.packageId;
      const pkg = selectedPackage();
      state.selectedTaskId = pkg.tasks[0]?.id || '';
      closeDetail();
      renderAll();
      return;
    }

    if (target.dataset.packageFilter) {
      state.packageFilter = target.dataset.packageFilter;
      $$('.filter-chip').forEach(chip => chip.classList.toggle('active', chip.dataset.packageFilter === state.packageFilter));
      renderPackages();
      return;
    }

    if (target.dataset.detailTab) {
      state.reader = null;
      state.detailTab = target.dataset.detailTab;
      renderDrawer();
      return;
    }

    const row = target.closest('[data-task-id]');
    const task = row ? selectedPackage().tasks.find(item => item.id === row.dataset.taskId) : selectedTask();
    const action = target.dataset.action;

    if (action === 'open-detail' || target.dataset.taskId && !target.dataset.action) {
      if (task) openDetail(task.id, 'articles');
      return;
    }
    if (!task && !['new', 'close-lightbox', 'copy-image-path'].includes(action)) return;

    if (action === 'inc-count') updateTaskCount(task, 1);
    if (action === 'dec-count') updateTaskCount(task, -1);
    if (action === 'run-task') runTask(task);
    if (action === 'request-revision') requestRevision(task);
    if (action === 'show-json') showJson();
    if (action === 'show-trace') {
      state.reader = null;
      state.detailTab = 'trace';
      renderDrawer();
    }
    if (action === 'preview-source') openMarkdownReader('source', target.dataset.sourceIndex, 'route');
    if (action === 'view-article') openMarkdownReader('article', target.dataset.variantId, 'articles');
    if (action === 'close-reader') {
      state.reader = null;
      renderDrawer();
    }
    if (action === 'edit-markdown') {
      const item = readerItem(task);
      if (item) {
        state.reader.editing = true;
        state.reader.draft = item.markdown;
        renderDrawer();
        setTimeout(() => $('#markdownEditor')?.focus(), 0);
      }
    }
    if (action === 'cancel-markdown-edit') {
      state.reader.editing = false;
      state.reader.draft = '';
      renderDrawer();
    }
    if (action === 'save-markdown-edit') {
      const item = readerItem(task);
      if (item) {
        item.markdown = state.reader.draft;
        item.userEdited = true;
        state.reader.editing = false;
        state.reader.draft = '';
        renderDrawer();
        showToast('Markdown 已保存到当前 Demo 会话。');
      }
    }
    if (action === 'copy-markdown') {
      const item = readerItem(task);
      if (item) copyText(item.markdown, '已复制 Markdown。');
    }
    if (action === 'open-lightbox') openImageLightbox(target.dataset.imageSrc, target.dataset.imageTitle);
    if (action === 'close-lightbox') closeImageLightbox();
    if (action === 'copy-image-path') copyText($('#lightboxImagePath').textContent, '已复制图片路径。');
    if (action === 'run-auto') runAutoTasks();
    if (action === 'pause-package') {
      const pkg = selectedPackage();
      pkg.status = pkg.status === 'paused' ? 'generating' : 'paused';
      pkg.updatedAt = '刚刚';
      renderAll();
      showToast(pkg.status === 'paused' ? '任务包已暂停。' : '任务包已继续。');
    }
    if (action === 'retry-failed') {
      const pkg = selectedPackage();
      pkg.tasks.filter(item => ['failed', 'blocked'].includes(item.status)).forEach(item => {
        item.status = 'ready';
      });
      renderAll();
      showToast('失败任务已回到自动队列。');
    }
    if (action === 'archive-package') {
      const pkg = selectedPackage();
      pkg.archived = !pkg.archived;
      pkg.status = pkg.archived ? 'archived' : 'partial';
      renderAll();
      showToast(pkg.archived ? '任务包已归档，证据文件不会删除。' : '任务包已取消归档。');
    }
    if (action === 'rerun-package') {
      const pkg = selectedPackage();
      pkg.tasks.forEach(item => {
        item.status = 'ready';
        item.articlesDone = 0;
        item.variants = buildVariants(item);
      });
      pkg.status = 'partial';
      renderAll();
      showToast('已创建一轮新的全自动生成计划，旧版本仍保留。');
    }
    if (action === 'copy-package') copyText(packageSummary(selectedPackage()), '已复制任务包摘要。');
    if (action === 'copy-json') copyText(JSON.stringify(routePlan(selectedTask()), null, 2), '已复制 RoutePlan JSON。');
    if (action === 'toggle-package-menu') showToast('Demo：这里预留复制、导出清单和删除等任务包操作。');
  }

  function bindEvents() {
    document.addEventListener('click', handleClick);
    document.addEventListener('input', event => {
      if (event.target.id === 'markdownEditor' && state.reader) state.reader.draft = event.target.value;
    });
    document.addEventListener('error', event => {
      if (event.target.matches?.('.markdown-image img')) event.target.closest('.markdown-image')?.classList.add('broken');
    }, true);
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && $('#imageLightbox').classList.contains('open')) {
        closeImageLightbox();
        return;
      }
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's' && state.reader?.editing) {
        event.preventDefault();
        const item = readerItem(selectedTask());
        if (item) {
          item.markdown = state.reader.draft;
          item.userEdited = true;
          state.reader.editing = false;
          renderDrawer();
          showToast('Markdown 已保存到当前 Demo 会话。');
        }
      }
    });
    $('#newPackageBtn').addEventListener('click', openPackageModal);
    $('#newPackageTopBtn').addEventListener('click', openPackageModal);
    $('#modalClose').addEventListener('click', closePackageModal);
    $('#modalCancel').addEventListener('click', closePackageModal);
    $('#packageModal').addEventListener('click', event => {
      if (event.target === $('#packageModal')) closePackageModal();
    });
    $('#newPackageForm').addEventListener('submit', createPackage);
    $('#newPackageKeywords').addEventListener('input', () => {
      const keywords = parseKeywords($('#newPackageKeywords').value);
      $('#parsePreview').innerHTML = `<span class="parse-count">${keywords.length} 个关键词</span><span>${keywords.length ? keywords.map(escapeHtml).join(' · ') : '提交后自动去重并创建任务'}</span>`;
    });
    $('#revisionClose').addEventListener('click', closeRevisionModal);
    $('#revisionCancel').addEventListener('click', closeRevisionModal);
    $('#revisionSubmit').addEventListener('click', submitRevision);
    $('#revisionModal').addEventListener('click', event => {
      if (event.target === $('#revisionModal')) closeRevisionModal();
    });
    $('#drawerClose').addEventListener('click', closeDetail);
    $('#drawerBackdrop').addEventListener('click', closeDetail);
    $('#imageLightbox').addEventListener('click', event => {
      if (event.target === $('#imageLightbox')) closeImageLightbox();
    });
    $('#packageSearch').addEventListener('input', event => {
      state.packageQuery = event.target.value;
      renderPackages();
    });
    $('#taskSearch').addEventListener('input', event => {
      state.taskSearch = event.target.value;
      renderQueue(selectedPackage());
    });
    $('#taskFilter').addEventListener('change', event => {
      state.taskFilter = event.target.value;
      renderQueue(selectedPackage());
    });
    $('#openOutputBtn').addEventListener('click', () => showToast(`Demo：输出目录为 ${selectedPackage().outputDir}`));
    $('#copyManifestBtn').addEventListener('click', () => copyText(packageSummary(selectedPackage()), '已复制批次摘要。'));
  }

  window.setMode = () => {};
  window.__batchDemo = { packages, state, renderMarkdown };
  bindEvents();
  renderAll();
})();
