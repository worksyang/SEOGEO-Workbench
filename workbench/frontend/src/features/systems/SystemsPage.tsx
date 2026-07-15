import {useEffect, useMemo, useState} from 'react'
import {apiGet, apiRequest, ApiError} from '../../api/client'

type Connection = {
  system_key: string
  display_name: string
  status: string
  last_checked_at: string
  base_url?: string | null
  capabilities: string[]
  details: Record<string, unknown>
}
type StatusResponse = {
  ok: boolean
  data: {
    service: {name: string; version: string; bind: string; frontend_built: boolean}
    database: {status: string; integrity: string; schema_version: number; missing_core_tables: string[]}
    connections: Connection[]
    readonly_contract: {source: boolean; demo: boolean}
  }
}

type PageState = 'loading' | 'ready' | 'offline' | 'error'

function record(value: unknown, fallback: Record<string, unknown> = {}): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : fallback
}
function text(value: unknown, fallback = ''): string {
  return typeof value === 'string' || typeof value === 'number' ? String(value) : fallback
}

const STATUS_LABELS: Record<string, {label: string; tag: string}> = {
  healthy: {label: '健康', tag: 'green'},
  ready: {label: '健康', tag: 'green'},
  online: {label: '健康', tag: 'green'},
  degraded: {label: '降级', tag: 'amber'},
  partial: {label: '历史回放', tag: 'amber'},
  unknown: {label: '待接入', tag: 'gray'},
  offline: {label: '离线', tag: 'red'},
  unavailable: {label: '不可达', tag: 'red'},
}

export default function SystemsPage() {
  const [state, setState] = useState<PageState>('loading')
  const [error, setError] = useState('')
  const [data, setData] = useState<StatusResponse['data'] | null>(null)
  const [signalSummary, setSignalSummary] = useState<Record<string, number> | null>(null)
  const [jobsCount, setJobsCount] = useState<number | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    setState('loading')
    apiGet<StatusResponse>('/api/v1/system/status', controller.signal)
      .then((res) => {
        setData(res?.data || null)
        setState('ready')
      })
      .catch((err) => {
        if (err instanceof ApiError) {
          setError(err.message)
          setState('offline')
        } else {
          setError(String(err))
          setState('error')
        }
      })

    apiGet<{ok: boolean; data: {summary: Record<string, number>; total: number}}>('/api/v1/signals?limit=200')
      .then((res) => setSignalSummary(res?.data?.summary || {}))
      .catch(() => undefined)

    apiGet<{ok: boolean; data: {items: unknown[]}}>('/api/v1/jobs?limit=200')
      .then((res) => setJobsCount((res?.data?.items || []).length))
      .catch(() => undefined)

    return () => controller.abort()
  }, [])

  const sortedConnections = useMemo(() => {
    if (!data) return []
    return [...data.connections].sort((a, b) => a.system_key.localeCompare(b.system_key))
  }, [data])

  return (
    <div className="module-frame demo-module-page systems-page">
      <header className="module-top">
        <strong className="module-logo">系统状态 · 七套连接</strong>
        <span className="sep" />
        <span className="module-meta">
          数据库 <b>{data?.database.status ?? '—'}</b> · SQLite migration <b>{data?.database.schema_version ?? '—'}</b>
        </span>
        <span className="module-right">{data?.service.bind || '127.0.0.1:8799'}</span>
      </header>

      {state === 'offline' && (
        <div className="module-placeholder">
          <strong>Hub 暂时不可达</strong>
          <span>{error}</span>
        </div>
      )}

      {data && (
        <>
          <section className="systems-summary">
            <article>
              <span>v3.3 架构</span>
              <b>已对齐</b>
              <small>14 张业务核心表 · Markdown + SQLite</small>
            </article>
            <article>
              <span>SQLite migration</span>
              <b>v{data.database.schema_version}</b>
              <small>数据库完整性：{data.database.integrity}</small>
            </article>
            <article>
              <span>信号累计</span>
              <b>{signalSummary ? Object.values(signalSummary).reduce((a, b) => a + b, 0) : '—'}</b>
              <small>last 200 from /signals</small>
            </article>
            <article>
              <span>持久任务</span>
              <b>{jobsCount ?? '—'}</b>
              <small>queued / running / completed</small>
            </article>
            <article>
              <span>服务端</span>
              <b>{data.service.version}</b>
              <small>
                源码 <span className={data.readonly_contract.source ? 'tag green' : 'tag amber'}>保护</span>·
                Demo <span className={data.readonly_contract.demo ? 'tag green' : 'tag amber'}>保护</span>
              </small>
            </article>
          </section>

          <section className="systems-connections panel">
            <div className="panel-heading">
              <div>
                <p className="eyebrow">七套系统适配器</p>
                <h3>真实连接状态</h3>
              </div>
              <span className="count-pill">{sortedConnections.length} 已登记</span>
            </div>
            {sortedConnections.length === 0 ? (
              <div className="empty-block">暂无连接记录</div>
            ) : (
              <table className="data-table">
                <thead>
                  <tr>
                    <th>系统</th>
                    <th>状态</th>
                    <th>上游地址</th>
                    <th>能力</th>
                    <th>最后检查</th>
                  </tr>
                </thead>
                <tbody>
                  {sortedConnections.map((conn) => {
                    const meta = STATUS_LABELS[conn.status] || STATUS_LABELS.unknown
                    return (
                      <tr key={conn.system_key}>
                        <td>
                          <strong>{conn.display_name}</strong>
                          <small>{conn.system_key}</small>
                        </td>
                        <td>
                          <span className={`tag ${meta.tag}`}>{meta.label}</span>
                        </td>
                        <td>{conn.base_url || '—'}</td>
                        <td>{(conn.capabilities || []).join(' · ') || '—'}</td>
                        <td>{conn.last_checked_at ? new Date(conn.last_checked_at).toLocaleString('zh-CN') : '—'}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            )}
          </section>
        </>
      )}
    </div>
  )
}
