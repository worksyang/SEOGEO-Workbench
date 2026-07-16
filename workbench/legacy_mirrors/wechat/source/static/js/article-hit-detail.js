(function () {
  const DETAIL_API_URL = '/api/article-hit-detail';
  const CONTENT_API_URL = '/api/article-content';
  const WECHAT_AUX_VERSION = 'wechat-v1';

  if (window.marked) {
    window.marked.setOptions({ gfm: true, breaks: true });
  }

  function $(id) {
    return document.getElementById(id);
  }

  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/[&<>"']/g, ch => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;'
      }[ch]))
      .replace(/`/g, '&#96;')
      .replace(/\$\{/g, '&#36;{');
  }

  function compact(value, max) {
    const text = String(value || '').trim();
    return text.length > max ? `${text.slice(0, max)}…` : text;
  }

  function formatDateTime(value) {
    if (!value) return '—';
    return String(value).replace('T', ' ').slice(0, 16);
  }

  function normalizeTitleForCompare(text) {
    return String(text || '')
      .normalize('NFKC')
      .toLowerCase()
      .replace(/\s+/g, '')
      .replace(/[·•|｜:：,，。！？!?—\-_\[\]【】()（）<>《》“”"'‘’`]+/g, '');
  }

  function preprocessArticleMarkdown(md, title) {
    const lines = String(md || '')
      .replace(/\u0000/g, '')
      .replace(/\r\n/g, '\n')
      .replace(/\r/g, '\n')
      .split('\n');
    const out = [];
    const titleKey = normalizeTitleForCompare(title);
    let skippedIntroHeading = false;
    let skippedLinkLine = false;

    for (const rawLine of lines) {
      const line = rawLine.replace(/\u200b/g, '').trimEnd();
      const trimmed = line.trim();
      if (!trimmed) {
        if (out.length && out[out.length - 1] !== '') out.push('');
        continue;
      }
      if (trimmed === 'StartFragment' || trimmed === 'EndFragment') continue;
      if (!skippedLinkLine && /^链接[:：]\s*https?:\/\//.test(trimmed)) {
        skippedLinkLine = true;
        continue;
      }
      if (!skippedIntroHeading && trimmed.startsWith('#')) {
        const headingText = trimmed.replace(/^#+\s*/, '');
        if (normalizeTitleForCompare(headingText) === titleKey) {
          skippedIntroHeading = true;
          continue;
        }
      }
      out.push(line);
    }

    while (out.length && out[0] === '') out.shift();
    while (out.length && out[out.length - 1] === '') out.pop();
    return out.join('\n');
  }

  function metricText(value) {
    return value == null ? '—' : String(value);
  }

  function wechatAuxUrl(path, params = {}) {
    const query = new URLSearchParams(params);
    query.set('wbv', WECHAT_AUX_VERSION);
    return `${path}?${query.toString()}`;
  }

  function detailUrl(article) {
    if (article?.article_id) return wechatAuxUrl('/article-hit-detail', { article_id: article.article_id });
    if (article?.url) return wechatAuxUrl('/article-hit-detail', { url: article.url });
    return wechatAuxUrl('/article-hit-detail');
  }

  function renderKeywordCloud(items) {
    if (!items || items.length < 3) return '';
    const maxHits = Math.max(1, ...items.map(item => Number(item.hit_count || 0)));
    const tags = items.map((item, idx) => {
      const hitCount = Number(item.hit_count || 0);
      const cls = idx === 0 || hitCount === maxHits ? 'xl' : idx < 3 ? 'lg' : '';
      const title = `${item.keyword || ''} · 命中 ${hitCount} 次 · 最好第 ${item.best_rank || '—'} 名`;
      return `<span class="${cls}" title="${esc(title)}">${esc(item.keyword || '未知关键词')}</span>`;
    }).join('');
    return `
      <section class="turnover-section article-detail-section article-cloud-section">
        <div class="turnover-section-head">
          <div>
            <h2>跨关键词词云</h2>
            <p>这篇文章命中的关键词越多，词云越能看出它覆盖了哪些搜索意图。</p>
          </div>
          <div class="turnover-section-stat">${items.length} 个命中关键词</div>
        </div>
        <div class="article-keyword-cloud" aria-label="跨关键词词云">${tags}</div>
      </section>`;
  }

  function renderHitGroups(groups) {
    if (!groups || !groups.length) {
      return '<div class="article-detail-empty">这篇文章还没有命中记录。</div>';
    }
    return `
      <div class="article-hit-groups">
        ${groups.map((group, idx) => `
          <details class="article-hit-group" ${idx === 0 ? 'open' : ''}>
            <summary>
              <span><b>${esc(group.keyword || '未知关键词')}</b><em>${esc(group.keyword_bucket || '未分类')} · ${esc(group.topic || '未归类')}</em></span>
              <strong>${group.hit_count || 0} 次命中</strong>
            </summary>
            <div class="article-hit-table">
              <div class="article-hit-row head">
                <span>命中时间</span>
                <span>批次</span>
                <span>排名</span>
                <span>文章记录</span>
              </div>
              ${(group.hits || []).map(hit => `
                <div class="article-hit-row">
                  <span>${esc(hit.captured_at_label || '—')}</span>
                  <span>${esc(hit.batch_id || hit.snapshot_id || '—')}</span>
                  <strong>#${esc(hit.rank || '—')}</strong>
                  <span>${esc(hit.article_id || '—')}</span>
                </div>`).join('')}
            </div>
          </details>`).join('')}
      </div>`;
  }

  function renderTimeline(events) {
    if (!events || !events.length) return '';
    return `
      <section class="turnover-section article-detail-section">
        <div class="turnover-section-head">
          <div>
            <h2>命中生命周期</h2>
            <p>不是正文生命周期，而是这篇文章在搜索结果里被命中的轨迹。</p>
          </div>
          <div class="turnover-section-stat">${events.length} 个节点</div>
        </div>
        <div class="article-hit-timeline">
          ${events.map((event, idx) => `
            <div class="article-hit-event ${idx === 0 ? 'is-first' : ''}">
              <i></i>
              <div>
                <span>${esc(event.label || '')}</span>
                <strong>${esc(event.title || '')}</strong>
                <small>${esc(event.description || '')}</small>
              </div>
            </div>`).join('')}
        </div>
      </section>`;
  }

  function sparkline(points, key, className) {
    const values = points.map(item => Number(item[key])).filter(value => Number.isFinite(value));
    if (!values.length) {
      return '<div class="article-detail-empty compact">暂无数据</div>';
    }
    const max = Math.max(1, ...values);
    return `<div class="article-sparkline ${className || ''}">${
      values.map(value => `<i style="height:${Math.max(12, Math.round(value / max * 92))}%"></i>`).join('')
    }</div>`;
  }

  function renderMetrics(detail) {
    const article = detail.article || {};
    const points = detail.metric_points || [];
    const readNow = metricText(article.read_count);
    const likeNow = metricText(article.like_count);
    return `
      <section class="article-detail-side-card">
        <h2>阅读与互动</h2>
        <p>${points.length > 1 ? '下面按已归并的文章记录展示趋势。' : '当前只有一个指标点，先展示最新指标；后续有多次抓取指标后会自然形成趋势。'}</p>
        <div class="article-metric-lines">
          <div class="article-metric-line">
            <div class="article-metric-head"><span>阅读</span><strong>${esc(readNow)}</strong></div>
            ${sparkline(points, 'read_count', 'reads')}
          </div>
          <div class="article-metric-line">
            <div class="article-metric-head"><span>点赞</span><strong>${esc(likeNow)}</strong></div>
            ${sparkline(points, 'like_count', 'likes')}
          </div>
          <div class="article-metric-line">
            <div class="article-metric-head"><span>朋友关注</span><strong>${esc(metricText(article.friends_follow_count))}</strong></div>
            ${sparkline(points, 'friends_follow_count', 'friends')}
          </div>
        </div>
      </section>`;
  }

  function renderContentFiles(detail) {
    const files = detail.content_files || [];
    if (!files.length) {
      return `
        <section class="article-detail-side-card">
          <h2>文章内容</h2>
          <p>当前没有找到本地正文文件，可以通过原文 URL 查看。</p>
        </section>`;
    }
    return `
      <section class="article-detail-side-card">
        <h2>文章内容</h2>
        <p>正文从本地 Markdown 读取；需要回看内容时，点下面按钮在侧边栏打开。</p>
        <div class="article-content-file-list">
          ${files.map(file => `
            <button class="article-content-open" type="button" data-content-path="${esc(file.path)}" data-content-title="${esc(file.title || detail.article?.title || '')}">
              <b>${file.is_primary ? '主正文' : '正文文件'}</b>
              <span>${esc(compact(file.path, 86))}</span>
            </button>`).join('')}
        </div>
      </section>`;
  }

  function renderUrlProfile(detail) {
    const profile = detail.url_profile || {};
    return `
      <section class="article-detail-side-card">
        <h2>URL 归并</h2>
        <div class="article-field-list">
          <span>文章记录：${profile.article_record_count || 0} 条</span>
          <span>文章 ID：${(profile.article_ids || []).map(esc).join('、') || '—'}</span>
          <span>标题版本：${(profile.title_variants || []).map(item => esc(compact(item, 26))).join(' / ') || '—'}</span>
        </div>
      </section>`;
  }

  function renderPage(detail) {
    const article = detail.article || {};
    const account = detail.account || {};
    const root = $('articleDetailRoot');
    $('articleDetailTopMeta').textContent = `${detail.keyword_count || 0} 个关键词 · ${detail.hit_count || 0} 次命中`;
    document.title = `${article.title || '文章命中详情'} - 文章命中详情`;

    root.innerHTML = `
      <section class="article-detail-hero article-detail-profile">
        <div class="article-detail-hero-main">
          <div class="article-detail-kicker">文章档案</div>
          <h1>${esc(article.title || '未知文章')}</h1>
          <div class="article-detail-meta">
            <span>${esc(account.name || '未知账号')}</span>
            <span>发布时间：${esc(formatDateTime(article.published_at))}</span>
            <span>article_id: ${esc(article.article_id || '—')}</span>
            ${article.original_article_count != null ? `<span>原创篇数：${esc(article.original_article_count)}</span>` : ''}
          </div>
          ${article.url ? `
            <div class="article-url-block">
              <span>原文 URL</span>
              <a href="${esc(article.url)}" target="_blank" rel="noopener">${esc(article.url)}</a>
            </div>` : ''}
        </div>

        <aside class="article-account-card">
          <span>账号身份</span>
          <strong>${esc(account.name || '未知账号')}</strong>
          <div class="article-account-facts">
            <div><b>account_id</b><em>${esc(account.account_id || '—')}</em></div>
            <div><b>首次命中</b><em>${esc(formatDateTime(article.first_seen_at))}</em></div>
            <div><b>最近命中</b><em>${esc(formatDateTime(article.last_seen_at))}</em></div>
          </div>
        </aside>
      </section>

      ${renderKeywordCloud(detail.keyword_cloud || [])}

      <section class="article-detail-layout">
        <div class="article-detail-main">
          <section class="turnover-section article-detail-section">
            <div class="turnover-section-head">
              <div>
                <h2>关键词命中明细</h2>
                <p>按关键词分组；同一个关键词命中过很多次，就在组内展开看。</p>
              </div>
              <div class="turnover-section-stat">${detail.hit_count || 0} 条记录</div>
            </div>
            ${renderHitGroups(detail.keyword_groups || [])}
          </section>
          ${renderTimeline(detail.timeline_events || [])}
        </div>

        <aside class="article-detail-side">
          ${renderMetrics(detail)}
          ${renderContentFiles(detail)}
          ${renderUrlProfile(detail)}
        </aside>
      </section>`;

    root.querySelectorAll('.article-content-open').forEach(button => {
      button.addEventListener('click', () => {
        openArticleContent(button.dataset.contentPath || '', button.dataset.contentTitle || article.title || '');
      });
    });
  }

  async function openArticleContent(contentPath, title) {
    const drawer = $('drawer');
    const drawerTitle = $('drawerTitle');
    const body = $('drawerBody');
    const foot = $('drawerFoot');
    if (!drawer || !drawerTitle || !body || !foot || !contentPath) return;
    drawerTitle.textContent = title || '文章正文';
    drawer.classList.add('open');
    body.innerHTML = '<div style="color:#94a3b8">正在加载正文…</div>';
    foot.innerHTML = `<span>${esc(contentPath)}</span>`;
    try {
      const resp = await fetch(`${CONTENT_API_URL}?path=${encodeURIComponent(contentPath)}`, { cache: 'no-store' });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const payload = await resp.json();
      const md = preprocessArticleMarkdown(payload.markdown || '', title);
      body.innerHTML = window.marked
        ? window.marked.parse(md).replace(/<img\s/gi, '<img referrerpolicy="no-referrer" ')
        : `<pre>${esc(md)}</pre>`;
    } catch (err) {
      body.innerHTML = `<div style="color:#991b1b">无法加载正文：${esc(err.message || err)}</div>
        <div style="margin-top:8px;font-size:12px;color:#999">路径：${esc(contentPath)}</div>`;
    }
  }

  function closeDrawer() {
    const drawer = $('drawer');
    if (drawer) drawer.classList.remove('open');
  }

  async function load() {
    const params = new URLSearchParams(window.location.search);
    const articleId = params.get('article_id') || '';
    const url = params.get('url') || '';
    if (!articleId && !url) {
      $('articleDetailRoot').innerHTML = '<div class="article-detail-loading is-error">缺少 article_id 或 url</div>';
      return;
    }
    try {
      const query = articleId ? `article_id=${encodeURIComponent(articleId)}` : `url=${encodeURIComponent(url)}`;
      const resp = await fetch(`${DETAIL_API_URL}?${query}`, { cache: 'no-store' });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const detail = await resp.json();
      renderPage(detail);
      const back = $('articleDetailBack');
      if (back && document.referrer && document.referrer !== window.location.href) {
        back.href = document.referrer;
      }
    } catch (err) {
      $('articleDetailRoot').innerHTML = `<div class="article-detail-loading is-error">加载失败：${esc(err.message || err)}</div>`;
    }
  }

  window.closeArticleDetailDrawer = closeDrawer;
  window.articleHitDetailUrl = detailUrl;
  window.addEventListener('keydown', event => {
    if (event.key === 'Escape') closeDrawer();
  });

  load();
})();
