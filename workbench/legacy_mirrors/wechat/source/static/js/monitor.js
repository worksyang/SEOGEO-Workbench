// ── 数据：由 Flask API 提供 ───────────────────────────
// Parser 见 scripts/parse_search_md.py；字段语义见 字段数据字典 v1.md

const DATA_URL = '/api/monitor-data/bootstrap';
const KEYWORD_DETAIL_API_BASE = '/api/monitor-data/keyword';
const ACCOUNT_DETAIL_API_BASE = '/api/monitor-data/account';
const CONTENT_API_URL = '/api/article-content';
const KEYWORD_API_BASE = '/api/keywords';
const COVER_API_URL = '/api/article-covers';
const COVER_PLACEHOLDER_URL = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="60" height="38" viewBox="0 0 60 38"%3E%3Crect fill="%23f0ece4" width="60" height="38" rx="4"/%3E%3Cpath d="M20 12h20v2H20zM20 18h14v2H20z" fill="%23c9c2b5"/%3E%3C/svg%3E';
const COVER_NO_URL_URL = 'https://mmbiz.qpic.cn/mmbiz_png/zEVibTTYbgHBxcdNXREKIJ3gibv5DE3xZoZf1S8yvLSIQicpoPhY7nKEHLKY8IJ6Vaq1iaFpcNicOKic1enxiad0xbyVLC38jLicD2BYqbxCO3ASv7Y/640?from=appmsg&tp=wxpic&wxfrom=5&wx_lazy=1';

marked.setOptions({
  gfm: true,
  breaks: true,
});

let MONITOR_DATA = null;          // 整份 monitor-data.json
let ALL_KEYWORDS = [];            // 关键词主视角列表
let ALL_ACCOUNTS = [];            // 账号副视角列表
const KEYWORD_BY_ID = new Map();
const KEYWORD_BY_NAME = new Map();
const ACCOUNT_BY_ID = new Map();
const ACCOUNT_BY_NAME = new Map();
const keywordDetailPending = new Map();
const accountDetailPending = new Map();
let mode = 'keyword';
let accountSortMode = 'score';  // 'score' | 'timeliness' | 'today'
let curKeyword = null;
let curAccount = null;
let filter = '';
let filterGroup = '';   // '' = 全部；按分组筛选
let filterStatus = 'all'; // 'all' | 'turnover_observing' | 'turnover_fast' | 'turnover_obvious' | 'turnover_light' | 'turnover_stable'
let sortActivity = 'heat';  // 'heat' | 'steady_read' | 'read_delta' | 'trend_up' | 'trend_down' | 'ops' | 'turnover_desc' | 'turnover_asc'
let kwGroupMap = {};    // keyword_text → group_label（由 loadGroups 维护）
let kwGroupOrder = [];
let kwGroupMoreOpen = false;
let detailChart = null;
let initialRouteApplied = false;
const coverCache = new Map();
const coverStateCache = new Map();
const coverPending = new Set();
let coverBatchInFlight = false;
const KW_GROUP_QUICK_LIMIT = 5;
const TURNOVER_FAST_THRESHOLD = 0.40;
const TURNOVER_OBVIOUS_THRESHOLD = 0.25;
const TURNOVER_LIGHT_THRESHOLD = 0.10;
const ACCOUNT_PAGE_SIZE = 100;
let accountPage = 1;
let accountPageStateKey = '';

window.__WX_PERF__ = window.__WX_PERF__ || {
  bootstrapMs: null,
  keywordDetailMs: {},
  accountDetailMs: {},
};

// ── 权重（与 Parser 完全一致，仅用于前端临时显示，主数据已带 score） ─
function rankWeight(r) {
  if (r<=0) return 0;
  const weights = {1:10.0, 2:8.2, 3:6.8, 4:5.6, 5:4.6, 6:3.7, 7:3.0, 8:2.4, 9:1.9, 10:1.5};
  return weights[r] || 0;
}

// ── 数据组织：消费 monitor-data.json ───────────────────────
function jsq(v) { return JSON.stringify(v); }

function escapeHtml(v) {
  return String(v == null ? '' : v)
    .replace(/[&<>"']/g, s => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;'
    }[s]))
    .replace(/`/g, '&#96;')
    .replace(/$\{/g, '&#36;{');
}

function scoreInt(v) {
  const n = Number(v || 0);
  if (!Number.isFinite(n)) return 0;
  return Math.max(0, Math.round(n));
}

const SCORE_BOARD_META = {
  score: {
    label: '账号分',
    scoreField: 'score',
    rawField: 'score_raw',
    yesterdayField: 'score_yesterday',
    deltaField: 'score_delta',
    partsField: 'account_score_parts',
    hexagonField: 'account_score_hexagon',
    explainField: 'score_explain',
    lead: '衡量这个账号是否在持续经营：既看历史与近期覆盖，也重点奖励经过时间验证、长期留在第 4–10 名的经典文章。',
    formula: '六维先分别与当前有效账号的 P99 比较，再按 15% / 15% / 30% / 20% / 10% / 10% 加权。',
  },
  timeliness: {
    label: '时效分',
    scoreField: 'timeliness_score',
    rawField: 'timeliness_score_raw',
    yesterdayField: 'timeliness_score_yesterday',
    deltaField: 'timeliness_score_delta',
    partsField: 'timeliness_score_parts',
    hexagonField: 'timeliness_score_hexagon',
    explainField: 'timeliness_explain',
    lead: '衡量最近 3 天谁在主动冲击头部：重点看 Top3 规模、覆盖广度、新进 Top3 与新文章冲榜。',
    formula: '六维先分别与当前有效账号的 P99 比较，再按 28% / 20% / 22% / 12% / 10% / 8% 加权。',
  },
  today: {
    label: '当天分',
    scoreField: 'today_score',
    rawField: 'today_score_raw',
    yesterdayField: 'today_score_yesterday',
    deltaField: 'today_score_delta',
    partsField: 'today_score_parts',
    hexagonField: 'today_score_hexagon',
    explainField: 'today_explain',
    lead: '只回答今天谁最强：同时看今日 Top3、关键词、文章、主题、排名质量，以及相对昨天的新进和上升。',
    formula: '六维先分别与今日有效账号的 P99 比较，再按 30% / 25% / 18% / 10% / 10% / 7% 加权。',
  },
};

function scoreBoardMeta(modeName) {
  return SCORE_BOARD_META[modeName] || SCORE_BOARD_META.score;
}

function scoreBoardValue(account, modeName) {
  return scoreInt(account?.[scoreBoardMeta(modeName).scoreField]);
}

function scoreBoardRawValue(account, modeName) {
  const config = scoreBoardMeta(modeName);
  const raw = Number(account?.[config.rawField]);
  return Number.isFinite(raw) ? raw : scoreBoardValue(account, modeName);
}

function scoreBoardYesterdayValue(account, modeName) {
  return scoreInt(account?.[scoreBoardMeta(modeName).yesterdayField]);
}

function scoreBoardParts(account, modeName) {
  return account?.[scoreBoardMeta(modeName).partsField] || {};
}

function scoreBoardHexagon(account, modeName) {
  return account?.[scoreBoardMeta(modeName).hexagonField] || null;
}

function accountScoreTitle(a, modeName) {
  const config = scoreBoardMeta(modeName);
  return a?.[config.explainField] || `${config.label}：100 是当前有效账号的 P99 基准线，不是上限。`;
}

function scorePart(v) {
  const n = Number(v || 0);
  return Number.isFinite(n) ? n.toFixed(2) : '0.00';
}

function scoreMoveSummary(a) {
  const m = a.move_summary || {};
  return `新进 ${m.new_count || 0} · 上升 ${m.up_count || 0} · 下降 ${m.down_count || 0}`;
}

const SCORE_RADAR_FALLBACK_META = [
  {key:'rank_strength', label:'强度', desc:'近7天每天表现，先除以当天市场头部基准'},
  {key:'stability', label:'稳定', desc:'近7天在榜天数 + 当前连续命中'},
  {key:'breadth', label:'广度', desc:'产品 topic 覆盖 + 搜索意图类目覆盖'},
  {key:'content', label:'厚度', desc:'近7天不同文章沉淀，按 log 递减'},
  {key:'timeliness', label:'时效', desc:'最近3天 Top 位冲榜强度'},
  {key:'momentum', label:'动能', desc:'今天相对昨天的新进和上升信号'},
];

function signedDelta(v) {
  const n = Number(v || 0);
  if (!Number.isFinite(n) || n === 0) return '0';
  return n > 0 ? `+${Math.round(n)}` : `${Math.round(n)}`;
}

function deltaClass(v) {
  const n = Number(v || 0);
  if (n > 0) return 'up';
  if (n < 0) return 'down';
  return 'flat';
}

function scoreDeltaValue(a, modeName) {
  return Number(a?.[scoreBoardMeta(modeName).deltaField] || 0);
}

function scoreRadarMeta(hexagon) {
  const meta = Array.isArray(hexagon?.axes_meta) && hexagon.axes_meta.length
    ? hexagon.axes_meta
    : SCORE_RADAR_FALLBACK_META;
  const seen = new Set();
  return meta
    .filter(item => item && item.key && !seen.has(item.key) && (seen.add(item.key), true))
    .slice(0, 6);
}

function scoreAxisValue(axes, key) {
  const n = Number((axes || {})[key]);
  if (!Number.isFinite(n)) return 0;
  return Math.max(0, Math.round(n));
}

function radarPoint(value, index, total, cx, cy, radius, scaleMax = 100) {
  const angle = (-90 + index * 360 / total) * Math.PI / 180;
  const safeScale = Math.max(Number(scaleMax) || 100, 1);
  const r = radius * (Math.max(0, Math.min(safeScale, Number(value) || 0)) / safeScale);
  return {
    x: cx + Math.cos(angle) * r,
    y: cy + Math.sin(angle) * r,
  };
}

function radarPolygonPoints(axes, metas, cx, cy, radius, scaleMax, fixed = 1) {
  return metas.map((meta, idx) => {
    const p = radarPoint(scoreAxisValue(axes, meta.key), idx, metas.length, cx, cy, radius, scaleMax);
    return `${p.x.toFixed(fixed)},${p.y.toFixed(fixed)}`;
  }).join(' ');
}

function scoreRadarScaleMax(metas, currentAxes, previousAxes) {
  const values = metas.flatMap(meta => [
    scoreAxisValue(currentAxes, meta.key),
    scoreAxisValue(previousAxes, meta.key),
  ]);
  const maxValue = Math.max(100, ...values);
  return maxValue > 100 ? Math.ceil(maxValue / 20) * 20 : 100;
}

function scoreRadarSvg(hexagon) {
  const metas = scoreRadarMeta(hexagon);
  if (!metas.length) return '';
  const curAxes = hexagon?.current?.axes || {};
  const prevAxes = hexagon?.previous?.axes || {};
  const scaleMax = scoreRadarScaleMax(metas, curAxes, prevAxes);
  const cx = 143;
  const cy = 120;
  const radius = 78;
  const ringLevels = scaleMax > 100 ? [25, 50, 75, 100, scaleMax] : [20, 40, 60, 80, 100];
  const rings = [...new Set(ringLevels)].map(level => {
    const ringAxes = Object.fromEntries(metas.map(meta => [meta.key, level]));
    const ringClass = level === 100
      ? 'radar-ring radar-benchmark'
      : level > 100
        ? 'radar-ring radar-overflow-ring'
        : 'radar-ring';
    return `<polygon class="${ringClass}" points="${radarPolygonPoints(ringAxes, metas, cx, cy, radius, scaleMax)}"></polygon>`;
  }).join('');
  const spokes = metas.map((meta, idx) => {
    const p = radarPoint(scaleMax, idx, metas.length, cx, cy, radius, scaleMax);
    return `<line class="radar-spoke" x1="${cx}" y1="${cy}" x2="${p.x.toFixed(1)}" y2="${p.y.toFixed(1)}"></line>`;
  }).join('');
  const labels = metas.map((meta, idx) => {
    const p = radarPoint(scaleMax, idx, metas.length, cx, cy, radius + 24, scaleMax);
    const anchor = Math.abs(p.x - cx) < 8 ? 'middle' : (p.x > cx ? 'start' : 'end');
    return `<text class="radar-label" x="${p.x.toFixed(1)}" y="${p.y.toFixed(1)}" text-anchor="${anchor}" dominant-baseline="middle">${escapeHtml(meta.label)}</text>`;
  }).join('');
  const prevPoints = radarPolygonPoints(prevAxes, metas, cx, cy, radius, scaleMax);
  const curPoints = radarPolygonPoints(curAxes, metas, cx, cy, radius, scaleMax);
  const dots = metas.map((meta, idx) => {
    const value = scoreAxisValue(curAxes, meta.key);
    const p = radarPoint(value, idx, metas.length, cx, cy, radius, scaleMax);
    return `<circle class="radar-dot${value > 100 ? ' is-breakthrough' : ''}" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${value > 100 ? '3.6' : '2.8'}"></circle>`;
  }).join('');
  return `
    <div class="score-radar-wrap">
      <svg class="score-radar" viewBox="0 0 286 242" role="img" aria-label="今天和昨天六维评分对比；100 为 P99 基准线">
        ${rings}
        ${spokes}
        <polygon class="radar-prev" points="${prevPoints}"></polygon>
        <polygon class="radar-cur" points="${curPoints}"></polygon>
        ${dots}
        ${labels}
      </svg>
      <div class="score-radar-legend"><span class="legend-cur">今天</span><span class="legend-prev">昨天</span><span class="legend-benchmark">100 基准</span>${scaleMax > 100 ? `<span class="legend-scale">外圈 ${scaleMax}</span>` : ''}</div>
    </div>`;
}

function scorePopulationStat(hexagon, key = null, previous = false) {
  const population = previous ? hexagon?.previous_population : hexagon?.population;
  return key ? population?.axes?.[key] : population?.score;
}

function scorePercentileText(value) {
  const percentile = Number(value);
  if (!Number.isFinite(percentile)) return '—';
  return Number.isInteger(percentile) ? `${percentile}` : percentile.toFixed(1);
}

function scorePopulationText(stat, compact = false) {
  const rank = Number(stat?.rank || 0);
  const total = Number(stat?.total || 0);
  const tieCount = Number(stat?.tie_count || 0);
  if (!rank || !total) return '暂无全站对比';
  const rankText = tieCount > 1 ? `并列第${rank}` : `第${rank}`;
  const percentileText = scorePercentileText(stat?.percentile);
  if (compact) return `${rankText}/${total}${tieCount > 1 ? ` · ${tieCount}同分` : ''} · 超${percentileText}%`;
  return `全站${rankText} / ${total} · 超过 ${percentileText}% 账号${tieCount > 1 ? ` · ${tieCount} 个账号同分` : ''}`;
}

function scoreAxisFact(modeName, key, details = {}) {
  const count = name => Math.max(0, Math.round(Number(details[name] || 0)));
  const percent = name => Math.max(0, Math.round(Number(details[name] || 0) * 100));
  const facts = {
    score: {
      history_coverage: () => `15天覆盖 ${count('history_keyword_count')} 词 · ${count('history_article_count')} 篇 · ${count('history_topic_count')} 主题`,
      recent_coverage: () => `近7天覆盖 ${count('recent_keyword_count')} 词 · ${count('recent_article_count')} 篇`,
      classic_articles: () => `${count('classic_article_count')} 篇经典文章 · ${count('classic_pair_count')} 组稳定命中`,
      continuity: () => `近7天活跃 ${count('recent_active_days')} 天 · 当前连续 ${count('current_streak')} 天`,
      content_matrix: () => `有效文章约 ${count('effective_article_count')} 篇 · 单篇集中度 ${percent('article_concentration')}%`,
      battle_breadth: () => `${count('history_topic_count')} 个主题 · ${count('history_bucket_count')} 类搜索意图`,
    },
    timeliness: {
      top3_volume: () => `近3天 Top3 ${count('top3_hit_count')} 次 · ${count('top3_article_count')} 篇文章`,
      top3_breadth: () => `Top3 覆盖 ${count('top3_keyword_count')} 词 · ${count('top3_topic_count')} 主题`,
      new_top3: () => `${count('new_top3_pair_count')} 组文章×关键词新进 Top3`,
      fresh_top3: () => `${count('fresh_top3_article_count')} 篇发布21天内文章冲入 Top3`,
      top3_continuity: () => `最近3天有 ${count('top3_active_days')} 天出现 Top3`,
      upward_momentum: () => `较昨天新进 ${count('new_count')} 个 · 上升 ${count('up_count')} 个`,
    },
    today: {
      today_top3: () => `今天 Top3 ${count('today_top3_count')} 次 · 总命中 ${count('today_hit_count')} 次`,
      today_keywords: () => `今天覆盖 ${count('today_keyword_count')} 个关键词`,
      today_articles: () => `今天由 ${count('today_article_count')} 篇不同文章贡献`,
      today_themes: () => `今天覆盖 ${count('today_topic_count')} 主题 · ${count('today_bucket_count')} 类搜索意图`,
      today_rank_quality: () => `综合排名质量 ${percent('average_rank_quality')}/100 · 第1名权重最高`,
      today_growth: () => `较昨天新进 ${count('new_count')} 个 · 上升 ${count('up_count')} 个`,
    },
  };
  return facts[modeName]?.[key]?.() || '暂无可解释的事实明细';
}

function scoreRankMoveText(currentStat, previousStat, compact = false) {
  const currentRank = Number(currentStat?.rank || 0);
  const previousRank = Number(previousStat?.rank || 0);
  if (!currentRank || !previousRank) return '';
  if (currentRank < previousRank) return `${compact ? '' : '全站'}升 ${previousRank - currentRank} 位`;
  if (currentRank > previousRank) return `${compact ? '' : '全站'}降 ${currentRank - previousRank} 位`;
  return compact ? '名次持平' : '全站名次持平';
}

function scoreAxisBenchmarkText(value) {
  const score = scoreInt(value);
  if (score > 100) return `突破 P99 +${score - 100}`;
  if (score === 100) return '达到 P99 基准';
  return `距 P99 基准 ${100 - score} 分`;
}

function scoreAxisCards(modeName, hexagon) {
  const metas = scoreRadarMeta(hexagon);
  const curAxes = hexagon?.current?.axes || {};
  const prevAxes = hexagon?.previous?.axes || {};
  const deltas = hexagon?.delta || {};
  const details = hexagon?.current?.details || {};
  const scaleMax = scoreRadarScaleMax(metas, curAxes, prevAxes);
  const benchmarkLeft = Math.min(100, 10000 / scaleMax);
  return metas.map(meta => {
    const cur = scoreAxisValue(curAxes, meta.key);
    const prev = scoreAxisValue(prevAxes, meta.key);
    const d = Number.isFinite(Number(deltas[meta.key])) ? Number(deltas[meta.key]) : cur - prev;
    const population = scorePopulationStat(hexagon, meta.key);
    const previousPopulation = scorePopulationStat(hexagon, meta.key, true);
    const currentLeft = Math.min(100, cur / scaleMax * 100);
    const previousLeft = Math.min(100, prev / scaleMax * 100);
    const yesterdayText = d === 0
      ? `昨 ${prev}`
      : `昨 ${prev} · ${signedDelta(d)}`;
    return `<div class="score-axis-card${cur > 100 ? ' is-breakthrough' : ''}" title="${escapeHtml(meta.desc || '')}">
      <div class="axis-card-head">
        <b>${escapeHtml(meta.label)}</b>
        <span>${scorePopulationText(population, true)}</span>
        <strong>${cur}</strong>
      </div>
      <div class="axis-card-fact">${escapeHtml(scoreAxisFact(modeName, meta.key, details))}</div>
      <div class="axis-bullet" role="img" aria-label="${escapeHtml(`${meta.label}今天${cur}分，昨天${prev}分，100为P99基准`)}">
        <span class="axis-bullet-fill${cur > 100 ? ' is-breakthrough' : ''}" style="width:${currentLeft.toFixed(2)}%"></span>
        <i class="axis-bullet-benchmark" style="left:${benchmarkLeft.toFixed(2)}%"></i>
        <em class="axis-bullet-previous" style="left:${previousLeft.toFixed(2)}%"></em>
      </div>
      <div class="axis-card-foot"><span>${yesterdayText} · ${scoreRankMoveText(population, previousPopulation, true)}</span><b>${scoreAxisBenchmarkText(cur)}</b></div>
    </div>`;
  }).join('');
}

function scoreWeightText(hexagon) {
  const weights = hexagon?.weights || {};
  const metas = scoreRadarMeta(hexagon);
  return metas.map(meta => `${escapeHtml(meta.label)}${Math.round(Number(weights[meta.key] || 0) * 100)}%`).join(' · ');
}

function scoreEvidenceText(a, modeName, hexagon) {
  const cur = hexagon?.current || {};
  const d = cur.details || {};
  const parts = scoreBoardParts(a, modeName);
  const confidence = `${Math.round(Number(parts.confidence || 0) * 100)}%`;
  if (modeName === 'timeliness') {
    return `近3天有效观察 <b>${d.observed_days ?? 0}</b> 天；Top3 命中 <b>${d.top3_hit_count ?? 0}</b> 次、覆盖 <b>${d.top3_keyword_count ?? 0}</b> 个关键词；新进 Top3 <b>${d.new_top3_pair_count ?? 0}</b> 组，新文冲榜 <b>${d.fresh_top3_article_count ?? 0}</b> 篇。置信度 <b>${confidence}</b>，观察满 3 天后取得完整置信度。`;
  }
  if (modeName === 'today') {
    return `今天命中 <b>${d.today_hit_count ?? 0}</b> 次，其中 Top3 <b>${d.today_top3_count ?? 0}</b> 次；覆盖 <b>${d.today_keyword_count ?? 0}</b> 个关键词、<b>${d.today_article_count ?? 0}</b> 篇文章、<b>${d.today_topic_count ?? 0}</b> 个主题；较昨天新进 <b>${d.new_count ?? 0}</b>、上升 <b>${d.up_count ?? 0}</b>。`;
  }
  return `滚动15天活跃 <b>${d.history_active_days ?? 0}</b> 天，近7天活跃 <b>${d.recent_active_days ?? 0}</b> 天，当前连续 <b>${d.current_streak ?? 0}</b> 天；覆盖 <b>${d.history_keyword_count ?? 0}</b> 个关键词、<b>${d.history_article_count ?? 0}</b> 篇文章；经典文章 <b>${d.classic_article_count ?? 0}</b> 篇，共 <b>${d.classic_pair_count ?? 0}</b> 组“文章×关键词”通过 3 天验证。置信度 <b>${confidence}</b>，观察满 5 天后取得完整置信度。`;
}

function scoreInsightRows(modeName, hexagon) {
  const metas = scoreRadarMeta(hexagon);
  const axes = hexagon?.current?.axes || {};
  const details = hexagon?.current?.details || {};
  const ranked = metas.map(meta => {
    const population = scorePopulationStat(hexagon, meta.key);
    return {
      ...meta,
      value: scoreAxisValue(axes, meta.key),
      population,
      percentile: Number(population?.percentile || 0),
      rank: Number(population?.rank || Number.MAX_SAFE_INTEGER),
    };
  }).sort((left, right) => (
    right.percentile - left.percentile
    || left.rank - right.rank
    || right.value - left.value
  ));
  if (!ranked.length) return '';
  const strongest = ranked[0];
  const second = ranked[1] || strongest;
  const weakest = [...ranked].sort((left, right) => (
    left.percentile - right.percentile
    || right.rank - left.rank
    || left.value - right.value
  ))[0];
  return [
    ['最突出', strongest],
    ['第二强', second],
    ['相对短板', weakest],
  ].map(([tag, item]) => `
    <div class="score-insight-row">
      <span>${tag}</span>
      <div><b>${escapeHtml(item.label)} ${item.value}</b><em>${escapeHtml(scoreAxisFact(modeName, item.key, details))}</em></div>
      <small>${escapeHtml(scorePopulationText(item.population, true))}</small>
    </div>
  `).join('');
}

function scoreTimeStory(a, modeName, hexagon) {
  const current = scoreBoardValue(a, modeName);
  const previous = scoreBoardYesterdayValue(a, modeName);
  const delta = scoreDeltaValue(a, modeName);
  const population = scorePopulationStat(hexagon);
  const previousPopulation = scorePopulationStat(hexagon, null, true);
  const position = scorePopulationText(population, true);
  const rankMove = scoreRankMoveText(population, previousPopulation);
  if (current === previous && current === 100) {
    return `<b>连续两天 100：</b>守住 P99 总分基准；${position}。同分数会明确并列，所以 100 不等于独占第一。`;
  }
  if (current === previous && current > 100) {
    return `<b>连续两天 ${current}：</b>维持突破；${position}，${rankMove}。`;
  }
  if (current === previous) {
    return `<b>连续两天 ${current}：</b>${position}，${rankMove}；分数不变不代表对手关系不变。`;
  }
  return `<b>${previous} → ${current}（${signedDelta(delta)}）：</b>${position}，${rankMove}。`;
}

function scoreAxisDefinitionText(hexagon) {
  return scoreRadarMeta(hexagon)
    .map(meta => `<li><b>${escapeHtml(meta.label)}：</b>${escapeHtml(meta.desc || '')}</li>`)
    .join('');
}

function scoreBreakthroughText(account, modeName) {
  const parts = scoreBoardParts(account, modeName);
  const score = scoreBoardValue(account, modeName);
  const overCount = Array.isArray(parts.over_axes) ? parts.over_axes.length : 0;
  if (score > 100) {
    return `已触发突破：基础分达到 ${Math.round(Number(parts.base_score || 0))}，${overCount} 个维度超过 P99，并包含本榜关键维度；突破能量 +${scorePart(parts.breakthrough_energy)}。`;
  }
  if (parts.breakthrough_gate) {
    return `已满足突破门槛，${overCount} 个维度超过 P99；但基础分加突破能量后的总分仍为 ${score}，尚未越过 100 基准线。`;
  }
  if (overCount > 0) {
    return `已有 ${overCount} 个维度超过 P99，但尚未同时满足“完整置信度、基础分至少 85、至少两轴突破且包含关键轴”，总分暂不突破 100。`;
  }
  return '当前没有维度超过 P99。100 是市场基准线，不是硬上限；只有多维同时超出当前认知时，总分才允许突破。';
}

function fallbackScoreTooltipHtml(a, modeName) {
  const config = scoreBoardMeta(modeName);
  const name = escapeHtml(a.name || '该账号');
  const score = scoreBoardValue(a, modeName);
  return `
    <div class="score-tip-kicker">${config.label} · 六维评分</div>
    <div class="score-tip-title"><strong>${name}</strong><span>${score}</span></div>
    <div class="score-tip-lead">100 是当前有效账号的 P99 基准线，不是上限。当前数据缺少完整六维明细，暂时无法解释它在全站的相对位置。</div>
  `;
}

