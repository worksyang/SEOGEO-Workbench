import {useEffect, useMemo, useRef, useState} from 'react'
import {ApiError, apiGet, apiRequest} from '../../api/client'
import type {
  GeoAnswer,
  GeoBootstrapData,
  GeoQuestion,
  GeoQuestionDetail,
  GeoRefreshResult,
  GeoSnapshot,
  GeoSourceOverview,
} from '../../types'

type PageState = 'loading' | 'ready' | 'empty' | 'offline' | 'error'
type RecordValue = Record<string, unknown>

const STATUS_LABELS: Record<string, string> = {
  healthy: '健康', ready: '健康', online: '健康', degraded: '降级', partial: '降级',
  offline: '离线', unavailable: '离线', running: '运行中', completed: '已完成',
  failed: '失败', blocked: '受阻', pending: '等待中',
  input_not_ready: '输入未就绪',
}
const SORT_LABELS: Record<string, string> = {recent: '最近', citations: '引用数', snapshots: '快照数'}
const CITATION_LABELS: Record<string, string> = {
  search_result: '搜索结果',
  text_reference: '文本引用',
  image_reference: '图片引用',
  related_video: '相关视频',
}

function record(value: unknown): RecordValue {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as RecordValue : {}
}
function text(value: unknown, fallback = ''): string {
  return typeof value === 'string' || typeof value === 'number' ? String(value) : fallback
}
function number(value: unknown, fallback = 0): number {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : fallback
}
function array(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}
function first(row: RecordValue, keys: string[], fallback = ''): string {
  for (const key of keys) {
    const result = text(row[key])
    if (result) return result
  }
  return fallback
}
function dateText(value: unknown): string {
  const raw = text(value)
  if (!raw) return '时间不可用'
  const date = new Date(raw)
  return Number.isNaN(date.getTime()) ? raw : new Intl.DateTimeFormat('zh-CN', {dateStyle: 'short', timeStyle: 'short'}).format(date)
}
function formatNumber(value: unknown): string {
  return new Intl.NumberFormat('zh-CN').format(number(value))
}
function displayValue(value: unknown): string {
  if (typeof value === 'number' && Number.isFinite(value)) return formatNumber(value)
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? formatNumber(parsed) : value
  }
  return '—'
}
function statusTone(value: unknown): string {
  const normalized = text(record(value).status || value, 'unknown').toLowerCase()
  if (['healthy', 'ready', 'online', 'completed'].includes(normalized)) return 'healthy'
  if (['degraded', 'partial', 'blocked', 'pending', 'running'].includes(normalized)) return 'degraded'
  if (['offline', 'unavailable', 'failed', 'error'].includes(normalized)) return 'offline'
  return 'unknown'
}
function statusLabel(value: unknown): string {
  const normalized = text(record(value).status || value, 'unknown').toLowerCase()
  return STATUS_LABELS[normalized] ?? normalized
}
function safeUrl(value: unknown): string {
  const raw = text(value).trim()
  if (!/^https?:\/\//i.test(raw)) return ''
  try {
    const url = new URL(raw)
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : ''
  } catch { return '' }
}
function listFrom(value: unknown, keys: string[] = ['items', 'data', 'rows', 'results', 'list']): unknown[] {
  if (Array.isArray(value)) return value
  const object = record(value)
  for (const key of keys) if (Array.isArray(object[key])) return object[key] as unknown[]
  return []
}
function displayList(value: unknown, fallback = '—'): string {
  if (Array.isArray(value)) {
    const values = value.flatMap((item) => typeof item === 'string' || typeof item === 'number' ? [String(item)] : [])
    return values.length ? values.join('、') : fallback
  }
  return text(value, fallback)
}
function valueFrom(row: RecordValue, keys: string[]): unknown {
  for (const key of keys) if (row[key] !== undefined && row[key] !== null && row[key] !== '') return row[key]
  return undefined
}
function citationMetric(source: RecordValue, row: RecordValue, key: string): unknown {
  const metricKey: Record<string, string> = {
    read: 'read_count',
    like: 'like_count',
    comment: 'comment_count',
    favorite: 'favorite_count',
    share: 'share_count',
  }
  const backendKey = metricKey[key] ?? key
  return valueFrom(record(source.metrics), [backendKey]) ?? valueFrom(record(row.metrics), [backendKey]) ?? valueFrom(source, [backendKey]) ?? valueFrom(row, [backendKey])
}
function dataOf<T>(response: {data?: T} | T): T {
  return record(response).data !== undefined ? (record(response).data as T) : response as T
}
function countsFrom(value: unknown): Record<string, number> {
  return Object.fromEntries(Object.entries(record(value)).map(([key, item]) => [key, number(item)]))
}
function normalizeSnapshot(value: unknown): GeoSnapshot {
  const row = record(value)
  return {
    id: first(row, ['id', 'snapshot_id', 'answer_id']),
    status: first(row, ['status', 'state'], 'unknown'),
    captured_at: text(row.captured_at || row.created_at) || null,
    markdown_available: row.markdown_available === true || row.markdown_available === 1,
    relation_count: number(row.relation_count),
    source_count: number(row.source_count),
    platform_count: number(row.platform_count),
    creator_count: number(row.creator_count),
    relation_type_counts: countsFrom(row.relation_type_counts),
    ...row,
  }
}
function normalizeQuestion(value: unknown): GeoQuestion {
  const row = record(value)
  return {
    question_id: first(row, ['question_id', 'id']),
    question: first(row, ['question', 'title', 'query'], '未命名问题'),
    answer_count: number(row.answer_count),
    first_captured_at: text(row.first_captured_at) || null,
    latest_captured_at: text(row.latest_captured_at) || null,
    latest_answer_id: text(row.latest_answer_id) || null,
    status_counts: countsFrom(row.status_counts),
    answers: array(row.answers).map(normalizeSnapshot),
    ...row,
  }
}
function normalizeAnswer(value: unknown): GeoAnswer {
  const row = record(value)
  return {id: first(row, ['id', 'answer_id', 'snapshot_id']), ...row} as GeoAnswer
}
function latestSnapshot(snapshots: GeoSnapshot[], latestId?: string | null): GeoSnapshot | null {
  if (latestId) {
    const matched = snapshots.find((snapshot) => snapshot.id === latestId)
    if (matched) return matched
  }
  return [...snapshots].sort((a, b) => String(b.captured_at ?? '').localeCompare(String(a.captured_at ?? '')))[0] ?? null
}
function matrixColumnLabel(column: RecordValue): string {
  const captured = dateText(column.captured_at)
  const status = statusLabel(column.status)
  return `${captured} · ${status}`
}
function markdownContent(value: unknown): {exists: boolean; content: string; path: string} {
  if (typeof value === 'string') return {exists: Boolean(value), content: value, path: ''}
  const row = record(value)
  return {
    exists: row.exists === true,
    content: typeof row.content === 'string' ? row.content : '',
    path: text(row.path),
  }
}
function searchKeywords(value: unknown): string[] {
  return listFrom(value, ['items', 'tools', 'results']).flatMap((item, itemIndex) => {
    if (typeof item === 'string') return [item]
    const row = record(item)
    const nestedKeywords = Array.isArray(row.search_keywords) ? row.search_keywords : row.search_keywords ? [row.search_keywords] : []
    return nestedKeywords.map((keyword, keywordIndex) => {
      if (typeof keyword === 'string') return {keyword, position: keywordIndex}
      const keywordRow = record(keyword)
      return [{keyword: text(keywordRow.keyword), position: number(keywordRow.position, keywordIndex)}]
    }).flat().filter((keyword) => keyword.keyword).sort((a, b) => a.position - b.position).map((keyword) => keyword.keyword)
  })
}

function InlineText({value}: {value: string}) {
  const nodes: React.ReactNode[] = []
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\(([^)]+)\)|https?:\/\/[^\s)]+)/g
  let cursor = 0
  value.replace(pattern, (match, _group, url, offset) => {
    nodes.push(value.slice(cursor, offset))
    if (match.startsWith('**')) nodes.push(<strong key={`${offset}-b`}>{match.slice(2, -2)}</strong>)
    else if (match.startsWith('`')) nodes.push(<code key={`${offset}-c`}>{match.slice(1, -1)}</code>)
    else {
      const href = safeUrl(url || match)
      nodes.push(href ? <a key={`${offset}-a`} href={href} target="_blank" rel="noreferrer">{url ? match.slice(1, match.indexOf(']')) : match}</a> : match)
    }
    cursor = offset + match.length
    return match
  })
  nodes.push(value.slice(cursor))
  return <>{nodes}</>
}

