import {useEffect, useMemo, useRef, useState} from 'react'
import {apiGet, apiRequest, ApiError} from '../../api/client'
import type {
  JsonRecord,
  XhsApiEnvelope,
  XhsArticle,
  XhsBootstrapData,
  XhsCounts,
  XhsHit,
  XhsKeyword,
  XhsObservation,
  XhsSnapshot,
  XhsRun,
  XhsStatus,
} from '../../types'

type ViewState = 'loading' | 'ready' | 'empty' | 'offline' | 'error'
type SortMode = 'published' | 'title'
type ActionResult = JsonRecord & {result?: JsonRecord; job_id?: string; status?: string}

const EMPTY_COUNTS: XhsCounts = {
  keywords: 0,
  accounts: 0,
  snapshots: 0,
  ranking_hits: 0,
  articles: 0,
  snapshot_terms: 0,
}

const STATUS_LABELS: Record<string, string> = {
  healthy: '健康',
  degraded: '历史回放',
  offline: '离线',
  unknown: '未知',
}

function record(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {}
}

function text(value: unknown, fallback = ''): string {
  return typeof value === 'string' || typeof value === 'number' ? String(value) : fallback
}

function number(value: unknown): number | null {
  if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null
  const parsed = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

function dateText(value: unknown): string {
  const raw = text(value)
  if (!raw) return '时间不可用'
  const date = new Date(raw)
  return Number.isNaN(date.getTime()) ? raw : new Intl.DateTimeFormat('zh-CN', {dateStyle: 'short', timeStyle: 'short'}).format(date)
}

function statusTone(value: unknown): string {
  const status = text(record(value).status || value).toLowerCase()
  if (['healthy', 'ready', 'online'].includes(status)) return 'healthy'
  if (['degraded', 'partial', 'blocked'].includes(status)) return 'degraded'
  if (['offline', 'unavailable', 'error'].includes(status)) return 'offline'
  return 'unknown'
}

function statusLabel(value: unknown): string {
  const status = statusTone(value)
  return STATUS_LABELS[status] ?? status
}

function keywordStateLabel(item: XhsKeyword): string {
  const status = text(item.status).toLowerCase()
  if (status) return status
  if (typeof item.enabled === 'boolean') return item.enabled ? 'active' : 'paused'
  return 'unknown'
}

function errorText(reason: unknown, fallback: string): string {
  return reason instanceof ApiError || reason instanceof Error ? reason.message : fallback
}

function asArray<T>(value: unknown): T[] {
  return Array.isArray(value) ? value as T[] : []
}

function scrubValue(value: unknown): unknown {
  const isSensitiveKey = (key: string) => {
    const normalized = key.toLowerCase().replace(/-/g, '_')
    if (['auth', 'authorization', 'cookie', 'session', 'xsec_source'].includes(normalized)
      || normalized.startsWith('auth_') || normalized.endsWith('_auth')) return true
    return ['token', 'secret', 'password', 'api_key', 'apikey', 'private_key', 'client_secret'].some((part) => normalized.includes(part))
  }
  if (Array.isArray(value)) return value.map(scrubValue)
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).filter(([key]) => !isSensitiveKey(key)).map(([key, item]) => [key, scrubValue(item)]))
  }
  return value
}

function jsonPayload(value: JsonRecord | undefined): Array<[string, string]> {
  if (!value) return []
  return Object.entries(scrubValue(value) as JsonRecord)
    .filter(([, item]) => item !== null && item !== undefined && item !== '')
    .slice(0, 16)
    .map(([key, item]) => [key, typeof item === 'string' ? item : JSON.stringify(item)])
}

function externalUrl(value: unknown): string {
  const raw = text(value).trim()
  if (!raw) return ''
  try {
    const url = new URL(raw)
    if (!['http:', 'https:'].includes(url.protocol)) return ''
    for (const key of [...url.searchParams.keys()]) {
      const normalized = key.toLowerCase().replace(/-/g, '_')
      if (['auth', 'authorization', 'cookie', 'session', 'xsec_source'].includes(normalized)
        || normalized.startsWith('auth_') || normalized.endsWith('_auth')
        || ['token', 'secret', 'password', 'api_key', 'apikey', 'private_key', 'client_secret'].some((part) => normalized.includes(part))) {
        url.searchParams.delete(key)
      }
    }
    url.hash = ''
    return url.toString()
  } catch {
    return ''
  }
}

