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
    })
  }, [group, keywords, query, statusFilter])

  useEffect(() => {
    if (!selectedId && visibleKeywords[0]?.id) setSelectedId(visibleKeywords[0].id)
    if (selectedId && !keywords.some((item) => item.id === selectedId)) setSelectedId(visibleKeywords[0]?.id ?? '')
  }, [keywords, selectedId, visibleKeywords])

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
    <div className="wechat-page">
      <section className="wechat-source-banner">
        <div>
          <p className="eyebrow">WECHAT SEARCH · REAL SOURCE</p>
          <h2>微信搜一搜</h2>
          <p className="subtle-copy">关键词、时间切片和排名文章均来自微信搜一搜适配器，不以 Demo 数据填充。</p>
        </div>
        <div className="source-status-block">
          <span className={`status-dot large ${sourceTone(bootstrap?.source_status)}`} />
          <div><strong>{sourceLabel(bootstrap?.source_status)}</strong><small>更新于 {formatDate(bootstrap?.updated_at ?? summary.generated_at)}</small></div>
        </div>
      </section>

      {state === 'error' || state === 'offline' ? (
        <div className="module-notice error" role="alert"><strong>{state === 'offline' ? '旧服务离线' : '接口读取失败'}</strong><span>{error || '暂时没有可用回执。'}</span><button type="button" onClick={() => loadBootstrap()}>重试</button></div>
      ) : null}
      {state === 'empty' ? <div className="module-notice"><strong>暂无关键词数据</strong><span>服务已响应，但当前没有可展示的关键词或快照。</span></div> : null}

      <section className="wechat-summary-grid" aria-label="微信搜一搜汇总">
        {SUMMARY_LABELS.map(([keys, label]) => {
          const value = keys.map((key) => summary[key]).find((item) => item !== undefined && item !== null)
          return value === undefined ? null : <article className="metric-card" key={label}><span>{label}</span><strong>{numberText(value) || '—'}</strong><small>来自真实汇总</small></article>
        })}
        {summary.account_count !== undefined ? <article className="metric-card" key="account_count"><span>账号</span><strong>{numberText(summary.account_count) || '—'}</strong><small>来自真实汇总</small></article> : null}
      </section>

      <section className="wechat-layout">
        <aside className="panel wechat-keywords-panel">
          <div className="panel-heading"><div><p className="eyebrow">KEYWORDS</p><h3>关键词列表</h3></div><span className="count-pill">{visibleKeywords.length} 项</span></div>
          <div className="wechat-filters">
            <label className="sr-only" htmlFor="wechat-query">搜索关键词</label>
            <input id="wechat-query" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索关键词 / 分组" />
            <select value={group} onChange={(event) => setGroup(event.target.value)} aria-label="按分组筛选"><option value="all">全部分组</option>{groups.map((item) => <option key={item} value={item}>{item}</option>)}</select>
            <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)} aria-label="按状态筛选"><option value="all">全部状态</option>{statuses.map((item) => <option key={item} value={item}>{sourceLabel(item)}</option>)}</select>
          </div>
          <div className="wechat-keyword-list">
            {state === 'loading' ? <div className="empty-state"><strong>正在读取关键词…</strong></div> : visibleKeywords.length ? visibleKeywords.map((item, index) => <button type="button" key={item.id ?? `${item.name}-${index}`} disabled={!item.id} className={`wechat-keyword-row ${item.id === selectedId ? 'selected' : ''}`} onClick={() => { if (item.id) setSelectedId(item.id) }}><span><strong>{item.name}</strong><small>{item.id ? item.group : `${item.group} · 缺少稳定 ID，详情不可用`}</small></span><em className={`status-badge ${sourceTone(item.status)}`}>{sourceLabel(item.status)}</em></button>) : <div className="empty-state"><strong>没有匹配的关键词</strong><p>调整搜索词或筛选条件后重试。</p></div>}
          </div>
        </aside>

        <div className="wechat-detail-column">
          <section className="panel wechat-detail-panel">
            <div className="panel-heading"><div><p className="eyebrow">SELECTED KEYWORD</p><h3>{selectedKeyword?.name ?? '未选择关键词'}</h3></div><button className="secondary-button" type="button" disabled={!selectedId} onClick={refresh}>{refreshState === 'running' || refreshState === 'queued' ? '再次提交刷新' : '刷新数据'}</button></div>
            {refreshState !== 'idle' ? <div className={`refresh-receipt ${refreshState}`} role="status"><strong>{refreshState === 'running' ? 'running · 刷新进行中' : refreshState === 'queued' ? 'queued · 刷新已排队' : refreshState === 'rejected' ? 'rejected · 刷新被拒绝' : refreshState === 'success' ? '刷新已提交' : '刷新失败'}</strong><span>{refreshMessage}</span></div> : null}
            {keywordState === 'loading' ? <div className="empty-state"><strong>正在读取关键词详情…</strong></div> : keywordState === 'offline' ? <div className="empty-state"><strong>详情服务离线</strong><p>列表保留当前已获取内容，详情等待旧服务恢复。</p></div> : keywordState === 'error' ? <div className="empty-state"><strong>详情接口错误</strong><p>{error || '没有收到有效的详情数据。'}</p></div> : keywordData ? <div className="wechat-detail-body">
              <div className="slice-toolbar"><span>快照时间切片</span>{snapshotRows.length ? snapshotRows.map((item, index) => { const value = firstText(item, ['snapshot_id', 'id', 'captured_at', 'observed_at', 'date']) || String(index); return <button className={(selectedSlice || selectedSnapshotId) === value ? 'slice-button active' : 'slice-button'} type="button" key={value} onClick={() => setSelectedSlice(value)}>{formatDate(firstText(item, ['captured_at', 'observed_at', 'date', 'created_at']) || value)}</button> }) : <small>暂无快照时间切片</small>}</div>
              {keywordFacts.length ? <div className="keyword-facts">{keywordFacts.map(([label, value]) => <div key={label}><span>{label}</span><strong>{value}</strong></div>)}</div> : null}
              {observedMetrics.length ? <div className="observation-strip">{observedMetrics.map(([label, value]) => <div key={label}><span>{label}</span><strong>{numberText(value) || text(value)}</strong></div>)}</div> : null}
              <div className="wechat-detail-grid">
                <div><div className="section-title"><span>Top 排名文章</span><small>{articleRows.length ? `${articleRows.length} 篇` : '暂无'}</small></div>{articleRows.length ? <div className="article-list">{articleRows.map((article, index) => <button type="button" className="article-row" key={article.id ?? `${article.title}-${index}`} disabled={!article.id} onClick={() => openArticle(article)}><span className="rank">{article.rank || '—'}</span><span><strong>{article.title}</strong><small>{[article.account, article.url].filter(Boolean).join(' · ') || (article.id ? '来源信息不可用' : '缺少稳定 ID，详情不可用')}</small></span><span aria-hidden="true">{article.id ? '↗' : '—'}</span></button>)}</div> : <div className="compact-empty">当前快照没有排名文章。</div>}</div>
                <div><div className="section-title"><span>下拉词 / 关联词</span><small>{visibleRelatedTerms.length ? `${visibleRelatedTerms.length} 项` : '暂无'}</small></div>{visibleRelatedTerms.length ? <div className="term-list">{visibleRelatedTerms.map(([kind, item], index) => <span key={`${kind}-${text(item)}-${index}`}><b>{kind}</b>{text(item) || firstText(record(item), ['term', 'keyword', 'name']) || '未命名词项'}</span>)}</div> : <div className="compact-empty">当前快照没有关联词观测。</div>}</div>
              </div>
            </div> : <div className="empty-state"><strong>没有可展示的详情</strong></div>}
          </section>
        </div>
      </section>

      {articleData ? <section className="panel article-detail-panel"><div className="panel-heading"><div><p className="eyebrow">ARTICLE DETAIL</p><h3>{firstText({...articleData, ...record(articleData.article)}, ['title', 'name', 'headline']) || '文章详情'}</h3></div><button className="secondary-button" type="button" onClick={() => setArticleData(null)}>关闭</button></div>{articleData.error ? <div className="empty-state"><strong>正文不可用</strong><p>{text(articleData.error)}</p></div> : <div className="article-detail-content"><p>{firstText({...articleData, ...record(articleData.article)}, ['markdown_path', 'md_path', 'path', 'content_file_path']) ? `Markdown：${firstText({...articleData, ...record(articleData.article)}, ['markdown_path', 'md_path', 'path', 'content_file_path'])}` : '正文不可用，接口未返回 Markdown 路径。'}</p>{safeExternalUrl(firstText({...articleData, ...record(articleData.article)}, ['url', 'article_url', 'link', 'normalized_url', 'raw_url'])) ? <a href={safeExternalUrl(firstText({...articleData, ...record(articleData.article)}, ['url', 'article_url', 'link', 'normalized_url', 'raw_url']))} target="_blank" rel="noreferrer">打开原文 ↗</a> : null}</div>}</section> : null}
    </div>
  )
}
