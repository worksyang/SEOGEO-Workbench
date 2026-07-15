import {useEffect, useMemo, useState} from 'react'
import {apiGet} from '../../api/client'
import type {OverviewResponse, StatusResponse} from '../../types'

type AnyRecord = Record<string, unknown>
type NavKey = 'wechat' | 'mp' | 'xhs' | 'geo' | 'wiki' | 'mother' | 'batch' | 'publish'

type HomePageProps = {
  overview: OverviewResponse['data'] | null
  status: StatusResponse['data'] | null
  onNavigate: (key: NavKey) => void
}

type HomeSummary = {
  wechatKeywords: number | null
  mpAccounts: number | null
  xhsKeywords: number | null
  motherArticles: number | null
  publishedToday: number | null
  writingJobs: number | null
  events: Array<{time: string; title: string; body: string}>
}

const EMPTY_SUMMARY: HomeSummary = {
  wechatKeywords: null,
  mpAccounts: null,
  xhsKeywords: null,
  motherArticles: null,
  publishedToday: null,
  writingJobs: null,
  events: [],
}

const MODULES: Array<{
  key: NavKey
  mark: string
  title: string
  sub: string
  description: string
}> = [
  {key: 'wechat', mark: 'WX', title: '微信关键词', sub: '关键词视角 · 账号透视 · 文章 List', description: '左侧关键词榜单，右侧排名快照、常态阅读、阅读增量和账号透视。'},
  {key: 'mp', mark: 'MP', title: '公众号监控', sub: '公众号 · 执行任务 · 设置', description: '按分类选择公众号，配置抓取任务，查看日志并保留 Markdown 入库。'},
  {key: 'xhs', mark: 'XHS', title: '小红书关键词', sub: '关键词视角 · 博主透视 · 笔记 List', description: '沿用关键词监控结构，将文章、账号和阅读指标换为笔记、博主与互动数据。'},
  {key: 'geo', mark: 'GEO', title: 'GEO 观察', sub: '问题 · 引用源', description: '左侧问题或平台列表，右侧回答快照、引用位次、来源和作者。'},
  {key: 'wiki', mark: 'MD', title: '母文章 Wiki', sub: '文件树 · Markdown 正文 · 编辑 · 图片 OCR', description: '保留原来的知识库与编辑器形态，只把数据访问切到安全 Repository。'},
  {key: 'mother', mark: 'WM', title: '母文章铸造', sub: '母文章决策 · 素材三态 · 成稿', description: '保留项目列表、三阶段工作区、Wiki 素材和 URL 临时素材流程。'},
  {key: 'batch', mark: 'BT', title: '批量成稿', sub: '关键词 · 母文章匹配 · 成稿队列', description: '批次独立管理，支持 0..N 母文章匹配、缺口回跳和任务恢复。'},
  {key: 'publish', mark: 'PUB', title: '写作与发布', sub: '写作处理 · 账号 · 发布调度', description: '保留旧五步发布流程，首版默认只开放预览、草稿和 dry-run。'},
]

function record(value: unknown): AnyRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as AnyRecord : {}
}

function numberValue(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && value.trim() && Number.isFinite(Number(value))) return Number(value)
  return null
}

function firstNumber(source: AnyRecord, keys: string[]): number | null {
  for (const key of keys) {
    const value = numberValue(source[key])
    if (value !== null) return value
  }
  return null
}

function list(value: unknown): AnyRecord[] {
  if (Array.isArray(value)) return value.map(record)
  const object = record(value)
  for (const key of ['items', 'data', 'rows', 'results', 'list']) {
    if (Array.isArray(object[key])) return object[key]!.map(record)
  }
  return []
}

function countTree(nodes: unknown): number {
  return list(nodes).reduce((total, node) => {
    const files = Array.isArray(node.files) ? node.files.length : 0
    return total + files + countTree(node.sub_dirs)
  }, 0)
}

function formatCount(value: number | null): string {
  return value === null ? '—' : new Intl.NumberFormat('zh-CN').format(value)
}

function dateTime(value: unknown): {time: string; sort: number} {
  const raw = typeof value === 'string' ? value : ''
  const timestamp = raw ? new Date(raw).getTime() : NaN
  if (!Number.isFinite(timestamp)) return {time: '—', sort: 0}
  return {
    time: new Intl.DateTimeFormat('zh-CN', {hour: '2-digit', minute: '2-digit'}).format(timestamp),
    sort: timestamp,
  }
}

function jobLabel(job: AnyRecord): {title: string; body: string} {
  const type = String(job.job_type || job.task_type || '任务')
  const title = type.includes('wechat') || type.includes('search') ? '微信监控'
    : type.includes('geo') ? 'GEO'
      : type.includes('writing') || type.includes('mother') || type.includes('batch') ? '内容生产'
        : type.includes('publish') ? '发布系统'
          : String(job.system_key || '工作台任务')
  const body = String(job.status || job.state || '已更新')
  return {title, body}
}

