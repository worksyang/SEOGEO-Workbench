import {useEffect, useMemo, useState} from 'react'
import {apiGet} from '../../api/client'
import type {OverviewResponse, StatusResponse} from '../../types'

type AnyRecord = Record<string, unknown>
type NavKey = 'wechat' | 'xhs' | 'geo' | 'mp'
type HomePageProps = {
  overview: OverviewResponse['data'] | null
  status: StatusResponse['data'] | null
  onNavigate: (key: NavKey) => void
}
type HomeSummary = {
  wechatKeywords: number | null
  mpAccounts: number | null
  xhsKeywords: number | null
  events: Array<{time: string; title: string; body: string}>
}
const EMPTY_SUMMARY: HomeSummary = {wechatKeywords: null, mpAccounts: null, xhsKeywords: null, events: []}
const MODULES = [
  {key: 'wechat' as const, mark: 'WX', title: '微信关键词', sub: '关键词视角 · 账号透视 · 文章 List', description: '观察关键词排名、文章快照、常态阅读和账号变化。'},
  {key: 'xhs' as const, mark: 'XHS', title: '小红书关键词', sub: '关键词视角 · 博主透视 · 笔记 List', description: '观察笔记、博主、互动数据和关键词变化。'},
  {key: 'geo' as const, mark: 'GEO', title: 'GEO 观察', sub: '问题 · 引用源', description: '观察问题回答、引用位次、来源和作者。'},
  {key: 'mp' as const, mark: 'MP', title: '公众号监控', sub: '公众号 · 执行任务 · 设置', description: '按账号配置采集任务，保留 Markdown 入库和日志。'},
]
function record(value: unknown): AnyRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as AnyRecord : {}
}
function list(value: unknown): AnyRecord[] {
  if (Array.isArray(value)) return value.map(record)
  const object = record(value)
  for (const key of ['items', 'data', 'rows', 'results', 'list']) {
    if (Array.isArray(object[key])) return object[key]!.map(record)
  }
  return []
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
function formatCount(value: number | null): string {
  return value === null ? '—' : new Intl.NumberFormat('zh-CN').format(value)
}
function timeOf(value: unknown): string {
  const parsed = new Date(typeof value === 'string' ? value : '').getTime()
  return Number.isFinite(parsed) ? new Intl.DateTimeFormat('zh-CN', {hour: '2-digit', minute: '2-digit'}).format(parsed) : '—'
}
export default function HomePage({overview, status, onNavigate}: HomePageProps) {
  const [summary, setSummary] = useState<HomeSummary>(EMPTY_SUMMARY)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  useEffect(() => {
    let cancelled = false
    Promise.allSettled([
      apiGet<{data?: AnyRecord}>('/api/v1/wechat/bootstrap'),
      apiGet<{data?: AnyRecord}>('/api/v1/mp/bootstrap'),
      apiGet<{data?: AnyRecord}>('/api/v1/xhs/bootstrap'),
      apiGet<{data?: AnyRecord}>('/api/v1/jobs?limit=20'),
    ]).then((results) => {
      if (cancelled) return
      const value = (index: number): AnyRecord => results[index]?.status === 'fulfilled' ? record(results[index].value.data) : {}
      const jobs = list(value(3).items)
      setSummary({
        wechatKeywords: firstNumber(record(value(0).summary), ['keyword_count', 'keywords_count', 'total_keywords']),
        mpAccounts: firstNumber(record(value(1).summary), ['account_count', 'accounts_count', 'total_accounts']),
        xhsKeywords: firstNumber(record(value(2).counts), ['keywords', 'keyword_count', 'total_keywords']),
        events: jobs.slice(0, 5).map((job) => ({
          time: timeOf(job.updated_at || job.created_at),
          title: String(job.job_type || job.system_key || '采集任务'),
          body: String(job.status || job.state || '已更新'),
        })),
      })
      setLoading(false)
      if (results.every((result) => result.status === 'rejected')) setError('模块数据尚未返回；首页仅展示已确认的 Hub 事实。')
    })
    return () => { cancelled = true }
  }, [])
  const systemStatus = useMemo(() => new Map((status?.connections ?? []).map((item) => [item.system_key, item.status])), [status])
  const cards = [
    ['微信监控词', summary.wechatKeywords, '微信适配器返回'],
    ['公众号监控', summary.mpAccounts, '账号数量'],
    ['小红书监控词', summary.xhsKeywords, '关键词数量'],
  ] as const
  return (
    <div className="demo-home">
      <div className="home-head">
        <div><div className="eyebrow">CONTENT OS / DISCOVERY & COLLECTION</div><h1>发现值得关注的内容，保留真实采集证据</h1><p className="home-lead">Content OS 只负责选题发现、外部观察和内容采集；内容资产、生产和发布由独立 Production OS 承担。</p></div>
        <div className="home-actions"><button className="demo-button primary" type="button" onClick={() => onNavigate('wechat')}>查看微信监控</button></div>
      </div>
      <div className="home-metrics" aria-label="Content OS 实时统计">
        {cards.map(([label, value, note]) => <div className="home-metric" key={label}><span>{label}</span><b>{loading ? '—' : formatCount(value)}</b><small>{error || note}</small></div>)}
      </div>
      <div className="home-grid">
        <section className="demo-card">
          <div className="demo-card-head"><span className="card-title">业务入口</span><span className="demo-tag green">Content OS</span></div>
          <div className="system-entry-list">
            {MODULES.map((module) => {
              const systemKey = module.key === 'wechat' ? 'wechat-search' : module.key === 'mp' ? 'wechat-mp' : module.key === 'xhs' ? 'xhs-search' : 'geo'
              const statusLabel = systemStatus.get(systemKey)
              return <button className="system-entry" key={module.key} type="button" onClick={() => onNavigate(module.key)}><span className="entry-mark">{module.mark}</span><span className="entry-copy"><b>{module.title}</b><small>{module.sub}</small></span><span className="entry-description">{module.description}</span><span className={`entry-status ${statusLabel || 'unknown'}`}>{statusLabel || '未检查'} <span aria-hidden="true">›</span></span></button>
            })}
          </div>
        </section>
        <section className="demo-card events-card">
          <div className="demo-card-head"><span className="card-title">采集任务事件</span><span className="demo-tag">{summary.events.length ? '真实任务' : '等待回执'}</span></div>
          {summary.events.length ? summary.events.map((event, index) => <div className="event" key={`${event.time}-${index}`}><time>{event.time}</time><p><b>{event.title}</b> {event.body}</p></div>) : <div className="home-empty-event"><strong>{loading ? '正在读取任务事件' : '暂无真实任务事件'}</strong><p>首页不使用静态事件冒充运行状态。</p></div>}
        </section>
      </div>
      <div className="home-runtime-note">Hub：{overview?.data_state === 'ready' ? '已返回真实数据' : '等待数据回执'} · 数据库：{status?.database.status || '未检查'}</div>
    </div>
  )
}