function accountScoreTooltipHtml(a, modeName) {
  const config = scoreBoardMeta(modeName);
  const name = escapeHtml(a.name || '该账号');
  const hexagon = scoreBoardHexagon(a, modeName);
  if (!hexagon?.current?.axes) return fallbackScoreTooltipHtml(a, modeName);

  const score = scoreBoardValue(a, modeName);
  const scoreY = scoreBoardYesterdayValue(a, modeName);
  const scoreD = scoreDeltaValue(a, modeName);
  const scoreClass = score > 100 ? ' is-breakthrough' : '';
  const population = scorePopulationStat(hexagon);

  return `
    <div class="score-tip-kicker">${escapeHtml(config.label)} · ${escapeHtml(hexagon.window_label || '')} · 六维对比</div>
    <div class="score-tip-title">
      <div><strong>${name}</strong><small>${escapeHtml(scorePopulationText(population))}</small></div>
      <span class="${scoreClass.trim()}">${score}<small>${escapeHtml(config.label)}<br>P99=100</small><em class="score-main-delta ${deltaClass(scoreD)}">${signedDelta(scoreD)}</em></span>
    </div>
    <div class="score-time-story">${scoreTimeStory(a, modeName, hexagon)}</div>
    <div class="score-core-layout">
      ${scoreRadarSvg(hexagon)}
      <div class="score-insight-panel">
        <div class="score-section-label">先看结论</div>
        ${scoreInsightRows(modeName, hexagon)}
        <div class="score-legend-note"><b>怎么看：</b>分数看强度，名次看对手，同分数看并列数；深蓝是今天，浅蓝是昨天。</div>
      </div>
    </div>
    <div class="score-axis-section-head"><b>六项事实对比</b><span>细线=昨天 · 虚线=100基准</span></div>
    <div class="score-axis-grid">${scoreAxisCards(modeName, hexagon)}</div>
    <details class="score-tech-details">
      <summary>想看算法细节</summary>
      <div class="score-tech-body">
        <p>${config.lead}</p>
        <p>${scoreEvidenceText(a, modeName, hexagon)}</p>
        <p class="score-breakthrough-line${scoreClass}">${scoreBreakthroughText(a, modeName)}</p>
        <p><b>公式直觉：</b>${config.formula}<br><b>本项权重：</b>${scoreWeightText(hexagon)}。单轴在基准内按 <code>100 × 原始值 ÷ P99</code> 换算；超出后按 <code>100 + 40 × log₂(原始值 ÷ P99)</code> 计算突破能量。</p>
        <ul>${scoreAxisDefinitionText(hexagon)}</ul>
        <p class="score-tech-yesterday">昨日总分 ${scoreY}；昨日人口位置按当前账号池回看，便于比较同一批账号的变化。</p>
      </div>
    </details>
  `;
}

let activeScoreTooltipAnchor = null;
let scoreTooltipHideTimer = null;

function ensureAccountScoreTooltip() {
  let tip = document.getElementById('accountScoreTooltip');
  if (!tip) {
    tip = document.createElement('div');
    tip.id = 'accountScoreTooltip';
    tip.className = 'account-score-tooltip';
    tip.addEventListener('mouseenter', () => {
      if (scoreTooltipHideTimer) clearTimeout(scoreTooltipHideTimer);
    });
    tip.addEventListener('mouseleave', () => hideAccountScoreTooltip());
    document.body.appendChild(tip);
  }
  return tip;
}

function placeAccountScoreTooltip(anchor, tip) {
  const margin = 12;
  const topMargin = 56;
  const gap = 12;
  const rect = anchor.getBoundingClientRect();
  const vw = window.innerWidth || document.documentElement.clientWidth;
  const vh = window.innerHeight || document.documentElement.clientHeight;
  tip.style.maxWidth = `${Math.max(280, vw - margin * 2)}px`;
  tip.style.maxHeight = `${Math.max(220, vh - topMargin - margin)}px`;
  const tw = tip.offsetWidth;
  const th = tip.offsetHeight;

  let left = rect.left - tw - gap;
  if (left < margin) left = rect.right + gap;
  if (left + tw > vw - margin) left = vw - tw - margin;
  left = Math.max(margin, left);

  let top = rect.top + rect.height / 2 - th / 2;
  if (top < topMargin) top = topMargin;
  if (top + th > vh - margin) top = Math.max(topMargin, vh - th - margin);

  tip.style.left = `${Math.round(left)}px`;
  tip.style.top = `${Math.round(top)}px`;
}

function showAccountScoreTooltip(anchor) {
  const accountId = anchor.getAttribute('data-account-id') || '';
  const scoreMode = anchor.getAttribute('data-score-tooltip-mode') || 'score';
  const account = ACCOUNT_BY_ID.get(accountId);
  if (!account) return;
  if (scoreTooltipHideTimer) {
    clearTimeout(scoreTooltipHideTimer);
    scoreTooltipHideTimer = null;
  }
  activeScoreTooltipAnchor = anchor;
  const tip = ensureAccountScoreTooltip();
  tip.innerHTML = hasAccountDetail(account)
    ? accountScoreTooltipHtml(account, scoreMode)
    : '<div class="score-tooltip-loading">正在加载完整评分依据…</div>';
  tip.classList.add('show');
  placeAccountScoreTooltip(anchor, tip);
  if (!hasAccountDetail(account)) {
    fetchAccountDetail(account).then(() => {
      if (activeScoreTooltipAnchor !== anchor || !anchor.isConnected) return;
      tip.innerHTML = accountScoreTooltipHtml(account, scoreMode);
      placeAccountScoreTooltip(anchor, tip);
    }).catch(error => {
      if (activeScoreTooltipAnchor !== anchor || !anchor.isConnected) return;
      tip.innerHTML = `<div class="score-tooltip-loading is-error">评分详情加载失败：${escapeHtml(error.message || error)}</div>`;
      placeAccountScoreTooltip(anchor, tip);
    });
  }
}

function hideAccountScoreTooltip(anchor) {
  if (anchor && activeScoreTooltipAnchor && anchor !== activeScoreTooltipAnchor) return;
  const tip = document.getElementById('accountScoreTooltip');
  if (tip) tip.classList.remove('show');
  activeScoreTooltipAnchor = null;
}

function initAccountScoreTooltip() {
  if (window.__accountScoreTooltipInited) return;
  window.__accountScoreTooltipInited = true;

  const scheduleHide = (anchor) => {
    if (scoreTooltipHideTimer) clearTimeout(scoreTooltipHideTimer);
    scoreTooltipHideTimer = setTimeout(() => {
      hideAccountScoreTooltip(anchor);
    }, 180);
  };

  document.addEventListener('mouseover', (event) => {
    const anchor = event.target.closest?.('.js-score-tooltip');
    if (!anchor) return;
    if (anchor.contains(event.relatedTarget)) return;
    showAccountScoreTooltip(anchor);
  });
  document.addEventListener('mouseout', (event) => {
    const anchor = event.target.closest?.('.js-score-tooltip');
    if (!anchor) return;
    if (anchor.contains(event.relatedTarget)) return;
    if (event.relatedTarget?.closest?.('#accountScoreTooltip')) return;
    scheduleHide(anchor);
  });
  document.addEventListener('focusin', (event) => {
    const anchor = event.target.closest?.('.js-score-tooltip');
    if (anchor) showAccountScoreTooltip(anchor);
  });
  document.addEventListener('focusout', (event) => {
    const anchor = event.target.closest?.('.js-score-tooltip');
    if (anchor) hideAccountScoreTooltip(anchor);
  });
  window.addEventListener('scroll', () => hideAccountScoreTooltip(), true);
  window.addEventListener('resize', () => hideAccountScoreTooltip());
}

function hasRealUrl(url) {
  return !!url && !String(url).startsWith('placeholder://');
}

function articleHitDetailHref(meta = {}, url = '') {
  if (meta.article_id) return `/article-hit-detail?article_id=${encodeURIComponent(meta.article_id)}`;
  if (url) return `/article-hit-detail?url=${encodeURIComponent(url)}`;
  return '/article-hit-detail';
}

function metricsChipHtml(art) {
  const parts = [];
  if (art.read_count != null) parts.push(`<span class="metric-chip">👁 ${art.read_count}</span>`);
  if (art.like_count != null) parts.push(`<span class="metric-chip">👍 ${art.like_count}</span>`);
  return parts.join('');
}

function buildCoverProxyUrl(coverUrl) {
  return `/api/article-cover-image?url=${encodeURIComponent(coverUrl)}`;
}

function primeCoverCache(article) {
  if (!article?.article_id || coverCache.has(article.article_id)) return;
  if (typeof article.cover_url === 'string' && article.cover_url.trim()) {
    coverCache.set(article.article_id, article.cover_url.trim());
    coverStateCache.set(article.article_id, 'cached');
  }
}

function getArticleCoverValue(article) {
  if (!article?.article_id) return undefined;
  primeCoverCache(article);
  return coverCache.get(article.article_id);
}

function renderArticleCoverCard(kind, rankText) {
  const toneClass = kind === 'no_url'
    ? 'is-no-url'
    : (kind === 'retry' ? 'is-retry' : 'is-no-cover');
  const mainText = kind === 'no_url'
    ? '仅榜单'
    : (kind === 'retry' ? '待重试' : '有链接');
  const subText = kind === 'no_url'
    ? ''
    : (kind === 'retry' ? '封面抓取失败' : '暂无封面');
  return `
    <div class="article-cover article-cover-text ${toneClass}" data-cover-ready="1">
      <div class="cover-text-rank">${escapeHtml(String(rankText || '—'))}</div>
      <div class="cover-text-label">${mainText}</div>
      ${subText ? `<div class="cover-text-sub">${subText}</div>` : ''}
    </div>`;
}

function buildArticleCoverHtml(article) {
  const articleId = article?.article_id || '';
  const url = article?.url || '';
  const rankText = article.best_rank || article.rank || '—';
  const coverUrl = getArticleCoverValue(article);
  const coverState = articleId ? coverStateCache.get(articleId) : '';

  if (coverUrl) {
    return `
      <div class="article-cover" data-cover-ready="1">
        <img src="${escapeHtml(buildCoverProxyUrl(coverUrl))}" alt="" loading="lazy" />
      </div>`;
  }

  if (!hasRealUrl(url)) {
    return renderArticleCoverCard('no_url', rankText);
  }

  if (coverState === 'not_found') {
    return renderArticleCoverCard('no_cover', rankText);
  }
  if (coverState === 'request_error' || coverState === 'http_error') {
    return renderArticleCoverCard('retry', rankText);
  }

  const attrs = articleId
    ? ` data-cover-article-id="${escapeHtml(articleId)}" data-cover-url="${escapeHtml(url)}" data-cover-rank="${escapeHtml(String(rankText))}"`
    : '';
  return `
    <div class="article-cover is-loading"${attrs}>
      <img src="${COVER_PLACEHOLDER_URL}" alt="" loading="lazy" />
    </div>`;
}

function applyArticleCover(item) {
  document.querySelectorAll(`[data-cover-article-id="${item.article_id}"]`).forEach(node => {
    const rankText = node.getAttribute('data-cover-rank') || '—';
    if (item.cover_url) {
      node.innerHTML = `<img src="${escapeHtml(buildCoverProxyUrl(item.cover_url))}" alt="" loading="lazy" />`;
      node.className = 'article-cover';
    } else if (item.status === 'not_found') {
      node.outerHTML = renderArticleCoverCard('no_cover', rankText);
      return;
    } else if (item.status === 'request_error' || item.status === 'http_error') {
      node.outerHTML = renderArticleCoverCard('retry', rankText);
      return;
    } else {
      node.outerHTML = renderArticleCoverCard('no_cover', rankText);
      return;
    }
    node.removeAttribute('data-cover-article-id');
    node.removeAttribute('data-cover-url');
    node.removeAttribute('data-cover-rank');
    node.setAttribute('data-cover-ready', '1');
  });
}

async function loadQueuedArticleCovers() {
  if (coverBatchInFlight) return;

  const batch = [];
  const seen = new Set();
  const nodes = Array.from(document.querySelectorAll('[data-cover-article-id]'));
  for (const node of nodes) {
    const articleId = node.getAttribute('data-cover-article-id') || '';
    const url = node.getAttribute('data-cover-url') || '';
    if (!articleId || seen.has(articleId) || coverPending.has(articleId) || coverCache.has(articleId) || coverStateCache.has(articleId)) continue;
    seen.add(articleId);
    batch.push({ article_id: articleId, url });
    if (batch.length >= 10) break;
  }
  if (!batch.length) return;

  batch.forEach(item => coverPending.add(item.article_id));
  coverBatchInFlight = true;
  try {
    const resp = await fetch(COVER_API_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ articles: batch })
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const payload = await resp.json();
    for (const item of payload.items || []) {
      if (item.cover_url) coverCache.set(item.article_id, item.cover_url);
      coverStateCache.set(item.article_id, item.status || 'unknown');
      coverPending.delete(item.article_id);
      applyArticleCover(item);
    }
  } catch (e) {
    batch.forEach(item => {
      coverPending.delete(item.article_id);
      coverStateCache.set(item.article_id, 'request_error');
      applyArticleCover({ article_id: item.article_id, status: 'request_error', cover_url: null });
    });
    console.warn('article cover batch failed', e);
  } finally {
    coverBatchInFlight = false;
    if (document.querySelector('[data-cover-article-id]')) {
      setTimeout(loadQueuedArticleCovers, 0);
    }
  }
}