function findJobId(data: ActionResult): string {
  const result = record(data.result)
  return text(data.job_id || result.job_id || result.id || data.id)
}

function metricLabel(key: string): string {
  return ({liked: '点赞', collected: '收藏', comment: '评论', shared: '分享'}[key] ?? key)
}

function metricKey(value: unknown): string {
  return text(value).split('.').at(-1)?.replace('_count', '') ?? ''
}

function termText(value: unknown): string {
  if (typeof value === 'string' || typeof value === 'number') return String(value)
  const item = record(value)
  return text(item.term_text || item.term || item.keyword || item.name)
}

export default function XhsPage({onSourceStatus}: {onSourceStatus: (status: string) => void}) {
  const [bootstrap, setBootstrap] = useState<XhsBootstrapData | null>(null)
  const [pageState, setPageState] = useState<ViewState>('loading')
  const [error, setError] = useState('')
  const [query, setQuery] = useState('')
  const [topic, setTopic] = useState('all')
  const [bucket, setBucket] = useState('all')
  const [keywordStatus, setKeywordStatus] = useState('all')
  const [keywordSort, setKeywordSort] = useState<'recent' | 'name'>('recent')
  const [selectedKeywordId, setSelectedKeywordId] = useState('')
  const [keywordReloadNonce, setKeywordReloadNonce] = useState(0)
  const [keywordDetail, setKeywordDetail] = useState<JsonRecord | null>(null)
  const [keywordState, setKeywordState] = useState<ViewState>('empty')
  const [selectedSnapshotId, setSelectedSnapshotId] = useState('')
  const [articleRows, setArticleRows] = useState<XhsArticle[]>([])
  const [articleState, setArticleState] = useState<ViewState>('loading')
  const [articleQuery, setArticleQuery] = useState('')
  const [articleSort, setArticleSort] = useState<SortMode>('published')
  const [accountQuery, setAccountQuery] = useState('')
  const [accountSort, setAccountSort] = useState<'score' | 'name'>('score')
  const [articleDetail, setArticleDetail] = useState<XhsArticle | null>(null)
  const [accountDetail, setAccountDetail] = useState<JsonRecord | null>(null)
  const [drawer, setDrawer] = useState<'article' | 'account' | null>(null)
  const [actionError, setActionError] = useState('')
  const [actionMessage, setActionMessage] = useState('')
  const [busy, setBusy] = useState('')
  const [importResult, setImportResult] = useState<JsonRecord | null>(null)
  const refreshTimer = useRef<number | null>(null)
  const articleControllerRef = useRef<AbortController | null>(null)
  const accountControllerRef = useRef<AbortController | null>(null)
  const articleRequestRef = useRef(0)
  const accountRequestRef = useRef(0)

  const loadBootstrap = async (signal?: AbortSignal) => {
    setPageState('loading')
    setError('')
    try {
      const response = await apiGet<XhsApiEnvelope<XhsBootstrapData>>('/api/v1/xhs/bootstrap', signal)
      const data = response.data ?? null
      setBootstrap(data)
      const source = data?.source_status?.status ?? 'unknown'
      onSourceStatus(source)
      setPageState(statusTone(source) === 'offline' ? 'offline' : data ? 'ready' : 'empty')
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === 'AbortError') return
      setPageState('error')
      setError(errorText(reason, '小红书工作台暂时无法连接'))
      onSourceStatus('offline')
    }
  }

  const loadArticles = async (signal?: AbortSignal) => {
    setArticleState('loading')
    try {
      const response = await apiGet<XhsApiEnvelope<{articles?: XhsArticle[]}>>('/api/v1/xhs/articles?limit=500', signal)
      const rows = response.data?.articles ?? []
      setArticleRows(rows)
      setArticleState(rows.length ? 'ready' : 'empty')
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === 'AbortError') return
      setArticleState('error')
      setActionError(errorText(reason, '小红书笔记读取失败'))
    }
  }

  useEffect(() => {
    const controller = new AbortController()
    void Promise.all([loadBootstrap(controller.signal), loadArticles(controller.signal)])
    return () => controller.abort()
  }, [])

  useEffect(() => () => {
    if (refreshTimer.current !== null) window.clearTimeout(refreshTimer.current)
    articleControllerRef.current?.abort()
    accountControllerRef.current?.abort()
  }, [])

  const keywords = useMemo(() => bootstrap?.keywords ?? [], [bootstrap])
  const accounts = useMemo(() => bootstrap?.accounts ?? [], [bootstrap])
  const counts = bootstrap?.counts ?? EMPTY_COUNTS
  const topics = useMemo(() => Array.from(new Set(keywords.map((item) => text(item.topic)).filter(Boolean))), [keywords])
  const buckets = useMemo(() => Array.from(new Set(keywords.map((item) => text(item.keyword_bucket)).filter(Boolean))), [keywords])
  const statuses = useMemo(() => Array.from(new Set(keywords.map(keywordStateLabel))), [keywords])
  const visibleKeywords = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return keywords.filter((item) => {
      const name = text(item.keyword, text(item.payload?.source_keyword_id))
      return (!needle || `${name} ${text(item.topic)} ${text(item.keyword_bucket)}`.toLowerCase().includes(needle))
        && (topic === 'all' || text(item.topic) === topic)
        && (bucket === 'all' || text(item.keyword_bucket) === bucket)
        && (keywordStatus === 'all' || keywordStateLabel(item) === keywordStatus)
    }).sort((left, right) => keywordSort === 'name'
      ? text(left.keyword).localeCompare(text(right.keyword), 'zh-CN')
      : text(right.latest_captured_at || right.updated_at || right.created_at).localeCompare(text(left.latest_captured_at || left.updated_at || left.created_at)))
  }, [bucket, keywordSort, keywordStatus, keywords, query, topic])

  useEffect(() => {
    if (!selectedKeywordId && visibleKeywords[0]?.keyword_id) setSelectedKeywordId(visibleKeywords[0].keyword_id)
    if (selectedKeywordId && !visibleKeywords.some((item) => item.keyword_id === selectedKeywordId)) setSelectedKeywordId(visibleKeywords[0]?.keyword_id ?? '')
  }, [selectedKeywordId, visibleKeywords])

  useEffect(() => {
    const controller = new AbortController()
    if (!selectedKeywordId) {
      setKeywordDetail(null)
      setKeywordState('empty')
      return () => controller.abort()
    }
    setKeywordState('loading')
    apiGet<XhsApiEnvelope<JsonRecord>>(`/api/v1/xhs/keywords/${encodeURIComponent(selectedKeywordId)}`, controller.signal)
      .then((response) => {
        setKeywordDetail(response.data ?? null)
        setKeywordState(response.data ? 'ready' : 'empty')
        setError('')
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === 'AbortError') return
        setKeywordState('error')
        setError(errorText(reason, '关键词详情读取失败'))
      })
    return () => controller.abort()
  }, [keywordReloadNonce, selectedKeywordId])

  const detailKeyword = record(keywordDetail?.keyword)
  const snapshots = useMemo<XhsSnapshot[]>(() => {
    const hubSnapshots = asArray<XhsSnapshot>(keywordDetail?.snapshots)
    const runs = asArray<XhsRun>(keywordDetail?.runs).map((run) => ({
      snapshot_id: text(run.id || run.captured_at),
      captured_at: run.captured_at,
      features: {
        suggestions: asArray<JsonRecord>(run.terms?.suggestions),
        related: asArray<JsonRecord>(run.terms?.related),
      },
      hits: asArray<XhsArticle>(run.articles).map((article) => ({
        hit_id: `${text(run.id || run.captured_at)}:${text(article.rank, '—')}:${text(article.article_id)}`,
        snapshot_id: text(run.id || run.captured_at),
        rank: number(article.rank),
        content_id: article.article_id,
        title_raw: article.title,
        url_raw: article.url || article.canonical_url,
        creator_name_raw: typeof article.account === 'string'
          ? article.account
          : text(record(article.account).name || record(article.account).canonical_name),
        work_type: article.work_type,
        liked_count: article.liked_count,
        collected_count: article.collected_count,
        comment_count: article.comment_count,
        shared_count: article.shared_count,
        published_at: article.published_at,
        payload: record(article.payload),
      })),
    } as XhsSnapshot))
    return [...hubSnapshots, ...runs].sort((left, right) => text(left.captured_at).localeCompare(text(right.captured_at)))
  }, [keywordDetail])
  const selectedSnapshot = snapshots.find((item) => item.snapshot_id === selectedSnapshotId) ?? snapshots.at(-1)
  const selectedHits = selectedSnapshot?.hits ?? asArray<XhsHit>(keywordDetail?.hits).filter((item) => item.snapshot_id === selectedSnapshot?.snapshot_id)
  const selectedFeatures = record(selectedSnapshot?.features)
  const visibleArticles = useMemo(() => {
    const needle = articleQuery.trim().toLowerCase()
    return [...articleRows]
      .filter((item) => !needle || `${text(item.title)} ${text(item.author_name)} ${JSON.stringify(item.payload ?? {})}`.toLowerCase().includes(needle))
      .sort((left, right) => articleSort === 'title'
        ? text(left.title).localeCompare(text(right.title), 'zh-CN')
        : text(right.published_at).localeCompare(text(left.published_at)))
  }, [articleQuery, articleRows, articleSort])
  const visibleAccounts = useMemo(() => {
    const needle = accountQuery.trim().toLowerCase()
    return accounts
      .filter((item) => !needle || `${text(item.name || item.canonical_name)} ${text(item.account_id || item.external_id)} ${JSON.stringify(item)}`.toLowerCase().includes(needle))
      .sort((left, right) => accountSort === 'name'
        ? text(left.name || left.canonical_name).localeCompare(text(right.name || right.canonical_name), 'zh-CN')
        : (number(right.score) ?? -Infinity) - (number(left.score) ?? -Infinity))
  }, [accountQuery, accountSort, accounts])

  const refreshData = async () => {
    const controller = new AbortController()
    await Promise.all([loadBootstrap(controller.signal), loadArticles(controller.signal)])
  }

  const openArticle = async (id: string) => {
    articleControllerRef.current?.abort()
    const controller = new AbortController()
    articleControllerRef.current = controller
    const requestId = ++articleRequestRef.current
    setBusy(`article-${id}`)
    setActionError('')
    try {
      const response = await apiGet<XhsApiEnvelope<XhsArticle>>(`/api/v1/xhs/articles/${encodeURIComponent(id)}`, controller.signal)
      const data = record(response.data)
      setArticleDetail(data ? {...record(data.article), ...data} as XhsArticle : null)
      setDrawer('article')
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === 'AbortError') return
      if (requestId === articleRequestRef.current) setActionError(errorText(reason, '笔记详情读取失败'))
    } finally {
      if (requestId === articleRequestRef.current) setBusy('')
    }
  }

  const openAccount = async (id: string) => {
    accountControllerRef.current?.abort()
    const controller = new AbortController()
    accountControllerRef.current = controller
    const requestId = ++accountRequestRef.current
    setBusy(`account-${id}`)
    setActionError('')
    try {
      const response = await apiGet<XhsApiEnvelope<JsonRecord>>(`/api/v1/xhs/accounts/${encodeURIComponent(id)}`, controller.signal)
      setAccountDetail(response.data ?? null)
      setDrawer('account')
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === 'AbortError') return
      if (requestId === accountRequestRef.current) setActionError(errorText(reason, '账号详情读取失败'))
    } finally {
      if (requestId === accountRequestRef.current) setBusy('')
    }
  }

  const pollRefresh = (jobId: string) => {
    if (refreshTimer.current !== null) window.clearTimeout(refreshTimer.current)
    const poll = async () => {
      try {
        const response = await apiGet<XhsApiEnvelope<JsonRecord>>(`/api/v1/xhs/refresh-status/${encodeURIComponent(jobId)}`)
        const result = record(response.data?.result ?? response.data)
        const status = text(result.status || result.state).toLowerCase()
        if (['queued', 'running', 'pending', 'processing', 'started', 'in_progress'].includes(status)) {
          refreshTimer.current = window.setTimeout(poll, 1800)
          return
        }
        setActionMessage(`刷新任务已结束：${status || '上游已返回终态'}。`)
        await refreshData()
        setBusy('')
      } catch (reason) {
        setActionError(errorText(reason, '刷新状态读取失败'))
        setBusy('')
      }
    }
    refreshTimer.current = window.setTimeout(poll, 900)
  }

  const refreshKeyword = async () => {
    if (!selectedKeywordId || !window.confirm('确认刷新该小红书关键词？此操作会请求真实上游。')) return
    if (refreshTimer.current !== null) {
      window.clearTimeout(refreshTimer.current)
      refreshTimer.current = null
    }
    setBusy('refresh')
    setActionError('')
    setActionMessage('')
    try {
      const response = await apiRequest<XhsApiEnvelope<ActionResult>>(`/api/v1/xhs/keywords/${encodeURIComponent(selectedKeywordId)}/refresh`, 'POST', {confirm: true})
      const data = response.data ?? {}
      const result = record(data.result ?? data) as ActionResult
      const status = text(result.status).toLowerCase()
      setActionMessage(status === 'running' ? '上游已确认运行中。' : status === 'queued' ? '上游已排队，正在等待终态。' : '刷新已收到上游成功回执。')
      const jobId = findJobId(result)
      if (jobId) pollRefresh(jobId)
      else {
        await refreshData()
        setBusy('')
      }
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409) setActionError('上游拒绝刷新：当前任务冲突或正在运行。')
      else setActionError(errorText(reason, '刷新未获得成功回执'))
      setBusy('')
    }
  }

  const runImport = async (dryRun: boolean) => {
    if (!dryRun && !window.confirm('确认执行小红书历史正式导入？该操作会写入 Hub。')) return
    setBusy(dryRun ? 'dry-run' : 'import')
    setActionError('')
    setActionMessage('')
    try {
      const response = await apiRequest<XhsApiEnvelope<JsonRecord>>('/api/v1/xhs/import', 'POST', {dry_run: dryRun})
      setImportResult(response.data ?? null)
      setActionMessage(dryRun ? 'dry-run 已返回真实统计，未写入 Hub。' : '正式导入已完成并返回批次回执。')
      if (!dryRun) await refreshData()
    } catch (reason) {
      setActionError(errorText(reason, '导入未获得成功回执'))
    } finally {
      setBusy('')
    }
  }

  const source = bootstrap?.source_status ?? {status: 'unknown' as XhsStatus}
  const selectedKeyword = keywords.find((item) => item.keyword_id === selectedKeywordId)
  const articleDetailRecord = record(articleDetail)
  const articlePayload = record(articleDetailRecord.payload)
  const observations = asArray<XhsObservation>(articleDetailRecord.observations)
  const accountRecord = record(accountDetail?.account ?? accountDetail)
  const accountFacts = Object.fromEntries(
    Object.entries({...accountRecord, ...record(accountRecord.payload)}).filter(([key]) => key !== 'payload'),
  ) as JsonRecord
  const hitMetricTotal = (key: string): number | null => {
    const values = selectedHits.map((hit) => number(record(hit)[key])).filter((value): value is number => value !== null)
    return values.length ? values.reduce((sum, value) => sum + value, 0) : null
  }
  const interactionMetrics = [
    ['点赞', hitMetricTotal('liked_count')],
    ['收藏', hitMetricTotal('collected_count')],
    ['评论', hitMetricTotal('comment_count')],
    ['分享', hitMetricTotal('shared_count')],
  ] as const

  return (
    <div className="xhs-page demo-module-page">
      <header className="module-top">
        <strong className="module-logo">小红书关键词监测</strong>
        <span className="sep" />
        <span className="module-meta">真实来源 · <b>{pageState === 'loading' ? '检查中' : statusLabel(source)}</b></span>
        <div className="module-switch"><button className="mini-btn" type="button" onClick={() => void runImport(true)} disabled={busy !== ''}>dry-run 导入</button><button className="mini-btn primary" type="button" onClick={() => void runImport(false)} disabled={busy !== ''}>正式导入</button></div>
        <span className="module-right">{source.source ?? '等待来源回执'}</span>
        <button className="mini-btn" type="button" onClick={() => void refreshData()} disabled={busy !== ''}>重新读取</button>
      </header>

      <div className="monitor-layout">
        <aside className="monitor-left">
          <div className="monitor-tools">
            <div className="monitor-tool-row"><input className="monitor-search" aria-label="搜索小红书关键词" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索关键词 / 主题 / 分组" /><span className="tag">{visibleKeywords.length}/{keywords.length}</span></div>
            <div className="filter-line"><span className="filter-label">主题</span><div className="filter-chips"><button className={`chip ${topic === 'all' ? 'active' : ''}`} type="button" onClick={() => setTopic('all')}>全部</button>{topics.map((item) => <button className={`chip ${topic === item ? 'active' : ''}`} key={item} type="button" onClick={() => setTopic(item)}>{item}</button>)}</div></div>
            <div className="filter-line"><span className="filter-label">分组</span><div className="filter-chips"><button className={`chip ${bucket === 'all' ? 'active' : ''}`} type="button" onClick={() => setBucket('all')}>全部</button>{buckets.map((item) => <button className={`chip ${bucket === item ? 'active' : ''}`} key={item} type="button" onClick={() => setBucket(item)}>{item}</button>)}</div></div>
            <div className="filter-line"><span className="filter-label">状态</span><div className="filter-chips"><button className={`chip ${keywordStatus === 'all' ? 'active' : ''}`} type="button" onClick={() => setKeywordStatus('all')}>全部</button>{statuses.map((item) => <button className={`chip ${keywordStatus === item ? 'active' : ''}`} key={item} type="button" onClick={() => setKeywordStatus(item)}>{item}</button>)}</div></div>
            <div className="filter-line"><span className="filter-label">排序</span><div className="filter-chips"><button className={`chip ${keywordSort === 'recent' ? 'active' : ''}`} type="button" onClick={() => setKeywordSort('recent')}>最近</button><button className={`chip ${keywordSort === 'name' ? 'active' : ''}`} type="button" onClick={() => setKeywordSort('name')}>名称</button></div></div>
          </div>
          <div className="monitor-list" role="list">
            {visibleKeywords.length ? visibleKeywords.map((item, index) => { const itemState = keywordStateLabel(item); return <button className={`monitor-item ${item.keyword_id === selectedKeywordId ? 'active' : ''}`} key={item.keyword_id} type="button" onClick={() => setSelectedKeywordId(text(item.keyword_id))}><span className="rank">{index + 1}</span><span className="mi-main"><span className="mi-title-row"><strong className="mi-title">{text(item.keyword, '未命名关键词')}</strong><span className={`tag ${itemState === 'active' ? 'green' : itemState === 'paused' ? 'amber' : ''}`}>{itemState}</span></span><span className="mi-tags">{text(item.topic) && <span className="tag">{text(item.topic)}</span>}{text(item.keyword_bucket) && <span className="tag">{text(item.keyword_bucket)}</span>}</span></span><span className="mi-side"><b>{text(item.snapshot_count, '—')}</b><span>快照</span></span></button> }) : <div className="compact-empty">{pageState === 'loading' ? '关键词加载中…' : '暂无关键词事实'}</div>}
          </div>
        </aside>

        <main className="monitor-right">
          <section className="card keyword-hero">
            <div className="kh-top"><div><strong className="kh-title">{text(selectedKeyword?.keyword, '选择一个关键词')}</strong><p className="kh-sub">{selectedKeyword ? `${text(selectedKeyword.topic, '无真实主题')} · ${text(selectedKeyword.keyword_bucket, '无真实分组')}` : '从左侧列表选择真实关键词观测对象。'}</p></div><div className="kh-actions"><button className="mini-btn primary" type="button" onClick={() => void refreshKeyword()} disabled={!selectedKeywordId || busy !== ''}>刷新关键词</button></div></div>
            <div className="stat-row"><div className="stat"><b>{snapshots.length || '—'}</b><span>历史快照</span></div><div className="stat"><b>{selectedHits.length || '—'}</b><span>排名命中</span></div><div className="stat"><b>{asArray(selectedFeatures.suggestions).length || '—'}</b><span>建议词</span></div><div className="stat"><b>{asArray(selectedFeatures.related).length || '—'}</b><span>关联词</span></div></div>
          </section>

          <section className="card read-card xhs-heat-card">
            <div className="read-head"><div className="read-main"><span className="subtle">互动热度</span><b>{interactionMetrics.reduce((sum, [, value]) => sum + (value ?? 0), 0) || '—'}</b><strong>当前快照互动总量</strong><p>点赞、收藏、评论与分享仅汇总当前排名列表中的真实值。</p></div><div className="window"><span>当前切片</span><b>{selectedSnapshot ? dateText(selectedSnapshot.captured_at) : '—'}</b></div></div>
            <div className="metric-grid">{interactionMetrics.map(([label, value]) => <div className="metric-box" key={label}><span>{label}</span><b>{value ?? '—'}</b></div>)}</div>
          </section>

          <section className="card snapshot-card">
            <div className="card-head"><strong className="card-title">采集快照与笔记排名</strong><span className="subtle">{selectedHits.length ? `${selectedHits.length} 条` : '暂无命中'}</span></div>
            <div className="snapshots" role="tablist" aria-label="小红书历史快照">{snapshots.length ? snapshots.map((item) => <button className={`snapshot ${item.snapshot_id === selectedSnapshot?.snapshot_id ? 'active' : ''}`} key={item.snapshot_id} type="button" role="tab" aria-selected={item.snapshot_id === selectedSnapshot?.snapshot_id} onClick={() => setSelectedSnapshotId(text(item.snapshot_id))}><b>{dateText(item.captured_at)}</b><span>{asArray(item.hits).length} 条命中</span></button>) : <span className="subtle">live 回执未提供历史快照；Hub 导入后可回放。</span>}</div>
            <div className="article-list">{selectedHits.length ? selectedHits.map((hit, index) => { const contentId = text(hit.content_id); const url = externalUrl(hit.url_raw); return <div className="article-row" key={hit.hit_id ?? `${contentId}-${index}`}><span className="article-rank">{number(hit.rank) ?? '—'}</span><span className="article-main"><b>{contentId ? <button className="link-button" type="button" onClick={() => void openArticle(contentId)}>{text(hit.title_raw, '无标题')}</button> : text(hit.title_raw, '无标题')}</b><span>{text(hit.creator_name_raw, '作者未知')} · {text(hit.work_type, '类型未知')} · 赞 {number(hit.liked_count) ?? '—'} · 藏 {number(hit.collected_count) ?? '—'} · 评 {number(hit.comment_count) ?? '—'} · 分享 {number(hit.shared_count) ?? '—'}</span></span><span className="article-score">{url ? <a href={url} target="_blank" rel="noreferrer">打开</a> : <span>—</span>}</span></div> }) : <div className="compact-empty">当前快照没有笔记排名。</div>}</div>
          </section>

          {error && <div className="module-notice error" role="alert"><strong>读取未完成</strong><span>{error}</span><button type="button" onClick={() => void refreshData()}>重试</button></div>}
          {(actionError || actionMessage) && <div className={`module-notice ${actionError ? 'error' : ''}`} role={actionError ? 'alert' : 'status'}><strong>{actionError ? '操作未完成' : '已收到真实回执'}</strong><span>{actionError || actionMessage}</span></div>}
          {keywordState === 'loading' && <div className="module-notice"><strong>正在读取历史详情</strong><span>等待真实接口返回。</span></div>}
          {keywordState === 'error' && <div className="module-notice error"><strong>详情读取失败</strong><button type="button" onClick={() => setKeywordReloadNonce((value) => value + 1)}>重试</button></div>}
          {importResult && <section className="card monitor-extra-card"><div className="card-head"><strong className="card-title">{text(importResult.dry_run) === 'true' || importResult.dry_run === true ? 'dry-run 回执' : '正式导入回执'}</strong><span className="subtle">batch {text(importResult.batch_id, '—')}</span></div><div className="xhs-receipt-grid"><span>manifest <strong>{text(record(importResult.audit).manifest_id, '—')}</strong></span><span>rejected <strong>{asArray(record(importResult.audit).rejected).length}</strong></span></div></section>}

          <section className="card monitor-extra-card"><div className="card-head"><strong className="card-title">笔记资产</strong><span className="subtle">{visibleArticles.length}/{articleRows.length}</span></div><div className="xhs-filters compact-controls"><label>搜索笔记<input value={articleQuery} onChange={(event) => setArticleQuery(event.target.value)} placeholder="标题、作者或事实 payload" /></label><label>排序<select value={articleSort} onChange={(event) => setArticleSort(event.target.value as SortMode)}><option value="published">发布时间</option><option value="title">标题</option></select></label></div>{articleState === 'loading' ? <div className="compact-empty">笔记加载中…</div> : <div className="xhs-table-wrap"><table><thead><tr><th>标题</th><th>作者</th><th>发布时间</th><th>链接</th></tr></thead><tbody>{visibleArticles.map((article) => <tr key={text(article.content_id || article.article_id)}><td className="ellipsis-cell"><button className="link-button" type="button" onClick={() => void openArticle(text(article.content_id || article.article_id))}>{text(article.title, '无标题')}</button></td><td>{text(article.author_name, '—')}</td><td>{dateText(article.published_at)}</td><td>{externalUrl(article.canonical_url) ? <a href={externalUrl(article.canonical_url)} target="_blank" rel="noreferrer">打开</a> : '—'}</td></tr>)}</tbody></table>{!visibleArticles.length && <div className="compact-empty">暂无笔记事实</div>}</div>}</section>

          <section className="card monitor-extra-card"><div className="card-head"><strong className="card-title">账号透视</strong><span className="subtle">{accounts.length}</span></div><div className="xhs-account-controls"><input className="xhs-account-search" placeholder="搜索账号、account_id 或画像" value={accountQuery} onChange={(event) => setAccountQuery(event.target.value)} /><select value={accountSort} onChange={(event) => setAccountSort(event.target.value as 'score' | 'name')}><option value="score">综合分</option><option value="name">名称</option></select></div><div className="xhs-account-list">{visibleAccounts.slice(0, 80).map((account, index) => { const id = text(account.creator_id || account.account_id || account.external_id, `account-${index}`); const externalId = text(account.account_id || account.external_id); return <button className="xhs-account-row" type="button" key={id} onClick={() => { if (externalId) void openAccount(externalId) }} disabled={!externalId || busy === `account-${externalId}`}><span><strong>{text(account.name || account.canonical_name, '未命名账号')}</strong><small>{externalId}{number(account.score) !== null ? ` · 综合分 ${number(account.score)}` : ''}</small></span><span className="tag">{externalId ? '查看' : '无 ID'}</span></button> })}{!visibleAccounts.length && <div className="compact-empty">暂无符合条件的账号</div>}</div></section>
        </main>
      </div>

      {drawer && <div className="xhs-drawer-backdrop" role="presentation" onClick={() => setDrawer(null)}><aside className="xhs-drawer" role="dialog" aria-modal="true" aria-label={drawer === 'article' ? '笔记详情' : '账号详情'} onClick={(event) => event.stopPropagation()}><button className="xhs-drawer-close" type="button" aria-label="关闭详情抽屉" onClick={() => setDrawer(null)}>×</button>{drawer === 'article' ? <><p className="eyebrow">NOTE DETAIL</p><h3>{text(articleDetailRecord.title, '笔记详情')}</h3><p className="subtle-copy">{text(articleDetailRecord.author_name, '作者未知')} · {dateText(articleDetailRecord.published_at)}</p>{externalUrl(articleDetailRecord.canonical_url) && <a href={externalUrl(articleDetailRecord.canonical_url)} target="_blank" rel="noreferrer">打开 canonical URL</a>}<h4>命中排名</h4><p>{asArray(articleDetailRecord.hits).map((hit) => `#${text(record(hit).rank, '—')}`).join(' · ') || '无事实'}</p><h4>指标时间序列</h4><div className="xhs-observation-list">{observations.map((item) => <div key={item.observation_id}><span>{metricLabel(metricKey(item.metric_key))}</span><strong>{item.numeric_value ?? '—'}</strong><small>{dateText(item.observed_at)}</small></div>)}</div><h4>事实 payload</h4><div className="xhs-payload-list">{jsonPayload(articlePayload).map(([key, value]) => <div key={key}><span>{key}</span><strong>{value}</strong></div>)}</div></> : <><p className="eyebrow">ACCOUNT DETAIL</p><h3>{text(accountRecord.name || accountRecord.canonical_name, '账号详情')}</h3><p className="subtle-copy">来源：{statusLabel(record(accountDetail?.source_status))}</p><div className="xhs-feature-grid"><div><span>账号 ID</span><strong>{text(accountRecord.account_id || accountRecord.external_id, '—')}</strong></div><div><span>粉丝</span><strong>{text(accountRecord.fans, '—')}</strong></div><div><span>作品</span><strong>{text(accountRecord.total_works, '—')}</strong></div><div><span>综合分</span><strong>{text(accountRecord.score, '—')}</strong></div><div><span>时效分</span><strong>{text(accountRecord.timeliness_score, '—')}</strong></div><div><span>今日分</span><strong>{text(accountRecord.today_score, '—')}</strong></div></div><p className="subtle-copy">{text(accountRecord.description, '无真实简介')} · 最近：{dateText(accountRecord.last_seen_at || accountRecord.updated_at || accountRecord.recent_at)}</p><h4>事实 payload</h4><div className="xhs-payload-list">{jsonPayload(accountFacts).map(([key, value]) => <div key={key}><span>{key}</span><strong>{value}</strong></div>)}</div></>}</aside></div>}
    </div>
  )
}
