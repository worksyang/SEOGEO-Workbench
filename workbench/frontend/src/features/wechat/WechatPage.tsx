import {useEffect, useMemo, useRef, useState} from 'react'
import type {
  WechatArticleResponse,
  WechatBootstrapResponse,
  WechatKeywordResponse,
  WechatSourceStatus,
} from '../../types'

type AnyRecord = Record<string, unknown>
type LoadState = 'loading' | 'ready' | 'empty' | 'offline' | 'error'

interface KeywordRow {
  id: string | null
  name: string
  group: string
  status: string
  raw: AnyRecord
}

interface ArticleRow {
  id: string | null
  title: string
  rank: string
  account: string
  url: string
  raw: AnyRecord
}

const STATUS_LABELS: Record<string, string> = {
  healthy: '健康',
  ready: '健康',
  online: '健康',
  degraded: '降级',
  partial: '降级',
  offline: '离线',
  unavailable: '离线',
  error: '错误',
  blocked: '受阻',
  unknown: '未知',
}

const SUMMARY_LABELS: Array<[string[], string]> = [
  [['keyword_count', 'keywords_count', 'total_keywords', 'keywords'], '关键词'],
  [['snapshot_count', 'snapshots_count', 'total_snapshots', 'snapshots'], '快照'],
  [['article_count', 'articles_count', 'total_articles', 'articles'], '文章'],
  [['observation_count', 'observations_count', 'total_observations', 'observations'], '观测'],
]

function record(value: unknown): AnyRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as AnyRecord : {}
}

function text(value: unknown): string {
  return typeof value === 'string' || typeof value === 'number' ? String(value) : ''
}

function firstText(source: AnyRecord, keys: string[]): string {
  for (const key of keys) {
    const value = text(source[key])
    if (value) return value
  }
  return ''
}

function list(value: unknown): unknown[] {
  if (Array.isArray(value)) return value
  const object = record(value)
  for (const key of ['items', 'data', 'results', 'rows', 'list', 'articles', 'snapshots', 'related_terms', 'suggestions']) {
    if (Array.isArray(object[key])) return object[key] as unknown[]
  }
  return []
}

function listFrom(source: AnyRecord, keys: string[]): unknown[] {
  for (const key of keys) {
    const value = source[key]
    if (Array.isArray(value)) return value
    const nested = list(value)
    if (nested.length) return nested
  }
  return []
}

function mergeLists(source: AnyRecord, keys: string[]): unknown[] {
  return keys.flatMap((key) => {
    const value = source[key]
    return Array.isArray(value) ? value : list(value)
  })
}

function formatDate(value: unknown): string {
  const raw = text(value)
  if (!raw) return '时间不可用'
  const date = new Date(raw)
  return Number.isNaN(date.getTime()) ? raw : new Intl.DateTimeFormat('zh-CN', {dateStyle: 'short', timeStyle: 'short'}).format(date)
}

function numberText(value: unknown): string {
  if (typeof value !== 'number' && typeof value !== 'string') return ''
  const number = Number(value)
  return Number.isFinite(number) ? new Intl.NumberFormat('zh-CN').format(number) : text(value)
}

function normalizeKeyword(value: unknown, index: number): KeywordRow {
  const raw = record(value)
  return {
    id: firstText(raw, ['keyword_id', 'id']) || null,
    name: firstText(raw, ['keyword', 'name', 'phrase', 'query']) || '未命名关键词',
    group: firstText(raw, ['group_name', 'group', 'category', 'folder', 'topic', 'keyword_bucket', 'bucket']) || '未分组',
    status: firstText(raw, ['status', 'state']) || firstText(record(raw.source_status), ['status']) || 'unknown',
    raw,
  }
}

function normalizeArticle(value: unknown, index: number): ArticleRow {
  const raw = record(value)
  return {
    id: firstText(raw, ['article_id', 'id', 'content_id']) || null,
    title: firstText(raw, ['title', 'name', 'headline']) || '未命名文章',
    rank: firstText(raw, ['rank', 'position', 'order']) || '',
    account: firstText(raw, ['account_name', 'account_name_raw', 'creator_name_raw', 'author', 'source_name', '公众号']) || '',
    url: firstText(raw, ['url', 'article_url', 'link', 'url_raw', 'normalized_url', 'raw_url']) || '',
    raw,
  }
}