function queueVisibleArticleCovers() {
  setTimeout(loadQueuedArticleCovers, 0);
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

function formatDateShort(date) {
  if (!date) return '';
  return date.slice(5).replace('-', '/');
}

function snapshotHitToneClass(days) {
  const n = Number(days || 0);
  if (n >= 10) return 'tone-5';
  if (n >= 7) return 'tone-4';
  if (n >= 5) return 'tone-3';
  if (n >= 3) return 'tone-2';
  return 'tone-1';
}

function formatRunType(triggerType) {
  if (triggerType === 'manual') return '手动复查';
  if (triggerType === 'backfill') return '补抓';
  return '定时抓取';
}

function formatRunTypeShort(triggerType) {
  if (triggerType === 'manual') return '手动';
  if (triggerType === 'backfill') return '补抓';
  return '定时';
}

function keywordDateInputId(kw) {
  return `custom-date-${encodeURIComponent(kw)}`;
}

function keywordTopicInputId(keywordId) {
  return `topic-input-${encodeURIComponent(keywordId)}`;
}

function keywordBucketInputId(keywordId) {
  return `bucket-input-${encodeURIComponent(keywordId)}`;
}

function dayIndexInWindow(dateStr) {
  if (!MONITOR_DATA?.window_start || !dateStr) return -1;
  const start = new Date(`${MONITOR_DATA.window_start}T00:00:00`);
  const target = new Date(`${dateStr}T00:00:00`);
  return Math.round((target - start) / 86400000);
}

function rebuildMonitorIndexes() {
  KEYWORD_BY_ID.clear();
  KEYWORD_BY_NAME.clear();
  ACCOUNT_BY_ID.clear();
  ACCOUNT_BY_NAME.clear();
  ALL_KEYWORDS.forEach(item => {
    if (item?.keyword_id) KEYWORD_BY_ID.set(String(item.keyword_id), item);
    if (item?.keyword) KEYWORD_BY_NAME.set(String(item.keyword), item);
  });
  ALL_ACCOUNTS.forEach(item => {
    if (item?.account_id) ACCOUNT_BY_ID.set(String(item.account_id), item);
    if (item?.name) ACCOUNT_BY_NAME.set(String(item.name), item);
  });
}

function hasKeywordDetail(item) {
  return !!item && Object.prototype.hasOwnProperty.call(item, 'runs');
}

function hasAccountDetail(item) {
  return !!item
    && Object.prototype.hasOwnProperty.call(item, 'topics')
    && Object.prototype.hasOwnProperty.call(item, 'keywords');
}

async function fetchMonitorDetail(url, metricBucket, metricKey) {
  const startedAt = performance.now();
  const resp = await fetch(url, { cache: 'no-cache' });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const payload = await resp.json();
  window.__WX_PERF__[metricBucket][metricKey] = Math.round((performance.now() - startedAt) * 10) / 10;
  return payload;
}

function fetchKeywordDetail(item) {
  if (!item?.keyword_id || hasKeywordDetail(item)) return Promise.resolve(item);
  const key = String(item.keyword_id);
  if (keywordDetailPending.has(key)) return keywordDetailPending.get(key);
  const promise = fetchMonitorDetail(
    `${KEYWORD_DETAIL_API_BASE}/${encodeURIComponent(key)}`,
    'keywordDetailMs',
    key
  ).then(detail => {
    Object.assign(item, detail);
    return item;
  }).finally(() => keywordDetailPending.delete(key));
  keywordDetailPending.set(key, promise);
  return promise;
}

function fetchAccountDetail(item) {
  if (!item?.account_id || hasAccountDetail(item)) return Promise.resolve(item);
  const key = String(item.account_id);
  if (accountDetailPending.has(key)) return accountDetailPending.get(key);
  const promise = fetchMonitorDetail(
    `${ACCOUNT_DETAIL_API_BASE}/${encodeURIComponent(key)}`,
    'accountDetailMs',
    key
  ).then(detail => {
    Object.assign(item, detail);
    return item;
  }).finally(() => accountDetailPending.delete(key));
  accountDetailPending.set(key, promise);
  return promise;
}

function getKeywordRuns(kw) {
  const item = KEYWORD_BY_NAME.get(kw);
  return Array.isArray(item?.runs) ? item.runs : [];
}

function getTurnoverRuns(item) {
  if (Array.isArray(item?.runs)) return item.runs;
  return Array.isArray(item?.turnover_runs) ? item.turnover_runs : [];
}

function refreshTopicSuggestions() {
  const datalist = document.getElementById('topicSuggestions');
  if (!datalist) return;
  const topics = [...new Set(
    ALL_KEYWORDS
      .map(item => String(item.topic || item.keyword || '').trim())
      .filter(Boolean)
  )].sort((a, b) => a.localeCompare(b, 'zh-CN'));
  datalist.innerHTML = topics.map(topic => `<option value="${escapeHtml(topic)}"></option>`).join('');
}

function applyInitialRouteState() {
  if (initialRouteApplied) return;
  initialRouteApplied = true;
  const params = new URLSearchParams(window.location.search);
  const view = params.get('view');
  const keyword = params.get('keyword');
  const account = params.get('account');
  if (view === 'account') mode = 'account';
  if (view === 'keywordManage' || view === 'keyword-manage') mode = 'keywordManage';
  if (keyword && ALL_KEYWORDS.some(item => item.keyword === keyword)) {
    curKeyword = keyword;
    mode = 'keyword';
    ensureKeywordRunState(keyword);
  }
  if (account && ALL_ACCOUNTS.some(item => item.name === account)) {
    curAccount = account;
    mode = 'account';
  }
}

function refreshBucketSuggestions() {
  const datalist = document.getElementById('bucketSuggestions');
  if (!datalist) return;
  const buckets = [...new Set(
    (MONITOR_DATA?.keyword_bucket_options || [])
      .concat(ALL_KEYWORDS.map(item => String(item.keyword_bucket || '').trim()))
      .filter(Boolean)
  )];
  datalist.innerHTML = buckets.map(bucket => `<option value="${escapeHtml(bucket)}"></option>`).join('');
}

const keywordRunState = {};

function ensureKeywordRunState(kw) {
  const runs = getKeywordRuns(kw);
  if (!runs.length) return null;
  if (!keywordRunState[kw]) {
    keywordRunState[kw] = { date: runs[0].date, runId: runs[0].id };
  }
  const exists = runs.some(run => run.id === keywordRunState[kw].runId);
  if (!exists) {
    keywordRunState[kw] = { date: runs[0].date, runId: runs[0].id };
  }
  return keywordRunState[kw];
}

function getSelectedKeywordRun(kw) {
  const runs = getKeywordRuns(kw);
  if (!runs.length) return null;
  const state = ensureKeywordRunState(kw);
  return runs.find(run => run.id === state.runId) || runs[0];
}

function getKeywordDates(kw) {
  return [...new Set(getKeywordRuns(kw).map(run => run.date))];
}

function getRecentKeywordDates(kw, limit = 9) {
  return getKeywordDates(kw).slice(0, limit);
}

function setKeywordDateState(kw, date) {
  const runs = getKeywordRuns(kw).filter(run => run.date === date);
  if (!runs.length) return;
  const preferred = runs[0];
  keywordRunState[kw] = { date, runId: preferred.id };
}

function rankMoveLabel(today, prev) {
  if (today == null) return '未上榜';
  if (prev == null) return '新入榜';
  if (prev > today) return `上升 ${prev}→${today}`;
  if (prev < today) return `回落 ${prev}→${today}`;
  return '保持不变';
}

function buildHeatRow(history, titleFn) {
  return (Array.isArray(history) ? history : []).map((r, i) => {
    if (r === 0) return '';
    return `<div class="heatcell ${r <= 3 ? 'c1' : 'c2'}" title="${escapeHtml(titleFn(r, i))}"></div>`;
  }).join('');
}

function formatAidsoCount(n) {
  if (!Number.isFinite(n) || n <= 0) return '0';
  if (n >= 100000000) return (n / 100000000).toFixed(n >= 1000000000 ? 0 : 1) + '亿';
  if (n >= 10000) return (n / 10000).toFixed(n >= 1000000 ? 0 : 1) + 'w';
  return String(n);
}

function keywordReadDelta(k) {
  return k?.keyword_read_delta || null;
}

function isFormalReadDelta(delta) {
  return !!delta && delta.status === 'ok';
}

function readDeltaPointCount(delta) {
  return Array.isArray(delta?.daily_read_delta_points)
    ? delta.daily_read_delta_points.length
    : 0;
}

function hasProvisionalReadValue(delta) {
  return !!delta
    && !isFormalReadDelta(delta)
    && delta.provisional_status
    && delta.provisional_status !== 'insufficient'
    && (
      Number.isFinite(Number(delta.provisional_steady_read_median))
      || Number.isFinite(Number(delta.provisional_read_delta_estimated))
    );
}

function canShowProvisionalReadValues(delta) {
  return hasProvisionalReadValue(delta) && provisionalSampleCount(delta) >= 2;
}

function provisionalSampleCount(delta) {
  const count = Number(delta?.provisional_sample_count);
  if (Number.isFinite(count) && count > 0) return Math.round(count);
  return Number(delta?.snapshot_count || readDeltaPointCount(delta) || 0);
}

function hasReadDeltaValue(delta) {
  return isFormalReadDelta(delta)
    && delta.read_delta_estimated !== null
    && Number.isFinite(Number(delta.read_delta_estimated));
}

function readDeltaNumeric(k) {
  const delta = keywordReadDelta(k);
  return hasReadDeltaValue(delta) ? Number(delta.read_delta_estimated) : null;
}

function trendRatioNumeric(k) {
  const delta = keywordReadDelta(k);
  if (!delta || delta.status !== 'ok') return null;
  const ratio = Number(delta.recent_vs_baseline_ratio);
  return Number.isFinite(ratio) ? ratio : null;
}

function hasSteadyReadValue(delta) {
  return isFormalReadDelta(delta)
    && delta.steady_read_median !== null
    && Number.isFinite(Number(delta.steady_read_median));
}

function steadyReadNumeric(k) {
  const delta = keywordReadDelta(k);
  return hasSteadyReadValue(delta) ? Number(delta.steady_read_median) : null;
}

function trendTone(delta) {
  if (!delta || delta.status !== 'ok') return 'neutral';
  const signal = Number(delta.trend_signal || 0);
  if (signal >= 0.2) return 'hot';
  if (signal <= -0.2) return 'cool';
  return 'neutral';
}

function confidenceLabel(delta) {
  return {
    high: '高置信',
    medium: '中置信',
    low: '低置信',
    insufficient: '观察中'
  }[delta?.confidence_level] || '观察中';
}

function formatTrendPercent(delta) {
  const ratio = Number(delta?.recent_vs_baseline_ratio);
  if (!Number.isFinite(ratio)) return '—';
  const pct = Math.round(ratio * 100);
  return `${pct > 0 ? '+' : ''}${pct}%`;
}

function readMetricTooltip(delta) {
  if (!delta || delta.status !== 'ok') {
    const sampleText = provisionalSampleCount(delta);
    return `早期试算/极低置信：仅基于${sampleText}次切片，正式回测口径仍为空；不参与排序、趋势榜或正式筛选`;
  }
  const observed = formatPercentValue(delta.observed_share);
  const estimated = formatPercentValue(delta.estimated_share);
  const slots = formatPercentValue(delta.slot_coverage_ratio);
  return `趋势口径：最近3天对比前7天，不是简单和15天总量相比；公平口径：每次按同一Top10尺子计算；有连续读数用实测速度，单点文章按同年龄×当前阅读量补值；实测校准${observed}，模型补值${estimated}，Top10完整度${slots}，${confidenceLabel(delta)}`;
}

function formatSignedNumber(n) {
  const value = Number(n || 0);
  const sign = value > 0 ? '+' : '';
  return `${sign}${value.toLocaleString('zh-CN')}`;
}

function formatReadDeltaValue(delta) {
  return hasReadDeltaValue(delta) ? formatSignedNumber(delta.read_delta_estimated) : '数据不足';
}

function formatSteadyReadValue(delta) {
  if (!hasSteadyReadValue(delta)) return '数据不足';
  return Math.round(Number(delta.steady_read_median)).toLocaleString('zh-CN');
}

function formatProvisionalReadValue(delta) {
  if (!canShowProvisionalReadValues(delta)) return '数据不足';
  const value = Number(delta?.provisional_read_delta_estimated);
  return Number.isFinite(value) ? formatSignedNumber(value) : '数据不足';
}

function formatProvisionalSteadyValue(delta) {
  if (!canShowProvisionalReadValues(delta)) return '数据不足';
  const value = Number(delta?.provisional_steady_read_median);
  return Number.isFinite(value)
    ? Math.round(value).toLocaleString('zh-CN')
    : '数据不足';
}

function formatIsoMinute(value) {
  if (!value) return '—';
  return String(value).replace('T', ' ').slice(5, 16);
}

function formatPercentValue(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  return `${Math.round(n * 100)}%`;
}

function readDeltaTagHtml(k) {
  const delta = keywordReadDelta(k);
  if (!delta) return '';
  const formal = isFormalReadDelta(delta);
  const cls = formal ? '' : ' is-insufficient';
  const tone = trendTone(delta);
  const text = formal
    ? `趋势 ${delta.trend_label || '平稳'} ${formatTrendPercent(delta)}`
    : hasProvisionalReadValue(delta) ? '早期试算 · 极低置信' : '阅读估算观察中';
  return `<span class="kw-tag kw-tag-read-delta tone-${tone}${cls}" title="${escapeHtml(readMetricTooltip(delta))}">${escapeHtml(text)}</span>`;
}

function readDeltaSideHtml(k) {
  const delta = keywordReadDelta(k);
  if (!delta) return '';
  const formal = isFormalReadDelta(delta);
  const hasValue = hasReadDeltaValue(delta) || canShowProvisionalReadValues(delta);
  const hasSteady = hasSteadyReadValue(delta) || canShowProvisionalReadValues(delta);
  const deltaTone = trendTone(delta);
  const tooltip = readMetricTooltip(delta);
  const steadyLabel = formal ? '常态阅读' : '试算常态';
  const steadyText = hasSteadyReadValue(delta)
    ? formatSteadyReadValue(delta)
    : formatProvisionalSteadyValue(delta);
  const deltaText = hasReadDeltaValue(delta)
    ? formatReadDeltaValue(delta)
    : formatProvisionalReadValue(delta);
  return `
    <div class="kw-read-delta-side ${(hasValue || hasSteady) ? '' : 'is-insufficient'}" title="${escapeHtml(tooltip)}">
      <div class="kw-read-metric-block is-primary ${hasSteady ? '' : 'is-insufficient'}">
        <div class="kw-read-delta-val">${hasSteady ? '≈' : ''}${escapeHtml(steadyText)}</div>
        <div class="kw-read-delta-lbl">${steadyLabel}</div>
      </div>
      <div class="kw-read-metric-block is-secondary tone-${deltaTone} ${hasValue ? '' : 'is-insufficient'}">
        <div class="kw-read-delta-val">${formal ? '' : '≈'}${escapeHtml(deltaText)}</div>
        <div class="kw-read-delta-lbl">${formal ? '15日增量' : '早期试算'} · ${escapeHtml(formal ? confidenceLabel(delta) : '极低置信')}</div>
      </div>
    </div>`;
}

function readDeltaChartShow(event) {
  const zone = event.currentTarget;
  const chart = zone.closest('.read-delta-chart');
  if (!chart) return;

  const tooltip = chart.querySelector('.read-delta-tooltip');
  const cursor = chart.querySelector('.read-delta-chart-cursor');
  const activeDot = chart.querySelector('.read-delta-active-dot');
  if (!tooltip || !cursor || !activeDot) return;

  const x = Number(zone.dataset.x || 0);
  const y = Number(zone.dataset.y || 0);
  const xPct = Number(zone.dataset.xPct || 0);
  const yPct = Number(zone.dataset.yPct || 0);
  const value = Number(zone.dataset.value || 0);
  const date = zone.dataset.date || '';
  const articles = Number(zone.dataset.articles || 0);
  const snapshots = Number(zone.dataset.snapshots || 0);
  const observed = Number(zone.dataset.observed || 0);
  const estimated = Number(zone.dataset.estimated || 0);
  const slotCoverage = Number(zone.dataset.slotCoverage || 0);
  const imputedDay = zone.dataset.imputedDay === 'true';

  cursor.setAttribute('x1', String(x));
  cursor.setAttribute('x2', String(x));
  cursor.classList.add('active');
  activeDot.style.left = `${xPct}%`;
  activeDot.style.top = `${yPct}%`;
  activeDot.classList.add('active');

  tooltip.classList.remove('is-left', 'is-right');
  if (xPct < 18) tooltip.classList.add('is-left');
  if (xPct > 82) tooltip.classList.add('is-right');
  tooltip.style.left = `${xPct}%`;
  tooltip.innerHTML = `
    <div class="read-delta-tip-date">${escapeHtml(formatDateShort(date))}${imputedDay ? ' · 缺失日补值' : ''}</div>
    <div class="read-delta-tip-value">≈${escapeHtml(formatSignedNumber(value))}</div>
    <div class="read-delta-tip-meta">实测校准 ${escapeHtml(formatSignedNumber(observed))} · 模型补值 ${escapeHtml(formatSignedNumber(estimated))}</div>
    <div class="read-delta-tip-meta">${articles} 篇结果 · ${snapshots} 次切片 · Top10完整 ${escapeHtml(formatPercentValue(slotCoverage))}</div>`;
  tooltip.classList.add('active');
}

function readDeltaChartHide(event) {
  const chart = event.currentTarget.closest('.read-delta-chart');
  if (!chart) return;
  chart.querySelector('.read-delta-tooltip')?.classList.remove('active');
  chart.querySelector('.read-delta-chart-cursor')?.classList.remove('active');
  chart.querySelector('.read-delta-active-dot')?.classList.remove('active');
}

function renderReadDeltaSparkline(delta) {
  const points = Array.isArray(delta.daily_read_delta_points)
    ? delta.daily_read_delta_points
    : [];
  if (!points.length) return '';
  const formal = isFormalReadDelta(delta);
  const canShowEstimate = canShowProvisionalReadValues(delta);

  const values = points.map(point => Math.max(0, Number(point.read_delta || 0)));
  const maxValue = Math.max(...values, 1);
  const width = 520;
  const height = 132;
  const padX = 18;
  const padY = 16;
  const baseY = height - padY;
  const innerWidth = width - padX * 2;
  const innerHeight = height - padY * 2 - 20;
  const coord = (value, index) => {
    const x = points.length === 1
      ? width / 2
      : padX + (innerWidth * index) / (points.length - 1);
    const y = baseY - 20 - (value / maxValue) * innerHeight;
    return [Number(x.toFixed(2)), Number(y.toFixed(2))];
  };
  const coords = values.map(coord);
  const line = coords.map(([x, y]) => `${x},${y}`).join(' ');
  const area = coords.length
    ? `M ${coords[0][0]} ${baseY - 20} L ${coords.map(([x, y]) => `${x} ${y}`).join(' L ')} L ${coords[coords.length - 1][0]} ${baseY - 20} Z`
    : '';
  const peakIndex = values.reduce((best, value, idx) => value > values[best] ? idx : best, 0);
  const dots = coords.map(([x, y], idx) => {
    const value = values[idx];
    const classes = [];
    if (idx === peakIndex && value > 0) classes.push('is-peak');
    if (points[idx]?.is_imputed_day) classes.push('is-imputed');
    const cls = classes.length ? ` class="${classes.join(' ')}"` : '';
    return `<span${cls} style="left:${(x / width * 100).toFixed(2)}%;top:${(y / height * 100).toFixed(2)}%"></span>`;
  }).join('');
  const gridLines = [0, 0.5, 1].map(rate => {
    const y = baseY - 20 - rate * innerHeight;
    return `<line class="read-delta-grid-line" x1="${padX}" x2="${width - padX}" y1="${y.toFixed(2)}" y2="${y.toFixed(2)}"></line>`;
  }).join('');
  const hitZones = coords.map(([x, y], idx) => {
    const prevX = idx === 0 ? padX : (coords[idx - 1][0] + x) / 2;
    const nextX = idx === coords.length - 1 ? width - padX : (x + coords[idx + 1][0]) / 2;
    const point = points[idx] || {};
    return `<rect class="read-delta-hit-zone"
      x="${prevX.toFixed(2)}"
      y="0"
      width="${Math.max(1, nextX - prevX).toFixed(2)}"
      height="${height}"
      data-x="${x}"
      data-y="${y}"
      data-x-pct="${(x / width * 100).toFixed(2)}"
      data-y-pct="${(y / height * 100).toFixed(2)}"
      data-date="${escapeHtml(point.date || '')}"
      data-value="${values[idx]}"
      data-articles="${Number(point.article_count || 0)}"
      data-snapshots="${Number(point.snapshot_count || 0)}"
      data-observed="${Number(point.observed_component || 0)}"
      data-estimated="${Number(point.estimated_component || 0)}"
      data-slot-coverage="${Number(point.slot_coverage_ratio || 0)}"
      data-imputed-day="${point.is_imputed_day ? 'true' : 'false'}"
      onmouseenter="readDeltaChartShow(event)"
      onmousemove="readDeltaChartShow(event)"
      onmouseleave="readDeltaChartHide(event)"></rect>`;
  }).join('');
  const labelIndexes = [...new Set([0, Math.floor((points.length - 1) / 2), points.length - 1])];
  const labels = labelIndexes.map(idx => {
    const [x] = coords[idx];
    return `<span style="left:${(x / width * 100).toFixed(2)}%">${escapeHtml(formatDateShort(points[idx]?.date || ''))}</span>`;
  }).join('');
  const peakPoint = points[peakIndex] || {};
  const total = values.reduce((sum, value) => sum + value, 0);
  const steadyReadText = formal
    ? formatSteadyReadValue(delta)
    : formatProvisionalSteadyValue(delta);
  const chartTitle = formal ? '公平日增量' : '早期观测图';
  const chartSubtitle = formal
    ? `每次抓取统一换算为Top10 · 常态阅读 ≈${escapeHtml(steadyReadText)}`
    : canShowEstimate
      ? `试算常态 ≈${escapeHtml(steadyReadText)} · 仅基于${provisionalSampleCount(delta)}次切片`
      : `仅基于${provisionalSampleCount(delta)}次切片 · 正式回测口径仍为空`;
  const chartPeak = formal || canShowEstimate
    ? `峰值 ≈${escapeHtml(formatSignedNumber(values[peakIndex]))} · ${escapeHtml(formatDateShort(peakPoint.date || ''))}`
    : `观测峰值 ${escapeHtml(formatSignedNumber(values[peakIndex]))} · ${escapeHtml(formatDateShort(peakPoint.date || ''))}`;
  const chartNote = formal
    ? `图中合计 ≈${escapeHtml(formatSignedNumber(total))}。深层逻辑是“实测速度校准 + 单点文章同类补值”；这是统一尺度下的估算，不等同微信后台精确阅读来源。`
    : `仅基于${provisionalSampleCount(delta)}次切片的早期观测图，正式回测口径仍为空；图中不用于排序、趋势榜或正式筛选。`;

  return `
    <div class="read-delta-chart">
      <div class="read-delta-chart-head">
        <div>
          <span>${chartTitle}</span>
          <em>${chartSubtitle}</em>
        </div>
        <strong>${chartPeak}</strong>
      </div>
      <div class="read-delta-chart-stage">
        <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="关键词每日观测阅读增量折线图">
          <defs>
            <linearGradient id="readDeltaAreaGradient" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="#059669" stop-opacity=".22"></stop>
              <stop offset="100%" stop-color="#059669" stop-opacity="0"></stop>
            </linearGradient>
          </defs>
          ${gridLines}
          <path class="read-delta-chart-area" d="${area}"></path>
          <polyline class="read-delta-chart-line" points="${line}"></polyline>
          <line class="read-delta-chart-cursor" x1="0" x2="0" y1="${padY}" y2="${baseY - 14}"></line>
          ${hitZones}
        </svg>
        <div class="read-delta-chart-dots" aria-hidden="true">${dots}</div>
        <div class="read-delta-active-dot" aria-hidden="true"></div>
      </div>
      <div class="read-delta-chart-labels">${labels}</div>
      <div class="read-delta-tooltip" aria-hidden="true"></div>
      <div class="read-delta-chart-note">${chartNote}</div>
    </div>`;
}

function renderKeywordReadDeltaCard(k) {
  const delta = keywordReadDelta(k);
  if (!delta) {
    return `
      <div class="card read-delta-card is-insufficient">
        <div class="card-title">阅读增量事实</div>
        <div class="read-delta-empty">还没有生成关键词阅读增量文件。</div>
      </div>`;
  }

  const formal = isFormalReadDelta(delta);
  const showProvisional = canShowProvisionalReadValues(delta);
  const hasValue = hasReadDeltaValue(delta) || showProvisional;
  const hasSteady = hasSteadyReadValue(delta) || showProvisional;
  const mainValue = hasReadDeltaValue(delta)
    ? formatReadDeltaValue(delta)
    : formatProvisionalReadValue(delta);
  const steadyValue = hasSteadyReadValue(delta)
    ? formatSteadyReadValue(delta)
    : formatProvisionalSteadyValue(delta);
  const deltaTone = trendTone(delta);
  const metricTitle = readMetricTooltip(delta);
  const steadyLabel = formal ? '常态阅读' : '试算常态';
  const statusText = formal && hasValue
    ? `${confidenceLabel(delta)} · ${delta.trend_label || '平稳'}`
    : hasProvisionalReadValue(delta) ? '早期试算 · 极低置信' : '观察中';
  const windowText = `${formatIsoMinute(delta.window_start)} → ${formatIsoMinute(delta.window_end)}`;
  const rawDeltaText = Number.isFinite(Number(delta.read_delta_raw))
    ? formatSignedNumber(delta.read_delta_raw)
    : '—';
  const singlePointArticles = Math.max(
    0,
    Number(delta.articles_with_metric || 0) - Number(delta.articles_with_enough_points || 0)
  );
  const field = (label, value, title = '') => `
    <div${title ? ` title="${escapeHtml(title)}"` : ''}>
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value == null || value === '' ? '—' : value)}</strong>
    </div>`;
  const provisionalNotice = !formal && hasProvisionalReadValue(delta)
    ? `<div class="read-delta-provisional-notice">早期试算/极低置信：仅基于${provisionalSampleCount(delta)}次切片、正式回测口径仍为空。</div>`
    : '';
  const singlePointNotice = !formal && readDeltaPointCount(delta) === 1
    ? `<div class="read-delta-provisional-notice">仅基于1次切片、正式回测口径仍为空；当前只展示观测图，不展示试算数值。</div>`
    : '';

  return `
    <div class="card read-delta-card ${formal && hasValue ? '' : 'is-insufficient'}">
      <div class="card-title-row">
        <span class="card-title">关键词阅读公平估算</span>
        <span class="read-delta-status tone-${deltaTone}">${escapeHtml(statusText)}</span>
      </div>
      <div class="read-delta-head">
        <div>
          <div class="read-delta-steady-row" title="${escapeHtml(metricTitle)}">
            <span class="read-delta-steady-label">${steadyLabel}</span>
            <strong class="read-delta-steady-value">${hasSteady ? '≈' : ''}${escapeHtml(hasSteady ? steadyValue : '数据不足')}</strong>
          </div>
          <div class="read-delta-secondary-row tone-${deltaTone}" title="${escapeHtml(metricTitle)}">
            <span class="read-delta-secondary-value">${hasValue ? '≈' : ''}${escapeHtml(mainValue)}</span>
            <span class="read-delta-secondary-label">${formal ? '15日阅读增量' : '早期试算'}</span>
          </div>
          <div class="read-delta-caption">${formal && hasValue ? `标准化Top10结果页 · 最近3天相对前7天 ${escapeHtml(formatTrendPercent(delta))}` : formal ? '至少完成15天成熟观察；数据不足不按0处理' : `仅基于${provisionalSampleCount(delta)}次切片、正式回测口径仍为空`}</div>
        </div>
        <div class="read-delta-window">
          <span>观测窗口</span>
          <strong>${escapeHtml(windowText)}</strong>
        </div>
      </div>
      ${provisionalNotice}
      ${singlePointNotice}
      ${renderReadDeltaSparkline(delta)}
      <div class="read-delta-grid">
        ${field(steadyLabel, hasSteady ? `≈${steadyValue}` : '—', metricTitle)}
        ${field(formal ? '15日增量' : '早期试算增量', hasValue ? `≈${mainValue}` : '—', metricTitle)}
        ${field('趋势信号', formal ? `${delta.trend_label || '观察中'} ${formatTrendPercent(delta)}` : '正式口径为空', '正式口径；早期试算不参与趋势榜')}
        ${field('实测校准', formatPercentValue(delta.observed_share), '由同一文章两次以上阅读读数推得的速度贡献')}
        ${field('模型补值', formatPercentValue(delta.estimated_share), '单点文章按发布时间年龄与当前阅读量的同类样本补值')}
        ${field('有效天', `${Number(delta.observed_days || 0)}/${Number(delta.window_days || 15)}`)}
        ${field('Top10完整', formatPercentValue(delta.slot_coverage_ratio), '每次抓取实际拿到的Top10槽位完整度')}
        ${field('原始观测增量', rawDeltaText, '旧口径：只有两次以上读数的文章才能进入，保留用于审计')}
        ${field('命中文章', delta.hit_articles)}
        ${field('单点文章', singlePointArticles, '旧算法会丢掉这些文章；新算法会补值并降低置信度')}
        ${field('切片数量', delta.snapshot_count)}
        ${field('关联词变化', `新增${Number(delta.new_term_count || 0)} · 上升${Number(delta.rising_term_count || 0)}`)}
      </div>
      <div class="read-delta-explain">
        <b>为什么更公平</b>
        <span>每次抓取都先换算成同一张Top10结果页：有连续读数的文章用实测阅读速度；只有一个读数的文章按“文章年龄 × 当前阅读量”同类样本补值；排名越靠前权重越高；缺失日期按相邻观测插值并向长期常态收缩；每个关键词独立计算，不再受同批次其他关键词数量影响。数值是可复算估算，不冒充微信后台精确来源。</span>
      </div>
    </div>`;
}

function renderAidsoHeatBlock(heat) {
  if (!heat || (!heat.dso && !heat.wso && !heat.wso_est)) return '';
  const douyinSvg = `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M13 3h3c.4 2.6 2.2 4.6 5 5v3c-2.1-.2-3.8-.9-5-2v6a6 6 0 1 1-6-6h1v3h-1a3 3 0 1 0 3 3V3z"/></svg>`;
  const wechatSvg = `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8.691 2.188C3.891 2.188 0 5.476 0 9.53c0 2.212 1.17 4.203 3.002 5.55a.59.59 0 01.213.665l-.39 1.48c-.019.07-.048.141-.048.213 0 .163.13.295.29.295a.326.326 0 00.167-.054l1.903-1.114a.864.864 0 01.717-.098 10.16 10.16 0 002.837.403c.276 0 .543-.027.811-.05-.857-2.578.157-4.972 1.932-6.446 1.703-1.415 3.882-1.98 5.853-1.838-.576-3.583-4.196-6.348-8.596-6.348zM5.785 5.991c.642 0 1.162.529 1.162 1.18a1.17 1.17 0 01-1.162 1.178A1.17 1.17 0 014.623 7.17c0-.651.52-1.18 1.162-1.18zm5.813 0c.642 0 1.162.529 1.162 1.18a1.17 1.17 0 01-1.162 1.178 1.17 1.17 0 01-1.162-1.178c0-.651.52-1.18 1.162-1.18zm5.34 2.867c-1.797-.052-3.746.512-5.28 1.786-1.72 1.428-2.687 3.72-1.78 6.22.833 2.358 3.09 3.773 5.742 3.773.344 0 .725-.034 1.079-.092l1.735 1.01a.294.294 0 00.152.049.263.263 0 00.263-.265c0-.064-.025-.129-.044-.193l-.356-1.354a.541.541 0 01.195-.607C20.105 18.075 21 16.4 21 14.57c0-3.165-2.512-5.712-4.062-5.712zm-1.343 2.33c.584 0 1.058.483 1.058 1.076a1.067 1.067 0 01-1.058 1.074 1.067 1.067 0 01-1.058-1.074c0-.593.474-1.076 1.058-1.076zm-4.088 0c.584 0 1.058.483 1.058 1.076a1.067 1.067 0 01-1.058 1.074 1.067 1.067 0 01-1.058-1.074c0-.593.474-1.076 1.058-1.076z"/></svg>`;
  const parts = [];
  if (heat.dso && Number.isFinite(heat.dso.month_cover_count) && heat.dso.month_cover_count > 0) {
    const n = heat.dso.month_cover_count;
    parts.push(`<span class="aidso-tag aidso-dso" title="DSO 月搜索量 ${n}">${douyinSvg}${formatAidsoCount(n)}</span>`);
  }
  if (heat.wso_est && heat.wso_est.estimated && Number.isFinite(heat.wso_est.month_cover_count)) {
    const n = heat.wso_est.month_cover_count;
    parts.push(`<span class="aidso-tag aidso-wso-est" title="WSO 月搜索量（估算）${n}">${wechatSvg}≈${formatAidsoCount(n)}</span>`);
  }
  if (heat.wso && Number.isFinite(heat.wso.month_cover_count) && heat.wso.month_cover_count > 0) {
    const n = heat.wso.month_cover_count;
    parts.push(`<span class="aidso-tag aidso-wso" title="WSO 月搜索量 ${n}">${wechatSvg}${formatAidsoCount(n)}</span>`);
  }
  return parts.join('');
}

function kmComputeStreaks(history) {
  let current = 0;
  let longest = 0;
  let running = 0;
  for (let i = 0; i < history.length; i++) {
    if (history[i] > 0) {
      running += 1;
      longest = Math.max(longest, running);
    } else {
      running = 0;
    }
  }
  for (let i = history.length - 1; i >= 0; i--) {
    if (history[i] > 0) current += 1;
    else break;
  }
  return { current, longest };
}

function applyKeywordManageStateToMonitorData(data) {
  return data;
}

// ── 加载真实数据 ─────────────────────────────────────
async function loadData(options = {}) {
  const preserveSelection = !!options.preserveSelection;
  const skipManageReload = !!options.skipManageReload;
  const prevKeyword = curKeyword;
  const prevAccount = curAccount;
  try {
    const bootstrapStartedAt = performance.now();
    const managePromise = skipManageReload ? Promise.resolve(null) : kmEnsureDataLoaded({ silent: true });
    const resp = await fetch(DATA_URL, { cache: 'no-cache' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    MONITOR_DATA = await resp.json();
    await managePromise;
    window.__WX_PERF__.bootstrapMs = Math.round((performance.now() - bootstrapStartedAt) * 10) / 10;
    MONITOR_DATA = applyKeywordManageStateToMonitorData(MONITOR_DATA);
  } catch (e) {
    document.getElementById('colRight').innerHTML = `
      <div style="padding:32px;color:#999;font-size:13px;line-height:1.8">
        <div style="font-weight:700;color:#991b1b;margin-bottom:8px">无法加载 ${escapeHtml(DATA_URL)}</div>
        <div>原因：${escapeHtml(e.message)}</div>
        <div style="margin-top:14px;padding:12px;background:#fafafa;border:1px solid #eee;border-radius:6px;font-family:Menlo,monospace;font-size:12px">
          请改用 Flask 本地服务启动：<br><br>
          <code>cd /Users/works14/.claude/监控/wechat-ybxhyyh-top3</code><br>
          <code>python3 run.py</code><br>
          <code>open http://127.0.0.1:8765</code>
        </div>
        <div style="margin-top:14px">或先跑一次 Parser：<code>python3 scripts/parse_search_md.py</code></div>
      </div>`;
    return false;
  }

  ALL_KEYWORDS = MONITOR_DATA.keywords || [];
  ALL_ACCOUNTS = MONITOR_DATA.accounts || [];
  rebuildMonitorIndexes();
  accountPage = 1;
  accountPageStateKey = '';
  await loadGroups(KM_DATA);  // 复用并行加载的关键词管理数据，避免重复请求。
  refreshTopicSuggestions();
  refreshBucketSuggestions();
  curKeyword = preserveSelection && prevKeyword && KEYWORD_BY_NAME.has(prevKeyword)
    ? prevKeyword
    : (ALL_KEYWORDS[0] ? ALL_KEYWORDS[0].keyword : null);
  curAccount = preserveSelection && prevAccount && ACCOUNT_BY_NAME.has(prevAccount)
    ? prevAccount
    : (ALL_ACCOUNTS[0] ? ALL_ACCOUNTS[0].name : null);
  if (!preserveSelection) applyInitialRouteState();
  document.getElementById('kwCountTop').textContent = ALL_KEYWORDS.length;
  document.getElementById('acctCountTop').textContent = ALL_ACCOUNTS.length;
  if (MONITOR_DATA.generated_at) {
    const latestRun = ALL_KEYWORDS
      .map(k => k.latest_run && k.latest_run.run_at)
      .filter(Boolean)
      .sort()
      .pop();
    const pinnedCount = ALL_KEYWORDS.filter(k => k.is_pinned).length;
    const meta = latestRun
      ? `最近抓取：${latestRun} · 置顶 ${pinnedCount} 个 · ${MONITOR_DATA.window_days}天窗口`
      : `置顶 ${pinnedCount} 个 · ${MONITOR_DATA.window_days}天窗口`;
    document.querySelector('.topbar-right').textContent = `默认入口：关键词 · ${meta}`;
  }
  return true;
}

function badgeHtml(type, label, title) {
  const tooltipAttr = title ? ` data-tooltip="${escapeHtml(title)}"` : '';
  return `<span class="badge badge-${type}"${title ? ` title="${escapeHtml(title)}"` : ''}${tooltipAttr}>${label}</span>`;
}

// 通用账号控件：圆形头像 + 名称，蓝底胶囊
// headimgUrl 可选，无头像时降级为纯文字 chip
function accountChipHtml(name, headimgUrl, opts = {}) {
  const { clickHandler = '', extraClass = '' } = opts;
  const avatarHtml = headimgUrl
    ? `<img class="acct-chip-avatar" src="${headimgUrl}" alt="" loading="lazy" onerror="this.style.display='none'">`
    : '';
  return `<span class="account-chip ${extraClass}" ${clickHandler}>${avatarHtml}<span class="acct-chip-name">${escapeHtml(name)}</span></span>`;
}

function accountBadge(a) {
  const summary = a.move_summary || {};
  let html = '';
  if (summary.primary_type) {
    const label = summary.primary_type === 'new' ? '新命中' : summary.primary_type === 'up' ? '上升' : summary.primary_type === 'down' ? '下降' : '稳定';
    html += badgeHtml(summary.primary_type, `${label} ${summary.primary_count || 0}`);
  }
  if (summary.secondary_type) {
    const label = summary.secondary_type === 'up' ? '上升' : '下降';
    html += badgeHtml(summary.secondary_type, `${label} ${summary.secondary_count || 0}`);
  }
  return html;
}

function calcTurnoverRate(runs) {
  return window.TurnoverViz ? window.TurnoverViz.calc(runs || []) : null;
}

function turnoverDetailUrl(k) {
  const params = new URLSearchParams();
  if (k.keyword_id) params.set('keyword_id', k.keyword_id);
  params.set('keyword', k.keyword || '');
  return `/keyword-turnover?${params.toString()}`;
}

function turnoverPercent(rate) {
  return `${(Number(rate || 0) * 100).toFixed(0)}%`;
}

function turnoverShareText(rate) {
  const pct = Math.round(Number(rate || 0) * 100);
  if (pct <= 0) return '几乎没换';
  if (pct < 10) return '不到1成';
  if (pct >= 45 && pct <= 55) return '约一半';
  if (pct > 90) return '几乎全换';
  return `约${Math.round(pct / 10)}成`;
}

function turnoverRunPairText(turnover) {
  const sameDay = turnover.lastCurrDate === turnover.lastPrevDate;
  const curr = `${turnover.lastCurrDate}${turnover.latest?.time ? ` ${turnover.latest.time}` : ''}`;
  const prev = turnover.latest?.prevDate
    ? `${turnover.latest.prevDate}${turnover.latest.prevTime ? ` ${turnover.latest.prevTime}` : ''}`
    : turnover.lastPrevDate;
  return sameDay
    ? `同一天的两次抓取在比：${curr} vs ${prev}`
    : `这次抓取 vs 上一次抓取：${curr} vs ${prev}`;
}

function buildTurnoverPreviewHtml(k, turnover) {
  const meta = TurnoverViz.statusMeta ? TurnoverViz.statusMeta(turnover) : TurnoverViz.levelMeta(turnover.rate);
  const pct = turnoverPercent(turnover.rate);
  const latestPct = turnoverPercent(turnover.lastRate);
  const averageShare = turnoverShareText(turnover.rate);
  const latestShare = turnoverShareText(turnover.lastRate);
  const mature = TurnoverViz.isMature ? TurnoverViz.isMature(turnover) : true;
  const cells = (turnover.comparisons || []).slice(-34);
  const cellHtml = cells.map(item => {
    const title = `${item.date} ${item.time || ''} · ${turnoverShareText(item.rate)}文章和上一次不一样 · 新出现${item.newCount}篇 · 掉出${item.gone}篇`;
    return `<span class="turnover-preview-cell" title="${escapeHtml(title)}" style="background:${TurnoverViz.rateColor(item.rate)}"></span>`;
  }).join('');
  const href = turnoverDetailUrl(k);
  const escapedHref = escapeHtml(href);
  return `
    <div class="turnover-preview ${meta.className}" role="button" tabindex="0" onclick="event.stopPropagation();window.location.href='${escapedHref}'" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();event.stopPropagation();window.location.href='${escapedHref}'}" title="打开完整换新热力图">
      <div class="turnover-preview-head">
        <div class="turnover-preview-rate" style="color:${meta.color}">${pct}</div>
        <div class="turnover-preview-copy">
          <div class="turnover-preview-level" style="color:${meta.color}">${meta.label}</div>
          <div class="turnover-preview-meta">${mature ? `平均每次${averageShare}文章和上一次不一样` : escapeHtml(meta.reason || '样本还在积累')}</div>
        </div>
        <div class="turnover-preview-cta">看明细</div>
      </div>
      <div class="turnover-preview-explain">${mature ? `过去${turnover.windowDays}天，把每次抓取结果都和“上一次抓取”比较，共比了${turnover.numComparisons}次。` : `当前只展示原始换新率 ${pct}，暂不进入四档判断。${escapeHtml(meta.reason || '')}`}</div>
      <div class="turnover-preview-grid" aria-label="最近轮换率热力图">${cellHtml}</div>
      <div class="turnover-preview-legend">
        <span>绿=变化少</span>
        <span class="turnover-preview-swatch" style="background:#1a8754"></span>
        <span class="turnover-preview-swatch" style="background:#86d49b"></span>
        <span class="turnover-preview-swatch" style="background:#f5d36e"></span>
        <span class="turnover-preview-swatch" style="background:#f59e0b"></span>
        <span class="turnover-preview-swatch" style="background:#ef4444"></span>
        <span>红=换得多</span>
        <strong>最近一次${latestShare}变了</strong>
      </div>
    </div>`;
}

function keywordBadge(k) {
  let html = '';
  if (k.is_pinned) html += badgeHtml('flat', '置顶');

  const turnover = calcTurnoverRate(getTurnoverRuns(k));
  if (turnover) {
    const { rate, numComparisons, windowDays, lastSame, lastNew, lastGone, lastCurrCount, lastPrevCount, lastCurrDate, lastPrevDate } = turnover;
    const mature = TurnoverViz.isMature ? TurnoverViz.isMature(turnover) : true;
    const meta = TurnoverViz.statusMeta ? TurnoverViz.statusMeta(turnover) : TurnoverViz.levelMeta(rate);
    if (!mature) {
      const title = `${meta.reason || '样本还在积累'} · 当前原始换新率${turnoverPercent(rate)} · 最近一次相同${lastSame}篇，新出现${lastNew}篇，掉出${lastGone}篇`;
      html += badgeHtml('flat', meta.label, title);
      return html;
    }
    let type;
    if (rate >= TURNOVER_OBVIOUS_THRESHOLD) {
      type = 'up';
    } else if (rate >= TURNOVER_LIGHT_THRESHOLD) {
      type = 'flat';
    } else {
      type = 'down';
    }
    const title = `平均每次${turnoverShareText(rate)}文章和上一次不一样 · 基于${windowDays}天内${numComparisons}次对比 · 最近一次相同${lastSame}篇，新出现${lastNew}篇，掉出${lastGone}篇 · 本次${lastCurrCount}篇，上次${lastPrevCount}篇`;
    html += badgeHtml(type, meta.label, title);
    return html;
  }

  const latestIdx = dayIndexInWindow(k.latest_run?.date);
  if (latestIdx < 0 && !html) return badgeHtml('flat', '暂无快照');
  const todayHits = k.history_hits?.[latestIdx] || 0;
  if (todayHits > 0) {
    html += badgeHtml('new', '新上榜');
  } else if (!html) {
    html += badgeHtml('flat', '暂无上榜');
  }
  return html;
}

function getAccountTopicNames(a) {
  if (Array.isArray(a?.topic_names) && a.topic_names.length) return a.topic_names;
  const names = Object.values(a.topics || {}).map(info => info.label || '').filter(Boolean);
  if (names.length) return names;
  if (Array.isArray(a?.keyword_names)) return a.keyword_names;
  return Object.keys(a?.keywords || {});
}

function sumTopicScores(info) {
  return (info.day_scores || []).reduce((sum, value) => sum + Number(value || 0), 0);
}

function syncModeUi() {
  document.getElementById('modeKeyword').classList.toggle('active', mode === 'keyword');
  document.getElementById('modeAccount').classList.toggle('active', mode === 'account');
  const kmBtn = document.getElementById('modeKeywordManage');
  if (kmBtn) kmBtn.classList.toggle('active', mode === 'keywordManage');
  const alBtn = document.getElementById('modeArticleList');
  if (alBtn) alBtn.classList.toggle('active', mode === 'articleList');

  // 筛选/排序栏：仅 keyword 模式显示
  const filterBar = document.getElementById('kwFilterBar');
  if (filterBar) filterBar.style.display = (mode === 'keyword') ? '' : 'none';

  const layout = document.querySelector('.layout');
  const kmView = document.getElementById('keywordManageView');
  const kmWrap = document.getElementById('kmGroupsWrap');
  const alView = document.getElementById('articleListView');
  if (mode === 'keywordManage') {
    if (layout) layout.style.display = 'none';
    if (kmView) kmView.classList.add('active');
    if (kmWrap) kmWrap.style.display = '';
    if (alView) alView.classList.remove('active');
  } else if (mode === 'articleList') {
    if (layout) layout.style.display = 'none';
    if (kmView) kmView.classList.remove('active');
    if (kmWrap) kmWrap.style.display = 'none';
    if (alView) alView.classList.add('active');
    if (window.alRestoreState && !window.alRestoreState()) {
      if (window.alInit) window.alInit();
    }
  } else {
    if (layout) layout.style.display = '';
    if (kmView) kmView.classList.remove('active');
    if (kmWrap) kmWrap.style.display = 'none';
    if (alView) alView.classList.remove('active');
  }

  const searchInput = document.getElementById('searchInput');
  const acctSortTabs = document.getElementById('acctSortTabs');
  if (acctSortTabs) acctSortTabs.style.display = mode === 'account' ? '' : 'none';
  if (searchInput) {
    searchInput.placeholder = mode === 'keyword' ? '搜索关键词…' : '搜索账号…';
  }
  kmSyncInlineRefreshUi();
}

function keywordTurnoverStatus(item) {
  const turnover = calcTurnoverRate(getTurnoverRuns(item));
  if (!turnover) return 'turnover_unknown';
  if (TurnoverViz.isMature && !TurnoverViz.isMature(turnover)) return 'turnover_observing';
  const rate = Number(turnover.rate || 0);
  if (rate >= TURNOVER_FAST_THRESHOLD) return 'turnover_fast';
  if (rate >= TURNOVER_OBVIOUS_THRESHOLD) return 'turnover_obvious';
  if (rate >= TURNOVER_LIGHT_THRESHOLD) return 'turnover_light';
  return 'turnover_stable';
}

function keywordMatchesStatus(item, status = filterStatus) {
  if (status && status.startsWith('turnover_')) return keywordTurnoverStatus(item) === status;
  return true;
}

function getFilteredKeywordBase(options = {}) {
  const {
    group = filterGroup,
    status = filterStatus,
  } = options;

  let base = filter ? ALL_KEYWORDS.filter(item => item.keyword.includes(filter)) : ALL_KEYWORDS;
  if (group) base = base.filter(item => (item._group || '') === group);
  if (status && status !== 'all') base = base.filter(item => keywordMatchesStatus(item, status));
  return base;
}

function compareKeywordsByHeat(a, b) {
  const aHasHeat = a.kw_score?.has_heat ? 1 : 0;
  const bHasHeat = b.kw_score?.has_heat ? 1 : 0;
  if (aHasHeat !== bHasHeat) return bHasHeat - aHasHeat;
  if (aHasHeat) {
    const heatDiff = (b.kw_score?.heat || 0) - (a.kw_score?.heat || 0);
    if (heatDiff) return heatDiff;
  }
  const richnessDiff = (b.kw_score?.richness || 0) - (a.kw_score?.richness || 0);
  if (richnessDiff) return richnessDiff;
  return String(a.keyword || '').localeCompare(String(b.keyword || ''), 'zh-CN');
}

function sortKeywordList(base) {
  const rateCache = new Map();
  const getTurnoverInfo = (item) => {
    const key = item.keyword_id || item.keyword;
    if (!rateCache.has(key)) {
      const turnover = calcTurnoverRate(getTurnoverRuns(item));
      rateCache.set(key, {
        turnover,
        rate: turnover?.rate ?? -1,
        mature: turnover ? !(TurnoverViz.isMature && !TurnoverViz.isMature(turnover)) : false
      });
    }
    return rateCache.get(key);
  };
  const tier = (info) => {
    if (!info.turnover || !info.mature) return 3;
    const rate = info.rate;
    if (rate < TURNOVER_LIGHT_THRESHOLD) return 0;
    if (rate >= TURNOVER_OBVIOUS_THRESHOLD) return 1;
    return 2;
  };

  return [...base].sort((a, b) => {
    const ap = a.is_pinned ? 1 : 0;
    const bp = b.is_pinned ? 1 : 0;
    if (ap !== bp) return bp - ap;

    if (sortActivity === 'ops') {
      const ia = getTurnoverInfo(a);
      const ib = getTurnoverInfo(b);
      const ra = ia.rate;
      const rb = ib.rate;
      const ta = tier(ia);
      const tb = tier(ib);
      if (ta !== tb) return ta - tb;
      if (rb !== ra) return rb - ra;
      return compareKeywordsByHeat(a, b);
    }

    if (sortActivity === 'read_delta') {
      const ra = readDeltaNumeric(a);
      const rb = readDeltaNumeric(b);
      const ah = ra !== null ? 1 : 0;
      const bh = rb !== null ? 1 : 0;
      if (ah !== bh) return bh - ah;
      if (ra !== null && rb !== null && rb !== ra) return rb - ra;
      return compareKeywordsByHeat(a, b);
    }

    if (sortActivity === 'steady_read') {
      const sa = steadyReadNumeric(a);
      const sb = steadyReadNumeric(b);
      const ah = sa !== null ? 1 : 0;
      const bh = sb !== null ? 1 : 0;
      if (ah !== bh) return bh - ah;
      if (sa !== null && sb !== null && sb !== sa) return sb - sa;
      return compareKeywordsByHeat(a, b);
    }

    if (sortActivity === 'trend_up' || sortActivity === 'trend_down') {
      const ta = trendRatioNumeric(a);
      const tb = trendRatioNumeric(b);
      const ah = ta !== null ? 1 : 0;
      const bh = tb !== null ? 1 : 0;
      if (ah !== bh) return bh - ah;
      if (ta !== null && tb !== null && ta !== tb) {
        return sortActivity === 'trend_up' ? tb - ta : ta - tb;
      }
      return compareKeywordsByHeat(a, b);
    }

    if (sortActivity === 'turnover_desc') {
      const ia = getTurnoverInfo(a);
      const ib = getTurnoverInfo(b);
      const ra = ia.mature ? ia.rate : -1;
      const rb = ib.mature ? ib.rate : -1;
      if (rb !== ra) return rb - ra;
      return compareKeywordsByHeat(a, b);
    }

    if (sortActivity === 'turnover_asc') {
      const ia = getTurnoverInfo(a);
      const ib = getTurnoverInfo(b);
      const ra = ia.mature ? ia.rate : Infinity;
      const rb = ib.mature ? ib.rate : Infinity;
      if (ra !== rb) return ra - rb;
      return compareKeywordsByHeat(a, b);
    }

    return compareKeywordsByHeat(a, b);
  });
}

function getKeywordGroupLabels() {
  const labels = kwGroupOrder.length
    ? kwGroupOrder
    : [...new Set(ALL_KEYWORDS.map(item => String(item._group || '').trim()).filter(Boolean))];
  return labels.filter(Boolean);
}

function renderFilterChip({ label, count = null, active = false, onClick = '', disabled = false, title = '' }) {
  const classNames = ['kw-filter-pill'];
  if (active) classNames.push('active');
  if (disabled && !active) classNames.push('is-empty');
  const disabledAttr = disabled && !active ? ' disabled' : '';
  return `<button class="${classNames.join(' ')}" type="button" title="${escapeHtml(title || label)}"${disabledAttr}${onClick ? ` onclick="${escapeHtml(onClick)}"` : ''}>
    <span class="kw-filter-pill-label">${escapeHtml(label)}</span>
    ${count == null ? '' : `<span class="kw-filter-pill-count">${count}</span>`}
  </button>`;
}

function renderKeywordFilterBar() {
  const summary = document.getElementById('kwFilterSummary');
  const resetBtn = document.getElementById('kwFilterReset');
  const groupChips = document.getElementById('kwGroupChips');
  const groupMoreBtn = document.getElementById('kwGroupMoreBtn');
  const groupMorePanel = document.getElementById('kwGroupMorePanel');
  const statusChips = document.getElementById('kwStatusChips');
  const sortChips = document.getElementById('kwSortChips');
  if (!summary || !groupChips || !groupMoreBtn || !groupMorePanel || !statusChips || !sortChips) return;

  const labels = getKeywordGroupLabels();
  if (filterGroup && !labels.includes(filterGroup)) filterGroup = '';

  const groupBase = getFilteredKeywordBase({ group: '', status: filterStatus });
  const groupCountMap = new Map();
  groupBase.forEach(item => {
    const label = String(item._group || '').trim();
    if (!label) return;
    groupCountMap.set(label, (groupCountMap.get(label) || 0) + 1);
  });

  const statusBase = getFilteredKeywordBase({ group: filterGroup, status: 'all' });
  const totalCount = ALL_KEYWORDS.length;
  const visibleBase = getFilteredKeywordBase();
  const visibleCount = visibleBase.length;
  summary.textContent = `已显示 ${visibleCount} / ${totalCount} 个关键词`;
  if (resetBtn) resetBtn.hidden = !(filterGroup || filterStatus !== 'all');

  const candidateGroups = labels.filter(label => (groupCountMap.get(label) || 0) > 0 || label === filterGroup);
  let visibleGroups = candidateGroups.slice(0, KW_GROUP_QUICK_LIMIT);
  if (filterGroup && !visibleGroups.includes(filterGroup)) {
    visibleGroups = visibleGroups.slice(0, Math.max(0, KW_GROUP_QUICK_LIMIT - 1)).concat(filterGroup);
  }
  visibleGroups = [...new Set(visibleGroups)];
  const hiddenGroups = candidateGroups.filter(label => !visibleGroups.includes(label));
  if (!hiddenGroups.length) kwGroupMoreOpen = false;

  groupChips.innerHTML = [
    renderFilterChip({
      label: '全部',
      count: groupBase.length,
      active: !filterGroup,
      onClick: "setKeywordGroup('')",
      disabled: groupBase.length === 0,
    }),
    ...visibleGroups.map(label => renderFilterChip({
      label,
      count: groupCountMap.get(label) || 0,
      active: filterGroup === label,
      onClick: `setKeywordGroup(${jsq(label)})`,
      disabled: (groupCountMap.get(label) || 0) === 0,
    }))
  ].join('');

  groupMoreBtn.hidden = !hiddenGroups.length;
  if (!hiddenGroups.length) {
    groupMorePanel.hidden = true;
    groupMorePanel.innerHTML = '';
  } else {
    groupMoreBtn.textContent = kwGroupMoreOpen ? '收起' : `更多 ${hiddenGroups.length}`;
    groupMorePanel.hidden = !kwGroupMoreOpen;
    groupMorePanel.innerHTML = hiddenGroups.map(label => renderFilterChip({
      label,
      count: groupCountMap.get(label) || 0,
      active: filterGroup === label,
      onClick: `setKeywordGroup(${jsq(label)})`,
      disabled: (groupCountMap.get(label) || 0) === 0,
    })).join('');
  }

  const statusOptions = [
    { value: 'all', label: '全部', count: statusBase.length },
    { value: 'turnover_observing', label: '观察中', count: statusBase.filter(item => keywordMatchesStatus(item, 'turnover_observing')).length },
    { value: 'turnover_fast', label: '换得很快', count: statusBase.filter(item => keywordMatchesStatus(item, 'turnover_fast')).length },
    { value: 'turnover_obvious', label: '换得明显', count: statusBase.filter(item => keywordMatchesStatus(item, 'turnover_obvious')).length },
    { value: 'turnover_light', label: '小幅换新', count: statusBase.filter(item => keywordMatchesStatus(item, 'turnover_light')).length },
    { value: 'turnover_stable', label: '基本没变', count: statusBase.filter(item => keywordMatchesStatus(item, 'turnover_stable')).length },
  ];
  statusChips.innerHTML = statusOptions.map(option => renderFilterChip({
    label: option.label,
    count: option.count,
    active: filterStatus === option.value,
    onClick: `setKeywordStatus(${jsq(option.value)})`,
    disabled: option.count === 0,
  })).join('');

  const readDeltaReadyCount = visibleBase.filter(item => readDeltaNumeric(item) !== null).length;
  const steadyReadReadyCount = visibleBase.filter(item => steadyReadNumeric(item) !== null).length;
  const trendReadyCount = visibleBase.filter(item => trendRatioNumeric(item) !== null).length;
  const sortOptions = [
    { value: 'heat', label: '默认', count: visibleCount },
    { value: 'steady_read', label: '常态阅读', count: steadyReadReadyCount },
    { value: 'read_delta', label: '阅读增量', count: readDeltaReadyCount },
    { value: 'trend_up', label: '趋势上升', count: trendReadyCount, title: '按最近3天相对前7天的变化比例从高到低排序' },
    { value: 'trend_down', label: '趋势下降', count: trendReadyCount, title: '按最近3天相对前7天的变化比例从低到高排序' },
  ];
  sortChips.innerHTML = sortOptions.map(option => renderFilterChip({
    label: option.label,
    count: option.count,
    title: option.title,
    active: sortActivity === option.value,
    onClick: `setKeywordSortMode(${jsq(option.value)})`,
    disabled: (option.value === 'read_delta' || option.value === 'steady_read') && option.count === 0,
  })).join('');
}

function setKeywordGroup(groupLabel) {
  filterGroup = groupLabel || '';
  kwGroupMoreOpen = false;
  refresh();
}

function setKeywordStatus(status) {
  filterStatus = filterStatus === status && status !== 'all' ? 'all' : status;
  refresh();
}

function setKeywordSortMode(nextSort) {
  sortActivity = nextSort || 'heat';
  refresh();
}

function toggleKwGroupPanel() {
  kwGroupMoreOpen = !kwGroupMoreOpen;
  renderKeywordFilterBar();
}

function resetKeywordFilters() {
  filterGroup = '';
  filterStatus = 'all';
  kwGroupMoreOpen = false;
  refresh();
}

function renderList() {
  if (mode === 'keyword') renderKeywordFilterBar();
  const list = mode === 'keyword'
    ? (() => {
        const sorted = sortKeywordList(getFilteredKeywordBase());
        if (sorted.length) {
          if (!curKeyword || !sorted.some(item => item.keyword === curKeyword)) {
            curKeyword = sorted[0].keyword;
            ensureKeywordRunState(curKeyword);
          }
        } else {
          curKeyword = null;
        }
        return sorted;
      })()
    : (() => {
        const base = filter ? ALL_ACCOUNTS.filter(a => a.name.includes(filter)) : ALL_ACCOUNTS;
        return [...base].sort((accountA, accountB) => {
          const scoreDelta = scoreBoardRawValue(accountB, accountSortMode) - scoreBoardRawValue(accountA, accountSortMode);
          if (scoreDelta) return scoreDelta;
          if (accountSortMode === 'today') {
            const hitDelta = Number(accountB.today_hit_count || 0) - Number(accountA.today_hit_count || 0);
            if (hitDelta) return hitDelta;
          }
          return String(accountA.name || '').localeCompare(String(accountB.name || ''), 'zh-CN');
        });
      })();

  let visibleList = list;
  if (mode === 'account') {
    const nextStateKey = `${mode}|${accountSortMode}|${filter}`;
    if (nextStateKey !== accountPageStateKey) {
      accountPageStateKey = nextStateKey;
      accountPage = 1;
    }
    visibleList = list.slice(0, accountPage * ACCOUNT_PAGE_SIZE);
  }

  let html = mode === 'keyword'
    ? visibleList.map((k, i) => {
        const rc = i === 0 ? 'r1' : '';
        const heat = buildHeatRow(k.history_best, (r, idx) => `D${idx + 1} · 最佳第${r}名 · 命中${k.history_hits[idx]}个账号`);
        const tags = [
          `topic ${k.topic || k.keyword}`,
          `类目 ${k.keyword_bucket || '未分类'}`,
          k.is_pinned ? `人工置顶` : null,
          `最新上榜 ${k.today_count}`,
          k.latest_run ? `快照 ${formatDateShort(k.latest_run.date)} ${k.latest_run.time}` : '暂无快照',
          `追踪账号 ${k.tracked_accounts}`,
          `沉淀文章 ${k.article_count}`
        ].filter(Boolean).map(t => `<span class="kw-tag">${escapeHtml(t)}</span>`).join('');
        const readDeltaTag = readDeltaTagHtml(k);
        const readDeltaSide = readDeltaSideHtml(k);
        const kwScore = k.kw_score;
        // V4：显示分 = heat / richness（取 0-100），让数字直观；置顶不影响数字本身
        const kwDisplay = kwScore
          ? Math.min(100, Math.round((kwScore.has_heat ? kwScore.heat : kwScore.richness) * 100))
          : 0;
        const scoreTitle = kwScore
          ? (kwScore.has_heat
            ? `外部热度分 ${kwDisplay}：基于当前监控关键词集合的AIDSO近月微信/抖音搜索覆盖量，经对数归一化后的相对分；不是今日榜、不是阅读量、不是趋势。`
            : `丰度分 ${kwDisplay}：当前监控关键词集合内的沉淀文章数相对值；该关键词暂无AIDSO外部搜索热度。`)
          : '';
        const scoreTag = kwScore ? `<span class="aidso-tag aidso-score" title="${escapeHtml(scoreTitle)}">🔥${kwDisplay}</span>` : '';
        const aidsoHeat = renderAidsoHeatBlock(k.heat_summary);
        const refreshedCls = kmRefreshedKeywords.has(k.keyword) ? ' is-refreshed' : '';
        return `<div class="acct-row ${curKeyword === k.keyword ? 'active' : ''}" onclick='selectKeyword(${jsq(k.keyword)})'>
          <div class="rank-no ${rc}${refreshedCls}">${i + 1}</div>
          <div class="acct-main">
            <div class="acct-name-row"><span class="acct-name">${escapeHtml(k.keyword)}</span>${keywordBadge(k)}${aidsoHeat}${scoreTag}</div>
            <div class="kw-tags">${readDeltaTag}${tags}</div>
            <div class="heatrow">${heat}</div>
          </div>
          ${readDeltaSide}
        </div>`;
      }).join('')
    : visibleList.map((a, i) => {
        const rc = i === 0 ? 'r1' : '';
        const heat = buildHeatRow(a.history, (r, idx) => `D${idx + 1} · 最佳第${r}名`);
        const topicHtml = getAccountTopicNames(a).slice(0, 6).map(topic => `<span class="kw-tag">${escapeHtml(topic)}</span>`).join('');
        const accountKeywordNames = Array.isArray(a.keyword_names)
          ? a.keyword_names
          : Object.keys(a.keywords || {});
        const acctRefreshed = accountKeywordNames.some(kw => kmRefreshedKeywords.has(kw));
        const scoreRefreshedCls = acctRefreshed ? ' is-refreshed' : '';
        const scoreDisplay = scoreBoardValue(a, accountSortMode);
        const scoreLabel = scoreBoardMeta(accountSortMode).label;
        const scoreBreakthroughClass = scoreDisplay > 100 ? ' is-breakthrough' : '';
        const scoreSubtitle = accountSortMode === 'today'
          ? `<div class="score-sub">今日上榜 ${a.today_hit_count ?? 0} · 历史上榜 ${a.article_count ?? 0}</div>`
          : '';
        const scoreTitle = accountScoreTitle(a, accountSortMode);
        return `<div class="acct-row ${curAccount === a.name ? 'active' : ''}" onclick='selectAccount(${jsq(a.name)})'>
          <div class="rank-no ${rc}">${i + 1}</div>
          <div class="acct-main">
            <div class="acct-name-row">${accountChipHtml(a.name, a.headimg_url)}${accountBadge(a)}</div>
            <div class="kw-tags">${topicHtml}</div>
            <div class="kw-tags"><span class="kw-tag">近7天在榜 ${a.recent_hit_days || 0} 天</span><span class="kw-tag">覆盖 ${a.topic_count || 0} 个产品</span><span class="kw-tag">触达 ${a.bucket_count || 0} 类意图</span><span class="kw-tag">文章 ${a.article_count || 0} 篇</span><span class="kw-tag">当天命中 ${a.today_hit_count || 0}/${a.article_count || 0}</span></div>
            <div class="heatrow">${heat}</div>
          </div>
          <div class="acct-score js-score-tooltip${scoreBreakthroughClass}" data-account-id="${escapeHtml(a.account_id || '')}" data-score-tooltip-mode="${escapeHtml(accountSortMode)}" aria-label="${escapeHtml(scoreTitle)}" tabindex="0"><div class="score-val${scoreRefreshedCls}${scoreBreakthroughClass}">${scoreDisplay}</div><div class="score-lbl">${scoreLabel}</div>${scoreSubtitle}</div>
        </div>`;
      }).join('');

  if (mode === 'account' && visibleList.length < list.length) {
    html += `
      <div class="load-more-row">
        <button class="load-more-btn" type="button" onclick="loadMoreAccounts(event)">
          再加载 ${Math.min(ACCOUNT_PAGE_SIZE, list.length - visibleList.length)} 个
          <span>已显示 ${visibleList.length} / ${list.length}</span>
        </button>
      </div>`;
  }
  document.getElementById('acctList').innerHTML = html || `<div class="empty-hint">没有匹配结果</div>`;
}

function loadMoreAccounts(event) {
  event?.stopPropagation?.();
  accountPage += 1;
  renderList();
}

function mountDetailChart(values, tooltipPrefix) {
  if (detailChart) {
    detailChart.destroy();
    detailChart = null;
  }
  setTimeout(() => {
    const ctx = document.getElementById('detailChart');
    if (!ctx) return;
    const existing = Chart.getChart && Chart.getChart(ctx);
    if (existing) existing.destroy();
    detailChart = new Chart(ctx, {
      type:'bar',
      data:{
        labels:Array.from({length:values.length}, (_, i) => `D${i + 1}`),
        datasets:[{
          data:values,
          backgroundColor:values.map(v => v >= 8 ? '#3b82f6' : v >= 3 ? '#93c5fd' : '#dbeafe'),
          borderRadius:3,
          borderSkipped:false
        }]
      },
      options:{
        responsive:true,
        maintainAspectRatio:false,
        scales:{
          y:{ beginAtZero:true, ticks:{color:'#bbb'}, grid:{color:'#f5f5f5'} },
          x:{ ticks:{color:'#bbb', font:{size:10}}, grid:{display:false} }
        },
        plugins:{
          legend:{display:false},
          tooltip:{ callbacks:{ label:c => c.raw ? `${tooltipPrefix}：${c.raw}` : '无数据' } }
        }
      }
    });
  }, 30);
}

function renderKeywordDetail(kw) {
  const k = KEYWORD_BY_NAME.get(kw);
  if (!k) {
    document.getElementById('colRight').innerHTML = `<div class="empty-hint">← 点击左侧关键词查看详情</div>`;
    return;
  }
  if (!hasKeywordDetail(k)) {
    document.getElementById('colRight').innerHTML = `
      <div class="detail-wrap"><div class="card detail-loading">
        <div class="card-title">${escapeHtml(k.keyword)}</div>
        <div class="empty-hint">正在加载关键词快照与文章详情…</div>
      </div></div>`;
    const expectedKeyword = kw;
    fetchKeywordDetail(k).then(() => {
      if (mode === 'keyword' && curKeyword === expectedKeyword) {
        ensureKeywordRunState(expectedKeyword);
        renderKeywordDetail(expectedKeyword);
      }
    }).catch(error => {
      if (mode === 'keyword' && curKeyword === expectedKeyword) {
        document.getElementById('colRight').innerHTML = `
          <div class="detail-wrap"><div class="card detail-loading is-error">
            <div class="card-title">${escapeHtml(k.keyword)}</div>
            <div class="empty-hint">详情加载失败：${escapeHtml(error.message || error)}</div>
          </div></div>`;
      }
    });
    return;
  }

  const runs = Array.isArray(k.runs) ? k.runs : [];
  const currentRun = getSelectedKeywordRun(kw);
  if (!runs.length || !currentRun) {
    const topic = k.topic || k.keyword;
    const bucket = k.keyword_bucket || '未分类';
    document.getElementById('colRight').innerHTML = `
    <div class="detail-wrap">
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:flex-start">
          <div>
            <div style="font-size:15px;font-weight:700">${escapeHtml(k.keyword)}</div>
            <div style="font-size:11px;color:#aaa;margin-top:3px">关键词主视角 · 暂无快照数据</div>
            <div style="font-size:11px;color:#999;margin-top:8px">归属 topic：${escapeHtml(topic)}</div>
            <div style="font-size:11px;color:#999;margin-top:4px">类目：${escapeHtml(bucket)}</div>
          </div>
          <div style="display:flex;align-items:flex-start;gap:10px">
            <button class="pin-btn ${k.is_pinned ? 'active' : ''}" onclick='toggleKeywordPin(event, ${jsq(k.keyword_id)}, ${jsq(k.keyword)}, ${k.is_pinned ? 'false' : 'true'})'>${k.is_pinned ? '取消置顶' : '置顶关键词'}</button>
            <button class="pin-btn" id="refresh-btn-${escapeHtml(k.keyword_id)}" onclick='startKeywordRefresh(event, ${jsq(k.keyword_id)}, ${jsq(k.keyword)})'>刷新数据</button>
            <button class="pin-btn" style="color:#dc2626;border-color:#fecaca" onclick='deleteKeywordFromDetail(event, ${jsq(k.keyword_id)}, ${jsq(k.keyword)})'>归档关键词</button>
          </div>
        </div>
        <div class="stat-row" style="margin-top:12px;padding-top:10px;border-top:1px solid #f5f5f5">
          <div class="stat-item"><div class="stat-n">0</div><div class="stat-l">当前快照上榜</div></div>
          <div class="stat-item"><div class="stat-n">0</div><div class="stat-l">当日快照数</div></div>
          <div class="stat-item"><div class="stat-n">${k.coverage_days || 0}</div><div class="stat-l">${MONITOR_DATA.window_days}天覆盖天数</div></div>
          <div class="stat-item"><div class="stat-n">${k.article_count || 0}</div><div class="stat-l">沉淀文章数</div></div>
        </div>
      </div>
      ${renderKeywordReadDeltaCard(k)}
      <div class="card" style="text-align:center;padding:40px 20px;color:#bbb;font-size:13px">
        该关键词暂无可回看的快照<br>
        <span style="font-size:11px;color:#ccc;margin-top:6px;display:inline-block">点击「刷新数据」可立即抓取一次</span>
      </div>
    </div>`;
    return;
  }

  const selectedDate = currentRun.date;
  const currentTopic = k.topic || k.keyword;
  const currentBucket = k.keyword_bucket || '未分类';
  const dateOptions = getKeywordDates(kw);
  const recentDateOptions = getRecentKeywordDates(kw);
  const customDateActive = !recentDateOptions.includes(selectedDate);
  const customDateLabel = customDateActive ? `自定义 ${formatDateShort(selectedDate)}` : '自定义日期';
  const runsOnDate = runs.filter(run => run.date === selectedDate);
  const lead = currentRun.articles[0];
  const topicPeers = ALL_KEYWORDS.filter(item => (item.topic || item.keyword) === currentTopic)
    .map(item => item.keyword)
    .sort((a, b) => a.localeCompare(b, 'zh-CN'));
      const articleRows = currentRun.articles.length
    ? currentRun.articles.map(art => `<div class="art-row keyword-article" onclick='openArtByUrl(${jsq(art.url)}, ${jsq(art.title)}, ${jsq(art.content_path)}, { article_id:${jsq(art.article_id || '')}, rank:${art.rank}, account:${jsq(art.account)}, kw:${jsq(k.keyword)}, read_count:${art.read_count != null ? art.read_count : 'null'}, like_count:${art.like_count != null ? art.like_count : 'null'}, friends_follow_count:${art.friends_follow_count != null ? art.friends_follow_count : 'null'}, original_article_count:${art.original_article_count != null ? art.original_article_count : 'null'} })'>
        ${buildArticleCoverHtml(art)}
        <div class="snapshot-article-main">
          <div class="snapshot-article-title">
            <span class="art-rank snapshot-title-rank rl-${Math.min(art.rank,10)}">${art.rank}</span>
            <span class="snapshot-title-text">${escapeHtml(art.title)}</span>
          </div>
          <div class="snapshot-article-meta">
            ${accountChipHtml(art.account, art.account_headimg, {clickHandler: `onclick='event.stopPropagation();selectAccount(${jsq(art.account)})'`, extraClass: 'snapshot-account-chip'})}
            <span class="snapshot-meta-item snapshot-hit-chip ${snapshotHitToneClass(art.hit_days)}">在榜 ${art.hit_days} 天</span>
            ${metricsChipHtml(art)}
            <span class="snapshot-meta-item">${escapeHtml(art.published_at || '—')}</span>
            <span class="row-link">${art.content_path ? '查看正文' : (hasRealUrl(art.url) ? '查看原文' : '仅榜单')}</span>
          </div>
        </div>
      </div>`).join('')
    : `<div class="empty-block">该快照暂无可展示文章</div>`;

  const accountRows = k.accounts.length
    ? k.accounts.map(a => {
      const fullAccount = ACCOUNT_BY_NAME.get(a.name) || a;
      return `<div class="mini-row" onclick='selectAccount(${jsq(a.name)})'>
        <div class="art-rank ${a.today_rank ? `rl-${Math.min(a.today_rank,10)}` : 'r-low'}">${a.today_rank || '—'}</div>
        <div class="mini-main">
          <div class="mini-name">${accountChipHtml(a.name, a.headimg_url)}</div>
          <div class="mini-meta">${MONITOR_DATA.window_days}天在榜 ${a.hit_days} 天 · ${a.best_rank ? `最佳第${a.best_rank}名` : '暂无历史'} · ${escapeHtml(rankMoveLabel(a.today_rank, a.today_prev))}</div>
        </div>
        <div class="mini-side js-score-tooltip" data-account-id="${escapeHtml(fullAccount.account_id || '')}" data-score-tooltip-mode="score" aria-label="${escapeHtml(accountScoreTitle(fullAccount, 'score'))}" tabindex="0">
          <div class="mini-rank">${scoreInt(fullAccount.score)}</div>
          <div class="mini-score-sm">账号分</div>
        </div>
      </div>`;
    }).join('')
    : `<div class="empty-block">暂无账号透视数据</div>`;

  // 搜索词信号
  const termsBlock = (() => {
    const t = currentRun.terms || {suggestions: [], related: []};
    const renderTerm = (x) => `<span class="kw-tag signal-term" onclick='signalTermClick(${jsq(x)})' style="cursor:pointer" title="点击添加到监控关键词">${escapeHtml(x)}</span>`;
    const sugg = t.suggestions.length
      ? t.suggestions.map(renderTerm).join(' ')
      : '<span style="color:#bbb;font-size:11px">无</span>';
    const rel = t.related.length
      ? t.related.map(renderTerm).join(' ')
      : '<span style="color:#bbb;font-size:11px">无</span>';
    return `
      <div style="margin-bottom:8px"><span style="font-size:11px;color:#999;margin-right:6px">下拉词</span>${sugg}</div>
      <div><span style="font-size:11px;color:#999;margin-right:6px">相关搜索</span>${rel}</div>`;
  })();

  const topicPeerTags = topicPeers.map(item => `<span class="kw-tag">${escapeHtml(item)}</span>`).join('');

  document.getElementById('colRight').innerHTML = `
    <div class="detail-wrap">
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:flex-start">
          <div>
            <div style="font-size:15px;font-weight:700">${escapeHtml(k.keyword)}</div>
            <div style="font-size:11px;color:#aaa;margin-top:3px">关键词主视角 · 快照榜单在前，${MONITOR_DATA.window_days}天盯号在后</div>
            <div style="font-size:11px;color:#999;margin-top:8px">${lead ? `当前快照领跑：${escapeHtml(lead.account)} · ${escapeHtml(lead.title)}` : '当前快照暂无领跑文章'}</div>
            <div style="font-size:11px;color:#999;margin-top:6px">归属 topic：${escapeHtml(k.topic || k.keyword)}</div>
          </div>
          <div style="display:flex;align-items:flex-start;gap:10px">
            <button class="pin-btn ${k.is_pinned ? 'active' : ''}" onclick='toggleKeywordPin(event, ${jsq(k.keyword_id)}, ${jsq(k.keyword)}, ${k.is_pinned ? 'false' : 'true'})'>${k.is_pinned ? '取消置顶' : '置顶关键词'}</button>
            <button class="pin-btn" id="refresh-btn-${escapeHtml(k.keyword_id)}" onclick='startKeywordRefresh(event, ${jsq(k.keyword_id)}, ${jsq(k.keyword)})'>刷新数据</button>
            <button class="pin-btn" style="color:#dc2626;border-color:#fecaca" onclick='deleteKeywordFromDetail(event, ${jsq(k.keyword_id)}, ${jsq(k.keyword)})'>归档关键词</button>
          </div>
        </div>
        <div class="stat-row" style="margin-top:12px;padding-top:10px;border-top:1px solid #f5f5f5">
          <div class="stat-item"><div class="stat-n hi">${currentRun.result_count}</div><div class="stat-l">当前快照上榜</div></div>
          <div class="stat-item"><div class="stat-n">${runsOnDate.length}</div><div class="stat-l">当日快照数</div></div>
          <div class="stat-item"><div class="stat-n">${k.coverage_days}</div><div class="stat-l">${MONITOR_DATA.window_days}天覆盖天数</div></div>
          <div class="stat-item"><div class="stat-n">${k.article_count}</div><div class="stat-l">沉淀文章数</div></div>
        </div>
      </div>

      ${renderKeywordReadDeltaCard(k)}

      <div class="card">
          <div class="card-title">外部热度分解读</div>
        ${(() => {
          const s = k.kw_score;
          if (!s) return '<div style="font-size:11px;color:#bbb">暂无评分数据</div>';
          // V2：有热度时显示热度（100% 权重），无热度时显示丰度（100% 权重）
          const total = s.has_heat
            ? Math.round(s.heat * 100)
            : Math.round(s.richness * 100);
          const heatBar = s.has_heat
            ? `<div class="score-dim"><div class="score-dim-label">热度 <span class="score-dim-pct">100%</span></div><div class="score-bar-bg"><div class="score-bar-fill score-bar-heat" style="width:${(s.heat*100).toFixed(0)}%"></div></div><div class="score-dim-val">${(s.heat*100).toFixed(0)}</div></div>`
            : '';
          const richnessBar = s.has_heat
            ? `<div class="score-dim" style="opacity:0.45"><div class="score-dim-label">丰度 <span class="score-dim-pct">参考</span></div><div class="score-bar-bg"><div class="score-bar-fill score-bar-richness" style="width:${(s.richness*100).toFixed(0)}%"></div></div><div class="score-dim-val">${(s.richness*100).toFixed(0)}</div></div>`
            : `<div class="score-dim"><div class="score-dim-label">丰度 <span class="score-dim-pct">100%</span></div><div class="score-bar-bg"><div class="score-bar-fill score-bar-richness" style="width:${(s.richness*100).toFixed(0)}%"></div></div><div class="score-dim-val">${(s.richness*100).toFixed(0)}</div></div>`;
          const wsoLine = s.wso_val ? `<span style="color:#10b981">微信 ${s.wso_val.toLocaleString()}</span>` : '';
          const dsoLine = s.dso_val ? `<span style="color:#333">抖音 ${s.dso_val.toLocaleString()}</span>` : '';
          const heatSource = (wsoLine || dsoLine) ? `<div class="score-source">${[wsoLine, dsoLine].filter(Boolean).join(' · ')}</div>` : '<div class="score-source" style="color:#bbb">无AIDSO数据</div>';
          return `
          <div class="score-total-row">
            <div class="score-total-num">${total}</div>
            <div class="score-total-label">${s.has_heat ? '外部热度分' : '丰度分'}</div>
          </div>
          ${heatSource}
          <div class="score-dims">
            ${heatBar}
            ${richnessBar}
            <div class="score-dim" style="opacity:0.45"><div class="score-dim-label">广度 <span class="score-dim-pct">仅展示</span></div><div class="score-bar-bg"><div class="score-bar-fill score-bar-breadth" style="width:${(s.breadth*100).toFixed(0)}%"></div></div><div class="score-dim-val">${(s.breadth*100).toFixed(0)}</div></div>
          </div>
          <div style="font-size:10px;color:#bbb;margin-top:6px">它有意义，但只回答“这个词在外部搜索平台上相对热不热”：分数相对于当前监控词集合计算，接近100表示接近本集合最高，不代表绝对热度100，也不代表今天榜单强。排序规则：置顶 &gt; 有热度(按热度) &gt; 无热度(按丰度)；广度只展示不参与排序。</div>`;
        })()}
      </div>

      ${(() => {
        const turnover = calcTurnoverRate(getTurnoverRuns(k));
        if (!turnover) return '';
        const { lastRate, lastSame, lastNew, lastGone, lastCurrCount, lastPrevCount, lastCurrDate, lastPrevDate } = turnover;
        const lastPct = turnoverPercent(lastRate);
        const latestShare = turnoverShareText(lastRate);
        const changeCount = lastNew + lastGone;
        const compareTotal = lastCurrCount + lastPrevCount;
        return `
        <div class="card turnover-card">
          <div class="card-title-row">
            <span class="card-title">上榜文章换新</span>
            <a class="turnover-card-link" href="${escapeHtml(turnoverDetailUrl(k))}" onclick="event.stopPropagation()">完整热力图</a>
          </div>
          ${buildTurnoverPreviewHtml(k, turnover)}
          <div class="turnover-latest-box">
            <div>
              <strong>最近一次怎么读</strong>
              <span>${escapeHtml(turnoverRunPairText(turnover))}</span>
            </div>
            <div>这次抓到 ${lastCurrCount} 篇，上一次抓到 ${lastPrevCount} 篇；其中 ${lastSame} 篇还在榜。</div>
            <div>${lastNew} 篇是这次新出现的，${lastGone} 篇从上次榜单里掉出，所以最近一次${latestShare}内容变了（${changeCount}/${compareTotal} = ${lastPct}）。</div>
          </div>
        </div>`;
      })()}

      <div class="card">
        <div class="card-title">快照时间选择</div>
        <div class="picker-stack">
          <div class="picker-group">
            <div class="picker-label">日期</div>
            <div class="chip-row">
              <span class="time-chip date-chip ${customDateActive ? 'active' : ''}" onclick='event.stopPropagation();openCustomKeywordDatePicker(${jsq(k.keyword)})'>
                ${customDateLabel}
                <input id="${keywordDateInputId(k.keyword)}" class="date-chip-input" type="date" value="${selectedDate}" min="${dateOptions[dateOptions.length - 1]}" max="${dateOptions[0]}" onchange='onCustomKeywordDateChange(${jsq(k.keyword)}, this.value)' />
              </span>
              ${recentDateOptions.map(date => `<span class="time-chip ${date === selectedDate ? 'active' : ''}" onclick='event.stopPropagation();selectKeywordDate(${jsq(k.keyword)},${jsq(date)})'>${formatDateShort(date)}</span>`).join('')}
            </div>
          </div>
          <div class="picker-group">
            <div class="picker-label">该日抓取时间点</div>
            <div class="chip-row">
              ${runsOnDate.map(run => `<span class="time-chip ${run.id === currentRun.id ? 'active' : ''}" onclick='event.stopPropagation();selectKeywordRun(${jsq(k.keyword)},${jsq(run.id)})'>${run.time} · ${formatRunTypeShort(run.trigger_type)}${run.is_primary ? ' · 主' : ''}</span>`).join('')}
            </div>
          </div>
          <div class="snapshot-meta">当前显示：${escapeHtml(currentRun.run_at)} · ${formatRunType(currentRun.trigger_type)} · ${currentRun.result_count} 个账号上榜${currentRun.note ? ` · ${escapeHtml(currentRun.note)}` : ''}</div>
        </div>
      </div>

      <div class="card">
        <div class="card-title">当前快照榜单</div>
        <div class="section-stack">${articleRows}</div>
      </div>

      <div class="card">
        <div class="card-title">搜索词信号（来自当前快照）</div>
        ${termsBlock}
      </div>

      <div class="card">
        <div class="card-title">账号透视（${MONITOR_DATA.window_days}天窗口）</div>
        <div style="font-size:11px;color:#aaa;margin-bottom:10px">这里不跟随上方快照切换，用来判断这个词长期值得盯哪些号。</div>
        <div class="section-stack">${accountRows}</div>
      </div>
    </div>`;

  queueVisibleArticleCovers();
}

function accountArticleKey(article) {
  if (!article) return '';
  return article.article_id || `${article.title || ''}|${article.url || ''}`;
}

function accountArticleSortValue(dateText) {
  const text = String(dateText || '').trim();
  const full = text.match(/^(\d{4})[-/](\d{1,2})[-/](\d{1,2})/);
  if (full) return Number(`${full[1]}${String(full[2]).padStart(2, '0')}${String(full[3]).padStart(2, '0')}`);
  const yySlash = text.match(/^(\d{2})\/(\d{1,2})\/(\d{1,2})$/);
  if (yySlash) return Number(`20${yySlash[1]}${String(yySlash[2]).padStart(2, '0')}${String(yySlash[3]).padStart(2, '0')}`);
  const short = text.match(/^(\d{1,2})\/(\d{1,2})$/);
  if (short) return Number(`${new Date().getFullYear()}${String(short[1]).padStart(2, '0')}${String(short[2]).padStart(2, '0')}`);
  return 0;
}

function emptyKeywordMeta() {
  return { topics: new Set(), buckets: new Set() };
}

function addKeywordMeta(map, keyword, updater) {
  if (!keyword) return;
  const meta = map.get(keyword) || emptyKeywordMeta();
  updater(meta);
  map.set(keyword, meta);
}

function mergeAccountArticle(articleMap, rawArticle) {
  const key = accountArticleKey(rawArticle);
  if (!key) return null;

  const current = articleMap.get(key) || {
    article_id: rawArticle.article_id || '',
    title: rawArticle.title || '未命名文章',
    url: rawArticle.url || '',
    cover_url: rawArticle.cover_url,
    published_at: rawArticle.published_at || '',
    content_path: rawArticle.content_path,
    best_rank: 0,
    topics: new Set(),
    buckets: new Set(),
    keywords: new Set(),
    theme_histories: [],
    read_count: null,
    like_count: null,
  };

  if (!current.article_id && rawArticle.article_id) current.article_id = rawArticle.article_id;
  if (!current.url && rawArticle.url) current.url = rawArticle.url;
  if (!current.cover_url && rawArticle.cover_url) current.cover_url = rawArticle.cover_url;
  if (!current.published_at && rawArticle.published_at) current.published_at = rawArticle.published_at;
  if (!current.content_path && rawArticle.content_path) current.content_path = rawArticle.content_path;
  if (!current.title && rawArticle.title) current.title = rawArticle.title;
  if (current.read_count == null && rawArticle.read_count != null) current.read_count = rawArticle.read_count;
  if (current.like_count == null && rawArticle.like_count != null) current.like_count = rawArticle.like_count;

  const rank = Number(rawArticle.rank || rawArticle.best_rank || 0);
  if (rank > 0 && (!current.best_rank || rank < current.best_rank)) current.best_rank = rank;

  articleMap.set(key, current);
  return current;
}

function buildAccountArticleFeed(account) {
  const articleMap = new Map();
  const keywordMetaMap = new Map();

  for (const [, info] of Object.entries(account.topics || {})) {
    const themeLabel = info.label || '';
    const isBucketTheme = info.theme_type === 'bucket';
    for (const keyword of info.keywords || []) {
      addKeywordMeta(keywordMetaMap, keyword, meta => {
        if (themeLabel) (isBucketTheme ? meta.buckets : meta.topics).add(themeLabel);
        for (const bucket of info.buckets || []) meta.buckets.add(bucket);
      });
    }
  }

  for (const [keyword, detail] of Object.entries(account.keywords || {})) {
    const meta = keywordMetaMap.get(keyword) || emptyKeywordMeta();
    for (const art of detail.articles || []) {
      const row = mergeAccountArticle(articleMap, art);
      if (!row) continue;
      row.keywords.add(keyword);
      for (const topic of meta.topics) row.topics.add(topic);
      for (const bucket of meta.buckets) row.buckets.add(bucket);
    }
  }

  for (const [, info] of Object.entries(account.topics || {})) {
    const themeLabel = info.label || '';
    const isBucketTheme = info.theme_type === 'bucket';
    for (const art of info.articles || []) {
      const row = mergeAccountArticle(articleMap, art);
      if (!row) continue;
      if (themeLabel) (isBucketTheme ? row.buckets : row.topics).add(themeLabel);
      for (const bucket of info.buckets || []) row.buckets.add(bucket);
      if (info.history?.length) row.theme_histories.push(info.history);
    }
  }

  return [...articleMap.values()].sort((a, b) => {
    const dateDelta = accountArticleSortValue(b.published_at) - accountArticleSortValue(a.published_at);
    if (dateDelta) return dateDelta;
    return (a.best_rank || 99) - (b.best_rank || 99);
  });
}

function renderAccountArticleRow(article, accountName) {
  const bestRank = article.best_rank || 0;
  const rankClass = bestRank ? `rl-${Math.min(bestRank, 10)}` : 'r-low';
  const rankText = bestRank || '—';
  const actionText = article.content_path ? '查看正文' : (hasRealUrl(article.url) ? '查看原文' : '仅榜单');
  const dateStr = article.published_at || '—';

  const topicList = [...article.topics];
  const bucketList = [...article.buckets];
  const kwList = [...article.keywords];

  const parts = [];
  if (topicList.length) parts.push(`<span class="acart-topic">📌 ${topicList.map(t => escapeHtml(t)).join(' · ')}</span>`);
  if (bucketList.length) parts.push(`<span class="acart-bucket">🏷 ${bucketList.map(b => escapeHtml(b)).join(' · ')}</span>`);
  if (kwList.length) parts.push(`<span class="acart-kw">🔍 匹配了${kwList.slice(0, 6).map(k => `"${escapeHtml(k)}"`).join('、')}${kwList.length > 6 ? `等 ${kwList.length} 个关键词` : ''}</span>`);
  const tagsLine = parts.length ? `<div class="acart-tags">${parts.join('<span class="acart-sep">丨</span>')}</div>` : '';

  return `<div class="art-row account-article-row${article.is_today ? ' is-today' : ''}" onclick='openArtByUrl(${jsq(article.url)}, ${jsq(article.title)}, ${jsq(article.content_path)}, { article_id:${jsq(article.article_id || '')}, rank:${bestRank || 'null'}, account:${jsq(accountName)}, read_count:${article.read_count != null ? article.read_count : 'null'}, like_count:${article.like_count != null ? article.like_count : 'null'}, friends_follow_count:${article.friends_follow_count != null ? article.friends_follow_count : 'null'}, original_article_count:${article.original_article_count != null ? article.original_article_count : 'null'} })'>
    ${buildArticleCoverHtml(article)}
    <div class="account-article-main">
      <div class="acart-header">
        <span class="art-rank ${rankClass}">${rankText}</span>
        <span class="acart-title${article.is_today ? ' is-today' : ''}">${escapeHtml(article.title)}</span>
      </div>
      <div class="acart-meta">
        <span>${dateStr}</span>
        ${metricsChipHtml(article)}
        <span class="row-link">${actionText}</span>
      </div>
      ${tagsLine}
    </div>
  </div>`;
}

function renderAccountDetail(name) {
  const a = ACCOUNT_BY_NAME.get(name);
  if (!a) {
    document.getElementById('colRight').innerHTML = `<div class="empty-hint">← 点击左侧账号查看详情</div>`;
    return;
  }
  if (!hasAccountDetail(a)) {
    document.getElementById('colRight').innerHTML = `
      <div class="detail-wrap"><div class="card detail-loading">
        <div class="card-title">${accountChipHtml(a.name, a.headimg_url)}</div>
        <div class="empty-hint">正在加载账号文章、主题与评分详情…</div>
      </div></div>`;
    const expectedAccount = name;
    fetchAccountDetail(a).then(() => {
      if (mode === 'account' && curAccount === expectedAccount) {
        renderAccountDetail(expectedAccount);
      }
    }).catch(error => {
      if (mode === 'account' && curAccount === expectedAccount) {
        document.getElementById('colRight').innerHTML = `
          <div class="detail-wrap"><div class="card detail-loading is-error">
            <div class="card-title">${escapeHtml(a.name)}</div>
            <div class="empty-hint">详情加载失败：${escapeHtml(error.message || error)}</div>
          </div></div>`;
      }
    });
    return;
  }

  const topicTags = getAccountTopicNames(a).map(topic => `<span class="kw-tag">${escapeHtml(topic)}</span>`).join('');
  const accountArticles = buildAccountArticleFeed(a);
  const initArticleTab = accountSortMode === 'timeliness' ? 'top3' : 'all';
  const detailScore = scoreBoardValue(a, accountSortMode);
  const detailScoreLabel = scoreBoardMeta(accountSortMode).label;
  const detailScoreClass = detailScore > 100 ? ' is-breakthrough' : '';
  const detailScoreSubtitle = accountSortMode === 'today'
    ? `<div class="detail-score-sub">今日上榜 ${a.today_hit_count ?? 0} · 历史上榜 ${a.article_count ?? 0}</div>`
    : '';
  const topicBlocks = Object.entries(a.topics || {})
    .sort(([, da], [, db]) => sumTopicScores(db) - sumTopicScores(da))
    .map(([topic, info]) => {
      const themeLabel = info.label || topic;
      const themeKind = info.theme_type === 'bucket' ? '类目主题' : '产品主题';
      const cells = info.history.map((r, i) => {
        if (r === 0) return `<div class="tl-cell" title="D${i + 1}: 未上榜"></div>`;
        return `<div class="tl-cell rl-${Math.min(r,10)}" title="D${i + 1}: 第${r}名">${r}</div>`;
      }).join('');
      const articleList = (info.articles || []).slice(0, 6).map(art => `<div class="art-row">
          <div class="art-rank rl-${Math.min(art.rank,10)}">${art.rank}</div>
          ${buildArticleCoverHtml(art)}
          <div class="art-main">
            <div class="art-title" onclick='openArtByUrl(${jsq(art.url)}, ${jsq(art.title)}, ${jsq(art.content_path)}, { article_id:${jsq(art.article_id || '')}, rank:${art.rank || 'null'}, read_count:${art.read_count != null ? art.read_count : 'null'}, like_count:${art.like_count != null ? art.like_count : 'null'} })'>${escapeHtml(art.title)}</div>
            <div class="art-sub">${escapeHtml(art.published_at || '')} ${metricsChipHtml(art)} · ${art.content_path ? '有正文' : '仅榜单/原文'}</div>
          </div>
        </div>`).join('');
      const score = sumTopicScores(info).toFixed(2);
      return `<div class="kw-section">
        <div class="kw-header">
          <div>
            <div class="kw-name">${escapeHtml(themeLabel)}</div>
            <div class="topic-summary">在榜 ${info.hit_days} 天 · 最佳第${info.best_rank || '—'}名 · 文章 ${info.article_count} 篇 · 关键词 ${info.keyword_count} 个 · 类目 ${info.bucket_count || 0} 个 · 基础分 ${score}</div>
          </div>
          <div class="score-pill">${escapeHtml(themeKind)}</div>
        </div>
        <div class="kw-tags">${(info.buckets || []).map(bucket => `<span class="kw-tag">${escapeHtml(bucket)}</span>`).join('')}</div>
        <div class="kw-tags">${(info.keywords || []).map(keyword => `<span class="kw-tag">${escapeHtml(keyword)}</span>`).join('')}</div>
        <div class="timeline">${cells}</div>
        <div class="stack-block">${articleList || '<div style="font-size:11px;color:#bbb;padding:8px 0">该 topic 下暂无文章记录</div>'}</div>
      </div>`;
    }).join('');

  const kwBlocks = Object.entries(a.keywords || {})
    .sort(([, da], [, db]) => (db.history || []).reduce((s,r)=>s+rankWeight(r),0)
                              - (da.history || []).reduce((s,r)=>s+rankWeight(r),0))
    .map(([kw, d]) => {
      const cells = d.history.map((r, i) => {
        if (r === 0) return `<div class="tl-cell" title="D${i + 1}: 未上榜"></div>`;
        return `<div class="tl-cell rl-${Math.min(r,10)}" title="D${i + 1}: 第${r}名">${r}</div>`;
      }).join('');
      const artList = (d.articles || []).map(art => `<div class="art-row">
          <div class="art-rank rl-${Math.min(art.rank,10)}" onclick='event.stopPropagation();selectKeyword(${jsq(kw)})'>${art.rank}</div>
          <div class="art-main">
            <div class="art-title" onclick='openArtByUrl(${jsq(art.url)}, ${jsq(art.title)}, ${jsq(art.content_path)}, { article_id:${jsq(art.article_id || '')}, rank:${art.rank || 'null'}, read_count:${art.read_count != null ? art.read_count : 'null'}, like_count:${art.like_count != null ? art.like_count : 'null'} })'>${escapeHtml(art.title)}</div>
            <div class="art-sub">${escapeHtml(art.published_at || '')} ${metricsChipHtml(art)} · ${art.content_path ? '有正文' : (hasRealUrl(art.url) ? '有原文链接' : '仅榜单记录')}</div>
          </div>
          <div class="row-actions">
            <span class="row-link" onclick='event.stopPropagation();openArtByUrl(${jsq(art.url)}, ${jsq(art.title)}, ${jsq(art.content_path)}, { article_id:${jsq(art.article_id || '')} })'>${art.content_path ? '正文' : (hasRealUrl(art.url) ? '原文' : '详情')}</span>
            <span class="row-link" onclick='event.stopPropagation();selectKeyword(${jsq(kw)})'>看词榜</span>
          </div>
        </div>`).join('');
      return `<div class="kw-section">
        <div class="kw-header">
          <div>
            <div class="kw-name">${escapeHtml(kw)}</div>
            <div class="kw-meta">在榜 ${d.hit_days} 天${d.best_rank ? ` · 最佳第${d.best_rank}名` : ''}</div>
          </div>
          <span class="jump-link" onclick='event.stopPropagation();selectKeyword(${jsq(kw)})'>查看关键词</span>
        </div>
        <div class="timeline">${cells}</div>
        <div style="display:flex;flex-direction:column;gap:2px">${artList || '<div style="font-size:11px;color:#bbb;padding:8px 0">该关键词下暂无文章记录</div>'}</div>
      </div>`;
    }).join('');

  document.getElementById('colRight').innerHTML = `
    <div class="detail-wrap">
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:flex-start">
          <div>
            <div style="font-size:15px;font-weight:700">${accountChipHtml(a.name, a.headimg_url, {extraClass: 'acct-chip-lg'})}</div>
            <div style="font-size:11px;color:#aaa;margin-top:3px">账号副视角 · 先看最近上榜文章，再用主题标签理解这个号在写什么。</div>
            <div style="font-size:11px;color:#999;margin-top:8px">最近上榜文章按发布时间倒序排列；主题和类目会作为标签挂在每篇文章下面。</div>
          </div>
          <div class="detail-score js-score-tooltip${detailScoreClass}" data-account-id="${escapeHtml(a.account_id || '')}" data-score-tooltip-mode="${escapeHtml(accountSortMode)}" aria-label="${escapeHtml(accountScoreTitle(a, accountSortMode))}" tabindex="0">
            <div class="detail-score-value${detailScoreClass}">${detailScore}</div>
            <div class="detail-score-label">${detailScoreLabel}</div>
            ${detailScoreSubtitle}
          </div>
        </div>
        <div class="stat-row" style="margin-top:12px;padding-top:10px;border-top:1px solid #f5f5f5">
          <div class="stat-item"><div class="stat-n hi">${a.recent_hit_days || 0}</div><div class="stat-l">近7天在榜天数</div></div>
          <div class="stat-item"><div class="stat-n">${a.current_streak || 0}</div><div class="stat-l">当前连击</div></div>
          <div class="stat-item"><div class="stat-n">${a.longest_streak || 0}</div><div class="stat-l">窗口最长连击</div></div>
          <div class="stat-item"><div class="stat-n">${a.topic_count || 0}</div><div class="stat-l">近7天产品数</div></div>
          <div class="stat-item"><div class="stat-n">${a.bucket_count || 0}</div><div class="stat-l">近7天类目数</div></div>
          <div class="stat-item"><div class="stat-n">${a.kw_count}</div><div class="stat-l">关联关键词数</div></div>
          <div class="stat-item"><div class="stat-n">${a.article_count}</div><div class="stat-l">沉淀文章数</div></div>
          ${a.friends_follow_count != null ? `<div class="stat-item"><div class="stat-n">${a.friends_follow_count}</div><div class="stat-l">朋友关注</div></div>` : ''}
          ${a.original_article_count != null ? `<div class="stat-item"><div class="stat-n">${a.original_article_count}</div><div class="stat-l">原创篇数</div></div>` : ''}
        </div>
        <div class="kw-tags" style="margin-top:10px">${topicTags}</div>
      </div>

      <div class="card account-article-card">
        <div class="card-title-row">
          <span class="card-title">最近命中文章 · 按发布时间</span>
          <div class="acct-sort-tabs art-filter-tabs">
            <button class="acct-sort-tab ${initArticleTab === 'top3' ? 'active' : ''}" onclick="setArticleFilterTab('top3')">时效 Top3</button>
            <button class="acct-sort-tab ${initArticleTab === 'all' ? 'active' : ''}" onclick="setArticleFilterTab('all')">全部</button>
          </div>
        </div>
        <div class="section-stack account-article-list" id="accountArticleList"></div>
      </div>

      <div class="card account-trend-card">
        <div class="card-title">${MONITOR_DATA.window_days}天加权基础影响力趋势</div>
        <div style="font-size:11px;color:#aaa;margin-bottom:8px">保留一个小趋势图，辅助判断这个号是不是最近持续出现。</div>
        <div class="chart-wrap"><canvas id="detailChart"></canvas></div>
      </div>

      <details class="detail-fold">
        <summary>按研究主题拆解</summary>
        <div class="fold-body">${topicBlocks || '<div class="empty-block">暂无 topic 拆解数据</div>'}</div>
      </details>

      <details class="detail-fold">
        <summary>关键词原始命中明细</summary>
        <div class="fold-body">${kwBlocks}</div>
      </details>
    </div>`;

  mountDetailChart(a.day_scores, '加权基础分');

  // 今日文章集合由账号详情接口一次性返回，避免为一个账号扫描所有关键词快照。
  const todayArticleIds = new Set(a._today_article_ids || []);
  const todayTitles = new Set(a._today_article_titles || []);
  accountArticles.forEach(art => {
    art.is_today = (art.article_id && todayArticleIds.has(art.article_id))
      || todayTitles.has(art.title);
  });

  // 文章列表初始渲染
  window._curAccountArticles = accountArticles;
  window._curAccountName = a.name;
  _renderArticleList(initArticleTab);
  queueVisibleArticleCovers();
}

function _renderArticleList(tab) {
  const articles = window._curAccountArticles || [];
  const name = window._curAccountName || '';
  const filtered = tab === 'top3'
    ? articles.filter(art => (art.best_rank || 99) <= 3)
    : articles;
  const html = filtered.length
    ? filtered.map(art => renderAccountArticleRow(art, name)).join('')
    : `<div class="empty-block">${tab === 'top3' ? '近期暂无 Top3 文章' : '该账号暂无可展示文章'}</div>`;
  const el = document.getElementById('accountArticleList');
  if (el) { el.innerHTML = html; queueVisibleArticleCovers(); }
  // 同步 tab active 状态
  const tabs = document.querySelectorAll('.art-filter-tabs .acct-sort-tab');
  tabs.forEach(btn => {
    btn.classList.toggle('active', btn.textContent.includes('Top3') ? tab === 'top3' : tab === 'all');
  });
}

function setArticleFilterTab(tab) {
  _renderArticleList(tab);
}

function refresh() {
  syncModeUi();

  if (mode === 'keywordManage') {
    loadKeywordManageView();
    return;
  }

  if (mode === 'articleList') {
    if (window.alInit) window.alInit();
    return;
  }

  renderList();
  if (mode === 'keyword') {
    if (!curKeyword && ALL_KEYWORDS[0]) curKeyword = ALL_KEYWORDS[0].keyword;
    renderKeywordDetail(curKeyword);
    return;
  }
  if (!curAccount && ALL_ACCOUNTS[0]) curAccount = ALL_ACCOUNTS[0].name;
  renderAccountDetail(curAccount);
}

function setAccountSortMode(m) {
  hideAccountScoreTooltip();
  accountSortMode = m;
  document.getElementById('sortTabScore').classList.toggle('active', m === 'score');
  document.getElementById('sortTabTimeliness').classList.toggle('active', m === 'timeliness');
  document.getElementById('sortTabToday').classList.toggle('active', m === 'today');
  renderList();
  if (curAccount) renderAccountDetail(curAccount);
}

function setMode(next) {
  const modeChanged = mode !== next;
  if (modeChanged) {
    filter = '';
    const search = document.getElementById('searchInput');
    if (search) search.value = '';
  }
  mode = next;
  if (modeChanged) {
    closeDrawer();
    closeMobileSidebar();
    kmCloseSettingsModal();
    kmCloseKeywordModal();
  }
  refresh();
}

function selectKeywordDate(kw, date) {
  curKeyword = kw;
  mode = 'keyword';
  setKeywordDateState(kw, date);
  refresh();
}

function openCustomKeywordDatePicker(kw) {
  const input = document.getElementById(keywordDateInputId(kw));
  if (!input) return;
  if (typeof input.showPicker === 'function') {
    input.showPicker();
    return;
  }
  input.focus();
  input.click();
}

function onCustomKeywordDateChange(kw, date) {
  if (!date) return;
  selectKeywordDate(kw, date);
}

function selectKeywordRun(kw, runId) {
  const run = getKeywordRuns(kw).find(item => item.id === runId);
  if (!run) return;
  curKeyword = kw;
  mode = 'keyword';
  keywordRunState[kw] = { date: run.date, runId };
  refresh();
}

function selectKeyword(kw) {
  curKeyword = kw;
  mode = 'keyword';
  ensureKeywordRunState(kw);
  collapseLeftOnMobile();
  refresh();
}

function selectAccount(name) {
  if (window.alSaveState) window.alSaveState();
  curAccount = name;
  mode = 'account';
  collapseLeftOnMobile();
  refresh();
}

function filterList(v) {
  filter = v.trim();
  refresh();
}

// 拉取分组数据 → 建 keyword_text → group_label 映射 → 注入 ALL_KEYWORDS._group → 渲染新筛选条
async function loadGroups(existingData = null) {
  try {
    let data = existingData;
    if (!data) {
      const resp = await fetch('/api/keyword-manage', { cache: 'no-cache' });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      data = await resp.json();
      KM_DATA = data;
      kmLoaded = true;
    }
    const groups = (data && data.groups) || [];
    kwGroupMap = {};
    kwGroupOrder = [];
    groups.forEach(g => {
      if (g?.label) kwGroupOrder.push(g.label);
      (g.keywords || []).forEach(kw => {
        if (kw && kw.keyword_text) kwGroupMap[kw.keyword_text] = g.label || '';
      });
    });
    kwGroupOrder = [...new Set(kwGroupOrder.filter(Boolean))];
  } catch (e) {
    kwGroupMap = {};
    kwGroupOrder = [];
  }
  if (typeof ALL_KEYWORDS !== 'undefined' && ALL_KEYWORDS) {
    ALL_KEYWORDS.forEach(k => { k._group = kwGroupMap[k.keyword] || ''; });
  }
  renderKeywordFilterBar();
}

async function openArtByUrl(url, title, contentPath, meta = {}) {
  document.getElementById('drawerTitle').textContent = title;
  document.getElementById('drawer').classList.add('open');
  const body = document.getElementById('drawerBody');
  body.innerHTML = '<div style="color:#bbb">正在加载正文…</div>';

  let html = '';
  if (contentPath) {
    try {
      const resp = await fetch(`${CONTENT_API_URL}?path=${encodeURIComponent(contentPath)}`, { cache: 'no-store' });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const payload = await resp.json();
      const md = preprocessArticleMarkdown(payload.markdown || '', title);
      html = marked.parse(md).replace(/<img\s/gi, '<img referrerpolicy="no-referrer" ');
    } catch (e) {
      html = `<div style="color:#991b1b">无法加载正文：${escapeHtml(e.message)}</div>
              <div style="margin-top:8px;font-size:12px;color:#999">路径：${escapeHtml(contentPath)}</div>`;
    }
  } else {
    html = hasRealUrl(url)
      ? `<div style="color:#bbb">本文未找到本地正文文件。</div>
         <div style="margin-top:8px;font-size:12px;color:#999">原文链接：<a href="${escapeHtml(url)}" target="_blank" rel="noopener">${escapeHtml(url)}</a></div>`
      : `<div style="color:#bbb">当前只有榜单快照，尚未抓取正文和原文链接。</div>
         <div style="margin-top:8px;font-size:12px;color:#999">如果这个词后续升级为全文模式，抽屉会自动显示正文内容。</div>`;
  }
  body.innerHTML = html;

  const footParts = [];
  if (meta.read_count != null) footParts.push(`👁 阅读${meta.read_count}`);
  if (meta.like_count != null) footParts.push(`👍 赞${meta.like_count}`);
  if (meta.kw) footParts.push(escapeHtml(meta.kw));
  if (meta.rank) footParts.push(`第${meta.rank}名`);
  if (hasRealUrl(url)) footParts.push(`<a href="${escapeHtml(url)}" target="_blank" rel="noopener" style="color:#3b82f6">原文</a>`);
  footParts.push(`<a href="${escapeHtml(articleHitDetailHref(meta, url))}" style="color:#3b82f6">命中详情</a>`);
  document.getElementById('drawerFoot').innerHTML = footParts.map(p => `<span>${p}</span>`).join('');
}

function closeDrawer() {
  document.getElementById('drawer').classList.remove('open');
}

async function toggleKeywordPin(event, keywordId, keyword, nextPinned) {
  if (event) event.stopPropagation();
  try {
    const endpoint = `${KEYWORD_API_BASE}/${encodeURIComponent(keywordId)}/${nextPinned ? 'pin' : 'unpin'}`;
    const resp = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ keyword })
    });
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(text || `HTTP ${resp.status}`);
    }
    await loadData({ preserveSelection: true });
    refresh();
  } catch (e) {
    window.alert(`置顶状态更新失败：${e.message}`);
  }
}

