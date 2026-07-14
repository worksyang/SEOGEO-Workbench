(function (global) {
  const STABILITY_RANK = { '常驻': 3, '活跃': 2, '闪现': 1 };
  const TURNOVER_FAST_THRESHOLD = 0.40;
  const TURNOVER_OBVIOUS_THRESHOLD = 0.25;
  const TURNOVER_LIGHT_THRESHOLD = 0.10;
  const MIN_TURNOVER_DAYS = 4;
  const MIN_TURNOVER_COMPARISONS = 4;

  function escapeHtml(value) {
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

  function articleKey(article) {
    if (!article) return '';
    return article.article_id || `${article.title || ''}|${article.url || ''}`;
  }

  function parseDate(value) {
    const [year, month, day] = String(value || '').split('-').map(Number);
    return new Date(Date.UTC(year || 1970, (month || 1) - 1, day || 1));
  }

  function formatDate(date) {
    const year = date.getUTCFullYear();
    const month = String(date.getUTCMonth() + 1).padStart(2, '0');
    const day = String(date.getUTCDate()).padStart(2, '0');
    return `${year}-${month}-${day}`;
  }

  function addDays(dateStr, offset) {
    const date = parseDate(dateStr);
    date.setUTCDate(date.getUTCDate() + offset);
    return formatDate(date);
  }

  function dateDiffDays(a, b) {
    return Math.round((parseDate(b) - parseDate(a)) / 86400000);
  }

  function dateRange(startDate, endDate) {
    const days = [];
    const total = Math.max(0, dateDiffDays(startDate, endDate));
    for (let idx = 0; idx <= total; idx += 1) {
      days.push(addDays(startDate, idx));
    }
    return days;
  }

  function longestStreak(days) {
    const uniq = [...new Set(days || [])].sort();
    if (!uniq.length) return 0;
    let best = 1;
    let current = 1;
    for (let idx = 1; idx < uniq.length; idx += 1) {
      if (dateDiffDays(uniq[idx - 1], uniq[idx]) === 1) {
        current += 1;
      } else {
        best = Math.max(best, current);
        current = 1;
      }
    }
    return Math.max(best, current);
  }

  function classifyStability({ dayRatio, runRatio, maxStreak }) {
    if (dayRatio >= 0.32 && (maxStreak >= 5 || runRatio >= 0.30)) return '常驻';
    if (dayRatio >= 0.16 || maxStreak >= 3 || runRatio >= 0.18) return '活跃';
    return '闪现';
  }

  function levelMeta(rate) {
    if (rate >= TURNOVER_FAST_THRESHOLD) return { label: '换得很快', color: '#ef4444', className: 'is-hot' };
    if (rate >= TURNOVER_OBVIOUS_THRESHOLD) return { label: '换得明显', color: '#f59e0b', className: 'is-active' };
    if (rate >= TURNOVER_LIGHT_THRESHOLD) return { label: '小幅换新', color: '#3b82f6', className: 'is-stable' };
    return { label: '基本没变', color: '#10b981', className: 'is-frozen' };
  }

  function observingMeta(turnover) {
    const maturity = turnover?.maturity || {};
    const observedDays = Number(maturity.observedDays || 0);
    const comparisons = Number(maturity.comparisons || 0);
    const minDays = Number(maturity.minDays || MIN_TURNOVER_DAYS);
    const minComparisons = Number(maturity.minComparisons || MIN_TURNOVER_COMPARISONS);
    return {
      label: '观察中',
      color: '#64748b',
      className: 'is-observing',
      reason: `已观察${observedDays}天、${comparisons}次对比；满${minDays}天且${minComparisons}次对比后再判断换新状态`
    };
  }

  function isMature(turnover) {
    return !!turnover?.maturity?.isMature;
  }

  function statusMeta(turnover) {
    if (!turnover) return null;
    return isMature(turnover) ? levelMeta(turnover.rate) : observingMeta(turnover);
  }

  function rateColor(rate) {
    if (rate == null || Number.isNaN(rate)) return '#edf2f7';
    if (rate < 0.10) return '#1a8754';
    if (rate < 0.20) return '#86d49b';
    if (rate < 0.30) return '#f5d36e';
    if (rate < 0.45) return '#f59e0b';
    if (rate < 0.60) return '#ef4444';
    return '#b91c1c';
  }

  function getLifeState({ present, prevPresent, nextPresent, runCount, firstSeen, date }) {
    if (!present) return 'absent';
    if (date === firstSeen) return 'state-new';
    if (!prevPresent) return 'state-return';
    if (runCount >= 2) return 'state-core';
    if (prevPresent && nextPresent) return 'state-stable';
    return 'state-short';
  }

  function stateLabel(state) {
    return {
      absent: '当天没上榜',
      'state-new': '第一次看到',
      'state-return': '消失后又出现',
      'state-short': '短暂停留',
      'state-stable': '连续在榜',
      'state-core': '同一天多次抓到'
    }[state] || state;
  }

  function calcTurnover(runs, options = {}) {
    const windowDays = Number(options.windowDays || 30);
    const allRuns = (runs || [])
      .filter(run => run?.date && (run.articles || []).length > 0)
      .sort((a, b) => `${a.date} ${a.time || ''} ${a.id || ''}`.localeCompare(`${b.date} ${b.time || ''} ${b.id || ''}`));

    if (allRuns.length < 2) return null;

    const lastDate = allRuns[allRuns.length - 1].date;
    const cutoff = addDays(lastDate, -windowDays);
    const windowRuns = allRuns.filter(run => run.date >= cutoff);
    if (windowRuns.length < 2) return null;

    const comparisons = [];
    for (let idx = 1; idx < windowRuns.length; idx += 1) {
      const curr = windowRuns[idx];
      const prev = windowRuns[idx - 1];
      const currKeys = new Set((curr.articles || []).map(articleKey).filter(Boolean));
      const prevKeys = new Set((prev.articles || []).map(articleKey).filter(Boolean));
      if (!currKeys.size || !prevKeys.size) continue;

      const same = [...currKeys].filter(key => prevKeys.has(key)).length;
      const newCount = [...currKeys].filter(key => !prevKeys.has(key)).length;
      const gone = [...prevKeys].filter(key => !currKeys.has(key)).length;
      const denominator = currKeys.size + prevKeys.size;
      const rate = denominator ? (newCount + gone) / denominator : 0;

      comparisons.push({
        index: idx,
        date: curr.date,
        time: curr.time || '',
        currRunId: curr.id || '',
        prevRunId: prev.id || '',
        currDate: curr.date,
        prevDate: prev.date,
        prevTime: prev.time || '',
        currCount: currKeys.size,
        prevCount: prevKeys.size,
        curr_count: currKeys.size,
        prev_count: prevKeys.size,
        same,
        newCount,
        new_count: newCount,
        new: newCount,
        gone,
        rate
      });
    }

    if (!comparisons.length) return null;

    const avgRate = comparisons.reduce((sum, item) => sum + item.rate, 0) / comparisons.length;
    const latest = comparisons[comparisons.length - 1];
    const dates = dateRange(cutoff, lastDate);
    const daySnapshotCounts = {};
    for (const run of windowRuns) {
      daySnapshotCounts[run.date] = (daySnapshotCounts[run.date] || 0) + 1;
    }
    const observedDates = Object.keys(daySnapshotCounts).sort();
    const maturity = {
      isMature: observedDates.length >= MIN_TURNOVER_DAYS && comparisons.length >= MIN_TURNOVER_COMPARISONS,
      observedDays: observedDates.length,
      minDays: MIN_TURNOVER_DAYS,
      comparisons: comparisons.length,
      minComparisons: MIN_TURNOVER_COMPARISONS
    };

    const dayRates = dates.map(date => {
      const items = comparisons.filter(item => item.date === date);
      if (!items.length) return { date, count: 0, rate: null, avgRate: null, maxRate: null, items: [] };
      const maxItem = items.reduce((best, item) => item.rate > best.rate ? item : best, items[0]);
      return {
        date,
        count: items.length,
        rate: maxItem.rate,
        avgRate: items.reduce((sum, item) => sum + item.rate, 0) / items.length,
        maxRate: maxItem.rate,
        items
      };
    });

    const articleMeta = new Map();
    const articleDayRuns = new Map();
    for (const run of windowRuns) {
      for (const article of run.articles || []) {
        const key = articleKey(article);
        if (!key) continue;
        if (!articleMeta.has(key)) {
          articleMeta.set(key, {
            article_id: key,
            title: article.title || '',
            account: article.account || '',
            url: article.url || '',
            content_path: article.content_path || '',
            published_at: article.published_at || '',
            read_count: article.read_count != null ? article.read_count : null,
            like_count: article.like_count != null ? article.like_count : null,
            friends_follow_count: article.friends_follow_count != null ? article.friends_follow_count : null,
            original_article_count: article.original_article_count != null ? article.original_article_count : null,
            first_seen: run.date,
            latest_rank: article.rank || article.best_rank || null,
            best_rank: article.rank || article.best_rank || null
          });
        }
        const meta = articleMeta.get(key);
        meta.last_seen = run.date;
        meta.account = meta.account || article.account || '';
        meta.url = meta.url || article.url || '';
        if (article.content_path) meta.content_path = article.content_path;
        if (article.published_at) meta.published_at = article.published_at;
        if (article.read_count != null) meta.read_count = article.read_count;
        if (article.like_count != null) meta.like_count = article.like_count;
        if (article.friends_follow_count != null) meta.friends_follow_count = article.friends_follow_count;
        if (article.original_article_count != null) meta.original_article_count = article.original_article_count;
        meta.latest_rank = article.rank || article.best_rank || meta.latest_rank || null;
        if (article.rank || article.best_rank) {
          const rank = Number(article.rank || article.best_rank);
          const bestRank = Number(meta.best_rank || rank);
          meta.best_rank = rank > 0 ? Math.min(bestRank || rank, rank) : meta.best_rank;
        }

        if (!articleDayRuns.has(key)) articleDayRuns.set(key, {});
        const dayMap = articleDayRuns.get(key);
        dayMap[run.date] = (dayMap[run.date] || 0) + 1;
      }
    }

    const totalDays = dates.length || 1;
    const totalRuns = windowRuns.length || 1;
    const articles = [];
    let hiddenSingletons = 0;

    for (const [key, meta] of articleMeta.entries()) {
      const dayMap = articleDayRuns.get(key) || {};
      const activeDays = Object.keys(dayMap).sort();
      const dayCount = activeDays.length;
      const runAppearances = Object.values(dayMap).reduce((sum, value) => sum + Number(value || 0), 0);
      const dayRatio = dayCount / totalDays;
      const runRatio = runAppearances / totalRuns;
      const maxStreak = longestStreak(activeDays);
      const stability = classifyStability({ dayRatio, runRatio, maxStreak });
      const enriched = {
        ...meta,
        appearances: dayCount,
        day_count: dayCount,
        run_appearances: runAppearances,
        presence_ratio: Number(dayRatio.toFixed(3)),
        day_presence_ratio: Number(dayRatio.toFixed(3)),
        run_presence_ratio: Number(runRatio.toFixed(3)),
        longest_streak: maxStreak,
        stability,
        stability_rank: STABILITY_RANK[stability] || 0,
        active_days: activeDays
      };

      if (dayCount === 1 && runAppearances === 1) {
        hiddenSingletons += 1;
        continue;
      }
      articles.push(enriched);
    }

    articles.sort((a, b) => (
      (b.stability_rank - a.stability_rank) ||
      (b.day_count - a.day_count) ||
      (b.run_appearances - a.run_appearances) ||
      (b.longest_streak - a.longest_streak) ||
      b.last_seen.localeCompare(a.last_seen) ||
      a.title.localeCompare(b.title, 'zh-CN')
    ));

    const articlePresence = {};
    const articleDayRunsExport = {};
    for (const article of articles) {
      const dayMap = articleDayRuns.get(article.article_id) || {};
      articlePresence[article.article_id] = {};
      for (const date of dates) {
        articlePresence[article.article_id][date] = dayMap[date] ? 1 : 0;
      }
      articleDayRunsExport[article.article_id] = { ...dayMap };
    }

    const stabilityCounts = { '常驻': 0, '活跃': 0, '闪现': 0 };
    for (const article of articles) {
      stabilityCounts[article.stability] = (stabilityCounts[article.stability] || 0) + 1;
    }

    return {
      rate: avgRate,
      avgRate,
      avgRatePct: avgRate * 100,
      numComparisons: comparisons.length,
      windowDays: Math.min(windowDays, dateDiffDays(windowRuns[0].date, lastDate) + 1),
      windowStart: cutoff,
      windowEnd: lastDate,
      snapshotCount: windowRuns.length,
      isMature: maturity.isMature,
      maturity,
      observedDates,
      dates,
      windowRuns,
      comparisons,
      dayRates,
      daySnapshotCounts,
      articles,
      articlePresence,
      articleDayRuns: articleDayRunsExport,
      hiddenSingletons,
      distinctArticles: articles.length,
      distinctArticlesTotal: articleMeta.size,
      stabilityCounts,
      latest,
      lastRate: latest.rate,
      lastSame: latest.same,
      lastNew: latest.newCount,
      lastGone: latest.gone,
      lastCurrCount: latest.currCount,
      lastPrevCount: latest.prevCount,
      lastCurrDate: latest.currDate,
      lastPrevDate: latest.prevDate
    };
  }

  global.TurnoverViz = {
    escapeHtml,
    articleKey,
    addDays,
    dateDiffDays,
    dateRange,
    longestStreak,
    classifyStability,
    levelMeta,
    observingMeta,
    statusMeta,
    isMature,
    rateColor,
    getLifeState,
    stateLabel,
    calc: calcTurnover,
    thresholds: {
      fast: TURNOVER_FAST_THRESHOLD,
      obvious: TURNOVER_OBVIOUS_THRESHOLD,
      light: TURNOVER_LIGHT_THRESHOLD,
      minDays: MIN_TURNOVER_DAYS,
      minComparisons: MIN_TURNOVER_COMPARISONS
    }
  };
})(window);
