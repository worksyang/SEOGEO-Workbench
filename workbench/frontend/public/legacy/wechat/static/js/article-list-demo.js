/**
 * 文章List Demo — 纯静态假数据，仅用于 UI 原型验证
 * 不对接任何真实接口，所有数据为内嵌 mock
 */

(function () {
  'use strict';

  // ── 假数据 ──────────────────────────────────────────
  var MOCK_KEYWORDS = [
    '大模型', 'RAG', 'Agent', '多模态', '微调',
    '提示工程', '向量数据库', 'RLHF', 'MoE', '推理优化'
  ];

  var ACCOUNT_PREFIXES = [
    'AI', '智能', '未来', '科技', '深度', '量子', '极客', '前沿',
    '数据', '算法', '机器', '神经', '认知', '创新', '数字', '云'
  ];
  var ACCOUNT_SUFFIXES = [
    '前沿', '实验室', '观察', '日报', '周刊', '洞察', '研究所',
    '评论', '笔记', '视界', '前线', '内参', '简报', '百科', '说'
  ];

  function generateAccounts(total) {
    var accounts = [];
    var used = {};
    var i = 0;
    while (accounts.length < total && i < total * 20) {
      var p = ACCOUNT_PREFIXES[Math.floor(Math.random() * ACCOUNT_PREFIXES.length)];
      var s = ACCOUNT_SUFFIXES[Math.floor(Math.random() * ACCOUNT_SUFFIXES.length)];
      var name = p + s;
      // 加些随机后缀避免重复
      if (used[name]) {
        name += Math.floor(Math.random() * 900 + 100);
      }
      if (!used[name]) {
        used[name] = true;
        accounts.push(name);
      }
      i++;
    }
    return accounts;
  }

  // 生成 834 个账号，贴近真实数量
  var ALL_ACCOUNT_NAMES = generateAccounts(834);

  function pick(arr) { return arr[Math.floor(Math.random() * arr.length)]; }
  function pickN(arr, n) {
    var copy = arr.slice();
    var result = [];
    for (var i = 0; i < n && copy.length > 0; i++) {
      var idx = Math.floor(Math.random() * copy.length);
      result.push(copy.splice(idx, 1)[0]);
    }
    return result;
  }
  function randomInt(min, max) { return Math.floor(Math.random() * (max - min + 1)) + min; }

  var TITLE_TEMPLATES = [
    'GPT-5 发布在即，OpenAI 的下一步棋该怎么走？',
    'RAG 已死？2026 年知识增强的三个新方向',
    'Agent 框架横评：LangGraph vs AutoGen vs CrewAI',
    '多模态大模型实战：从 CLIP 到 GPT-4o 的演进',
    '微调一个垂直领域模型的完整指南',
    '提示工程进阶：Chain-of-Thought 的 5 种变体',
    '向量数据库选型：Pinecone vs Milvus vs Weaviate',
    'RLHF 之外：DPO 和 ORPO 的对比实验',
    'MoE 架构深度解析：DeepSeek-V3 的稀疏激活策略',
    '推理优化全景：量化、蒸馏、剪枝怎么选',
    'AI 编程助手对决：Cursor vs Copilot vs Windsurf',
    '开源大模型生态 2026 年度盘点',
    '从 0 到 1 搭建企业级 AI 知识库',
    '端侧大模型的突围之路：Apple Intelligence 启示录',
    'Sora 之后的视频生成：技术路线与商业化',
    '大模型 API 价格战背后的经济学',
    'AI Agent 在企业落地中的 5 个坑',
    '具身智能元年：机器人 + 大模型的化学反应',
    'AI 搜索之战：Perplexity vs Google vs 秘塔',
    '大模型幻觉问题：根源分析与缓解策略'
  ];

  function generateMockArticles(count) {
    var articles = [];
    var now = Date.now();
    var dayMs = 86400000;
    for (var i = 0; i < count; i++) {
      var daysAgo = randomInt(0, 40);
      var publishTs = now - daysAgo * dayMs - randomInt(0, dayMs);
      var date = new Date(publishTs);
      var dateStr = date.getFullYear() + '-' +
        String(date.getMonth() + 1).padStart(2, '0') + '-' +
        String(date.getDate()).padStart(2, '0');
      var hitCount = randomInt(0, 5);
      var hits = hitCount > 0 ? pickN(MOCK_KEYWORDS, hitCount) : [];
      articles.push({
        id: i + 1,
        title: pick(TITLE_TEMPLATES),
        account: pick(ALL_ACCOUNT_NAMES),
        reads: randomInt(100, 100000),
        likes: randomInt(5, 5000),
        hitCount: hitCount,
        hitKeywords: hits,
        accountScore: randomInt(30, 98),
        publishTime: publishTs,
        publishDate: dateStr
      });
    }
    return articles;
  }

  var ALL_ARTICLES = generateMockArticles(300);

  // 预计算每个账号的文章数
  var ACCOUNT_ARTICLE_COUNTS = {};
  ALL_ACCOUNT_NAMES.forEach(function (a) { ACCOUNT_ARTICLE_COUNTS[a] = 0; });
  ALL_ARTICLES.forEach(function (a) {
    ACCOUNT_ARTICLE_COUNTS[a.account] = (ACCOUNT_ARTICLE_COUNTS[a.account] || 0) + 1;
  });

  // ── 状态 ──────────────────────────────────────────
  var state = {
    sort: 'reads',
    timeRange: 15,
    minHits: 0,
    account: 'all',
    search: ''
  };

  var initialized = false;

  // ── 筛选与排序 ──────────────────────────────────────
  function filterArticles() {
    var now = Date.now();
    var dayMs = 86400000;
    var cutoff = state.timeRange > 0 ? now - state.timeRange * dayMs : 0;

    return ALL_ARTICLES.filter(function (a) {
      if (state.timeRange > 0 && a.publishTime < cutoff) return false;
      if (a.hitCount < state.minHits) return false;
      if (state.account !== 'all' && a.account !== state.account) return false;
      if (state.search && a.title.indexOf(state.search) === -1) return false;
      return true;
    });
  }

  function sortArticles(list) {
    var key = state.sort;
    return list.slice().sort(function (a, b) {
      return b[key] - a[key];
    });
  }

  // ── 账号下拉搜索选择器 ──────────────────────────────
  var acctDropdownOpen = false;
  var acctSearchTerm = '';

  function renderAcctDropdown() {
    var list = document.getElementById('alAcctList');
    if (!list) return;

    var filtered = state.account === 'all'
      ? ALL_ACCOUNT_NAMES
      : ALL_ACCOUNT_NAMES;

    if (acctSearchTerm) {
      filtered = filtered.filter(function (name) {
        return name.toLowerCase().indexOf(acctSearchTerm.toLowerCase()) !== -1;
      });
    }

    // 有文章的账号排前面
    filtered = filtered.slice().sort(function (a, b) {
      var ca = ACCOUNT_ARTICLE_COUNTS[a] || 0;
      var cb = ACCOUNT_ARTICLE_COUNTS[b] || 0;
      if (ca !== cb) return cb - ca;
      return a.localeCompare(b);
    });

    if (filtered.length === 0) {
      list.innerHTML = '<div class="al-acct-empty">没有匹配的账号</div>';
      return;
    }

    // 只渲染前 200 条，避免 DOM 过重
    var visible = filtered.slice(0, 200);
    var html = '';

    // "全部" 选项
    html += '<div class="al-acct-option' + (state.account === 'all' ? ' selected' : '') + '" data-acct="all">' +
      '<span class="al-acct-option-name">全部账号</span>' +
      '<span class="al-acct-option-count">' + ALL_ARTICLES.length + ' 篇</span>' +
      '</div>';

    visible.forEach(function (name) {
      var count = ACCOUNT_ARTICLE_COUNTS[name] || 0;
      html += '<div class="al-acct-option' + (state.account === name ? ' selected' : '') + '" data-acct="' + name + '">' +
        '<span class="al-acct-option-name">' + name + '</span>' +
        '<span class="al-acct-option-count">' + (count > 0 ? count + ' 篇' : '—') + '</span>' +
        '</div>';
    });

    if (filtered.length > 200) {
      html += '<div class="al-acct-empty">还有 ' + (filtered.length - 200) + ' 个账号，请输入关键词搜索…</div>';
    }

    list.innerHTML = html;

    list.querySelectorAll('.al-acct-option').forEach(function (opt) {
      opt.addEventListener('click', function () {
        state.account = opt.dataset.acct;
        closeAcctDropdown();
        updateAcctTrigger();
        renderTable();
      });
    });
  }

  function updateAcctTrigger() {
    var label = document.getElementById('alAcctTriggerLabel');
    var count = document.getElementById('alAcctTriggerCount');
    var trigger = document.getElementById('alAcctTrigger');
    if (!label) return;
    if (state.account === 'all') {
      label.textContent = '全部账号';
      if (count) count.textContent = '';
      if (trigger) trigger.classList.remove('active');
    } else {
      label.textContent = state.account;
      var n = ACCOUNT_ARTICLE_COUNTS[state.account] || 0;
      if (count) count.textContent = n + ' 篇';
      if (trigger) trigger.classList.add('active');
    }
  }

  function openAcctDropdown() {
    var dropdown = document.getElementById('alAcctDropdown');
    var trigger = document.getElementById('alAcctTrigger');
    var search = document.getElementById('alAcctSearch');
    if (!dropdown) return;
    acctDropdownOpen = true;
    dropdown.classList.add('open');
    if (trigger) trigger.classList.add('active');
    if (search) {
      acctSearchTerm = '';
      search.value = '';
      setTimeout(function () { search.focus(); }, 50);
    }
    renderAcctDropdown();
  }

  function closeAcctDropdown() {
    var dropdown = document.getElementById('alAcctDropdown');
    var trigger = document.getElementById('alAcctTrigger');
    acctDropdownOpen = false;
    if (dropdown) dropdown.classList.remove('open');
    // 恢复 trigger 的 active 状态取决于是否选中了具体账号
    if (trigger) trigger.classList.toggle('active', state.account !== 'all');
  }

  function toggleAcctDropdown() {
    if (acctDropdownOpen) closeAcctDropdown();
    else openAcctDropdown();
  }

  // ── 渲染 ──────────────────────────────────────────
  function hitBadgeClass(n) {
    if (n === 0) return 'al-hit-0';
    if (n === 1) return 'al-hit-1';
    if (n === 2) return 'al-hit-2';
    if (n === 3) return 'al-hit-3';
    return 'al-hit-4plus';
  }

  function rankClass(n) {
    if (n === 1) return 'top1';
    if (n === 2) return 'top2';
    if (n === 3) return 'top3';
    return '';
  }

  function fmtNum(n) {
    if (n >= 10000) return (n / 10000).toFixed(1) + '万';
    return n.toLocaleString();
  }

  function renderTable() {
    var filtered = sortArticles(filterArticles());
    var tbody = document.getElementById('alTableBody');
    var countEl = document.getElementById('alResultCount');
    if (countEl) countEl.textContent = filtered.length + ' 篇文章';

    if (filtered.length === 0) {
      tbody.innerHTML = '<tr><td colspan="9" class="al-empty">没有符合条件的文章</td></tr>';
      return;
    }

    var html = '';
    filtered.forEach(function (a, i) {
      var rank = i + 1;
      var kwHtml = a.hitKeywords.length > 0
        ? a.hitKeywords.map(function (k) { return '<span class="al-kw-tag">' + k + '</span>'; }).join('')
        : '<span style="color:#ccc;font-size:11px">—</span>';

      html += '<tr>' +
        '<td class="al-col-rank"><span class="al-rank ' + rankClass(rank) + '">' + rank + '</span></td>' +
        '<td class="al-col-title"><span class="al-title">' + a.title + '</span></td>' +
        '<td class="al-col-acct"><span class="al-acct">' + a.account + '</span></td>' +
        '<td class="al-col-reads"><span class="al-num al-num-hi">' + fmtNum(a.reads) + '</span></td>' +
        '<td class="al-col-likes"><span class="al-num">' + fmtNum(a.likes) + '</span></td>' +
        '<td class="al-col-hits"><span class="al-hit-badge ' + hitBadgeClass(a.hitCount) + '">' + a.hitCount + '</span></td>' +
        '<td class="al-col-kws"><div class="al-kw-tags">' + kwHtml + '</div></td>' +
        '<td class="al-col-score"><span class="al-num al-score-val">' + a.accountScore + '</span></td>' +
        '<td class="al-col-date"><span class="al-date">' + a.publishDate + '</span></td>' +
        '</tr>';
    });
    tbody.innerHTML = html;
  }

  function bindEvents() {
    // 排序
    var sortContainer = document.getElementById('alSortChips');
    if (sortContainer) {
      sortContainer.querySelectorAll('.al-chip').forEach(function (btn) {
        btn.addEventListener('click', function () {
          sortContainer.querySelectorAll('.al-chip').forEach(function (b) { b.classList.remove('active'); });
          btn.classList.add('active');
          state.sort = btn.dataset.sort;
          renderTable();
        });
      });
    }

    // 时间
    var timeContainer = document.getElementById('alTimeChips');
    if (timeContainer) {
      timeContainer.querySelectorAll('.al-chip').forEach(function (btn) {
        btn.addEventListener('click', function () {
          timeContainer.querySelectorAll('.al-chip').forEach(function (b) { b.classList.remove('active'); });
          btn.classList.add('active');
          state.timeRange = parseInt(btn.dataset.time, 10);
          renderTable();
        });
      });
    }

    // 命中词数
    var hitContainer = document.getElementById('alHitChips');
    if (hitContainer) {
      hitContainer.querySelectorAll('.al-chip').forEach(function (btn) {
        btn.addEventListener('click', function () {
          hitContainer.querySelectorAll('.al-chip').forEach(function (b) { b.classList.remove('active'); });
          btn.classList.add('active');
          state.minHits = parseInt(btn.dataset.hits, 10);
          renderTable();
        });
      });
    }

    // 标题搜索
    var search = document.getElementById('alSearch');
    if (search) {
      search.addEventListener('input', function () {
        state.search = this.value.trim();
        renderTable();
      });
    }

    // 账号选择器
    var trigger = document.getElementById('alAcctTrigger');
    if (trigger) {
      trigger.addEventListener('click', function (e) {
        e.stopPropagation();
        toggleAcctDropdown();
      });
    }

    var acctSearch = document.getElementById('alAcctSearch');
    if (acctSearch) {
      acctSearch.addEventListener('input', function () {
        acctSearchTerm = this.value.trim();
        renderAcctDropdown();
      });
      acctSearch.addEventListener('click', function (e) { e.stopPropagation(); });
    }

    // 点击外部关闭下拉
    document.addEventListener('click', function (e) {
      if (!acctDropdownOpen) return;
      var picker = document.getElementById('alAcctPicker');
      if (picker && !picker.contains(e.target)) {
        closeAcctDropdown();
      }
    });
  }

  function init() {
    if (initialized) return;
    initialized = true;
    updateAcctTrigger();
    bindEvents();
    renderTable();
  }

  // 暴露给外部调用（被 syncModeUi / refresh 调用）
  window.alInit = init;

  // 如果 DOM 已经就绪就自动初始化
  if (document.readyState !== 'loading') {
    init();
  } else {
    document.addEventListener('DOMContentLoaded', init);
  }
})();