async function saveKeywordTopic(keywordId, keyword) {
  const input = document.getElementById(keywordTopicInputId(keywordId));
  const topic = input ? input.value.trim() : '';
  try {
    const endpoint = `${KEYWORD_API_BASE}/${encodeURIComponent(keywordId)}/topic`;
    const resp = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ keyword, topic })
    });
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(text || `HTTP ${resp.status}`);
    }
    await loadData({ preserveSelection: true });
    refresh();
  } catch (e) {
    window.alert(`topic 更新失败：${e.message}`);
  }
}

async function resetKeywordTopic(keywordId, keyword) {
  const input = document.getElementById(keywordTopicInputId(keywordId));
  if (input) input.value = '';
  await saveKeywordTopic(keywordId, keyword);
}

async function saveKeywordBucket(keywordId, keyword) {
  const input = document.getElementById(keywordBucketInputId(keywordId));
  const keyword_bucket = input ? input.value.trim() : '';
  try {
    const endpoint = `${KEYWORD_API_BASE}/${encodeURIComponent(keywordId)}/bucket`;
    const resp = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ keyword, keyword_bucket })
    });
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(text || `HTTP ${resp.status}`);
    }
    await loadData({ preserveSelection: true });
    refresh();
  } catch (e) {
    window.alert(`类目更新失败：${e.message}`);
  }
}

