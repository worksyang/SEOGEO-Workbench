import {useEffect, useMemo, useRef, useState} from 'react'
import {apiGet} from '../../api/client'
import type {
  GeoAnswer,
  GeoBootstrapData,
  GeoQuestion,
  GeoQuestionDetail,
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
  const [questionCategory, setQuestionCategory] = useState('all')
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
      const category = first(record(item), ['category', 'topic', 'group'], '')
      return (!needle || item.question.toLowerCase().includes(needle)) && statusMatch && (questionCategory === 'all' || category === questionCategory)
    }).sort((a, b) => {
      if (sort === 'citations') return (latestSnapshot(b.answers, b.latest_answer_id)?.relation_count ?? 0) - (latestSnapshot(a.answers, a.latest_answer_id)?.relation_count ?? 0)
      if (sort === 'snapshots') return b.answer_count - a.answer_count
      return String(b.latest_captured_at ?? '').localeCompare(String(a.latest_captured_at ?? ''))
    })
  }, [questionCategory, questionQuery, questionStatus, questions, sort])

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
  const answerRecord = record(answer)
  const citationCounts = countsFrom(answer?.citation_type_counts)
  const citationTypes = Object.keys(citationCounts)
  const visibleCitationEntries = Object.entries(citationCounts).filter(([key]) => citationTypeFilter === 'all' || key === citationTypeFilter)
  const citations = listFrom(answer?.citations, ['items', 'citations', 'results', 'rows'])
  const visibleCitations = citations.filter((item) => {
    const row = record(item)
    const type = text(row.type || row.citation_type)
    return citationTypeFilter === 'all' || type === citationTypeFilter
  }).sort((left, right) => number(record(left).position || record(left).rank, Number.MAX_SAFE_INTEGER) - number(record(right).position || record(right).rank, Number.MAX_SAFE_INTEGER))
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
  const questionCategories = Array.from(new Set(questions.map((item) => first(record(item), ['category', 'topic', 'group'], '')).filter(Boolean)))
  const sourcePlatforms = array(sourceOverview?.platforms).filter((item) => {
    const needle = sourceQuery.trim().toLowerCase()
    return !needle || JSON.stringify(item).toLowerCase().includes(needle)
  })
  const sourceCreators = array(sourceOverview?.creators).filter((item) => { const needle = sourceAuthor.trim().toLowerCase(); return !needle || JSON.stringify(item).toLowerCase().includes(needle) })
  const sourceTotals = sourceOverview?.totals ?? {}

  return <div className="geo-page demo-module-page">
    <header className="module-top geo-toolbar">
      <strong className="module-logo">GEO 公域引用观察</strong>
      <span className="sep" />
      <span className="module-meta">真实来源 · <b>{statusLabel(bootstrap?.source_status)}</b></span>
      <div className="module-switch" role="tablist" aria-label="GEO视角"><button className={`pill ${view === 'questions' ? 'active' : ''}`} onClick={openQuestions} type="button" role="tab" aria-selected={view === 'questions'}>问题观察</button><button className={`pill ${view === 'sources' ? 'active' : ''}`} onClick={openSources} type="button" role="tab" aria-selected={view === 'sources'}>引用源透视</button></div>
      <span className="module-right">{formatNumber(questions.length)} 个真实问题</span>
    </header>

    {state === 'loading' ? <div className="geo-state">正在读取 GEO 真实数据…</div> : state === 'offline' || state === 'error' ? <div className="geo-state geo-state-error"><strong>{state === 'offline' ? 'GEO 当前离线' : 'GEO 读取失败'}</strong><span>{error}</span></div> : view === 'questions' ? <div className="geo-layout geo-question-layout">
      <aside className="geo-left">
        <div className="geo-tools">
          <div className="monitor-tool-row"><input className="monitor-search" aria-label="搜索 GEO 问题" value={questionQuery} onChange={(event) => setQuestionQuery(event.target.value)} placeholder="搜索问题" /><span className="tag">{visibleQuestions.length}/{questions.length}</span></div>
          <div className="sort-tabs">{Object.entries(SORT_LABELS).map(([key, label]) => <button className={`pill ${sort === key ? 'active' : ''}`} key={key} type="button" onClick={() => setSort(key)}>{label}</button>)}</div>
          <div className="filter-line"><span className="filter-label">类目</span><div className="filter-chips"><button className={`chip ${questionCategory === 'all' ? 'active' : ''}`} type="button" onClick={() => setQuestionCategory('all')}>全部</button>{questionCategories.map((item) => <button className={`chip ${questionCategory === item ? 'active' : ''}`} key={item} type="button" onClick={() => setQuestionCategory(item)}>{item}</button>)}</div></div>
          <div className="filter-line"><span className="filter-label">状态</span><div className="filter-chips"><button className={`chip ${questionStatus === 'all' ? 'active' : ''}`} type="button" onClick={() => setQuestionStatus('all')}>全部</button>{questionsStatuses.map((item) => <button className={`chip ${questionStatus === item ? 'active' : ''}`} key={item} type="button" onClick={() => setQuestionStatus(item)}>{statusLabel(item)}</button>)}</div></div>
        </div>
        <div className="geo-list" aria-live="polite">{visibleQuestions.length ? visibleQuestions.map((item, index) => { const latest = latestSnapshot(item.answers, item.latest_answer_id); return <button key={item.question_id} type="button" className={`geo-item ${selectedQuestionId === item.question_id ? 'active' : ''}`} onClick={() => setSelectedQuestionId(item.question_id)}><span className="rank">{index + 1}</span><span className="geo-main"><strong className="geo-title">{item.question}</strong><span className="geo-tags">{Object.entries(item.status_counts).map(([key, value]) => <span className="tag" key={key}>{statusLabel(key)} {formatNumber(value)}</span>)}</span></span><span className="geo-score"><b>{latest ? formatNumber(latest.relation_count) : '—'}</b><span>引用</span></span></button> }) : <div className="compact-empty">没有匹配问题。</div>}</div>
      </aside>

      <main className="geo-right">
        {!selectedQuestionId ? <div className="geo-state">选择一个问题查看真实快照。</div> : <>
          <section className="card geo-hero">
            <div className="kh-top"><div><h2>{first(record(detail?.summary), ['question']) || selectedQuestion?.question}</h2><p>{selectedQuestion ? `首次 ${dateText(selectedQuestion.first_captured_at)} · 最近 ${dateText(selectedQuestion.latest_captured_at)}` : '时间信息不可用'}</p></div><span className={`tag ${statusTone(answer?.status) === 'healthy' ? 'green' : statusTone(answer?.status) === 'degraded' ? 'amber' : statusTone(answer?.status) === 'offline' ? 'red' : ''}`}>{statusLabel(answer?.status)}</span></div>
            <div className="geo-stats"><div className="geo-stat"><b>{snapshots.length || '—'}</b><span>快照</span></div><div className="geo-stat"><b>{selectedSnapshot ? formatNumber(selectedSnapshot.relation_count) : '—'}</b><span>引用关系</span></div><div className="geo-stat"><b>{selectedSnapshot ? formatNumber(selectedSnapshot.source_count) : '—'}</b><span>来源</span></div><div className="geo-stat"><b>{selectedSnapshot ? formatNumber(selectedSnapshot.platform_count) : '—'}</b><span>平台</span></div><div className="geo-stat"><b>{selectedSnapshot ? formatNumber(selectedSnapshot.creator_count) : '—'}</b><span>作者</span></div></div>
          </section>

          <section className="card snapshot-card geo-snapshot-card">
            <div className="card-head"><strong className="card-title">采集快照</strong><span className="subtle">只读回执</span></div>
            <div className="snap-strip" role="tablist" aria-label="GEO回答快照">{snapshots.length ? snapshots.map((snapshot) => <button key={snapshot.id} type="button" role="tab" aria-selected={selectedSnapshot?.id === snapshot.id} className={`geo-snap ${selectedSnapshot?.id === snapshot.id ? 'active' : ''}`} onClick={() => setSelectedSnapshotId(snapshot.id)}><b>{dateText(snapshot.captured_at)}</b><span>{statusLabel(snapshot.status)}</span><span>{snapshot.markdown_available ? '有 Markdown' : '缺 Markdown'}</span></button>) : <span className="subtle">暂无回答快照</span>}</div>
            <div className="source-rail">{platformSummary.length || creatorSummary.length ? <>{platformSummary.map((item, index) => { const row = record(item); return <div className="source-pill" key={`platform-${index}`}><div className="source-logo">{first(row, ['canonical_platform', 'platform', 'name'], '—').slice(0, 2)}<i>{displayValue(valueFrom(row, ['relation_count', 'citation_count', 'count']))}</i></div><span>{first(row, ['canonical_platform', 'platform', 'name'], '未知平台')}</span></div> })}{creatorSummary.map((item, index) => { const row = record(item); const profile = safeUrl(row.profile_url || row.homepage || row.url); const name = first(row, ['author', 'name', 'creator_name'], '未知作者'); return <div className="source-pill" key={`creator-${index}`}><div className="source-logo">{name.slice(0, 2)}</div><span>{profile ? <a href={profile} target="_blank" rel="noreferrer">{name}</a> : name}</span></div> })}</> : <span className="subtle">当前快照暂无平台或作者来源。</span>}</div>
          </section>

          <section className="card answer-card">
            <div className="answer-head"><strong className="card-title">AI 回答原文</strong><span className="subtle">{selectedSnapshot ? dateText(selectedSnapshot.captured_at) : '—'}</span></div>
            {answerState === 'loading' ? <div className="compact-empty">正在读取快照正文…</div> : answerState === 'error' ? <div className="compact-empty">当前快照读取失败，未使用其他快照正文。</div> : !markdown.exists ? <div className="compact-empty">当前快照缺少 Markdown 正文，未回退到其他快照。</div> : markdown.content ? <SafeMarkdown markdown={markdown.content} /> : <div className="compact-empty">当前快照标记有 Markdown，但正文为空。</div>}
          </section>

          <section className="card geo-ranking-card">
            <div className="card-head"><strong className="card-title">当前快照引用源榜单</strong><select className="geo-inline-select" value={citationTypeFilter} onChange={(event) => setCitationTypeFilter(event.target.value)}><option value="all">全部类型</option>{citationTypes.map((key) => <option key={key} value={key}>{CITATION_LABELS[key] ?? key}</option>)}</select></div>
            <table className="source-table"><thead><tr><th>位次</th><th>来源</th><th>平台</th><th>作者</th><th>指标</th></tr></thead><tbody>{visibleCitations.map((item, index) => { const row = record(item); const source = record(row.source); const sourceUrl = safeUrl(valueFrom(source, ['url', 'canonical_url']) || valueFrom(row, ['url', 'source_url'])); const authorUrl = safeUrl(valueFrom(source, ['author_profile_link', 'profile_url']) || valueFrom(row, ['author_profile_link', 'profile_url'])); const title = text(valueFrom(source, ['title']) || valueFrom(row, ['title', 'source_title']), '未命名来源'); const author = text(valueFrom(source, ['author', 'author_name']) || valueFrom(row, ['author', 'author_name']), '—'); return <tr key={index}><td>{text(valueFrom(row, ['position', 'rank']), '—')}</td><td>{sourceUrl ? <a href={sourceUrl} target="_blank" rel="noreferrer">{title}</a> : title}<small>{CITATION_LABELS[text(row.type || row.citation_type)] ?? text(row.type || row.citation_type, '引用')}</small></td><td>{text(valueFrom(source, ['raw_platform']) || valueFrom(row, ['raw_platform']), '—')}<small>{text(valueFrom(source, ['canonical_platform']) || valueFrom(row, ['canonical_platform']), '—')}</small></td><td>{authorUrl ? <a href={authorUrl} target="_blank" rel="noreferrer">{author}</a> : author}</td><td>{[['阅', 'read'], ['赞', 'like'], ['评', 'comment'], ['藏', 'favorite'], ['享', 'share']].map(([label, key]) => `${label}${displayValue(citationMetric(source, row, key))}`).join(' · ')}</td></tr> })}</tbody></table>{!visibleCitations.length && <div className="compact-empty">当前筛选没有引用明细。</div>}
          </section>

          <section className="card geo-extension-card"><div className="card-head"><strong className="card-title">回答工具与扩展事实</strong><span className="subtle">当前快照</span></div><div className="geo-metric-grid"><Metric label="引用关系" value={selectedSnapshot?.relation_count} /><Metric label="来源数" value={selectedSnapshot?.source_count} /><Metric label="平台数" value={selectedSnapshot?.platform_count} /><Metric label="作者数" value={selectedSnapshot?.creator_count} /><Metric label="观测时间" value={dateText(answer?.metrics_observed_at)} /></div><div className="geo-subsection"><strong>工具链</strong><div className="geo-tag-list">{tools.length ? tools.map((tool, index) => { const row = record(tool); return <span className="geo-tag" key={index}>{first(row, ['type'], '未命名工具')}{text(row.content) ? ` · ${text(row.content)}` : ''}</span> }) : <small>暂无工具链记录</small>}</div></div><div className="geo-subsection"><strong>原始搜索词</strong><p>{rawSearchTerms.length ? rawSearchTerms.join('、') : '未返回原始搜索词'}</p></div><div className="geo-subsection"><strong>推荐追问</strong><div className="geo-followups">{suggestedQuestions.length ? suggestedQuestions.map((item, index) => <span key={index}>{item}</span>) : <small>暂无推荐追问</small>}</div></div><div className="geo-citation-groups">{visibleCitationEntries.length ? visibleCitationEntries.map(([key, value]) => <div key={key}><span>{CITATION_LABELS[key] ?? key}</span><strong>{formatNumber(value)}</strong></div>) : <div className="compact-empty">当前快照没有引用类型统计。</div>}</div></section>

          <section className="card geo-extension-card"><div className="card-head"><strong className="card-title">平台与作者事实</strong><span className="subtle">真实关系汇总</span></div><div className="geo-entity-columns"><div><strong>平台</strong>{platformSummary.length ? platformSummary.map((item, index) => <div className="geo-entity-row" key={index}><span>{first(record(item), ['canonical_platform', 'platform', 'name'], '未命名平台')}<small>raw：{displayList(record(item).raw_platforms)}</small></span><b>{displayValue(valueFrom(record(item), ['relation_count', 'citation_count', 'count']))}</b></div>) : <small>暂无平台分布</small>}</div><div><strong>作者</strong>{creatorSummary.length ? creatorSummary.map((item, index) => { const row = record(item); const profile = safeUrl(row.profile_url || row.homepage || row.url); const name = first(row, ['author', 'name', 'creator_name'], '未命名作者'); return <div className="geo-entity-row" key={index}><span>{profile ? <a href={profile} target="_blank" rel="noreferrer">{name}</a> : name}</span><b>{displayValue(valueFrom(row, ['relation_count', 'citation_count', 'count']))}</b></div> }) : <small>暂无作者记录</small>}</div></div></section>

          <section className="card geo-extension-card"><div className="card-head"><strong className="card-title">真实引用位次矩阵</strong><span className="subtle">{formatNumber(detail?.totals?.relation_count ?? 0)} 条引用</span></div>{detail?.citation_matrix?.columns?.length && detail.citation_matrix.rows?.length ? <div className="geo-matrix-wrap"><table className="geo-matrix"><thead><tr><th>来源</th>{detail.citation_matrix.columns.map((column) => <th key={text(column.answer_id)}><span>{matrixColumnLabel(record(column))}</span><small>{text(column.answer_id, 'answer_id 缺失')}</small></th>)}</tr></thead><tbody>{detail.citation_matrix.rows.map((row, index) => <tr key={index}><th><strong>{text(row.title, `来源 ${index + 1}`)}</strong><small>{text(row.canonical_platform || row.raw_platform)}{text(row.author) ? ` · ${text(row.author)}` : ''}</small>{safeUrl(row.url) && <a href={safeUrl(row.url)} target="_blank" rel="noreferrer">打开来源</a>}</th>{row.ranks.map((rank, rankIndex) => <td key={rankIndex}>{rank === null || rank === undefined ? '—' : rank}</td>)}</tr>)}</tbody></table></div> : <div className="compact-empty">当前问题没有返回完整位次矩阵。</div>}</section>
        </>}
      </main>
    </div> : <div className="geo-layout geo-sources-layout">
      <aside className="geo-left">
        <div className="geo-tools"><div className="filter-line"><span className="filter-label">平台</span><input className="monitor-search" value={sourcePlatform} onChange={(event) => setSourcePlatform(event.target.value)} placeholder="canonical 精确筛选" /></div><div className="filter-line"><span className="filter-label">搜索</span><input className="monitor-search" value={sourceQuery} onChange={(event) => setSourceQuery(event.target.value)} placeholder="平台名或 raw 别名" /></div></div>
        <div className="geo-list">{sourcePlatforms.map((item, index) => { const row = record(item); return <div className="geo-item source-overview-item" key={index}><span className="rank">{index + 1}</span><span className="geo-main"><strong className="geo-title">{first(row, ['canonical_platform', 'canonical', 'name'], '未命名平台')}</strong><span className="geo-tags"><span className="tag">raw {displayList(row.raw_platforms, '—')}</span></span></span><span className="geo-score"><b>{displayValue(row.share_of_citations)}</b><span>引用占比</span></span></div> })}{sourceState === 'loading' && <div className="compact-empty">正在读取平台来源…</div>}{sourceState === 'empty' && <div className="compact-empty">暂无平台来源。</div>}</div>
      </aside>
      <main className="geo-right"><section className="card geo-hero"><h2>作者与来源透视</h2><p>平台和作者均来自 `/api/v1/geo/source-overview` 真实回执。</p><div className="geo-stats">{Object.entries(sourceTotals).slice(0, 5).map(([key, value]) => <div className="geo-stat" key={key}><b>{displayValue(value)}</b><span>{key}</span></div>)}</div></section><section className="card geo-extension-card"><div className="card-head"><strong className="card-title">作者列表</strong><input className="monitor-search source-author-search" value={sourceAuthor} onChange={(event) => setSourceAuthor(event.target.value)} placeholder="搜索作者或主页" /></div><div className="geo-source-list">{sourceCreators.map((item, index) => { const row = record(item); const homepage = safeUrl(row.profile_url || row.homepage || row.url); const name = first(row, ['name', 'creator_name', 'author'], '未命名作者'); return <div className="geo-source-row" key={index}><div><strong>{homepage ? <a href={homepage} target="_blank" rel="noreferrer">{name}</a> : name}</strong><small>source {formatNumber(row.source_count)} · citation {formatNumber(row.citation_count)} · question {formatNumber(row.question_count)} · 最佳位次 {text(row.best_rank, '—')} · {dateText(row.first_cited_at || row.first_captured_at)} / {dateText(row.last_cited_at || row.latest_captured_at)}</small></div></div> })}{sourceState === 'error' && <div className="compact-empty">{error || '来源透视读取失败'}</div>}{sourceState === 'ready' && !sourceCreators.length && <div className="compact-empty">暂无匹配作者。</div>}</div></section></main>
    </div>}
  </div>
}