export default function HomePage({overview, status, onNavigate}: HomePageProps) {
  const [summary, setSummary] = useState<HomeSummary>(EMPTY_SUMMARY)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    const requests = [
      apiGet<{data?: AnyRecord}>('/api/v1/wechat/bootstrap'),
      apiGet<{data?: AnyRecord}>('/api/v1/mp/bootstrap'),
      apiGet<{data?: AnyRecord}>('/api/v1/xhs/bootstrap'),
      apiGet<{data?: unknown}>('/api/v1/wiki/tree'),
      apiGet<{data?: AnyRecord}>('/api/v1/writing/jobs?limit=200'),
      apiGet<{data?: AnyRecord}>('/api/v1/publishing/attempts?limit=200'),
      apiGet<{data?: AnyRecord}>('/api/v1/jobs?limit=20'),
    ]
    Promise.allSettled(requests).then((results) => {
      if (cancelled) return
      const value = (index: number): AnyRecord => {
        const result = results[index]
        return result?.status === 'fulfilled' ? record(result.value.data) : {}
      }
      const wechat = value(0)
      const mp = value(1)
      const xhs = value(2)
      const wikiResult = results[3]?.status === 'fulfilled' ? results[3].value.data : null
      const writing = value(4)
      const publishing = value(5)
      const jobs = value(6)
      const writingItems = list(writing.items)
      const attempts = list(publishing.items)
      const today = new Date().toISOString().slice(0, 10)
      const successfulToday = attempts.filter((item) => {
        const attemptedAt = String(item.attempted_at || '')
        return attemptedAt.startsWith(today) && ['success', 'succeeded', 'draft_saved'].includes(String(item.status).toLowerCase())
      }).length
      const events = list(jobs.items)
        .map((job) => {
          const parsed = dateTime(job.updated_at || job.created_at || job.attempted_at)
          const label = jobLabel(job)
          return {time: parsed.time, sort: parsed.sort, ...label}
        })
        .sort((left, right) => right.sort - left.sort)
        .slice(0, 5)
        .map(({time, title, body}) => ({time, title, body}))
      setSummary({
        wechatKeywords: firstNumber(record(wechat.summary), ['keyword_count', 'keywords_count', 'total_keywords']),
        mpAccounts: firstNumber(record(mp.summary), ['account_count', 'accounts_count', 'total_accounts']),
        xhsKeywords: firstNumber(record(xhs.counts), ['keywords', 'keyword_count', 'total_keywords']),
        motherArticles: countTree(wikiResult),
        publishedToday: attempts.length ? successfulToday : null,
        writingJobs: writingItems.length,
        events,
      })
      setLoading(false)
      if (results.every((result) => result.status === 'rejected')) setError('模块数据尚未返回；首页仅展示已确认的 Hub 事实。')
    })
    return () => {
      cancelled = true
    }
  }, [])

  const systemStatus = useMemo(() => new Map((status?.connections ?? []).map((item) => [item.system_key, item.status])), [status])
  const cards = [
    ['微信监控词', summary.wechatKeywords, '已由微信适配器返回'],
    ['公众号监控', summary.mpAccounts, '账号数量'],
    ['小红书监控词', summary.xhsKeywords, '关键词数量'],
    ['母文章', summary.motherArticles, 'Wiki 当前索引'],
    ['今日发布', summary.publishedToday, summary.publishedToday === null ? '等待发布回执' : '成功/草稿回执'],
  ] as const

  return (
    <div className="demo-home">
      <div className="home-head">
        <div>
          <div className="eyebrow">CONTENTOS / SYSTEM LAUNCHER</div>
          <h1>所有熟悉的系统，放进同一个入口</h1>
          <p className="home-lead">每套系统的内部页面、工作习惯和核心结构不变。统一层只提供导航、状态和未来的数据交接。</p>
        </div>
        <div className="home-actions">
          <button className="demo-button" type="button" onClick={() => onNavigate('wechat')}>查看微信监控</button>
          <button className="demo-button primary" type="button" onClick={() => onNavigate('mother')}>进入母文章铸造</button>
        </div>
      </div>

      <div className="home-metrics" aria-label="统一工作台实时统计">
        {cards.map(([label, value, note]) => (
          <div className="home-metric" key={label}>
            <span>{label}</span>
            <b>{loading ? '—' : formatCount(value)}</b>
            <small>{error || note}</small>
          </div>
        ))}
      </div>

      <div className="home-grid">
        <section className="demo-card">
          <div className="demo-card-head"><span className="card-title">系统入口</span><span className="demo-tag green">不重写原结构</span></div>
          <div className="system-entry-list">
            {MODULES.map((module) => {
              const systemKey = module.key === 'wechat' ? 'wechat-search'
                : module.key === 'mp' ? 'wechat-mp'
                  : module.key === 'xhs' ? 'xhs-search'
                    : module.key === 'publish' ? 'publishing'
                      : module.key === 'geo' ? 'geo'
                        : module.key === 'wiki' ? 'wiki'
                          : undefined
              const statusLabel = systemKey ? systemStatus.get(systemKey) : undefined
              return (
                <button className="system-entry" key={module.key} type="button" onClick={() => onNavigate(module.key)}>
                  <span className="entry-mark">{module.mark}</span>
                  <span className="entry-copy"><b>{module.title}</b><small>{module.sub}</small></span>
                  <span className="entry-description">{module.description}</span>
                  <span className={`entry-status ${statusLabel || 'unknown'}`}>{statusLabel || '未检查'} <span aria-hidden="true">›</span></span>
                </button>
              )
            })}
          </div>
        </section>

        <section className="demo-card events-card">
          <div className="demo-card-head"><span className="card-title">今天正在发生</span><span className="demo-tag">{summary.events.length ? '真实任务' : '等待回执'}</span></div>
          {summary.events.length ? summary.events.map((event, index) => (
            <div className="event" key={`${event.time}-${index}`}>
              <time>{event.time}</time>
              <p><b>{event.title}</b> {event.body}</p>
            </div>
          )) : (
            <div className="home-empty-event">
              <strong>{loading ? '正在读取任务事件' : '暂无真实任务事件'}</strong>
              <p>首页不使用 Demo 静态事件冒充运行状态。</p>
            </div>
          )}
        </section>
      </div>

      <div className="home-runtime-note">
        Hub：{overview?.data_state === 'ready' ? '已返回真实数据' : '等待数据回执'} · 数据库：{status?.database.status || '未检查'} · 当前运行任务：{formatCount(summary.writingJobs)}
      </div>
    </div>
  )
}