async function resetKeywordBucket(keywordId, keyword) {
  const input = document.getElementById(keywordBucketInputId(keywordId));
  if (input) input.value = '未分类';
  await saveKeywordBucket(keywordId, keyword);
}

// ── 移动端侧栏抽屉 ───────────────────────────────────────
function isMobile() {
  return window.innerWidth <= 768;
}

function isSidebarOpen() {
  return document.querySelector('.col-left')?.classList.contains('mobile-open');
}

function toggleMobileSidebar() {
  if (isSidebarOpen()) { closeMobileSidebar(); }
  else { openMobileSidebar(); }
}

function openMobileSidebar() {
  const col = document.querySelector('.col-left');
  const overlay = document.querySelector('.mobile-overlay');
  if (col) col.classList.add('mobile-open');
  if (overlay) overlay.classList.add('show');
}

function closeMobileSidebar() {
  const col = document.querySelector('.col-left');
  const overlay = document.querySelector('.mobile-overlay');
  if (col) col.classList.remove('mobile-open');
  if (overlay) overlay.classList.remove('show');
}

function collapseLeftOnMobile() {
  if (!isMobile()) return;
  closeMobileSidebar();
}

// ── 关键词管理（第三视角） ────────────────────────────────
const KM_API = '/api/keyword-manage';
const REFRESH_ALL_STATUS_URL = '/api/refresh-all/status';
const REFRESH_ALL_LAUNCH_URL = '/api/refresh-all';
const REFRESH_ALL_CANCEL_URL = '/api/refresh-all/cancel';
const KM_REFRESH_BATCH_STORAGE_KEY = 'km-refresh-batch-state-v2';
const KM_REFRESH_LEGACY_STORAGE_KEY = 'km-refresh-demo-state-v1';
const KM_REFRESH_DONE_HOLD_MS = 8000;
const KM_REFRESH_POLL_MS = 3000;
let KM_DATA = null;
let kmLoading = false;
let kmLoaded = false;
let kmFilter = '';
let kmEditGroupId = null;
let kmActiveKeywordId = null;
let kmActiveSettingsGroupId = null;
const kmCollapsedGroups = new Set();
let kmRefreshInlineTimer = null;
let kmCancelBatchPending = false;
let kmActiveBatchId = null;
let kmActiveBatchSnapshot = null;
let kmActiveBatchFinishedAt = 0;
let kmLastProcessedCount = -1;
const kmRefreshedKeywords = new Set();

