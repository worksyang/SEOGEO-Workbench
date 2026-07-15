/**
 * 文章List — 真实数据驱动
 * 调用 /api/articles 和 /api/articles/accounts
 * 复用 openArtByUrl 打开文章抽屉, selectAccount 跳转账号透视
 */

(function () {
  'use strict';

  // ── 状态 ──────────────────────────────────────────
  var state = {
    sort: 'reads',
    timeRange: 15,
    minHits: 0,
    account: '',       // account_id, 空为全部
    accountName: '全部账号',
    search: '',
    page: 1,
    pageSize: 50,
    total: 0,
    totalPages: 0
  };

  var alSavedState = null;
  var initialized = false;
  var loading = false;

  // ── API 调用 ──────────────────────────────────────
  function buildParams(extra) {
    var p = new URLSearchParams();
    p.set('page', state.page);
    p.set('page_size', state.pageSize);
    p.set('sort', state.sort);
    p.set('time_range', state.timeRange);
    p.set('min_hits', state.minHits);
    if (state.account) p.set('account', state.account);
    if (state.search) p.set('search', state.search);
    if (extra) Object.keys(extra).forEach(function (k) { p.set(k, extra[k]); });
    return p.toString();
  }

  async function fetchArticles() {
    loading = true;
    renderLoading();
    try {
      var resp = await fetch('/api/articles?' + buildParams(), { cache: 'no-store' });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      var data = await resp.json();
      state.total = data.total;
      state.totalPages = Math.max(1, Math.ceil(data.total / state.pageSize));
      renderTable(data.articles);
      renderPagination();
    } catch (e) {
      renderError('加载文章列表失败: ' + e.message);
    } finally {
      loading = false;
    }
  }

  var allAccounts = [];

  async function fetchAccounts() {
    try {
      var resp = await fetch('/api/articles/accounts', { cache: 'no-store' });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      var data = await resp.json();
      allAccounts = data.accounts || [];
      renderAcctDropdown();
    } catch (e) {
      // 静默失败, 下拉列表保持空
    }
  }

  // ── 工具函数 (复用 monitor.js 的 escapeHtml / jsq) ──────────
  function esc(s) {
    if (typeof s !== 'string') s = String(s || '');
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function fmtNum(n) {
    if (n == null) return '—';
    if (n >= 10000) return (n / 10000).toFixed(1) + '万';
    return n.toLocaleString();
  }

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

  function dateShort(s) {
    if (!s) return '—';
    return s.length >= 10 ? s.substring(0, 10) : s;
  }

  function onRankToneClass(days) {
    var n = Number(days || 0);
    if (n >= 10) return 'tone-5';
    if (n >= 7) return 'tone-4';
    if (n >= 5) return 'tone-3';
    if (n >= 3) return 'tone-2';
    return 'tone-1';
  }

  // ── 渲染: 文章表格 ────────────────────────────────
  function renderTable(articles) {
    var tbody = document.getElementById('alTableBody');
    var countEl = document.getElementById('alResultCount');
    if (countEl) countEl.textContent = state.total + ' 篇文章';

    if (!articles || articles.length === 0) {
      tbody.innerHTML = '<tr><td colspan="10" class="al-empty">没有符合条件的文章</td></tr>';
      return;
    }

    var pageStart = (state.page - 1) * state.pageSize;
    var html = '';

    articles.forEach(function (a, i) {
      var rank = pageStart + i + 1;
      var kwHtml = a.hit_keywords && a.hit_keywords.length > 0
        ? a.hit_keywords.map(function (k) { return '<span class="al-kw-tag">' + esc(k) + '</span>'; }).join('')
        : '<span style="color:#ccc;font-size:11px">—</span>';

      // 用 data 属性存储, 避免引号转义问题
      var dataAttrs = 'data-article-id="' + esc(a.article_id || '') + '" ' +
        'data-url="' + esc(a.url) + '" ' +
        'data-title="' + esc(a.title) + '" ' +
        'data-content-path="' + esc(a.content_file_path || '') + '" ' +
        'data-rank="' + rank + '" ' +
        'data-account="' + esc(a.account_name) + '" ' +
        'data-read-count="' + (a.read_count != null ? a.read_count : '') + '" ' +
        'data-like-count="' + (a.like_count != null ? a.like_count : '') + '"';

      var avatarHtml = a.account_headimg
        ? '<img class="acct-chip-avatar" src="' + esc(a.account_headimg) + '" alt="" loading="lazy" onerror="this.style.display=\'none\'">'
        : '';
      var acctHtml = '<span class="account-chip al-acct" data-acct-select="' + esc(a.account_name) + '">' + avatarHtml + '<span class="acct-chip-name">' + esc(a.account_name) + '</span></span>';
      var onRankDays = Number(a.on_rank_days || 0);
      var onRankHtml = '<span class="snapshot-hit-chip ' + onRankToneClass(onRankDays) + '">在榜 ' + onRankDays + ' 天</span>';

      html += '<tr class="al-art-row" ' + dataAttrs + '>' +
        '<td class="al-col-rank"><span class="al-rank ' + rankClass(rank) + '">' + rank + '</span></td>' +
        '<td class="al-col-title"><span class="al-title">' + esc(a.title) + '</span></td>' +
        '<td class="al-col-acct">' + acctHtml + '</td>' +
        '<td class="al-col-reads"><span class="al-num al-num-hi">' + fmtNum(a.read_count) + '</span></td>' +
        '<td class="al-col-onrank">' + onRankHtml + '</td>' +
        '<td class="al-col-likes"><span class="al-num">' + fmtNum(a.like_count) + '</span></td>' +
        '<td class="al-col-hits"><span class="al-hit-badge ' + hitBadgeClass(a.hit_count) + '">' + a.hit_count + '</span></td>' +
        '<td class="al-col-kws"><div class="al-kw-tags">' + kwHtml + '</div></td>' +
        '<td class="al-col-score"><span class="al-num al-score-val">' + Math.round(a.account_score || 0) + '</span></td>' +
        '<td class="al-col-date"><span class="al-date">' + dateShort(a.published_at) + '</span></td>' +
        '</tr>';
    });
    tbody.innerHTML = html;
  }

  function renderLoading() {
    var tbody = document.getElementById('alTableBody');
    if (tbody) tbody.innerHTML = '<tr><td colspan="10" class="al-empty">正在加载…</td></tr>';
  }

  function renderError(msg) {
    var tbody = document.getElementById('alTableBody');
    if (tbody) tbody.innerHTML = '<tr><td colspan="10" class="al-empty" style="color:#dc2626">' + esc(msg) + '</td></tr>';
  }

  // ── 渲染: 分页 ────────────────────────────────────
  function renderPagination() {
    var container = document.getElementById('alPagination');
    if (!container) return;
    if (state.totalPages <= 1) {
      container.innerHTML = '<span class="al-page-info">共 ' + state.total + ' 篇</span>';
      return;
    }

    var cur = state.page;
    var parts = [];

    parts.push('<span class="al-page-info">第 ' + cur + '/' + state.totalPages + ' 页 · 共 ' + state.total + ' 篇</span>');

    parts.push('<div class="al-page-btns">');
    parts.push('<button class="al-page-btn" onclick="window.alGoPage(1)"' + (cur <= 1 ? ' disabled' : '') + '>首页</button>');
    parts.push('<button class="al-page-btn" onclick="window.alGoPage(' + (cur - 1) + ')"' + (cur <= 1 ? ' disabled' : '') + '>上一页</button>');

    // 页码: 显示当前页前后各2页
    var start = Math.max(1, cur - 2);
    var end = Math.min(state.totalPages, cur + 2);
    for (var p = start; p <= end; p++) {
      parts.push('<button class="al-page-btn' + (p === cur ? ' active' : '') + '" onclick="window.alGoPage(' + p + ')">' + p + '</button>');
    }

    parts.push('<button class="al-page-btn" onclick="window.alGoPage(' + (cur + 1) + ')"' + (cur >= state.totalPages ? ' disabled' : '') + '>下一页</button>');
    parts.push('<button class="al-page-btn" onclick="window.alGoPage(' + state.totalPages + ')"' + (cur >= state.totalPages ? ' disabled' : '') + '>末页</button>');
    parts.push('</div>');

    container.innerHTML = parts.join('');
  }

  // ── 渲染: 账号下拉 ────────────────────────────────
  var acctDropdownOpen = false;
  var acctSearchTerm = '';

  function renderAcctDropdown() {
    var list = document.getElementById('alAcctList');
    if (!list) return;

    var filtered = allAccounts;
    if (acctSearchTerm) {
      var term = acctSearchTerm.toLowerCase();
      filtered = filtered.filter(function (a) {
        return a.name.toLowerCase().indexOf(term) !== -1;
      });
    }

    if (filtered.length === 0) {
      list.innerHTML = '<div class="al-acct-empty">没有匹配的账号</div>';
      return;
    }

    var visible = filtered.slice(0, 200);
    var html = '';

    html += '<div class="al-acct-option' + (state.account === '' ? ' selected' : '') + '" data-acct="" data-name="全部账号">' +
      '<span class="al-acct-option-name">全部账号</span>' +
      '<span class="al-acct-option-count">' + state.total + ' 篇</span>' +
      '</div>';

    visible.forEach(function (a) {
      html += '<div class="al-acct-option' + (state.account === a.account_id ? ' selected' : '') + '" data-acct="' + esc(a.account_id) + '" data-name="' + esc(a.name) + '">' +
        '<span class="al-acct-option-name">' + esc(a.name) + '</span>' +
        '<span class="al-acct-option-count">' + a.article_count + ' 篇</span>' +
        '</div>';
    });

    if (filtered.length > 200) {
      html += '<div class="al-acct-empty">还有 ' + (filtered.length - 200) + ' 个账号，请输入关键词搜索…</div>';
    }

    list.innerHTML = html;

    list.querySelectorAll('.al-acct-option').forEach(function (opt) {
      opt.addEventListener('click', function () {
        state.account = opt.dataset.acct;
        state.accountName = opt.dataset.name || '全部账号';
        state.page = 1;
        closeAcctDropdown();
        updateAcctTrigger();
        fetchArticles();
      });
    });
  }

  function updateAcctTrigger() {
    var label = document.getElementById('alAcctTriggerLabel');
    var count = document.getElementById('alAcctTriggerCount');
    var trigger = document.getElementById('alAcctTrigger');
    if (!label) return;
    if (state.account === '') {
      label.textContent = '全部账号';
      if (count) count.textContent = '';
      if (trigger) trigger.classList.remove('active');
    } else {
      label.textContent = state.accountName;
      var matched = allAccounts.find(function (a) { return a.account_id === state.account; });
      if (count) count.textContent = matched ? matched.article_count + ' 篇' : '';
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
    if (trigger) trigger.classList.toggle('active', state.account !== '');
  }

  function toggleAcctDropdown() {
    if (acctDropdownOpen) closeAcctDropdown();
    else openAcctDropdown();
  }

  // ── 事件绑定 ──────────────────────────────────────
  function bindEvents() {
    // 表格行点击 — 事件委托
    var tbody = document.getElementById('alTableBody');
    if (tbody) {
      tbody.addEventListener('click', function (e) {
        // 账号点击 → 跳转账号透视
        var acctEl = e.target.closest('[data-acct-select]');
        if (acctEl) {
          e.stopPropagation();
          var name = acctEl.dataset.acctSelect;
          if (name) window.alSelectAccount(name);
          return;
        }
        // 行点击 → 打开文章抽屉
        var row = e.target.closest('.al-art-row');
        if (row) {
          var url = row.dataset.url || '';
          var title = row.dataset.title || '';
          var contentPath = row.dataset.contentPath || '';
          var meta = {
            article_id: row.dataset.articleId || '',
            rank: parseInt(row.dataset.rank, 10) || null,
            account: row.dataset.account || '',
            read_count: row.dataset.readCount !== '' ? parseInt(row.dataset.readCount, 10) : null,
            like_count: row.dataset.likeCount !== '' ? parseInt(row.dataset.likeCount, 10) : null
          };
          window.alOpenArticle(url, title, contentPath, meta);
        }
      });
    }

    // 排序
    var sortContainer = document.getElementById('alSortChips');
    if (sortContainer) {
      sortContainer.querySelectorAll('.al-chip').forEach(function (btn) {
        btn.addEventListener('click', function () {
          sortContainer.querySelectorAll('.al-chip').forEach(function (b) { b.classList.remove('active'); });
          btn.classList.add('active');
          state.sort = btn.dataset.sort;
          state.page = 1;
          fetchArticles();
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
          state.page = 1;
          fetchArticles();
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
          state.page = 1;
          fetchArticles();
        });
      });
    }

    // 标题搜索
    var search = document.getElementById('alSearch');
    if (search) {
      var debounceTimer = null;
      search.addEventListener('input', function () {
        clearTimeout(debounceTimer);
        var val = this.value.trim();
        debounceTimer = setTimeout(function () {
          state.search = val;
          state.page = 1;
          fetchArticles();
        }, 300);
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

  // ── 状态保存/恢复 ─────────────────────────────────
  function saveState() {
    alSavedState = {
      sort: state.sort,
      timeRange: state.timeRange,
      minHits: state.minHits,
      account: state.account,
      accountName: state.accountName,
      search: state.search,
      page: state.page,
      scrollTop: document.getElementById('articleListView')?.scrollTop || 0
    };
  }

  function restoreState() {
    if (!alSavedState) return false;
    state.sort = alSavedState.sort;
    state.timeRange = alSavedState.timeRange;
    state.minHits = alSavedState.minHits;
    state.account = alSavedState.account;
    state.accountName = alSavedState.accountName;
    state.search = alSavedState.search;
    state.page = alSavedState.page;

    // 恢复 chip active 状态
    setActiveChip('alSortChips', 'sort', state.sort);
    setActiveChip('alTimeChips', 'time', String(state.timeRange));
    setActiveChip('alHitChips', 'hits', String(state.minHits));

    // 恢复搜索框
    var search = document.getElementById('alSearch');
    if (search) search.value = state.search;

    // 恢复账号 trigger
    updateAcctTrigger();

    // 重新加载数据, 完成后恢复滚动位置
    fetchArticles().then(function () {
      var view = document.getElementById('articleListView');
      if (view && alSavedState) {
        view.scrollTop = alSavedState.scrollTop;
      }
      alSavedState = null;
    });

    return true;
  }

  function setActiveChip(containerId, attr, val) {
    var container = document.getElementById(containerId);
    if (!container) return;
    container.querySelectorAll('.al-chip').forEach(function (btn) {
      btn.classList.toggle('active', btn.dataset[attr] === val);
    });
  }

  // ── 暴露给外部 ────────────────────────────────────
  window.alInit = function () {
    if (initialized) return;
    initialized = true;
    bindEvents();
    updateAcctTrigger();
    fetchAccounts();
    fetchArticles();
  };

  window.alSaveState = saveState;
  window.alRestoreState = restoreState;

  window.alRefresh = function () {
    if (!initialized) return;
    fetchAccounts();
    fetchArticles();
  };

  window.alGoPage = function (p) {
    if (loading) return;
    if (p < 1 || p > state.totalPages) return;
    state.page = p;
    fetchArticles();
    // 滚动到表格顶部
    var view = document.getElementById('articleListView');
    if (view) {
      var tableWrap = document.querySelector('.al-table-wrap');
      if (tableWrap) view.scrollTo({ top: tableWrap.offsetTop - 20, behavior: 'smooth' });
    }
  };

  window.alSelectAccount = function (name) {
    saveState();
    if (typeof selectAccount === 'function') {
      selectAccount(name);
    }
  };

  window.alOpenArticle = function (url, title, contentPath, meta) {
    if (typeof openArtByUrl === 'function') {
      openArtByUrl(url, title, contentPath, meta);
    }
  };

  // 自动初始化
  if (document.readyState !== 'loading') {
    // 由 monitor.js 的 refresh() 触发 alInit, 不自动调用
  } else {
    document.addEventListener('DOMContentLoaded', function () {
      // 同上, 等待 monitor.js 触发
    });
  }
})();