function sourceLabel(status: unknown): string {
  const normalized = (text(record(status).status) || text(status)).toLowerCase() || 'unknown'
  return STATUS_LABELS[normalized] || normalized
}

function sourceTone(status: unknown): string {
  const normalized = (text(record(status).status) || text(status)).toLowerCase()
  if (['healthy', 'ready', 'online'].includes(normalized)) return 'healthy'
  if (['degraded', 'partial', 'blocked'].includes(normalized)) return 'degraded'
  if (['offline', 'unavailable', 'error'].includes(normalized)) return 'offline'
  return 'unknown'
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, {headers: {Accept: 'application/json'}, signal})
  const body = await response.json().catch(() => null)
  if (!response.ok) {
    const error = record(body).error
    throw new Error(firstText(record(error), ['message']) || `请求失败（${response.status}）`)
  }
  return body as T
}

function detailValue(data: AnyRecord, keys: string[]): unknown {
  for (const key of keys) {
    if (data[key] !== undefined && data[key] !== null) return data[key]
  }
  return undefined
}

function safeExternalUrl(value: unknown): string {
  const candidate = text(value).trim()
  if (!/^https?:\/\//i.test(candidate)) return ''
  try {
    const parsed = new URL(candidate)
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.href : ''
  } catch {
    return ''
  }
}

function factText(value: unknown): string {
  if (Array.isArray(value)) return `${value.length} 项`
  if (typeof value === 'number') return numberText(value)
  if (typeof value === 'string') return value
  const object = record(value)
  return firstText(object, ['value', 'score', 'rank', 'count', 'status', 'date', 'captured_at']) || (Object.keys(object).length ? '有数据' : '')
}

function metricValue(source: AnyRecord, keys: string[]): unknown {
  const direct = detailValue(source, keys)
  if (direct !== undefined) return direct
  for (const containerKey of ['observations', 'metrics']) {
    const container = source[containerKey]
    if (Array.isArray(container)) {
      const match = container.map(record).find((item) => keys.some((key) => [firstText(item, ['metric_key', 'key', 'name', 'metric']), key].some((candidate) => candidate && (candidate === key || candidate.includes(key) || key.includes(candidate)))))
      if (match) return detailValue(match, ['numeric_value', 'value', 'count', 'reading'])
    } else {
      const nested = record(container)
      const nestedValue = detailValue(nested, keys)
      if (nestedValue !== undefined) return nestedValue
    }
  }
  return undefined
}

export default function WechatPage({onSourceStatus}: {onSourceStatus: (status: string) => void}) {
  const [bootstrap, setBootstrap] = useState<WechatBootstrapResponse['data'] | null>(null)
  const [state, setState] = useState<LoadState>('loading')
  const [error, setError] = useState('')
  const [query, setQuery] = useState('')
  const [group, setGroup] = useState('all')
  const [statusFilter, setStatusFilter] = useState('all')
  const [sortMode, setSortMode] = useState<'recent' | 'name'>('recent')
  const [selectedId, setSelectedId] = useState('')
  const [keywordData, setKeywordData] = useState<AnyRecord | null>(null)
  const [keywordState, setKeywordState] = useState<LoadState>('loading')
  const [selectedSlice, setSelectedSlice] = useState('')
  const [articleData, setArticleData] = useState<AnyRecord | null>(null)
  const articleRequestRef = useRef(0)
  const articleControllerRef = useRef<AbortController | null>(null)
  const [refreshState, setRefreshState] = useState<'idle' | 'running' | 'queued' | 'success' | 'rejected' | 'failure'>('idle')
  const [refreshMessage, setRefreshMessage] = useState('')

  const loadBootstrap = (signal?: AbortSignal) => {
    setState('loading')
    setError('')
    getJson<WechatBootstrapResponse>('/api/v1/wechat/bootstrap', signal)
      .then((response) => {
        const data = response?.data ?? null
        setBootstrap(data)
        const status = text(record(data?.source_status).status) || text(data?.source_status) || 'unknown'
        onSourceStatus(status)
        setState(sourceTone(status) === 'offline' ? 'offline' : data?.keywords?.length ? 'ready' : 'empty')
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === 'AbortError') return
        setState('offline')
        setError(reason instanceof Error ? reason.message : '微信搜一搜服务暂时无法连接')
        onSourceStatus('offline')
      })
  }

  useEffect(() => {
    const controller = new AbortController()
    loadBootstrap(controller.signal)
    return () => controller.abort()
  }, [])

  const keywords = useMemo(() => (bootstrap?.keywords ?? []).map(normalizeKeyword), [bootstrap])
  const groups = useMemo(() => Array.from(new Set(keywords.map((item) => item.group))), [keywords])
  const statuses = useMemo(() => Array.from(new Set(keywords.map((item) => item.status))), [keywords])
  const visibleKeywords = useMemo(() => {
    const lowered = query.trim().toLowerCase()
    return keywords.filter((item) => {
      const matchesQuery = !lowered || `${item.name} ${item.group}`.toLowerCase().includes(lowered)
      return matchesQuery && (group === 'all' || item.group === group) && (statusFilter === 'all' || item.status === statusFilter)
    }).sort((left, right) => sortMode === 'name'
      ? left.name.localeCompare(right.name, 'zh-CN')
      : firstText(right.raw, ['latest_captured_at', 'updated_at', 'captured_at']).localeCompare(firstText(left.raw, ['latest_captured_at', 'updated_at', 'captured_at'])))
  }, [group, keywords, query, sortMode, statusFilter])

  useEffect(() => {
    if (!selectedId && visibleKeywords[0]?.id) setSelectedId(visibleKeywords[0].id)
    if (selectedId && !visibleKeywords.some((item) => item.id === selectedId)) setSelectedId(visibleKeywords[0]?.id ?? '')
  }, [selectedId, visibleKeywords])

  useEffect(() => {
    const controller = new AbortController()
    if (!selectedId) {
      setKeywordData(null)
      setKeywordState(state === 'offline' ? 'offline' : 'empty')
      return () => controller.abort()
    }
    setKeywordState('loading')
    setKeywordData(null)
    getJson<WechatKeywordResponse>(`/api/v1/wechat/keywords/${encodeURIComponent(selectedId)}`, controller.signal)
      .then((response) => {
        const data = response?.data ?? null
        setKeywordData(data)
        setKeywordState(data ? 'ready' : 'empty')
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === 'AbortError') return
        setKeywordState(state === 'offline' ? 'offline' : 'error')
        setError(reason instanceof Error ? reason.message : '关键词详情读取失败')
      })
    return () => controller.abort()
  }, [selectedId])

  const selectedKeyword = keywords.find((item) => item.id === selectedId)
  const detail = {...(keywordData ?? {}), ...record(keywordData?.keyword)}
  const snapshots = list(detailValue(detail, ['snapshots', 'snapshot_slices', 'time_slices']))
  const snapshotRows = snapshots.map((item, index) => record(item))
  const latestSnapshot = snapshotRows.reduce<AnyRecord | null>((latest, item) => {
    if (!latest) return item
    const latestTime = new Date(firstText(latest, ['captured_at', 'observed_at', 'date', 'created_at'])).getTime()
    const itemTime = new Date(firstText(item, ['captured_at', 'observed_at', 'date', 'created_at'])).getTime()
    return Number.isFinite(itemTime) && (!Number.isFinite(latestTime) || itemTime > latestTime) ? item : latest
  }, null) ?? snapshotRows.at(-1) ?? null
  const selectedSnapshot = snapshotRows.find((item, index) => {
    const id = firstText(item, ['snapshot_id', 'id', 'captured_at', 'observed_at', 'date']) || String(index)
    return id === selectedSlice
  }) ?? latestSnapshot
  const selectedSnapshotId = selectedSnapshot
    ? firstText(selectedSnapshot, ['snapshot_id', 'id', 'captured_at', 'observed_at', 'date']) || String(snapshotRows.indexOf(selectedSnapshot))
    : ''
  const snapshotFeatures = record(selectedSnapshot?.features)
  const articles = listFrom(selectedSnapshot ?? {}, ['hits', 'articles', 'top_articles', 'ranking'])
  const fallbackArticles = listFrom(detail, ['top_articles', 'articles', 'ranking', 'hits'])
  const relatedTermGroups = [
    ['建议', mergeLists(snapshotFeatures, ['suggestions'])],
    ['关联', mergeLists(snapshotFeatures, ['related', 'related_terms'])],
  ] as Array<[string, unknown[]]>
  const fallbackRelatedTermGroups = [
    ['建议', [...mergeLists(record(detail.features), ['suggestions']), ...mergeLists(detail, ['suggestions'])]],
    ['关联', [...mergeLists(record(detail.features), ['related', 'related_terms']), ...mergeLists(detail, ['related', 'related_terms', 'downstream_terms'])]],
  ] as Array<[string, unknown[]]>
  const articleRows = (articles.length ? articles : fallbackArticles).map(normalizeArticle)
  const visibleRelatedTermGroups = relatedTermGroups.some(([, values]) => values.length) ? relatedTermGroups : fallbackRelatedTermGroups
  const visibleRelatedTerms = visibleRelatedTermGroups.flatMap(([kind, values]) => values.map((value) => [kind, value] as [string, unknown]))
  const summary = record(bootstrap?.summary)

  useEffect(() => {
    setSelectedSlice(selectedSnapshotId)
  }, [selectedId, keywordData])

  useEffect(() => {
    articleControllerRef.current?.abort()
    articleControllerRef.current = null
    articleRequestRef.current += 1
    setArticleData(null)
    return () => {
      articleControllerRef.current?.abort()
      articleControllerRef.current = null
      articleRequestRef.current += 1
    }
  }, [selectedId, selectedSlice])

  const openArticle = (article: ArticleRow) => {
    if (!article.id) return
    setArticleData(null)
    articleControllerRef.current?.abort()
    const requestId = articleRequestRef.current + 1
    articleRequestRef.current = requestId
    const controller = new AbortController()
    articleControllerRef.current = controller
    getJson<WechatArticleResponse>(`/api/v1/wechat/articles/${encodeURIComponent(article.id)}`, controller.signal)
      .then((response) => {
        if (requestId === articleRequestRef.current) {
          articleControllerRef.current = null
          setArticleData(response?.data ?? {})
        }
      })
      .catch((reason: unknown) => {
        if (requestId === articleRequestRef.current && !(reason instanceof DOMException && reason.name === 'AbortError')) {
          articleControllerRef.current = null
          setArticleData({error: reason instanceof Error ? reason.message : '正文详情读取失败'})
        }
      })
  }

  const refresh = () => {
    if (!selectedId) return
    const repeatSubmission = refreshState === 'running' || refreshState === 'queued'
    if (!window.confirm(`${repeatSubmission ? '确认再次提交刷新' : '确认刷新'}关键词“${selectedKeyword?.name ?? selectedId}”？${repeatSubmission ? '这会再次触发' : '这会触发'}真实数据源请求。`)) return
    setRefreshState('running')
    setRefreshMessage('正在向数据源发起刷新，请等待服务回执…')
    fetch(`/api/v1/wechat/keywords/${encodeURIComponent(selectedId)}/refresh`, {
      method: 'POST',
      headers: {'Accept': 'application/json', 'Content-Type': 'application/json'},
      body: JSON.stringify({confirm: true}),
    })
      .then(async (response) => {
        const body = await response.json().catch(() => null)
        const bodyRecord = record(body)
        const errorRecord = record(bodyRecord.error)
        const dataRecord = record(bodyRecord.data)
        const resultRecord = {...dataRecord, ...record(dataRecord.result)}
        const reason = firstText(errorRecord, ['reason', 'message']) || firstText(resultRecord, ['reason', 'message'])
        if (response.status === 409) {
          setRefreshState('rejected')
          setRefreshMessage(reason || '刷新被拒绝：已有批次正在运行。')
          return
        }
        if (!response.ok || bodyRecord.ok === false) throw new Error(reason || `刷新失败（${response.status}）`)
        const returnedStatus = (firstText(resultRecord, ['status', 'state']) || (response.status === 202 ? 'queued' : 'running')).toLowerCase()
        if (returnedStatus === 'queued') {
          setRefreshState('queued')
          setRefreshMessage(reason || '刷新已排队，等待数据源处理。')
        } else if (returnedStatus === 'running') {
          setRefreshState('running')
          setRefreshMessage(reason || '刷新正在进行，新的快照将在任务完成后出现。')
        } else if (['rejected', 'reject', 'failed', 'failure'].includes(returnedStatus)) {
          setRefreshState('rejected')
          setRefreshMessage(reason || '刷新被数据源拒绝。')
        } else {
          setRefreshState('success')
          setRefreshMessage(firstText(resultRecord, ['message', 'status']) || '刷新请求已被服务接受。')
        }
        loadBootstrap()
      })
      .catch((reason: unknown) => {
        setRefreshState('failure')
        setRefreshMessage(reason instanceof Error ? reason.message : '刷新失败，未收到有效回执')
      })
  }

  const observedMetrics: Array<[string, unknown]> = ([
    ['阅读', ['read_count', 'reads', 'reading_count']],
    ['点赞', ['like_count', 'likes']],
    ['朋友在看', ['friend_read_count', 'friends_read', 'friend_look_count', 'friends_follow_count']],
  ] as Array<[string, string[]]>).map(([label, keys]): [string, unknown] => [label, metricValue(selectedSnapshot ?? detail, keys) ?? metricValue(detail, keys)])
    .filter(([, value]) => value !== undefined && value !== null && value !== '')
  const keywordFacts: Array<[string, string]> = ([
    ['历史最佳', 'history_best'],
    ['历史命中', 'history_hits'],
    ['最近运行', 'latest_run'],
    ['周转运行', 'turnover_runs'],
    ['关键词分数', 'kw_score'],
  ] as Array<[string, string]>).map(([label, key]): [string, string] => [label, factText(detail[key])]).filter(([, value]) => Boolean(value))

  return (
    <div className="wechat-page demo-module-page">
      <header className="module-top">
        <strong className="module-logo">微信关键词监测</strong>
        <span className="sep" />
        <span className="module-meta">真实来源 · <b>{sourceLabel(bootstrap?.source_status)}</b></span>
        <span className="module-right">更新于 {formatDate(bootstrap?.updated_at ?? summary.generated_at)}</span>
        <button className="mini-btn" type="button" onClick={() => loadBootstrap()}>重新读取</button>
      </header>

      <div className="monitor-layout">
        <aside className="monitor-left">
          <div className="monitor-tools">
            <div className="monitor-tool-row">
              <input className="monitor-search" aria-label="搜索微信关键词" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索关键词 / 分组" />
              <span className="tag">{visibleKeywords.length}/{keywords.length}</span>
            </div>
            <div className="filter-line"><span className="filter-label">分组</span><div className="filter-chips"><button className={`chip ${group === 'all' ? 'active' : ''}`} type="button" onClick={() => setGroup('all')}>全部</button>{groups.map((item) => <button className={`chip ${group === item ? 'active' : ''}`} key={item} type="button" onClick={() => setGroup(item)}>{item}</button>)}</div></div>
            <div className="filter-line"><span className="filter-label">状态</span><div className="filter-chips"><button className={`chip ${statusFilter === 'all' ? 'active' : ''}`} type="button" onClick={() => setStatusFilter('all')}>全部</button>{statuses.map((item) => <button className={`chip ${statusFilter === item ? 'active' : ''}`} key={item} type="button" onClick={() => setStatusFilter(item)}>{sourceLabel(item)}</button>)}</div></div>
            <div className="filter-line"><span className="filter-label">排序</span><div className="filter-chips"><button className={`chip ${sortMode === 'recent' ? 'active' : ''}`} type="button" onClick={() => setSortMode('recent')}>最近</button><button className={`chip ${sortMode === 'name' ? 'active' : ''}`} type="button" onClick={() => setSortMode('name')}>名称</button></div></div>
          </div>
          <div className="monitor-list" aria-live="polite">
            {state === 'loading' ? <div className="compact-empty">正在读取关键词…</div> : visibleKeywords.length ? visibleKeywords.map((item, index) => <button type="button" key={item.id ?? `${item.name}-${index}`} disabled={!item.id} className={`monitor-item ${item.id === selectedId ? 'active' : ''}`} onClick={() => { if (item.id) setSelectedId(item.id) }}><span className="rank">{index + 1}</span><span className="mi-main"><span className="mi-title-row"><strong className="mi-title">{item.name}</strong><span className={`tag ${sourceTone(item.status) === 'healthy' ? 'green' : sourceTone(item.status) === 'degraded' ? 'amber' : sourceTone(item.status) === 'offline' ? 'red' : ''}`}>{sourceLabel(item.status)}</span></span><span className="mi-tags"><span className="tag">{item.group}</span></span></span><span className="mi-side"><b>{numberText(detailValue(item.raw, ['snapshot_count', 'answer_count'])) || '—'}</b><span>快照</span></span></button>) : <div className="compact-empty">没有匹配的关键词。</div>}
          </div>
        </aside>

        <main className="monitor-right">
          {(state === 'error' || state === 'offline') && <div className="module-notice error" role="alert"><strong>{state === 'offline' ? '旧服务离线' : '接口读取失败'}</strong><span>{error || '暂时没有可用回执。'}</span><button type="button" onClick={() => loadBootstrap()}>重试</button></div>}
          {state === 'empty' && <div className="module-notice"><strong>暂无关键词数据</strong><span>服务已响应，但当前没有可展示的关键词或快照。</span></div>}

          <section className="card keyword-hero">
            <div className="kh-top"><div><strong className="kh-title">{selectedKeyword?.name ?? '选择一个关键词'}</strong><p className="kh-sub">{selectedKeyword ? `${selectedKeyword.group} · ${sourceLabel(selectedKeyword.status)}` : '从左侧关键词列表选择真实观测对象。'}</p></div><div className="kh-actions"><button className="mini-btn primary" type="button" disabled={!selectedId} onClick={refresh}>{refreshState === 'running' || refreshState === 'queued' ? '再次提交刷新' : '刷新数据'}</button></div></div>
            <div className="stat-row"><div className="stat"><b>{snapshots.length || '—'}</b><span>历史快照</span></div><div className="stat"><b>{articleRows.length || '—'}</b><span>排名文章</span></div><div className="stat"><b>{visibleRelatedTerms.length || '—'}</b><span>关联词</span></div><div className="stat"><b>{selectedSnapshot ? formatDate(firstText(selectedSnapshot, ['captured_at', 'observed_at', 'date', 'created_at'])) : '—'}</b><span>当前切片</span></div></div>
          </section>

          <section className="card read-card">
            <div className="read-head"><div className="read-main"><span className="subtle">阅读趋势</span><b>{observedMetrics.length ? numberText(observedMetrics[0][1]) || text(observedMetrics[0][1]) : '—'}</b><strong>{observedMetrics[0]?.[0] ?? '暂无阅读指标'}</strong><p>当前快照真实观测；接口未返回时间序列时不补造曲线。</p></div><div className="window"><span>观测窗口</span><b>{selectedSnapshot ? formatDate(firstText(selectedSnapshot, ['captured_at', 'observed_at', 'date', 'created_at'])) : '—'}</b></div></div>
            <div className="metric-grid demo-observation-grid">{observedMetrics.length ? observedMetrics.map(([label, value]) => <div className="metric-box" key={label}><span>{label}</span><b>{numberText(value) || text(value) || '—'}</b></div>) : <div className="compact-empty">当前快照暂无阅读、点赞或朋友在看指标。</div>}</div>
          </section>

          <section className="card snapshot-card">
            <div className="card-head"><strong className="card-title">采集快照与当前排名文章</strong><span className="subtle">{articleRows.length ? `${articleRows.length} 篇` : '暂无文章'}</span></div>
            <div className="snapshots" role="tablist" aria-label="微信快照时间切片">{snapshotRows.length ? snapshotRows.map((item, index) => { const value = firstText(item, ['snapshot_id', 'id', 'captured_at', 'observed_at', 'date']) || String(index); return <button className={`snapshot ${(selectedSlice || selectedSnapshotId) === value ? 'active' : ''}`} type="button" role="tab" aria-selected={(selectedSlice || selectedSnapshotId) === value} key={value} onClick={() => setSelectedSlice(value)}><b>{formatDate(firstText(item, ['captured_at', 'observed_at', 'date', 'created_at']) || value)}</b><span>{firstText(item, ['status', 'state']) || '已采集'}</span></button> }) : <span className="subtle">暂无快照时间切片</span>}</div>
            <div className="article-list">{articleRows.length ? articleRows.map((article, index) => <button type="button" className="article-row" key={article.id ?? `${article.title}-${index}`} disabled={!article.id} onClick={() => openArticle(article)}><span className="article-rank">{article.rank || '—'}</span><span className="article-main"><b>{article.title}</b><span>{[article.account, article.url].filter(Boolean).join(' · ') || '来源信息不可用'}</span></span><span className="article-score">{article.id ? <b>查看</b> : <span>无 ID</span>}</span></button>) : <div className="compact-empty">当前快照没有排名文章。</div>}</div>
          </section>

          {(refreshState !== 'idle' || keywordState === 'loading' || keywordState === 'error') && <div className={`module-notice ${keywordState === 'error' || refreshState === 'failure' || refreshState === 'rejected' ? 'error' : ''}`} role="status"><strong>{keywordState === 'loading' ? '正在读取关键词详情' : refreshState === 'idle' ? '详情读取状态' : '刷新回执'}</strong><span>{keywordState === 'error' ? error : refreshMessage || '等待真实接口回执。'}</span></div>}

          {(keywordFacts.length || visibleRelatedTerms.length) ? <section className="card monitor-extra-card"><div className="card-head"><strong className="card-title">附加事实</strong><span className="subtle">不改变主观测顺序</span></div>{keywordFacts.length ? <div className="keyword-facts">{keywordFacts.map(([label, value]) => <div key={label}><span>{label}</span><strong>{value}</strong></div>)}</div> : null}{visibleRelatedTerms.length ? <div className="term-list">{visibleRelatedTerms.map(([kind, item], index) => <span key={`${kind}-${text(item)}-${index}`}><b>{kind}</b>{text(item) || firstText(record(item), ['term', 'keyword', 'name']) || '未命名词项'}</span>)}</div> : null}</section> : null}

          {articleData ? <section className="card monitor-extra-card"><div className="card-head"><strong className="card-title">{firstText({...articleData, ...record(articleData.article)}, ['title', 'name', 'headline']) || '文章详情'}</strong><button className="mini-btn" type="button" onClick={() => setArticleData(null)}>关闭</button></div>{articleData.error ? <div className="compact-empty">{text(articleData.error)}</div> : <div className="article-detail-content"><p>{firstText({...articleData, ...record(articleData.article)}, ['markdown_path', 'md_path', 'path', 'content_file_path']) ? `Markdown：${firstText({...articleData, ...record(articleData.article)}, ['markdown_path', 'md_path', 'path', 'content_file_path'])}` : '正文不可用，接口未返回 Markdown 路径。'}</p>{safeExternalUrl(firstText({...articleData, ...record(articleData.article)}, ['url', 'article_url', 'link', 'normalized_url', 'raw_url'])) ? <a href={safeExternalUrl(firstText({...articleData, ...record(articleData.article)}, ['url', 'article_url', 'link', 'normalized_url', 'raw_url']))} target="_blank" rel="noreferrer">打开原文</a> : null}</div>}</section> : null}
        </main>
      </div>
    </div>
  )
}