function kmShowToast(msg, ok = true) {
  const el = document.getElementById('kmToast');
  if (!el) return;
  el.textContent = msg;
  el.style.background = ok ? '#111' : '#dc2626';
  el.classList.add('show');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('show'), 2200);
}

function kmClearLegacyDemoState() {
  try {
    localStorage.removeItem(KM_REFRESH_LEGACY_STORAGE_KEY);
  } catch (_) {}
}

function kmReadBatchState() {
  try {
    const raw = localStorage.getItem(KM_REFRESH_BATCH_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return null;
    return {
      batch_id: parsed.batch_id || null,
      snapshot: parsed.snapshot || null,
      finished_at: Number(parsed.finished_at || 0),
    };
  } catch (_) {
    return null;
  }
}

function kmWriteBatchState(batchId, snapshot, finishedAt = 0) {
  try {
    localStorage.setItem(KM_REFRESH_BATCH_STORAGE_KEY, JSON.stringify({
      batch_id: batchId,
      snapshot,
      finished_at: finishedAt,
    }));
  } catch (_) {}
}

function kmClearBatchState() {
  try {
    localStorage.removeItem(KM_REFRESH_BATCH_STORAGE_KEY);
  } catch (_) {}
  kmActiveBatchId = null;
  kmActiveBatchSnapshot = null;
  kmActiveBatchFinishedAt = 0;
  kmLastProcessedCount = -1;
  kmCancelBatchPending = false;
  kmRefreshedKeywords.clear();
}

function kmAdoptBatchSnapshot(snapshot, { persist = true, finishedAt = null } = {}) {
  if (!snapshot) return;
  const batchId = snapshot.batch_id || kmActiveBatchId;
  if (batchId) kmActiveBatchId = batchId;
  kmActiveBatchSnapshot = snapshot;
  const isFinished = !!snapshot.is_finished;
  if (isFinished) {
    if (finishedAt != null) {
      kmActiveBatchFinishedAt = finishedAt;
    } else if (!kmActiveBatchFinishedAt) {
      kmActiveBatchFinishedAt = Date.now();
    }
  } else {
    kmActiveBatchFinishedAt = 0;
  }
  if (persist) {
    kmWriteBatchState(kmActiveBatchId, snapshot, kmActiveBatchFinishedAt);
  }
}

function kmBuildRefreshSnapshot(now = Date.now()) {
  if ((!kmActiveBatchId || !kmActiveBatchSnapshot) && typeof localStorage !== 'undefined') {
    const persisted = kmReadBatchState();
    if (persisted && persisted.batch_id && persisted.snapshot) {
      kmActiveBatchId = persisted.batch_id;
      kmActiveBatchSnapshot = persisted.snapshot;
      kmActiveBatchFinishedAt = persisted.finished_at || 0;
    }
  }

  if (!kmActiveBatchId || !kmActiveBatchSnapshot) {
    return { phase: 'idle' };
  }

  const s = kmActiveBatchSnapshot;
  const total = Number(s.total || 0);
  const success = Number(s.success_count || 0);
  const failed = Number(s.failed_count || 0);
  const processed = Number(
    s.processed_count != null ? s.processed_count : (success + failed)
  );
  const bounded = total ? Math.min(processed, total) : processed;
  const percent = total ? Math.max(2, Math.round((bounded / total) * 100)) : 4;
  const isActive = !!s.is_active;
  const isFinished = !!s.is_finished;
  const status = s.status || (isActive ? 'running' : 'unknown');

  if (isActive) {
    return {
      phase: 'running',
      status,
      total,
      completed: bounded,
      currentKeyword: s.current_keyword || '',
      attempt: s.current_attempt || null,
      percent,
      success_count: success,
      failed_count: failed,
      pending_count: Number(s.pending_count || 0),
      cancel_requested: !!s.cancel_requested,
      batch_id: s.batch_id || kmActiveBatchId,
    };
  }

  if (isFinished) {
    return {
      phase: 'done',
      status,
      total,
      completed: total ? Math.min(bounded, total) : bounded,
      currentKeyword: s.current_keyword || '',
      percent: status === 'cancelled' ? percent : 100,
      success_count: success,
      failed_count: failed,
      pending_count: Number(s.pending_count || 0),
      cancel_requested: !!s.cancel_requested,
      cancel_reason: s.cancel_reason || '',
      batch_id: s.batch_id || kmActiveBatchId,
      finished_at: s.finished_at || null,
    };
  }

  return { phase: 'idle' };
}

function kmStartBatchPolling() {
  if (kmRefreshInlineTimer) return;
  kmRefreshInlineTimer = window.setInterval(kmPollBatchStatus, KM_REFRESH_POLL_MS);
}

function kmStopBatchPolling() {
  if (kmRefreshInlineTimer) {
    window.clearInterval(kmRefreshInlineTimer);
    kmRefreshInlineTimer = null;
  }
}

async function kmPollBatchStatus() {
  if (!kmActiveBatchId) {
    kmStopBatchPolling();
    return;
  }
  const url = `${REFRESH_ALL_STATUS_URL}?batch_id=${encodeURIComponent(kmActiveBatchId)}`;
  try {
    const resp = await fetch(url, { cache: 'no-store' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    if (!data || data.error) {
      if (data && data.error === 'batch not found') {
        kmClearBatchState();
        kmSyncInlineRefreshUi();
      }
      return;
    }
    const wasActive = kmActiveBatchSnapshot && kmActiveBatchSnapshot.is_active;
    const prevProcessed = kmLastProcessedCount;
    kmAdoptBatchSnapshot(data, { persist: true });
    if (data.cancel_requested || data.is_finished) {
      kmCancelBatchPending = false;
    }
    kmSyncInlineRefreshUi();

    // 逐词增量刷新：processed_count 增长时更新已刷新词集合并增量拉取数据
    const curProcessed = Number(data.processed_count || 0);
    if (data.completed_keywords && Array.isArray(data.completed_keywords)) {
      data.completed_keywords.forEach(kw => kmRefreshedKeywords.add(kw));
    }
    if (prevProcessed >= 0 && curProcessed > prevProcessed && !data.is_finished) {
      if (mode === 'keyword' || mode === 'account') {
        kmIncrementalListRefresh();
      }
      if (mode === 'articleList' && window.alRefresh) {
        window.alRefresh();
      }
    }
    kmLastProcessedCount = curProcessed;

    if (data.is_finished && wasActive) {
      kmInitRefreshHistoryBtn();
      setTimeout(() => {
        kmRefreshMonitorWorkbench();
        if (window.alRefresh) window.alRefresh();
      }, 1500);
    }
  } catch (e) {
    console.warn('refresh-all status poll failed', e);
  }
}

async function kmBootstrapBatchStatus() {
  kmClearLegacyDemoState();
  // 只查一次：当前有没有活跃批次
  // 有活跃批次 → 接管它（不管是本地记住的还是后端在跑的）
  // 没有活跃批次 → 清掉旧状态，不显示进度条
  // 逻辑：如果调度器后台已经跑完了新批次，说明用户已经不在页面上盯着了，
  //       旧批次的进度条没有意义，直接清掉。只有正在跑的才需要显示。
  try {
    const resp = await fetch(REFRESH_ALL_STATUS_URL, { cache: 'no-store' });
    if (!resp.ok) return;
    const data = await resp.json();
    if (data && data.batch_id && data.is_active) {
      kmAdoptBatchSnapshot(data, { persist: true });
      kmLastProcessedCount = Number(data.processed_count || 0);
      if (Array.isArray(data.completed_keywords)) {
        data.completed_keywords.forEach(kw => kmRefreshedKeywords.add(kw));
      }
    } else {
      // 没有活跃批次：如果本地记住的那个批次还没查过完成状态，查一次
      const persisted = kmReadBatchState();
      if (persisted && persisted.batch_id && persisted.snapshot && !persisted.snapshot.is_finished) {
        try {
          const batchResp = await fetch(
            `${REFRESH_ALL_STATUS_URL}?batch_id=${encodeURIComponent(persisted.batch_id)}`,
            { cache: 'no-store' }
          );
          if (batchResp.ok) {
            const batchData = await batchResp.json();
            if (batchData && !batchData.error && batchData.is_finished) {
              kmAdoptBatchSnapshot(batchData, { persist: true, finishedAt: Date.now() });
              kmLastProcessedCount = Number(batchData.processed_count || 0);
              if (Array.isArray(batchData.completed_keywords)) {
                batchData.completed_keywords.forEach(kw => kmRefreshedKeywords.add(kw));
              }
              return;
            }
          }
        } catch (_) {}
      }
      kmClearBatchState();
    }
  } catch (_) {}
}

function kmSyncInlineRefreshUi() {
  const slot = document.getElementById('keywordRefreshSlot');
  const trigger = document.getElementById('kwRefreshTrigger');
  const progress = document.getElementById('kwInlineProgress');
  const label = document.getElementById('kwInlineProgressLabel');
  const bar = document.getElementById('kwInlineProgressBar');
  const meta = document.getElementById('kwInlineProgressMeta');
  const cancelBtn = document.getElementById('kwProgressCancel');
  if (!slot || !trigger || !progress || !label || !bar || !meta || !cancelBtn) return;

  const showSlot = mode === 'keyword';
  slot.classList.toggle('active', showSlot);
  if (!showSlot) {
    kmStopBatchPolling();
    return;
  }

  const snapshot = kmBuildRefreshSnapshot();
  const isBusy = snapshot.phase === 'running' || snapshot.phase === 'done';
  const cancelRequested = !!snapshot.cancel_requested || kmCancelBatchPending;
  trigger.classList.toggle('hidden', isBusy);
  progress.classList.toggle('active', isBusy);
  progress.classList.toggle('is-done', snapshot.phase === 'done');
  progress.classList.toggle('is-cancelled', snapshot.status === 'cancelled');
  cancelBtn.classList.toggle('visible', snapshot.phase === 'running');
  cancelBtn.disabled = cancelRequested;
  cancelBtn.classList.toggle('is-pending', cancelRequested);
  cancelBtn.textContent = cancelRequested ? '停止中…' : '停止';

  if (!isBusy) {
    label.textContent = '准备中…';
    bar.style.width = '0%';
    meta.textContent = '0 / 0';
    kmStopBatchPolling();
    return;
  }

  if (snapshot.phase === 'done') {
    const failedText = snapshot.failed_count > 0 ? ` · 失败 ${snapshot.failed_count}` : '';
    const finishedText = snapshot.finished_at ? ` · ${kmFormatFinishedTime(snapshot.finished_at)}` : '';
    if (snapshot.status === 'failed') {
      label.textContent = `批量刷新失败${finishedText}`;
    } else if (snapshot.status === 'cancelled') {
      const reason = snapshot.cancel_reason ? `（${snapshot.cancel_reason}）` : '';
      label.textContent = `停止成功${reason} · 成功 ${snapshot.success_count}${failedText}${finishedText}`;
    } else if (snapshot.status === 'completed_with_failures') {
      label.textContent = `全部完成（含失败）· 成功 ${snapshot.success_count}${failedText}${finishedText}`;
    } else {
      label.textContent = `全部完成 · 成功 ${snapshot.success_count}${failedText}${finishedText}`;
    }
  } else {
    if (cancelRequested) {
      label.textContent = '停止中，等待当前关键词结束…';
    } else {
      const cur = snapshot.currentKeyword
        ? `正在刷新：${snapshot.currentKeyword}`
        : '正在准备下一个关键词…';
      const attempt = snapshot.attempt ? ` · 第 ${snapshot.attempt} 次尝试` : '';
      const stageLabel = snapshot.status === 'starting' ? '正在启动批量刷新…' : cur;
      label.textContent = stageLabel + (snapshot.status === 'starting' ? '' : attempt);
    }
  }
  bar.style.width = `${snapshot.percent}%`;
  meta.textContent = `${snapshot.completed} / ${snapshot.total}`;

  if (snapshot.phase === 'running') {
    kmStartBatchPolling();
  } else {
    kmStopBatchPolling();
  }
}

async function kmProgressCancel() {
  if (!kmActiveBatchId || kmCancelBatchPending) return;
  const snapshot = kmBuildRefreshSnapshot();
  if (snapshot.phase !== 'running') return;
  const modal = document.getElementById('kmCancelConfirmModal');
  if (modal) modal.classList.add('open');
}

function kmCloseCancelConfirm() {
  const modal = document.getElementById('kmCancelConfirmModal');
  if (modal) modal.classList.remove('open');
}

async function kmConfirmCancelAndClose() {
  const btn = document.getElementById('kmCancelConfirmBtn');
  if (btn) {
    btn.disabled = true;
    btn.textContent = '停止中…';
  }
  kmCloseCancelConfirm();
  if (!kmActiveBatchId || kmCancelBatchPending) {
    if (btn) {
      btn.disabled = false;
      btn.textContent = '确定停止';
    }
    return;
  }
  kmCancelBatchPending = true;
  kmSyncInlineRefreshUi();
  try {
    const data = await kmApiFetch(REFRESH_ALL_CANCEL_URL, {
      method: 'POST',
      body: JSON.stringify({ batch_id: kmActiveBatchId }),
    });
    if (data && data.batch) {
      const finishedAt = data.batch.is_finished ? Date.now() : 0;
      kmAdoptBatchSnapshot(data.batch, { persist: true, finishedAt });
    }
    kmCancelBatchPending = false;
    kmSyncInlineRefreshUi();
    kmShowToast(data.message || '停止信号已发送，当前关键词跑完后停止');
  } catch (e) {
    kmCancelBatchPending = false;
    kmSyncInlineRefreshUi();
    kmShowToast('停止失败：' + e.message, false);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = '确定停止';
    }
  }
}

// ── 刷新历史 ────────────────────────────────────────────
const REFRESH_HISTORY_URL = '/api/refresh-all/history';
let kmRefreshHistoryLoaded = false;

async function kmFetchRefreshHistory() {
  try {
    const r = await fetch(REFRESH_HISTORY_URL);
    const data = await r.json();
    if (!Array.isArray(data)) return [];
    return data;
  } catch (e) {
    console.warn('fetch refresh history failed', e);
    return [];
  }
}

function kmFormatBatchStatus(status) {
  if (status === 'completed') return { label: '成功', cls: 'ok' };
  if (status === 'completed_with_failures') return { label: '部分失败', cls: 'warn' };
  if (status === 'cancelled') return { label: '已停止', cls: 'warn' };
  if (status === 'failed') return { label: '失败', cls: 'fail' };
  if (status === 'running') return { label: '进行中', cls: 'running' };
  return { label: status || '未知', cls: 'unknown' };
}

function kmFormatHistoryTime(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  if (isNaN(d.getTime())) return ts;
  return `${String(d.getMonth() + 1).padStart(2, '0')}/${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

function kmFormatFinishedTime(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  if (isNaN(d.getTime())) return ts;
  return `${d.getMonth() + 1}月${d.getDate()}日 ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

function kmFormatDuration(seconds) {
  if (!seconds || seconds < 0) return '';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return `${h}h${m}m`;
  return `${m}m`;
}

function kmCalcDuration(startedAt, finishedAt) {
  if (!startedAt || !finishedAt) return '';
  const s = new Date(startedAt).getTime();
  const f = new Date(finishedAt).getTime();
  if (isNaN(s) || isNaN(f) || f < s) return '';
  return kmFormatDuration(Math.round((f - s) / 1000));
}

function kmCalcGap(prevFinishedAt, currStartedAt) {
  if (!prevFinishedAt || !currStartedAt) return '';
  const p = new Date(prevFinishedAt).getTime();
  const c = new Date(currStartedAt).getTime();
  if (isNaN(p) || isNaN(c) || c < p) return '';
  return kmFormatDuration(Math.round((c - p) / 1000));
}

function kmRenderRefreshHistory(history) {
  const list = document.getElementById('kmRefreshHistoryList');
  if (!list) return;
  if (!history.length) {
    list.innerHTML = '<div style="padding:24px;text-align:center;color:#999">暂无刷新记录</div>';
    return;
  }
  list.innerHTML = history.map((item, i) => {
    const st = kmFormatBatchStatus(item.status);
    const reasons = (item.failure_reasons || []);
    const failedKws = (item.failed_keywords || []);
    const MAX_FAILED_DISPLAY = 5;
    const failedKwsHtml = failedKws.length
      ? (() => {
          const shown = failedKws.slice(0, MAX_FAILED_DISPLAY);
          const remaining = failedKws.length - shown.length;
          let html = shown.map(f => `<div class="km-history-failed-kw">❌ ${escapeHtml(f.keyword)} — ${escapeHtml(f.reason)}，已跳过</div>`).join('');
          if (remaining > 0) {
            html += `<div class="km-history-failed-more">还有 ${remaining} 个关键词已跳过</div>`;
          }
          return `<div class="km-history-failed-keywords">${html}</div>`;
        })()
      : '';
    const reasonsHtml = reasons.length
      ? `<div class="km-history-reasons">${reasons.map(r => `<span class="km-history-reason">❌ ${escapeHtml(r)}</span>`).join('')}${failedKwsHtml}</div>`
      : '';
    const duration = kmCalcDuration(item.started_at, item.finished_at);
    const prev = history[i + 1];
    const gap = prev ? kmCalcGap(prev.finished_at, item.started_at) : '';
    const isManual = item.source !== 'scheduler';
    const sourceLabel = isManual ? '手动刷新' : '自动刷新';
    const metaParts = [];
    if (duration) metaParts.push(`耗时 ${duration}`);
    if (gap) metaParts.push(`距上次 ${gap}`);
    metaParts.push(sourceLabel);
    const metaHtml = `<div class="km-history-meta${isManual ? ' km-history-meta-manual' : ''}">${metaParts.join(' · ')}</div>`;
    return `<div class="km-history-row ${st.cls}">
      <div class="km-history-row-head">
        <span class="km-history-status km-history-status-${st.cls}">${st.label}</span>
        <span class="km-history-time">${kmFormatHistoryTime(item.started_at)}</span>
        <span class="km-history-counts">${item.success_count}/${item.total} 成功${item.failed_count ? ` · ${item.failed_count} 失败` : ''}</span>
      </div>
      ${metaHtml}
      ${reasonsHtml}
    </div>`;
  }).join('');
}

function kmUpdateHistoryBtnState(history) {
  const btn = document.getElementById('kwRefreshHistoryBtn');
  if (!btn) return;
  btn.classList.remove('has-failure', 'has-warn');
  const last = history && history[0];
  if (!last) return;
  if (last.status === 'failed') btn.classList.add('has-failure');
  else if (last.status === 'completed_with_failures') btn.classList.add('has-warn');
}

async function kmOpenRefreshHistory() {
  const modal = document.getElementById('kmRefreshHistoryModal');
  if (!modal) return;
  modal.classList.add('open');
  const list = document.getElementById('kmRefreshHistoryList');
  if (list) list.innerHTML = '<div style="padding:24px;text-align:center;color:#999">加载中…</div>';
  const history = await kmFetchRefreshHistory();
  kmRenderRefreshHistory(history);
}

function kmCloseRefreshHistory() {
  const modal = document.getElementById('kmRefreshHistoryModal');
  if (modal) modal.classList.remove('open');
}

async function kmInitRefreshHistoryBtn() {
  const history = await kmFetchRefreshHistory();
  kmUpdateHistoryBtnState(history);
  kmRefreshHistoryLoaded = true;
}

async function kmApiFetch(url, opts = {}) {
  const r = await fetch(url, { headers: { 'Content-Type': 'application/json' }, ...opts });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

async function kmEnsureDataLoaded(options = {}) {
  const silent = !!options.silent;
  if (kmLoading) return false;
  if (kmLoaded && KM_DATA) return true;
  kmLoading = true;
  try {
    KM_DATA = await kmApiFetch(KM_API);
    kmLoaded = true;
    return true;
  } catch (e) {
    if (!silent) {
      const wrap = document.getElementById('kmGroupsWrap');
      if (wrap) wrap.innerHTML = `<div class="km-loading" style="color:#991b1b">加载失败：${escapeHtml(e.message)}</div>`;
    }
    return false;
  } finally {
    kmLoading = false;
  }
}

async function loadKeywordManageView() {
  const ok = await kmEnsureDataLoaded();
  if (ok) renderKeywordManageView();
}

async function reloadKeywordManageData() {
  kmLoaded = false;
  const ok = await kmEnsureDataLoaded();
  if (ok) {
    renderKeywordManageView();
    if (document.getElementById('kmSettingsModal')?.classList.contains('open')) {
      kmRenderSettingsGroups();
    }
  }
}

async function kmRefreshMonitorWorkbench() {
  const ok = await loadData({ preserveSelection: true, skipManageReload: true });
  if (ok && mode !== 'keywordManage') refresh();
}

async function kmIncrementalListRefresh() {
  const ok = await loadData({ preserveSelection: true, skipManageReload: true });
  if (ok && mode !== 'keywordManage') renderList();
}

// ── 刷新全部关键词（确认选择 → 左侧栏内联进度） ─────
function kmBatchCheckboxes() {
  const list = document.getElementById('kmRefreshKeywordList');
  return list ? list.querySelectorAll('input[type="checkbox"]') : [];
}

function kmBatchSyncSelectedCount() {
  const summary = document.getElementById('kmRefreshSelectedCount');
  if (!summary) return;
  const boxes = Array.from(kmBatchCheckboxes());
  const total = boxes.length;
  let checked = 0;
  boxes.forEach(cb => { if (cb.checked) checked += 1; });
  summary.textContent = total > 0
    ? `已选 ${checked} / ${total}`
    : '加载中…';
}

function kmBatchApplyChecked(checked) {
  kmBatchCheckboxes().forEach(cb => { cb.checked = checked; });
  kmBatchSyncSelectedCount();
}

function kmBatchSelectAll() {
  kmBatchApplyChecked(true);
}

function kmBatchSelectNone() {
  kmBatchApplyChecked(false);
}

function kmBatchSelectInvert() {
  kmBatchCheckboxes().forEach(cb => { cb.checked = !cb.checked; });
  kmBatchSyncSelectedCount();
}

function kmProgressRestart() {
  kmClearBatchState();
  kmStopBatchPolling();
  kmSyncInlineRefreshUi();
  kmOpenRefreshModal();
}

function kmProgressAck() {
  kmClearBatchState();
  kmStopBatchPolling();
  kmSyncInlineRefreshUi();
  kmInitRefreshHistoryBtn();
}

async function kmOpenRefreshModal() {
  const ok = await kmEnsureDataLoaded({ silent: true });
  if (!ok || !KM_DATA) {
    kmShowToast('关键词列表加载失败', false);
    return;
  }
  const list = document.getElementById('kmRefreshKeywordList');
  if (!list) return;
  const groups = KM_DATA.groups || [];
  let totalRows = 0;
  const html = groups.map((group) => {
    const keywordsList = Array.from(group.keywords || []);
    const keywords = keywordsList.map(kw => {
      const refreshMeta = `每${Number(kw.refresh_frequency_days || 1)}天 · ${kw.last_refresh_at ? `上次${kmFormatRefreshAt(kw.last_refresh_at)}` : '从未刷新'}`;
      return `<div class="km-refresh-row" data-kid="${escapeHtml(kw.keyword_id)}">
        <label class="km-refresh-check">
          <input type="checkbox" value="${escapeHtml(kw.keyword_id)}" data-text="${escapeHtml(kw.keyword_text)}" checked />
          <span class="km-refresh-check-text">刷新</span>
        </label>
        <span class="km-refresh-text" title="${escapeHtml(kw.keyword_text)}">${escapeHtml(kw.keyword_text)}<small>${escapeHtml(refreshMeta)}</small></span>
        <button class="km-refresh-delete" data-kid="${escapeHtml(kw.keyword_id)}" data-text="${escapeHtml(kw.keyword_text)}" onclick="kmRefreshDeleteKeyword(this)" title="归档关键词">归档</button>
      </div>`;
    }).join('');
    totalRows += keywordsList.length;
    const addRow = `<div class="km-refresh-add-row">
      <input class="km-refresh-add-input" type="text" placeholder="+ 新增关键词" data-group="${escapeHtml(group.group_id)}" onkeydown="if(event.key==='Enter'){event.preventDefault();kmRefreshAddKeyword(this)}" />
      <button class="km-refresh-add-btn" onclick="kmRefreshAddKeyword(this.previousElementSibling)">添加</button>
    </div>`;
    return `<div class="km-refresh-group" data-group="${escapeHtml(group.group_id)}">
      <div class="km-refresh-group-header">
        <span class="km-refresh-group-label">${escapeHtml(group.label)}</span>
        <span class="km-refresh-group-count">${keywordsList.length} 词</span>
      </div>
      <div class="km-refresh-group-body">
        ${keywords || '<div class="km-refresh-empty">暂无关键词</div>'}
        ${addRow}
      </div>
    </div>`;
  }).join('');
  list.innerHTML = html;
  const renderedRows = list.querySelectorAll('.km-refresh-row').length;
  if (renderedRows !== totalRows) {
    console.warn(`[kmOpenRefreshModal] 渲染异常: 期望 ${totalRows} 行, 实际 ${renderedRows} 行, groups=${groups.length}`);
  }
  console.info(`[kmOpenRefreshModal] groups=${groups.length} totalRows=${totalRows} rendered=${renderedRows} boot=${window.__KM_BOOT_ID || '?'}`);
  list.onchange = (event) => {
    if (event.target && event.target.matches('input[type="checkbox"]')) {
      kmBatchSyncSelectedCount();
    }
  };
  document.getElementById('kmRefreshModal')?.classList.add('open');
  kmBatchSyncSelectedCount();
}

function kmCloseRefreshModal() {
  document.getElementById('kmRefreshModal')?.classList.remove('open');
}

async function kmConfirmRefresh() {
  const list = document.getElementById('kmRefreshKeywordList');
  if (!list) return;
  const checkboxes = list.querySelectorAll('input[type="checkbox"]:checked');
  const keywordIds = Array.from(checkboxes).map(cb => cb.value).filter(Boolean);
  if (!keywordIds.length) {
    kmShowToast('未选择任何关键词', false);
    return;
  }
  kmCloseRefreshModal();
  try {
    const resp = await fetch(REFRESH_ALL_LAUNCH_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ keyword_ids: keywordIds }),
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.status === 409 && data && data.batch) {
      kmCancelBatchPending = false;
      const finishedAt = data.batch.is_finished ? Date.now() : 0;
      kmAdoptBatchSnapshot(data.batch, { persist: true, finishedAt });
      kmSyncInlineRefreshUi();
      const batchState = data.batch.status || '';
      if (batchState === 'single_refresh_running') {
        const cur = data.batch.current_keyword || '某关键词';
        kmShowToast(`当前正在单词刷新「${cur}」，请等待完成后再启动批量刷新`);
      } else {
        kmShowToast('已有批量刷新在运行中');
      }
      return;
    }
    if (!resp.ok) {
      throw new Error(data.error || `HTTP ${resp.status}`);
    }
    kmCancelBatchPending = false;
    kmAdoptBatchSnapshot(data, { persist: true, finishedAt: 0 });
    kmSyncInlineRefreshUi();
    kmShowToast(`已启动批量刷新 · 共 ${keywordIds.length} 个关键词`);
  } catch (e) {
    kmCancelBatchPending = false;
    kmShowToast('启动失败：' + e.message, false);
  }
}

const KM_GROUP_COLORS = [
  { bg:'#eef2ff', border:'#c7d2fe', header:'#4f46e5' },
  { bg:'#fdf2f8', border:'#f9a8d4', header:'#db2777' },
  { bg:'#f0fdf4', border:'#86efac', header:'#16a34a' },
  { bg:'#fff7ed', border:'#fdba74', header:'#ea580c' },
  { bg:'#f0f9ff', border:'#7dd3fc', header:'#0284c7' },
  { bg:'#fefce8', border:'#fde047', header:'#ca8a04' },
  { bg:'#fdf4ff', border:'#e879f9', header:'#a21caf' },
  { bg:'#f0fdfa', border:'#5eead4', header:'#0d9488' },
];

function renderKeywordManageView() {
  if (!KM_DATA) return;
  kmRenderStats();
  kmRenderBatchAddGroups();
  const wrap = document.getElementById('kmGroupsWrap');
  if (!wrap) return;
  if (!KM_DATA.groups.length) {
    wrap.innerHTML = `<div class="km-empty-group">暂无分组，请先在刷新弹窗或接口里创建一个分组。</div>`;
    return;
  }
  wrap.innerHTML = KM_DATA.groups.map(group => kmRenderManageGroup(group)).join('');
}

function kmRenderBatchAddGroups() {
  const select = document.getElementById('kmBatchAddGroup');
  if (!select || !KM_DATA) return;
  const groups = KM_DATA.groups || [];
  select.innerHTML = groups.map(group => `
    <option value="${escapeHtml(group.group_id)}">${escapeHtml(group.label || '未命名分组')}</option>
  `).join('');
}

function kmRenderManageGroup(group) {
  const keywords = group.keywords || [];
  const rows = keywords.length
    ? keywords.map(kw => kmRenderManageKeywordRow(group, kw)).join('')
    : `<div class="km-empty-group compact">这个分组还没有关键词，可以先在上方批量添加。</div>`;
  return `<section class="km-manage-group" id="km-main-group-${escapeHtml(group.group_id)}">
    <div class="km-manage-group-head">
      <div>
        <div class="km-manage-group-title">${escapeHtml(group.label || '未命名分组')}</div>
        <div class="km-manage-group-meta">${keywords.length} 个词${group.ranked_count ? ` · ${group.ranked_count} 个最近有排名` : ''}</div>
      </div>
      <button class="km-btn km-btn-ghost km-btn-sm" data-label="${escapeHtml(group.label || '')}" onclick="kmOpenRenameGroupModal('${escapeHtml(group.group_id)}', this.dataset.label)">改分组名</button>
    </div>
    <div class="km-manage-grid">
      ${rows}
    </div>
  </section>`;
}

function kmFormatRefreshAt(value) {
  if (!value) return '从未刷新';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value).replace('T', ' ');
  return parsed.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

function kmRefreshCadenceText(kw, { spaced = false } = {}) {
  const source = kw?.refresh_frequency_source || 'auto';
  const effectiveHours = Number(kw?.effective_refresh_interval_hours);
  if (
    source !== 'manual'
    && kw?.lifecycle_stage === 'observing'
    && Number.isFinite(effectiveHours)
    && effectiveHours === 3
  ) {
    return '观察期每3小时';
  }
  const days = Number(kw?.refresh_frequency_days || 1);
  return spaced ? `每 ${days} 天` : `每${days}天`;
}

function kmRefreshSummary(kw) {
  const source = kw.refresh_frequency_source === 'manual' ? '人工' : '自动';
  const last = kw.last_refresh_at
    ? `上次 ${kmFormatRefreshAt(kw.last_refresh_at)}`
    : '从未刷新';
  const next = kw.next_refresh_at
    ? `下次 ${kmFormatRefreshAt(kw.next_refresh_at)}`
    : '待首次刷新';
  const status = kw.last_refresh_status === 'failed'
    ? ' · 上次失败，自动延后重试'
    : '';
  const cadenceText = kmRefreshCadenceText(kw);
  const cadence = source === '自动'
    ? (cadenceText.startsWith('观察期') ? cadenceText : `自动${cadenceText}`)
    : `人工${cadenceText}`;
  return `${cadence} · ${last} · ${next}${status}`;
}

function kmRenderManageKeywordRow(group, kw) {
  return `<article class="km-manage-row" id="kmrow-${escapeHtml(kw.keyword_id)}" data-kid="${escapeHtml(kw.keyword_id)}" data-text="${escapeHtml(kw.keyword_text)}" title="${escapeHtml(kw.keyword_text)}" onclick="kmOpenKeywordModal('${escapeHtml(kw.keyword_id)}')" onkeydown="kmHandleKeywordCardKey(event, '${escapeHtml(kw.keyword_id)}')" role="button" tabindex="0">
    <span class="km-manage-keyword-text">${escapeHtml(kw.keyword_text)}</span>
  </article>`;
}

function kmParseBatchAddInput() {
  const input = document.getElementById('kmBatchAddInput');
  if (!input) return [];
  const seen = new Set();
  return input.value
    .split(/\r?\n|,|，|;|；/)
    .map(text => text.trim())
    .filter(text => {
      if (!text || seen.has(text)) return false;
      seen.add(text);
      return true;
    });
}

function kmExistingKeywordTexts() {
  const texts = new Set();
  (KM_DATA?.groups || []).forEach(group => {
    (group.keywords || []).forEach(kw => texts.add(String(kw.keyword_text || '').trim()));
  });
  return texts;
}

async function kmBatchAddKeywords(andRefresh = false) {
  if (!KM_DATA) return;
  const groupSelect = document.getElementById('kmBatchAddGroup');
  const input = document.getElementById('kmBatchAddInput');
  const groupId = groupSelect?.value || (KM_DATA.groups?.[0]?.group_id || '');
  if (!groupId) {
    kmShowToast('请先选择分组', false);
    return;
  }
  const existing = kmExistingKeywordTexts();
  const texts = kmParseBatchAddInput().filter(text => !existing.has(text));
  if (!texts.length) {
    kmShowToast('没有可添加的新关键词', false);
    return;
  }

  const created = [];
  const failed = [];
  for (const text of texts) {
    try {
      const kw = await kmApiFetch(`${KM_API}/keywords`, {
        method: 'POST',
        body: JSON.stringify({ group_id: groupId, keyword_text: text }),
      });
      created.push(kw);
      existing.add(text);
    } catch (e) {
      failed.push(`${text}：${e.message}`);
    }
  }

  if (created.length) {
    if (input) input.value = '';
    await reloadKeywordManageData();
    await kmRefreshMonitorWorkbench();
  }

  if (andRefresh && created.length) {
    for (const kw of created) {
      await startKeywordRefresh(null, kw.keyword_id, kw.keyword_text);
    }
  }

  if (failed.length) {
    kmShowToast(`已添加 ${created.length} 个，${failed.length} 个失败`, false);
    console.warn('[kmBatchAddKeywords] failed', failed);
    return;
  }
  kmShowToast(andRefresh ? `已添加并排队刷新 ${created.length} 个关键词` : `已添加 ${created.length} 个关键词`);
}

function kmAskDeleteKeywordInline(btn) {
  const kid = btn.dataset.kid;
  const text = btn.dataset.text || '';
  const box = document.getElementById('kmdelete-' + kid);
  if (!box) return;
  box.innerHTML = `
    <span class="km-delete-confirm-text">确定归档？</span>
    <button class="km-row-danger" data-kid="${escapeHtml(kid)}" data-text="${escapeHtml(text)}" onclick="kmDeleteKeyword(this, this.dataset.text)">是</button>
    <button class="km-btn km-btn-ghost km-btn-sm" onclick="renderKeywordManageView()">否</button>
  `;
}

function kmFindGroup(groupId) {
  return (KM_DATA?.groups || []).find(group => group.group_id === groupId) || null;
}

function kmOpenGroupSettings(groupId) {
  kmActiveSettingsGroupId = groupId;
  kmOpenSettingsModal(groupId);
}

function kmRenderSettingsGroups() {
  const wrap = document.getElementById('kmSettingsGroupsWrap');
  if (!wrap || !KM_DATA) return;
  const group = kmFindGroup(kmActiveSettingsGroupId);
  if (!group) {
    wrap.innerHTML = `<div style="text-align:center;padding:32px;color:#bbb;">这个分组不存在或已删除</div>`;
    return;
  }
  const title = document.getElementById('kmSettingsModalTitle');
  if (title) title.textContent = `${group.label} · 设置`;
  const desc = document.getElementById('kmSettingsModalDesc');
  if (desc) desc.textContent = '只管理当前分类：添加关键词、归档关键词、改名或删除空分组。';
  wrap.innerHTML = kmRenderSingleGroupSettings(group);
}

function kmRenderSingleGroupSettings(group) {
  const keywords = group.keywords || [];
  const stats = [
    `<span class="km-group-meta-chip">${group.total} 个词</span>`,
  ];
  if (group.ranked_count) stats.push(`<span class="km-group-meta-chip ranked">${group.ranked_count} 有排名</span>`);
  const rows = keywords.length
    ? keywords.map(kw => `
      <div class="km-settings-keyword-row" id="kmsetkwr-${kw.keyword_id}" data-text="${escapeHtml(kw.keyword_text)}">
        <div class="km-settings-keyword-main" onclick="kmOpenKeywordModal('${kw.keyword_id}')">
          <div class="km-settings-keyword-text">${escapeHtml(kw.keyword_text)}</div>
          <div class="km-settings-keyword-sub">
            ${kw.today_best ? `最新第${kw.today_best}` : '最新未命中'} · ${kw.coverage_days || 0} 天在榜 · ${kw.article_count || 0} 篇文章
          </div>
        </div>
        <button class="km-row-danger" data-kid="${kw.keyword_id}" data-text="${escapeHtml(kw.keyword_text)}" onclick="kmDeleteKeyword(this, this.dataset.text)" title="归档关键词">归档</button>
      </div>`).join('')
    : `<div class="km-empty-group">这个分组还没有关键词。</div>`;
  return `<div class="km-single-settings-card" id="kmgc-${group.group_id}">
    <div class="km-single-settings-top">
      <div class="km-single-settings-meta">${stats.join('')}</div>
      <div class="km-single-settings-actions">
        <button class="km-btn km-btn-ghost km-btn-sm" data-label="${escapeHtml(group.label)}" onclick="kmOpenRenameGroupModal('${group.group_id}', this.dataset.label)">改名</button>
        <button class="km-btn km-btn-danger km-btn-sm" data-label="${escapeHtml(group.label)}" onclick="kmDeleteGroup('${group.group_id}', this.dataset.label)">删组</button>
      </div>
    </div>
    <div class="km-single-add-row">
      <input class="km-add-kw-input" placeholder="输入关键词，回车添加…" data-group="${group.group_id}" onkeydown="kmHandleAddKw(event, this)" />
      <button class="km-btn km-btn-primary km-btn-sm" onclick="kmAddKeyword(this.previousElementSibling)">添加</button>
    </div>
    <div class="km-settings-keyword-list">${rows}</div>
  </div>`;
}

function kmRenderStats() {
  const stats = document.getElementById('kmStats');
  if (!stats || !KM_DATA) return;
  const parts = [
    `<span class="km-stat-chip">词库 ${KM_DATA.total}</span>`,
  ];
  if (KM_DATA.ranked_total > 0) parts.push(`<span class="km-stat-chip ranked">最新快照有排名 ${KM_DATA.ranked_total}</span>`);
  if (KM_DATA.not_ranked_total > 0) parts.push(`<span class="km-stat-chip">最新快照未命中 ${KM_DATA.not_ranked_total}</span>`);
  stats.innerHTML = parts.join('');
}

function kmGroupMetaHtml(group) {
  const meta = [
    `<span class="km-group-meta-chip">${group.total} 个词</span>`
  ];
  if (group.ranked_count) meta.push(`<span class="km-group-meta-chip ranked">${group.ranked_count} 有排名</span>`);
  return meta.join('');
}

function kmRenderGroup(group) {
  const body = group.keywords.length
    ? `<div class="km-group-grid" id="kmgrid-${group.group_id}">${group.keywords.map(kw => kmRenderKeyword(kw)).join('')}</div>`
    : `<div class="km-empty-group">这个分组还没有词，先在上面输入后添加。</div>`;
  const collapsedClass = kmCollapsedGroups.has(group.group_id) ? 'collapsed' : '';
  const toggleClass = kmCollapsedGroups.has(group.group_id) ? '' : 'open';
  return `<div class="km-group-card" id="kmgc-${group.group_id}">
    <div class="km-group-header" onclick="kmToggleGroup('${group.group_id}')">
      <span class="km-group-toggle ${toggleClass}" id="kmgt-${group.group_id}">▶</span>
      <div class="km-group-title-wrap">
        <span class="km-group-label" id="kmgl-${group.group_id}">${escapeHtml(group.label)}</span>
        <span class="km-group-meta" id="kmgm-${group.group_id}">${kmGroupMetaHtml(group)}</span>
      </div>
      <div class="km-group-actions" onclick="event.stopPropagation()">
        <button class="km-btn km-btn-ghost km-btn-sm" onclick="kmOpenRenameGroupModal('${group.group_id}', ${jsq(group.label)})">改名</button>
        <button class="km-btn km-btn-danger km-btn-sm" onclick="kmDeleteGroup('${group.group_id}', ${jsq(group.label)})">删组</button>
      </div>
    </div>
    <div class="km-group-body ${collapsedClass}" id="kmgb-${group.group_id}" data-label="${escapeHtml(group.label)}">
      <div class="km-group-tools">
        <div class="km-add-kw-row">
        <input class="km-add-kw-input" placeholder="输入关键词，回车添加…" data-group="${group.group_id}" onkeydown="kmHandleAddKw(event, this)" />
        <button class="km-btn km-btn-primary km-btn-sm km-add-kw-btn" onclick="kmAddKeyword(this.previousElementSibling)">添加</button>
      </div>
      </div>
      ${body}
    </div>
  </div>`;
}

function kmKeywordFactsHtml(kw) {
  const facts = [];
  if (kw.coverage_days > 0) facts.push(`<span class="km-kw-fact">在榜 ${kw.coverage_days} 天</span>`);
  if (kw.tracked_accounts > 0) facts.push(`<span class="km-kw-fact">${kw.tracked_accounts} 账号</span>`);
  if (kw.article_count > 0) facts.push(`<span class="km-kw-fact">${kw.article_count} 文章</span>`);
  if (!facts.length) facts.push(`<span class="km-kw-fact muted">暂无历史沉淀</span>`);
  return facts.join('');
}

function kmRefreshStateHtml(kw) {
  const source = kw.refresh_frequency_source === 'manual' ? '人工锁定' : '自动分档';
  const last = kw.last_refresh_at
    ? `上次刷新：${kmFormatRefreshAt(kw.last_refresh_at)}`
    : '上次刷新：从未刷新';
  const next = kw.next_refresh_at
    ? `下次自动刷新：${kmFormatRefreshAt(kw.next_refresh_at)}`
    : '下次自动刷新：待首次刷新';
  const result = kw.last_refresh_status === 'failed'
    ? ' · 最近一次失败，自动任务会延后重试'
    : '';
  const cadence = kmRefreshCadenceText(kw, { spaced: true });
  return `<span>${source} · ${escapeHtml(cadence)}</span><span>${escapeHtml(last)}</span><span>${escapeHtml(next)}${escapeHtml(result)}</span>`;
}

async function kmSaveRefreshPolicy(select) {
  const keywordId = kmActiveKeywordId;
  const found = keywordId ? kmFindKeyword(keywordId) : null;
  if (!found) return;
  const value = select?.value || 'auto';
  const payload = value === 'auto'
    ? { source: 'auto' }
    : { source: 'manual', refresh_frequency_days: Number(value) };
  try {
    const updated = await kmApiFetch(
      `${KM_API}/keywords/${encodeURIComponent(keywordId)}/refresh-policy`,
      { method: 'PATCH', body: JSON.stringify(payload) },
    );
    Object.assign(found.keyword, updated);
    kmSyncKeywordModal(found.keyword);
    renderKeywordManageView();
    kmShowToast(
      value === 'auto'
        ? `「${found.keyword.keyword_text}」已恢复自动分档`
        : `「${found.keyword.keyword_text}」已设为每 ${value} 天刷新`,
    );
  } catch (error) {
    kmShowToast(`刷新周期保存失败：${error.message}`, false);
    kmSyncKeywordModal(found.keyword);
  }
}

function kmRenderKeyword(kw, color) {
  const style = color ? `style="background:${color.bg};border-color:${color.border};color:${color.text};"` : '';
  return `<div class="km-kw-card" id="kmkwr-${kw.keyword_id}" data-text="${escapeHtml(kw.keyword_text)}" onclick="kmOpenKeywordModal('${kw.keyword_id}')" onkeydown="kmHandleKeywordCardKey(event, '${kw.keyword_id}')" role="button" tabindex="0" ${style}>
    <div class="km-kw-text">${escapeHtml(kw.keyword_text)}</div>
  </div>`;
}

function kmHandleKeywordCardKey(event, keywordId) {
  if (event.key !== 'Enter' && event.key !== ' ') return;
  event.preventDefault();
  kmOpenKeywordModal(keywordId);
}

function kmFindKeyword(keywordId) {
  if (!KM_DATA) return null;
  for (const group of KM_DATA.groups || []) {
    const keyword = (group.keywords || []).find(item => item.keyword_id === keywordId);
    if (keyword) return { group, keyword };
  }
  return null;
}

function kmSyncKeywordModal(keyword) {
  const modal = document.getElementById('kmKeywordModal');
  if (!modal) return;
  const title = document.getElementById('kmKeywordModalTitle');
  if (title) title.textContent = keyword.keyword_text;
  const nameInput = document.getElementById('kmKeywordModalName');
  if (nameInput) {
    nameInput.value = keyword.keyword_text;
    nameInput.dataset.kid = keyword.keyword_id;
    nameInput.dataset.lastSaved = keyword.keyword_text;
  }
  document.getElementById('kmKeywordModalMeta').innerHTML = [
    keyword.today_best
      ? `<span class="km-kw-seo ranked">最新快照第${keyword.today_best}</span>`
      : `<span class="km-kw-seo not-ranked">最新快照未命中</span>`
  ].join('');
  document.getElementById('kmKeywordModalFacts').innerHTML = kmKeywordFactsHtml(keyword);
  document.getElementById('kmKeywordRefreshState').innerHTML = kmRefreshStateHtml(keyword);
  const refreshPolicy = document.getElementById('kmKeywordRefreshPolicy');
  if (refreshPolicy) {
    refreshPolicy.value = keyword.refresh_frequency_source === 'manual'
      ? String(keyword.refresh_frequency_days || 1)
      : 'auto';
  }
  const refreshReason = document.getElementById('kmKeywordRefreshReason');
  if (refreshReason) {
    refreshReason.textContent = keyword.refresh_policy_reason || '自动策略将在下一次调度前重新计算';
  }
  const refreshButton = document.getElementById('kmKeywordModalRefresh');
  if (refreshButton) {
    refreshButton.dataset.kid = keyword.keyword_id;
    refreshButton.dataset.text = keyword.keyword_text;
  }
  const noteInput = document.getElementById('kmKeywordModalNote');
  noteInput.value = keyword.note || '';
  noteInput.dataset.kid = keyword.keyword_id;
  noteInput.dataset.lastSaved = keyword.note || '';
  noteInput.dataset.dirty = 'false';
  const status = document.getElementById('kmKeywordModalStatus');
  status.className = 'km-note-status';
  status.textContent = '';
  const del = document.getElementById('kmKeywordModalDelete');
  del.dataset.kid = keyword.keyword_id;
  del.dataset.text = keyword.keyword_text;
}

function kmOpenKeywordModal(keywordId) {
  const found = kmFindKeyword(keywordId);
  if (!found) return;
  kmActiveKeywordId = keywordId;
  document.getElementById('kmKeywordModal').classList.add('open');
  kmSyncKeywordModal(found.keyword);
}

function kmCloseKeywordModal() {
  kmActiveKeywordId = null;
  document.getElementById('kmKeywordModal').classList.remove('open');
}

function kmRefreshGroupMeta(groupId) {
  if (!KM_DATA) return;
  const group = (KM_DATA.groups || []).find(item => item.group_id === groupId);
  if (!group) return;
  const meta = document.getElementById('kmgm-' + groupId);
  if (meta) meta.innerHTML = kmGroupMetaHtml(group);
}

function kmToggleGroup(groupId) {
  const body = document.getElementById('kmgb-' + groupId);
  if (!body) return;
  const collapsed = body.classList.toggle('collapsed');
  if (collapsed) kmCollapsedGroups.add(groupId);
  else kmCollapsedGroups.delete(groupId);
  const toggle = document.getElementById('kmgt-' + groupId);
  if (toggle) toggle.classList.toggle('open', !collapsed);
}

async function kmOpenSettingsModal(groupId = null) {
  if (groupId) kmActiveSettingsGroupId = groupId;
  document.getElementById('kmSettingsModal')?.classList.add('open');
  if (!kmLoaded || !KM_DATA) {
    const ok = await kmEnsureDataLoaded();
    if (!ok) return;
    renderKeywordManageView();
  }
  kmRenderSettingsGroups();
  setTimeout(() => document.querySelector('#kmSettingsGroupsWrap .km-add-kw-input')?.focus(), 50);
}

function kmCloseSettingsModal() {
  document.getElementById('kmSettingsModal')?.classList.remove('open');
}

function kmApplySearch(q) {
  kmFilter = q || '';
  const s = kmFilter.trim().toLowerCase();
  document.querySelectorAll('#kmSettingsGroupsWrap .km-group-card').forEach(card => {
    const body = card.querySelector('.km-group-body');
    const label = (body?.dataset.label || '').toLowerCase();
    const cards = Array.from(card.querySelectorAll('.km-kw-card[data-text]'));
    let visibleCount = 0;
    const groupMatched = !!s && label.includes(s);
    cards.forEach(item => {
      const text = (item.dataset.text || '').toLowerCase();
      const matched = !s || groupMatched || text.includes(s);
      item.classList.toggle('hidden', !matched);
      if (matched) visibleCount += 1;
    });
    card.classList.toggle('hidden', !!s && visibleCount === 0 && !groupMatched);
  });
}

function kmMarkNoteDirty(input) {
  input.dataset.dirty = 'true';
  const status = input.id === 'kmKeywordModalNote'
    ? document.getElementById('kmKeywordModalStatus')
    : document.getElementById('kmnote-status-' + input.dataset.kid);
  if (status) {
    status.className = 'km-note-status';
    status.textContent = '待保存';
  }
}

async function kmSaveNote(input) {
  const kid = input.dataset.kid;
  const note = input.value.trim();
  if (input.dataset.saving === 'true') return;
  if (input.dataset.lastSaved === note && input.dataset.dirty !== 'true') return;
  input.dataset.saving = 'true';
  try {
    await kmApiFetch(`${KM_API}/keywords/${encodeURIComponent(kid)}`, {
      method: 'PATCH',
      body: JSON.stringify({ note }),
    });
    input.dataset.lastSaved = note;
    input.dataset.dirty = 'false';
    if (KM_DATA) {
      for (const group of KM_DATA.groups || []) {
        const item = (group.keywords || []).find(keyword => keyword.keyword_id === kid);
        if (item) {
          item.note = note;
          if (kmActiveKeywordId === kid) kmSyncKeywordModal(item);
        }
      }
    }
    const status = input.id === 'kmKeywordModalNote'
      ? document.getElementById('kmKeywordModalStatus')
      : document.getElementById('kmnote-status-' + kid);
    if (status) {
      status.className = 'km-note-status saved';
      status.textContent = '已保存';
    }
  } catch (e) {
    const status = input.id === 'kmKeywordModalNote'
      ? document.getElementById('kmKeywordModalStatus')
      : document.getElementById('kmnote-status-' + kid);
    if (status) {
      status.className = 'km-note-status error';
      status.textContent = '保存失败';
    }
    kmShowToast('备注保存失败：' + e.message, false);
  } finally {
    input.dataset.saving = 'false';
  }
}

async function kmSaveKeywordText(input) {
  const kid = input.dataset.kid;
  const text = (input.value || '').trim();
  if (!text) { input.value = input.dataset.lastSaved || ''; return; }
  if (text === input.dataset.lastSaved) return;
  try {
    const updated = await kmApiFetch(`${KM_API}/keywords/${encodeURIComponent(kid)}`, {
      method: 'PATCH',
      body: JSON.stringify({ keyword_text: text }),
    });
    input.dataset.lastSaved = updated.keyword_text;
    if (KM_DATA) {
      for (const group of KM_DATA.groups || []) {
        const item = (group.keywords || []).find(kw => kw.keyword_id === kid);
        if (item) {
          item.keyword_text = updated.keyword_text;
          if (kmActiveKeywordId === kid) kmSyncKeywordModal(item);
        }
      }
    }
    if (document.getElementById('kmGroupsWrap')) renderKeywordManageView();
    await kmRefreshMonitorWorkbench();
  } catch (e) {
    input.value = input.dataset.lastSaved || '';
    kmShowToast('改词失败：' + e.message, false);
  }
}

async function kmDeleteKeyword(btn, text) {
  const kid = btn.dataset.kid;
  try {
    if (kmActiveKeywordId === kid) kmCloseKeywordModal();
    await kmApiFetch(`${KM_API}/keywords/${encodeURIComponent(kid)}`, { method: 'DELETE' });
    await reloadKeywordManageData();
    await kmRefreshMonitorWorkbench();
    kmShowToast('已归档');
  } catch (e) {
    kmShowToast('归档失败：' + e.message, false);
  }
}

async function deleteKeywordFromDetail(event, keywordId, keywordText) {
  if (event) event.stopPropagation();
  if (!confirm(`确认归档关键词「${keywordText}」？\n\n归档会停止监控，并将此关键词从左侧列表和词库管理中隐藏。\n历史抓取数据不会删除——之前搜到的文章、排名、账号关联仍保留在底层数据库中。\n\n之后重新添加相同关键词时，系统会恢复这个关键词及其历史记录。\n\n确定要归档吗？`)) return;
  try {
    await kmApiFetch(`${KM_API}/keywords/${encodeURIComponent(keywordId)}`, { method: 'DELETE' });
    await kmEnsureDataLoaded({ silent: true });
    await kmRefreshMonitorWorkbench();
    kmShowToast('已归档');
  } catch (e) {
    kmShowToast('归档失败：' + e.message, false);
  }
}

async function kmRefreshDeleteKeyword(btn, text) {
  const kid = btn.dataset.kid;
  const deleteText = text || btn.dataset.text || '';
  if (!confirm(`确认归档关键词「${deleteText}」？\n\n归档会停止监控，并将此关键词从左侧列表和词库管理中隐藏。\n历史抓取数据不会删除——之前搜到的文章、排名、账号关联仍保留在底层数据库中。\n\n之后重新添加相同关键词时，系统会恢复这个关键词及其历史记录。\n\n确定要归档吗？`)) return;
  try {
    await kmApiFetch(`${KM_API}/keywords/${encodeURIComponent(kid)}`, { method: 'DELETE' });
    if (KM_DATA) {
      for (const group of KM_DATA.groups || []) {
        const idx = (group.keywords || []).findIndex(k => k.keyword_id === kid);
        if (idx > -1) {
          group.keywords.splice(idx, 1);
          break;
        }
      }
      KM_DATA.total = (KM_DATA.groups || []).reduce((sum, g) => sum + (g.keywords || []).length, 0);
    }
    const row = btn.closest('.km-refresh-row');
    if (row) row.remove();
    const groupBody = btn.closest('.km-refresh-group-body');
    if (groupBody) {
      const remaining = groupBody.querySelectorAll('.km-refresh-row');
      const emptyHint = groupBody.querySelector('.km-refresh-empty');
      if (remaining.length === 0 && !emptyHint) {
        groupBody.insertAdjacentHTML('afterbegin', '<div class="km-refresh-empty">暂无关键词</div>');
      } else if (remaining.length > 0 && emptyHint) {
        emptyHint.remove();
      }
      const groupHeader = btn.closest('.km-refresh-group').querySelector('.km-refresh-group-count');
      if (groupHeader) {
        const gid = btn.closest('.km-refresh-group').dataset.group;
        const group = KM_DATA?.groups?.find(g => g.group_id === gid);
        if (group) groupHeader.textContent = `${(group.keywords || []).length} 词`;
      }
    }
    kmRenderStats();
    renderKeywordManageView();
    await kmRefreshMonitorWorkbench();
    kmShowToast('已归档');
  } catch (e) {
    kmShowToast('归档失败：' + e.message, false);
  }
}

async function kmRefreshAddKeyword(input) {
  const text = input.value.trim();
  if (!text) return;
  const groupId = input.dataset.group;
  try {
    const kw = await kmApiFetch(`${KM_API}/keywords`, {
      method: 'POST',
      body: JSON.stringify({ group_id: groupId, keyword_text: text }),
    });
    input.value = '';
    if (KM_DATA) {
      const group = KM_DATA.groups.find(g => g.group_id === groupId);
      if (group) {
        group.keywords = group.keywords || [];
        group.keywords.push(kw);
        const groupBody = input.closest('.km-refresh-group-body');
        if (groupBody) {
          const emptyHint = groupBody.querySelector('.km-refresh-empty');
          if (emptyHint) emptyHint.remove();
          const row = document.createElement('div');
          row.className = 'km-refresh-row';
          row.dataset.kid = kw.keyword_id;
          row.innerHTML = `
            <label class="km-refresh-check">
              <input type="checkbox" value="${escapeHtml(kw.keyword_id)}" data-text="${escapeHtml(kw.keyword_text)}" checked />
              <span class="km-refresh-check-text">刷新</span>
            </label>
            <span class="km-refresh-text" title="${escapeHtml(kw.keyword_text)}">${escapeHtml(kw.keyword_text)}</span>
            <button class="km-refresh-delete" data-kid="${escapeHtml(kw.keyword_id)}" data-text="${escapeHtml(kw.keyword_text)}" onclick="kmRefreshDeleteKeyword(this)" title="归档关键词">归档</button>
          `;
          const addRow = groupBody.querySelector('.km-refresh-add-row');
          if (addRow) {
            groupBody.insertBefore(row, addRow);
          } else {
            groupBody.appendChild(row);
          }
          const groupHeader = groupBody.parentElement.querySelector('.km-refresh-group-count');
          if (groupHeader) groupHeader.textContent = `${(group.keywords || []).length} 词`;
        }
      }
      KM_DATA.total = (KM_DATA.groups || []).reduce((sum, g) => sum + (g.keywords || []).length, 0);
    }
    kmRenderStats();
    renderKeywordManageView();
    await kmRefreshMonitorWorkbench();
    kmShowToast(`已添加「${kw.keyword_text}」`);
  } catch (e) {
    kmShowToast('添加失败：' + e.message, false);
  }
}

function kmHandleAddKw(e, input) {
  if (e.key === 'Enter') { e.preventDefault(); kmAddKeyword(input); }
}

async function kmAddKeyword(input) {
  const text = input.value.trim();
  if (!text) return;
  const groupId = input.dataset.group;
  try {
    const kw = await kmApiFetch(`${KM_API}/keywords`, {
      method: 'POST',
      body: JSON.stringify({ group_id: groupId, keyword_text: text }),
    });
    input.value = '';
    await reloadKeywordManageData();
    await kmRefreshMonitorWorkbench();
    kmShowToast(`已添加「${kw.keyword_text}」`);
  } catch (e) {
    kmShowToast('添加失败：' + e.message, false);
  }
}

function kmOpenAddGroupModal() {
  kmEditGroupId = null;
  document.getElementById('kmModalTitle').textContent = '新增分组';
  document.getElementById('kmModalInput').value = '';
  document.getElementById('kmGroupModal').classList.add('open');
  setTimeout(() => document.getElementById('kmModalInput').focus(), 50);
}

function kmOpenRenameGroupModal(groupId, label) {
  kmEditGroupId = groupId;
  document.getElementById('kmModalTitle').textContent = '修改分组名称';
  document.getElementById('kmModalInput').value = label;
  document.getElementById('kmGroupModal').classList.add('open');
  setTimeout(() => document.getElementById('kmModalInput').focus(), 50);
}

function kmCloseGroupModal() {
  document.getElementById('kmGroupModal').classList.remove('open');
}

async function kmConfirmGroupModal() {
  const label = document.getElementById('kmModalInput').value.trim();
  if (!label) return;
  kmCloseGroupModal();
  try {
    if (kmEditGroupId) {
      await kmApiFetch(`${KM_API}/groups/${encodeURIComponent(kmEditGroupId)}`, {
        method: 'PATCH',
        body: JSON.stringify({ label }),
      });
      kmShowToast('分组已重命名');
    } else {
      await kmApiFetch(`${KM_API}/groups`, {
        method: 'POST',
        body: JSON.stringify({ label }),
      });
      kmShowToast('分组已创建');
    }
    await reloadKeywordManageData();
    await kmRefreshMonitorWorkbench();
  } catch (e) {
    kmShowToast('操作失败：' + e.message, false);
  }
}

async function kmDeleteGroup(groupId, label) {
  if (!confirm(`确认删除分组「${label}」？\n注意：分组内必须没有关键词才可删除。`)) return;
  try {
    await kmApiFetch(`${KM_API}/groups/${encodeURIComponent(groupId)}`, { method: 'DELETE' });
    await reloadKeywordManageData();
    await kmRefreshMonitorWorkbench();
    kmShowToast('分组已删除');
  } catch (e) {
    kmShowToast('删除失败：' + e.message, false);
  }
}

// 关闭 modal 点击遮罩
document.addEventListener('click', e => {
  const settingsMask = document.getElementById('kmSettingsModal');
  if (settingsMask && e.target === settingsMask) kmCloseSettingsModal();
  const mask = document.getElementById('kmGroupModal');
  if (mask && e.target === mask) kmCloseGroupModal();
  const keywordMask = document.getElementById('kmKeywordModal');
  if (keywordMask && e.target === keywordMask) kmCloseKeywordModal();
  const refreshMask = document.getElementById('kmRefreshModal');
  if (refreshMask && e.target === refreshMask) kmCloseRefreshModal();
});

// ── 单词刷新 ─────────────────────────────────────────
const _refreshJobs = {};  // keywordId -> jobId

async function startKeywordRefresh(event, keywordId, keyword) {
  if (event) event.stopPropagation();
  const sourceButton = event?.currentTarget instanceof HTMLElement
    && event.currentTarget.dataset?.kid === String(keywordId)
    ? event.currentTarget
    : null;
  const btn = sourceButton || document.getElementById(`refresh-btn-${keywordId}`);
  if (btn) { btn.textContent = '搜索中…'; btn.disabled = true; }
  try {
    const resp = await fetch(`/api/keywords/${encodeURIComponent(keywordId)}/refresh`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ keyword }),
    });
    const data = await resp.json();

    if (resp.status === 202 && data.status === 'queued') {
      const ahead = data.queued_ahead || 0;
      const aheadText = ahead > 0 ? `，前面还有 ${ahead} 个排队` : '';
      kmShowToast(`「${keyword}」已排队等待刷新（当前正在刷新「${data.current}」${aheadText}），完成后自动执行`);
      if (btn) { btn.textContent = '排队中…'; btn.disabled = true; }
      _refreshJobs[keywordId] = data.job_id;
      _pollRefreshJob(keywordId, data.job_id, btn);
    } else if (resp.status === 409 && data.status === 'rejected') {
      if (data.reason === 'batch_running') {
        kmShowToast(`当前正在批量刷新「${data.current || '关键词'}」，无法同时启动单词刷新，请等待批量刷新完成后再试`, false);
      } else {
        kmShowToast('当前有刷新任务进行中，请稍后再试', false);
      }
      if (btn) { btn.textContent = '刷新数据'; btn.disabled = false; }
    } else if (!resp.ok) {
      throw new Error(typeof data.error === 'string' ? data.error : 'refresh failed');
    } else {
      _refreshJobs[keywordId] = data.job_id;
      _pollRefreshJob(keywordId, data.job_id, btn);
    }
  } catch (e) {
    if (btn) { btn.textContent = '刷新失败'; btn.disabled = false; }
    console.error('refresh failed', e);
  }
}