function SafeMarkdown({markdown}: {markdown: string}) {
  const blocks = markdown.replace(/\r\n?/g, '\n').split(/\n{2,}/)
  return <div className="geo-markdown">
    {blocks.map((block, index) => {
      const lines = block.split('\n').filter(Boolean)
      if (!lines.length) return null
      const heading = lines[0].match(/^(#{1,3})\s+(.+)$/)
      if (heading) {
        const Tag = `h${heading[1].length}` as 'h1' | 'h2' | 'h3'
        return <Tag key={index}><InlineText value={heading[2]} /></Tag>
      }
      const ordered = lines.every((line) => /^\d+\.\s+/.test(line))
      const unordered = lines.every((line) => /^[-*+]\s+/.test(line))
      if (ordered || unordered) {
        const Tag = ordered ? 'ol' : 'ul'
        return <Tag key={index}>{lines.map((line, lineIndex) => <li key={lineIndex}><InlineText value={line.replace(ordered ? /^\d+\.\s+/ : /^[-*+]\s+/, '')} /></li>)}</Tag>
      }
      return <p key={index}>{lines.map((line, lineIndex) => <span key={lineIndex}>{lineIndex > 0 && <br />}<InlineText value={line} /></span>)}</p>
    })}
  </div>
}

function Metric({label, value}: {label: string; value: unknown}) {
  return <div className="geo-mini-metric"><span>{label}</span><strong>{displayValue(value)}</strong></div>
}

export default function GeoPage({onSourceStatus}: {onSourceStatus: (status: string) => void}) {
  const [view, setView] = useState<'questions' | 'sources'>('questions')
  const [bootstrap, setBootstrap] = useState<GeoBootstrapData | null>(null)
  const [questions, setQuestions] = useState<GeoQuestion[]>([])
  const [state, setState] = useState<PageState>('loading')
  const [error, setError] = useState('')
  const [questionQuery, setQuestionQuery] = useState('')
  const [questionStatus, setQuestionStatus] = useState('all')
  const [sort, setSort] = useState('recent')
  const [selectedQuestionId, setSelectedQuestionId] = useState('')
  const [detail, setDetail] = useState<GeoQuestionDetail | null>(null)
  const [selectedSnapshotId, setSelectedSnapshotId] = useState('')
  const [answer, setAnswer] = useState<GeoAnswer | null>(null)
  const [answerState, setAnswerState] = useState<PageState>('empty')
  const [sourceState, setSourceState] = useState<PageState>('empty')
  const [sourceOverview, setSourceOverview] = useState<GeoSourceOverview | null>(null)
  const [sourceQuery, setSourceQuery] = useState('')
  const [sourcePlatform, setSourcePlatform] = useState('')
  const [sourceAuthor, setSourceAuthor] = useState('')
  const [citationTypeFilter, setCitationTypeFilter] = useState('all')
  const [refreshState, setRefreshState] = useState<'idle' | 'previewing' | 'confirming' | 'done' | 'blocked' | 'error'>('idle')
  const [refreshResult, setRefreshResult] = useState<GeoRefreshResult | null>(null)
  const detailRequest = useRef(0)
  const answerRequest = useRef(0)

  const loadBootstrap = (signal: AbortSignal) => {
    setState('loading'); setError('')
    Promise.all([
      apiGet<{data?: GeoBootstrapData}>('/api/v1/geo/bootstrap', signal),
      apiGet<{items?: unknown[]; data?: unknown[]}>('/api/v1/geo/questions?limit=10000', signal),
    ]).then(([bootstrapResponse, questionsResponse]) => {
      const nextBootstrap = dataOf<GeoBootstrapData>(bootstrapResponse)
      const rawQuestions = listFrom(questionsResponse.items ?? questionsResponse.data ?? questionsResponse)
      const nextQuestions = rawQuestions.map(normalizeQuestion).filter((item) => item.question_id)
      setBootstrap(nextBootstrap)
      setQuestions(nextQuestions)
      const source = text(record(nextBootstrap.source_status).status || nextBootstrap.source_status, 'unknown')
      onSourceStatus(source)
      setState(statusTone(source) === 'offline' ? 'offline' : nextQuestions.length ? 'ready' : 'empty')
    }).catch((reason: unknown) => {
      if (reason instanceof DOMException && reason.name === 'AbortError') return
      setState('offline'); setError(reason instanceof Error ? reason.message : 'GEO 服务暂时无法连接'); onSourceStatus('offline')
    })
  }
  useEffect(() => {
    const controller = new AbortController()
    loadBootstrap(controller.signal)
    return () => controller.abort()
  }, [])

  const visibleQuestions = useMemo(() => {
    const needle = questionQuery.trim().toLowerCase()
    return questions.filter((item) => {
      const statusMatch = questionStatus === 'all' || Object.keys(item.status_counts).includes(questionStatus)
      return (!needle || item.question.toLowerCase().includes(needle)) && statusMatch
    }).sort((a, b) => {
      if (sort === 'citations') return (latestSnapshot(b.answers, b.latest_answer_id)?.relation_count ?? 0) - (latestSnapshot(a.answers, a.latest_answer_id)?.relation_count ?? 0)
      if (sort === 'snapshots') return b.answer_count - a.answer_count
      return String(b.latest_captured_at ?? '').localeCompare(String(a.latest_captured_at ?? ''))
    })
  }, [questionQuery, questionStatus, questions, sort])

  useEffect(() => {
    if (!selectedQuestionId && visibleQuestions[0]) setSelectedQuestionId(visibleQuestions[0].question_id)
    if (selectedQuestionId && !visibleQuestions.some((item) => item.question_id === selectedQuestionId)) setSelectedQuestionId(visibleQuestions[0]?.question_id ?? '')
  }, [selectedQuestionId, visibleQuestions])

  useEffect(() => {
    if (!selectedQuestionId) { setDetail(null); setAnswer(null); return }
    const controller = new AbortController()
    const requestId = ++detailRequest.current
    setDetail(null); setAnswer(null); setAnswerState('loading'); setSelectedSnapshotId('')
    apiGet<{data?: GeoQuestionDetail}>(`/api/v1/geo/questions/${encodeURIComponent(selectedQuestionId)}`, controller.signal)
      .then((response) => {
        if (requestId !== detailRequest.current) return
        const next = dataOf<GeoQuestionDetail>(response)
        setDetail(next)
        const snapshots = array(next.snapshots).map(normalizeSnapshot)
        const question = questions.find((item) => item.question_id === selectedQuestionId)
        const fallback = latestSnapshot(snapshots, question?.latest_answer_id) ?? latestSnapshot(question?.answers ?? [], question?.latest_answer_id)
        setSelectedSnapshotId(fallback?.id ?? '')
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === 'AbortError') return
        setAnswerState('error'); setError(reason instanceof Error ? reason.message : '问题详情读取失败')
      })
    return () => controller.abort()
  }, [selectedQuestionId])

  const snapshots = useMemo(() => (array(detail?.snapshots).map(normalizeSnapshot).length
    ? array(detail?.snapshots).map(normalizeSnapshot)
    : questions.find((item) => item.question_id === selectedQuestionId)?.answers ?? []), [detail, questions, selectedQuestionId])
  const selectedQuestion = questions.find((item) => item.question_id === selectedQuestionId)
  const selectedSnapshot = snapshots.find((item) => item.id === selectedSnapshotId) ?? latestSnapshot(snapshots, selectedQuestion?.latest_answer_id)

  useEffect(() => {
    if (!selectedSnapshot?.id) { setAnswer(null); setAnswerState('empty'); return }
    const controller = new AbortController()
    const requestId = ++answerRequest.current
    setAnswerState('loading'); setAnswer(null); setCitationTypeFilter('all')
    apiGet<{data?: GeoAnswer}>(`/api/v1/geo/answers/${encodeURIComponent(selectedSnapshot.id)}`, controller.signal)
      .then((response) => {
        if (requestId !== answerRequest.current) return
        setAnswer(dataOf<GeoAnswer>(response)); setAnswerState('ready')
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === 'AbortError') return
        setAnswerState('error')
      })
    return () => controller.abort()
  }, [selectedSnapshot?.id])

  useEffect(() => {
    if (view !== 'sources') return
    const controller = new AbortController()
    setSourceState('loading')
    const params = new URLSearchParams({limit: '500', offset: '0'})
    if (sourcePlatform) params.set('platform', sourcePlatform)
    if (sourceAuthor) params.set('q', sourceAuthor)
    apiGet<{data?: GeoSourceOverview}>(`/api/v1/geo/source-overview?${params.toString()}`, controller.signal)
      .then((response) => {
        const next = dataOf<GeoSourceOverview>(response)
        setSourceOverview(next)
        setSourceState((array(next.platforms).length || array(next.creators).length) ? 'ready' : 'empty')
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === 'AbortError') return
        setSourceState('error'); setError(reason instanceof Error ? reason.message : '来源透视读取失败')
      })
    return () => controller.abort()
  }, [view, sourceAuthor, sourcePlatform])

  const openSources = () => { setView('sources'); setSourceState('empty') }
  const openQuestions = () => setView('questions')
  const refresh = async () => {
    if (!selectedSnapshot?.id || refreshState === 'previewing' || refreshState === 'confirming') return
    setRefreshState('previewing'); setRefreshResult(null)
    try {
      const response = await apiRequest<{data?: GeoRefreshResult}>(`/api/v1/geo/answers/${encodeURIComponent(selectedSnapshot.id)}/refresh/preview`, 'POST')
      const result = dataOf<GeoRefreshResult>(response)
      setRefreshResult(result)
      if (result.available === true) {
        if (!window.confirm(result.message || '确认提交真实 GEO 刷新？该操作可能产生计费。')) { setRefreshState('idle'); return }
        setRefreshState('confirming')
        const confirmed = await apiRequest<{data?: GeoRefreshResult}>(`/api/v1/geo/answers/${encodeURIComponent(selectedSnapshot.id)}/refresh/confirm`, 'POST', {confirm: true})
        setRefreshResult(dataOf<GeoRefreshResult>(confirmed)); setRefreshState('done')
      } else {
        setRefreshState('blocked')
      }
    } catch (reason) {
      setRefreshState('error'); setRefreshResult({message: reason instanceof ApiError || reason instanceof Error ? reason.message : '刷新预览失败'})
    }
  }

  const answerRecord = record(answer)
  const citationCounts = countsFrom(answer?.citation_type_counts)
  const citationTypes = Object.keys(citationCounts)
  const visibleCitationEntries = Object.entries(citationCounts).filter(([key]) => citationTypeFilter === 'all' || key === citationTypeFilter)
  const citations = listFrom(answer?.citations, ['items', 'citations', 'results', 'rows'])
  const visibleCitations = citations.filter((item) => {
    const row = record(item)
    const type = text(row.type || row.citation_type)
    return citationTypeFilter === 'all' || type === citationTypeFilter
  })
  const platformSummary = listFrom(answer?.platform_summary)
  const creatorSummary = listFrom(answer?.creator_summary)
  const tools = listFrom(answer?.tools_nested, ['tools', 'items', 'results'])
  const markdown = markdownContent(answer?.markdown)
  const toolSearchTerms = searchKeywords(answer?.tools_nested)
  const rawSearchTerms = toolSearchTerms.length
    ? toolSearchTerms
    : [first(answerRecord, ['search_query', 'query', 'raw_query', 'search_term'])].filter(Boolean)
  const suggestedQuestions = array(answerRecord.suggested_questions).map((item) => typeof item === 'string' ? item : first(record(item), ['question'])).filter(Boolean)
  const questionsStatuses = Array.from(new Set(questions.flatMap((item) => Object.keys(item.status_counts))))
  const sourcePlatforms = array(sourceOverview?.platforms).filter((item) => {
    const needle = sourceQuery.trim().toLowerCase()
    return !needle || JSON.stringify(item).toLowerCase().includes(needle)
  })
  const sourceCreators = array(sourceOverview?.creators).filter((item) => { const needle = sourceAuthor.trim().toLowerCase(); return !needle || JSON.stringify(item).toLowerCase().includes(needle) })
  const sourceTotals = sourceOverview?.totals ?? {}

  return <div className="geo-page">
    <div className="geo-toolbar">
      <div>
        <p className="eyebrow">GEO / REAL OBSERVATION</p>
        <h2>回答如何被看见</h2>
        <p className="geo-lede">问题、回答快照、引用来源与位次矩阵全部来自 Hub 实时接口。</p>
      </div>
      <div className="geo-view-switch" role="tablist" aria-label="GEO视角">
        <button className={view === 'questions' ? 'active' : ''} onClick={openQuestions} type="button" role="tab" aria-selected={view === 'questions'}>问题观察</button>
        <button className={view === 'sources' ? 'active' : ''} onClick={openSources} type="button" role="tab" aria-selected={view === 'sources'}>来源透视</button>
      </div>
    </div>

    {state === 'loading' && <div className="geo-state">正在读取 GEO 真实数据…</div>}
    {state === 'offline' && <div className="geo-state geo-state-error"><strong>GEO 当前离线</strong><span>{error}</span></div>}
    {state === 'error' && <div className="geo-state geo-state-error">{error}</div>}
    {state === 'empty' && view === 'questions' && <div className="geo-state"><strong>暂无问题观察数据</strong><span>接口已返回，但当前没有可展示的问题或快照。</span></div>}

    {view === 'questions' && state !== 'loading' && state !== 'offline' && state !== 'error' && <div className="geo-question-layout">
      <aside className="geo-question-sidebar panel">
        <div className="panel-heading"><div><p className="eyebrow">QUESTION SET</p><h3>问题列表</h3></div><span className="count-pill">{formatNumber(questions.length)}</span></div>
        <div className="geo-filters">
          <label>搜索问题<input value={questionQuery} onChange={(event) => setQuestionQuery(event.target.value)} placeholder="输入问题关键词" /></label>
          <div className="geo-filter-row"><label>状态<select value={questionStatus} onChange={(event) => setQuestionStatus(event.target.value)}><option value="all">全部状态</option>{questionsStatuses.map((item) => <option key={item} value={item}>{statusLabel(item)}</option>)}</select></label><label>排序<select value={sort} onChange={(event) => setSort(event.target.value)}>{Object.entries(SORT_LABELS).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label></div>
        </div>
        <div className="geo-question-list" aria-live="polite">{visibleQuestions.map((item) => <button key={item.question_id} type="button" className={`geo-question-row ${selectedQuestionId === item.question_id ? 'selected' : ''}`} onClick={() => setSelectedQuestionId(item.question_id)}><strong>{item.question}</strong><small>{formatNumber(item.answer_count)} 个快照 · {dateText(item.latest_captured_at)}</small><span>{Object.entries(item.status_counts).map(([key, value]) => `${statusLabel(key)} ${formatNumber(value)}`).join(' · ') || '状态不可用'}</span></button>)}{!visibleQuestions.length && <div className="empty-state"><strong>没有匹配问题</strong><p>调整搜索词或状态筛选。</p></div>}</div>
      </aside>
      <section className="geo-detail">
        {!selectedQuestionId ? <div className="geo-state">选择一个问题查看真实快照。</div> : <><article className="panel geo-question-summary"><div><p className="eyebrow">QUESTION</p><h3>{first(record(detail?.summary), ['question']) || questions.find((item) => item.question_id === selectedQuestionId)?.question}</h3></div><div className="geo-summary-metrics"><Metric label="快照" value={snapshots.length} /><Metric label="最近捕获" value={dateText(selectedSnapshot?.captured_at)} /></div></article>
          <article className="panel"><div className="panel-heading"><div><p className="eyebrow">SNAPSHOTS</p><h3>回答快照</h3></div><button className="secondary-button" type="button" onClick={() => void refresh()} disabled={!selectedSnapshot?.id || refreshState === 'previewing' || refreshState === 'confirming'}>{refreshState === 'previewing' ? '检查刷新条件…' : '预览刷新'}</button></div><div className="geo-snapshot-tabs" role="tablist" aria-label="回答快照">{snapshots.map((snapshot) => <button key={snapshot.id} type="button" role="tab" aria-selected={selectedSnapshot?.id === snapshot.id} className={selectedSnapshot?.id === snapshot.id ? 'active' : ''} onClick={() => setSelectedSnapshotId(snapshot.id)}><strong>{dateText(snapshot.captured_at)}</strong><span>{statusLabel(snapshot.status)} · {snapshot.markdown_available ? '有 Markdown' : '缺 Markdown'}</span></button>)}</div>{refreshResult && <div className={`geo-refresh-receipt ${refreshState === 'blocked' || refreshState === 'error' ? 'is-error' : ''}`} role="status"><strong>{refreshResult.blocked_reason ? `刷新受阻：${refreshResult.blocked_reason}` : refreshState === 'done' ? '刷新请求已由上游确认' : '刷新预览结果'}</strong><span>{refreshResult.message || '上游未提供说明。'}</span></div>}</article>
          <div className="geo-detail-grid"><article className="panel"><div className="panel-heading"><div><p className="eyebrow">ANSWER</p><h3>当前回答</h3></div><span className={`status-chip ${statusTone(answer?.status)}`}>{statusLabel(answer?.status)}</span></div>{answerState === 'loading' && <div className="geo-inline-state">正在读取快照正文…</div>}{answerState === 'error' && <div className="geo-inline-state geo-state-error">当前快照读取失败，未使用其他快照正文。</div>}{answerState === 'ready' && !markdown.exists && <div className="geo-inline-state">当前快照缺少 Markdown 正文，未回退到其他快照。</div>}{answerState === 'ready' && markdown.exists && markdown.content && <SafeMarkdown markdown={markdown.content} />}{answerState === 'ready' && markdown.exists && !markdown.content && <div className="geo-inline-state">当前快照标记有 Markdown，但正文为空，未回退到其他快照。</div>}</article>
            <article className="panel"><div className="panel-heading"><div><p className="eyebrow">OBSERVATION</p><h3>当前观测</h3></div></div><div className="geo-metric-grid"><Metric label="引用关系" value={selectedSnapshot?.relation_count} /><Metric label="来源数" value={selectedSnapshot?.source_count} /><Metric label="平台数" value={selectedSnapshot?.platform_count} /><Metric label="作者数" value={selectedSnapshot?.creator_count} /><Metric label="观测时间" value={dateText(answer?.metrics_observed_at)} /></div><div className="geo-subsection"><strong>工具链</strong><div className="geo-tag-list">{tools.length ? tools.map((tool, index) => { const row = record(tool); return <span className="geo-tag" key={index}>{first(row, ['type'], '未命名工具')}{text(row.content) ? ` · ${text(row.content)}` : ''}</span> }) : <small>暂无工具链记录</small>}</div></div><div className="geo-subsection"><strong>原始搜索词</strong><p>{rawSearchTerms.length ? rawSearchTerms.join('、') : '未返回原始搜索词'}</p></div></article></div>
          <div className="geo-detail-grid"><article className="panel"><div className="panel-heading"><div><p className="eyebrow">CITATIONS</p><h3>引用分组</h3></div><select className="geo-inline-select" value={citationTypeFilter} onChange={(event) => setCitationTypeFilter(event.target.value)}><option value="all">全部类型</option>{citationTypes.map((key) => <option key={key} value={key}>{CITATION_LABELS[key] ?? key}</option>)}</select></div><div className="geo-citation-groups">{visibleCitationEntries.length ? visibleCitationEntries.map(([key, value]) => <div key={key}><span>{CITATION_LABELS[key] ?? key}</span><strong>{formatNumber(value)}</strong></div>) : <div className="geo-inline-state">当前快照没有引用类型统计。</div>}</div><div className="geo-subsection"><strong>推荐追问</strong><div className="geo-followups">{suggestedQuestions.length ? suggestedQuestions.map((item, index) => <span key={index}>{item}</span>) : <small>暂无推荐追问</small>}</div></div><div className="geo-subsection"><strong>引用明细</strong><div className="geo-citation-list">{visibleCitations.length ? visibleCitations.map((item, index) => { const row = record(item); const source = record(row.source); const sourceUrl = safeUrl(valueFrom(source, ['url', 'canonical_url']) || valueFrom(row, ['url', 'source_url'])); const authorLink = safeUrl(valueFrom(source, ['author_profile_link', 'profile_url']) || valueFrom(row, ['author_profile_link', 'profile_url'])); return <div className="geo-citation-detail" key={index}><div className="geo-citation-detail-head"><strong>{text(valueFrom(row, ['position', 'rank']), '—')}</strong><span>{CITATION_LABELS[text(row.type || row.citation_type)] ?? text(row.type || row.citation_type, '引用')}</span></div><div className="geo-citation-detail-source">{sourceUrl ? <a href={sourceUrl} target="_blank" rel="noreferrer">{text(valueFrom(source, ['title']) || valueFrom(row, ['title', 'source_title']), '未命名来源')}</a> : <strong>{text(valueFrom(source, ['title']) || valueFrom(row, ['title', 'source_title']), '未命名来源')}</strong>}<small>{text(valueFrom(source, ['raw_platform']) || valueFrom(row, ['raw_platform']), '—')} → {text(valueFrom(source, ['canonical_platform']) || valueFrom(row, ['canonical_platform']), '—')}</small></div><div className="geo-citation-detail-author">{authorLink ? <a href={authorLink} target="_blank" rel="noreferrer">{text(valueFrom(source, ['author', 'author_name']) || valueFrom(row, ['author', 'author_name']), '未命名作者')}</a> : text(valueFrom(source, ['author', 'author_name']) || valueFrom(row, ['author', 'author_name']), '未命名作者')}</div><div className="geo-citation-metrics">{[['阅读', 'read'], ['点赞', 'like'], ['评论', 'comment'], ['收藏', 'favorite'], ['分享', 'share']].map(([label, key]) => <span key={key}>{label} {displayValue(citationMetric(source, row, key))}</span>)}</div></div> }) : <small>当前快照没有引用明细。</small>}</div></div></article><article className="panel"><div className="panel-heading"><div><p className="eyebrow">ENTITIES</p><h3>平台与作者</h3></div></div><div className="geo-entity-columns"><div><strong>当前平台分布</strong>{platformSummary.length ? platformSummary.map((item, index) => <div className="geo-entity-row" key={index}><span>{first(record(item), ['canonical_platform', 'platform', 'name', 'canonical_name'], '未命名平台')}<small>raw：{displayList(record(item).raw_platforms)}</small></span><b>{displayValue(valueFrom(record(item), ['relation_count', 'citation_count', 'count']))}</b></div>) : <small>暂无平台分布</small>}</div><div><strong>作者与主页</strong>{creatorSummary.length ? creatorSummary.map((item, index) => <div className="geo-entity-row" key={index}><span>{safeUrl(record(item).profile_url || record(item).homepage || record(item).url) ? <a href={safeUrl(record(item).profile_url || record(item).homepage || record(item).url)} target="_blank" rel="noreferrer">{first(record(item), ['author', 'name', 'creator_name'], '未命名作者')}</a> : first(record(item), ['author', 'name', 'creator_name'], '未命名作者')}</span><b>{displayValue(valueFrom(record(item), ['relation_count', 'citation_count', 'count']))}</b></div>) : <small>暂无作者记录</small>}</div></div></article></div>
          <article className="panel"><div className="panel-heading"><div><p className="eyebrow">RANK MATRIX</p><h3>真实引用位次矩阵</h3></div><span className="count-pill">{formatNumber(detail?.totals?.relation_count ?? 0)} 条引用</span></div>{detail?.citation_matrix?.columns?.length && detail.citation_matrix.rows?.length ? <div className="geo-matrix-wrap"><table className="geo-matrix"><thead><tr><th>来源</th>{detail.citation_matrix.columns.map((column) => <th key={text(column.answer_id)}><span>{matrixColumnLabel(record(column))}</span><small>{text(column.answer_id, 'answer_id 缺失')}</small></th>)}</tr></thead><tbody>{detail.citation_matrix.rows.map((row, index) => <tr key={index}><th><strong>{text(row.title, `来源 ${index + 1}`)}</strong><small>{text(row.canonical_platform || row.raw_platform)}{text(row.author) ? <> · {safeUrl(row.author_profile_link) ? <a href={safeUrl(row.author_profile_link)} target="_blank" rel="noreferrer">{text(row.author)}</a> : text(row.author)}</> : ''}</small>{safeUrl(row.url) && <a href={safeUrl(row.url)} target="_blank" rel="noreferrer">打开来源</a>}</th>{row.ranks.map((rank, rankIndex) => <td key={rankIndex}>{rank === null || rank === undefined ? '—' : rank}</td>)}</tr>)}</tbody></table></div> : <div className="geo-inline-state">当前问题没有返回完整位次矩阵。</div>}</article>
        </>}
      </section>
    </div>}

    {view === 'sources' && <section className="geo-source-view"><div className="geo-source-filters panel"><label>平台筛选<input value={sourcePlatform} onChange={(event) => { setSourcePlatform(event.target.value); setSourceState('empty') }} placeholder="canonical 或 raw 别名" /></label><label>搜索平台<input value={sourceQuery} onChange={(event) => { setSourceQuery(event.target.value) }} placeholder="按平台名或别名搜索" /></label><label>搜索作者<input value={sourceAuthor} onChange={(event) => { setSourceAuthor(event.target.value); setSourceState('empty') }} placeholder="按作者名或主页搜索" /></label><div className="geo-source-totals">{Object.entries(sourceTotals).map(([key, value]) => <Metric key={key} label={key} value={value} />)}</div></div>{sourceState === 'loading' && <div className="geo-state">正在读取来源透视…</div>}{sourceState === 'error' && <div className="geo-state geo-state-error">{error}</div>}{sourceState === 'empty' && <div className="geo-state">暂无来源透视数据。</div>}{sourceState === 'ready' && <div className="geo-source-grid"><article className="panel"><div className="panel-heading"><div><p className="eyebrow">PLATFORMS</p><h3>平台列表</h3></div><span className="count-pill">{formatNumber(sourcePlatforms.length)}</span></div><div className="geo-source-list">{sourcePlatforms.map((item, index) => { const row = record(item); return <div className="geo-source-row" key={index}><div><strong>{first(row, ['canonical_platform', 'canonical', 'canonical_name', 'platform', 'name'], '未命名平台')}</strong><small>raw：{displayList(row.raw_platforms, '无 raw 别名')}</small></div><b>{displayValue(row.share_of_citations)}</b></div> })}</div></article><article className="panel"><div className="panel-heading"><div><p className="eyebrow">CREATORS</p><h3>作者列表</h3></div><span className="count-pill">{formatNumber(sourceCreators.length)}</span></div><div className="geo-source-list">{sourceCreators.map((item, index) => { const row = record(item); const homepage = safeUrl(row.profile_url || row.homepage || row.url); return <div className="geo-source-row" key={index}><div><strong>{homepage ? <a href={homepage} target="_blank" rel="noreferrer">{first(row, ['name', 'creator_name', 'author'], '未命名作者')}</a> : first(row, ['name', 'creator_name', 'author'], '未命名作者')}</strong><small>source {formatNumber(row.source_count)} · citation {formatNumber(row.citation_count)} · question {formatNumber(row.question_count)} · 最佳位次 {text(row.best_rank, '—')} · {dateText(row.first_cited_at || row.first_captured_at)} / {dateText(row.last_cited_at || row.latest_captured_at)}</small></div></div> })}</div></article></div>}</section>}
  </div>
}