function _pollRefreshJob(keywordId, jobId, refreshButton = null) {
  const btn = refreshButton || document.getElementById(`refresh-btn-${keywordId}`);
  const iv = setInterval(async () => {
    try {
      const resp = await fetch(`/api/refresh-status/${jobId}`);
      if (!resp.ok) return;
      const data = await resp.json();
      if (data.status === 'queued' || data.status === 'queued_to_running') {
        if (btn) { btn.textContent = '排队中…'; btn.disabled = true; }
        return;
      }
      if (data.status === 'running') {
        if (btn) { btn.textContent = '搜索中…'; btn.disabled = true; }
        return;
      }
      if (data.status === 'done') {
        clearInterval(iv);
        if (btn) { btn.textContent = '刷新数据'; btn.disabled = false; }
        await loadData({ preserveSelection: true });
        refresh();
        kmShowToast(`「${data.keyword || keywordId}」刷新完成`);
      } else if (data.status === 'failed') {
        clearInterval(iv);
        if (btn) { btn.textContent = '刷新失败'; btn.disabled = false; }
        kmShowToast(`「${data.keyword || keywordId}」刷新失败`, false);
      }
    } catch (_) {}
  }, 4000);
}

// ── 搜索词信号 → 添加关键词弹窗 ──────────────────────
let _signalTermPending = null;

function signalTermClick(term) {
  _signalTermPending = term;
  const modal = document.getElementById('signalTermModal');
  if (!modal) return;
  document.getElementById('signalTermText').textContent = term;

  const select = document.getElementById('signalTermGroupSelect');
  select.innerHTML = '';
  const groups = KM_DATA?.groups || [];
  if (!groups.length) {
    select.innerHTML = '<option value="">（暂无分组，请先在词库管理创建）</option>';
  } else {
    groups.forEach(g => {
      const opt = document.createElement('option');
      opt.value = g.group_id;
      opt.textContent = g.label;
      select.appendChild(opt);
    });
  }

  const existing = (KM_DATA?.groups || []).some(g =>
    (g.keywords || []).some(kw => kw.keyword_text === term)
  );
  const confirmBtn = document.getElementById('signalTermConfirmBtn');
  if (existing) {
    confirmBtn.textContent = '已存在';
    confirmBtn.disabled = true;
  } else {
    confirmBtn.textContent = '确认添加';
    confirmBtn.disabled = false;
  }

  modal.classList.add('open');
}

function signalTermClose() {
  _signalTermPending = null;
  document.getElementById('signalTermModal')?.classList.remove('open');
}

async function signalTermConfirm() {
  if (!_signalTermPending) return;
  const term = _signalTermPending;
  const groupId = document.getElementById('signalTermGroupSelect').value;
  if (!groupId) { kmShowToast('请先选择分类', false); return; }

  const confirmBtn = document.getElementById('signalTermConfirmBtn');
  confirmBtn.disabled = true;
  confirmBtn.textContent = '添加中…';

  try {
    await kmApiFetch(`${KM_API}/keywords`, {
      method: 'POST',
      body: JSON.stringify({ group_id: groupId, keyword_text: term }),
    });
    await reloadKeywordManageData();
    signalTermClose();
    kmShowToast(`已添加「${term}」`);

    await loadData({ preserveSelection: false });
    curKeyword = term;
    refresh();

    if (confirm(`关键词「${term}」已添加成功。\n\n是否立即刷新该关键词的数据？\n（如果上一个关键词还在刷新中，系统会自动排队处理，不会冲突。）`)) {
      const kw = (KM_DATA?.groups || []).flatMap(g => g.keywords || []).find(k => k.keyword_text === term);
      if (kw) {
        startKeywordRefresh(null, kw.keyword_id, term);
      }
    }
  } catch (e) {
    kmShowToast('添加失败：' + e.message, false);
    confirmBtn.disabled = false;
    confirmBtn.textContent = '确认添加';
  }
}

(async () => {
  initAccountScoreTooltip();
  await kmBootstrapBatchStatus();
  kmInitRefreshHistoryBtn();
  const ok = await loadData();
  if (ok) refresh();
})();
