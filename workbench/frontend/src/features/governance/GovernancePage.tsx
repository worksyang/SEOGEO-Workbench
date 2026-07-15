import {useEffect, useState} from 'react'
import {apiGet, apiRequest, ApiError} from '../../api/client'

type Tab = 'identity' | 'states' | 'lineage' | 'locks' | 'backups'

function record(value: unknown, fallback: Record<string, unknown> = {}): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : fallback
}
function text(value: unknown, fallback = ''): string {
  return typeof value === 'string' || typeof value === 'number' ? String(value) : fallback
}
function array<T = unknown>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : []
}

export default function GovernancePage() {
  const [tab, setTab] = useState<Tab>('identity')
  const [identity, setIdentity] = useState<{items: Array<Record<string, unknown>>; total: number}>({items: [], total: 0})
  const [states, setStates] = useState<{columns: Record<string, Array<Record<string, unknown>>>; total: number}>({columns: {}, total: 0})
  const [lineage, setLineage] = useState<{nodes: Array<Record<string, unknown>>; total: number}>({nodes: [], total: 0})
  const [locks, setLocks] = useState<{connections: Array<Record<string, unknown>>; audit: Array<Record<string, unknown>>}>({connections: [], audit: []})
  const [reconcile, setReconcile] = useState<{results: Array<Record<string, unknown>>; total: number; errors: number; warnings: number}>({results: [], total: 0, errors: 0, warnings: 0})
  const [backups, setBackups] = useState<{items: Array<Record<string, unknown>>; total: number; verifiable: number}>({items: [], total: 0, verifiable: 0})
  const [backupAction, setBackupAction] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    apiGet<{ok: boolean; data: typeof identity}>('/api/v1/governance/identity?limit=20', controller.signal)
      .then((res) => setIdentity(res?.data || {items: [], total: 0}))
      .catch((err) => {
        if (!(err instanceof ApiError)) setError(String(err))
      })
    apiGet<{ok: boolean; data: typeof states}>('/api/v1/governance/states?limit=30', controller.signal)
      .then((res) => setStates(res?.data || {columns: {}, total: 0}))
      .catch(() => undefined)
    apiGet<{ok: boolean; data: typeof lineage}>('/api/v1/governance/lineage', controller.signal)
      .then((res) => setLineage(res?.data || {nodes: [], total: 0}))
      .catch(() => undefined)
    apiGet<{ok: boolean; data: typeof locks}>('/api/v1/governance/locks', controller.signal)
      .then((res) => setLocks(res?.data || {connections: [], audit: []}))
      .catch(() => undefined)
    apiGet<{ok: boolean; data: {results: Array<Record<string, unknown>>; total: number; errors: number; warnings: number}}>('/api/v1/governance/reconcile')
      .then((res) => {
        setReconcile(res?.data || {results: [], total: 0, errors: 0, warnings: 0})
      })
      .catch(() => undefined)
    apiGet<{ok: boolean; data: typeof backups}>('/api/v1/governance/backups', controller.signal)
      .then((res) => setBackups(res?.data || {items: [], total: 0, verifiable: 0}))
      .catch(() => undefined)
    return () => controller.abort()
  }, [])

  async function createBackup() {
    setBackupAction('正在创建并验证…')
    try {
      const response = await apiRequest<{ok: boolean; data: {backup: Record<string, unknown>; reused: boolean}}>('/api/v1/governance/backups', 'POST', {label: 'online'})
      setBackupAction(response.data.reused ? '已复用现有可验证备份' : '已创建并验证在线备份')
      const refreshed = await apiGet<{ok: boolean; data: typeof backups}>('/api/v1/governance/backups')
      setBackups(refreshed.data)
    } catch (err) {
      setBackupAction(err instanceof Error ? err.message : '备份失败')
    }
  }

  async function drillBackup(name: string) {
    setBackupAction(`正在演练 ${name}…`)
    try {
      const response = await apiRequest<{ok: boolean; data: Record<string, unknown>}>(`/api/v1/governance/backups/${encodeURIComponent(name)}/restore-drill`, 'POST', {operator: 'user'})
      setBackupAction(response.data.runtime_database_unchanged ? '恢复演练通过，运行库未被覆盖' : '恢复演练未确认运行库状态')
    } catch (err) {
      setBackupAction(err instanceof Error ? err.message : '恢复演练失败')
    }
  }

  return (
    <div className="module-frame demo-module-page governance-page">
      <header className="module-top">
        <strong className="module-logo">数据治理</strong>
        <span className="sep" />
        <span className="module-meta">
          候选 <b>{identity.total}</b> · 任务 <b>{states.total}</b> · 对账 <b>{reconcile.total}</b>
        </span>
        <div className="module-switch">
          <button className={`pill ${tab === 'identity' ? 'active' : ''}`} onClick={() => setTab('identity')}>
            实体映射
          </button>
          <button className={`pill ${tab === 'states' ? 'active' : ''}`} onClick={() => setTab('states')}>
            任务状态
          </button>
          <button className={`pill ${tab === 'lineage' ? 'active' : ''}`} onClick={() => setTab('lineage')}>
            数据血缘
          </button>
          <button className={`pill ${tab === 'locks' ? 'active' : ''}`} onClick={() => setTab('locks')}>
            资源锁与风控
          </button>
          <button className={`pill ${tab === 'backups' ? 'active' : ''}`} onClick={() => setTab('backups')}>
            备份恢复
          </button>
        </div>
        <span className="module-right">
          对账告警 <b>{reconcile.errors}</b> 错误 / <b>{reconcile.warnings}</b> 警告
        </span>
      </header>

      {error && (
        <div className="module-placeholder">
          <strong>读取异常</strong>
          <span>{error}</span>
        </div>
      )}

      {tab === 'identity' && (
        <section className="governance-section">
          <h3>身份合并候选（按置信度排序）</h3>
          {identity.items.length === 0 ? (
            <div className="module-empty">暂无待合并候选</div>
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>候选</th>
                  <th>匹配方法</th>
                  <th>置信度</th>
                  <th>状态</th>
                  <th>证据</th>
                </tr>
              </thead>
              <tbody>
                {identity.items.map((item) => (
                  <tr key={text(item.candidate_id)}>
                    <td>
                      <strong>{text(item.candidate_content_id)}</strong>
                      <small>{text(item.candidate_id)}</small>
                    </td>
                    <td>
                      <span className="tag">{text(item.evidence_method)}</span>
                      <small>{text(item.matched_namespace)}:{text(item.matched_external_id)}</small>
                    </td>
                    <td>{(Number(item.confidence) || 0).toFixed(2)}</td>
                    <td>
                      <span className={`tag ${item.action === 'auto' ? 'green' : 'amber'}`}>{text(item.action, 'candidate')}</span>
                    </td>
                    <td>
                      <code>{JSON.stringify(item.evidence || {}, null, 2).slice(0, 60)}</code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}

      {tab === 'states' && (
        <section className="governance-section">
          <h3>成稿状态机</h3>
          {Object.keys(states.columns).length === 0 ? (
            <div className="module-empty">暂无任务</div>
          ) : (
            <div className="state-board">
              {Object.entries(states.columns).map(([status, items]) => (
                <div className="state-col" key={status}>
                  <div className="state-col-head">
                    <span>{status}</span>
                    <span>{items.length}</span>
                  </div>
                  {items.map((item) => (
                    <div key={text(item.job_id)} className="state-task">
                      <b>{text(item.job_type)}</b>
                      <p>{text(item.job_id)}</p>
                    </div>
                  ))}
                </div>
              ))}
            </div>
          )}
        </section>
      )}

      {tab === 'lineage' && (
        <section className="governance-section">
          <h3>数据血缘（最近 30 条）</h3>
          {lineage.total === 0 ? (
            <div className="module-empty">暂无血缘数据</div>
          ) : (
            <ul className="lineage-list">
              {lineage.nodes.map((node, idx) => (
                <li key={`${text(node.kind)}-${text(node.id)}-${idx}`}>
                  <span className={`tag ${node.kind === 'signal' ? 'amber' : node.kind === 'production' ? 'blue' : 'green'}`}>
                    {text(node.kind, 'unknown')}
                  </span>
                  <b>{text(node.label)}</b>
                  <small>{text(node.subject_id)} · {text(node.ts)}</small>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {tab === 'locks' && (
        <section className="governance-section">
          <h3>资源锁与风控</h3>
          {locks.connections.length === 0 && locks.audit.length === 0 ? (
            <div className="module-empty">暂无锁与审计记录</div>
          ) : (
            <div className="locks-grid">
              <article className="panel">
                <div className="panel-heading">
                  <h4>连接状态</h4>
                </div>
                <ul className="lock-list">
                  {locks.connections.map((conn) => (
                    <li key={text(conn.system_key)}>
                      <strong>{text(conn.system_key)}</strong>
                      <span className={`tag ${conn.status === 'healthy' ? 'green' : 'amber'}`}>{text(conn.status)}</span>
                      <small>{text(conn.last_checked_at)}</small>
                    </li>
                  ))}
                </ul>
              </article>
              <article className="panel">
                <div className="panel-heading">
                  <h4>最近审计</h4>
                </div>
                <ul className="lock-list">
                  {locks.audit.map((item, idx) => (
                    <li key={idx}>
                      <strong>{text(item.action)}</strong>
                      <span className="tag gray">{text(item.outcome, 'n/a')}</span>
                      <small>
                        {text(item.actor_id)} → {text(item.subject_id)}
                      </small>
                    </li>
                  ))}
                </ul>
              </article>
            </div>
          )}
          <section className="governance-reconcile panel">
            <div className="panel-heading">
              <h4>对账报告</h4>
              <span className="count-pill">
                {reconcile.errors} 错误 · {reconcile.warnings} 警告 · {reconcile.total} 检查
              </span>
            </div>
            {reconcile.results.length === 0 ? (
              <p className="empty-block">暂无对账结果</p>
            ) : (
              <ul>
                {reconcile.results.map((result, idx) => (
                  <li key={idx}>
                    <strong>{text(result.section)}</strong>
                    <span className={`tag ${result.severity === 'error' ? 'red' : result.severity === 'warn' ? 'amber' : 'green'}`}>
                      {text(result.severity)}
                    </span>
                    <span>{text(result.summary)}</span>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </section>
      )}

      {tab === 'backups' && (
        <section className="governance-section">
          <div className="governance-backup-head">
            <div>
              <h3>备份 / 恢复演练</h3>
              <p className="muted">仅操作工作台 SQLite；恢复演练始终写入隔离目录，不覆盖运行库。</p>
            </div>
            <button className="primary-button" onClick={createBackup}>创建在线备份</button>
          </div>
          {backupAction && <div className="module-placeholder"><span>{backupAction}</span></div>}
          {backups.items.length === 0 ? (
            <div className="module-empty">暂无可验证备份</div>
          ) : (
            <div className="backup-list">
              {backups.items.map((item) => (
                <article className="backup-card" key={text(item.name)}>
                  <div>
                    <strong>{text(item.name)}</strong>
                    <small>{text(item.modified_at)} · {text(item.size_bytes)} bytes · SHA-256 {text(item.sha256).slice(0, 12)}…</small>
                  </div>
                  <div className="backup-card-actions">
                    <span className={`tag ${item.verifiable ? 'green' : 'red'}`}>{item.verifiable ? '可验证' : '不可用'}</span>
                    {Boolean(item.verifiable) && <button className="secondary-button" onClick={() => drillBackup(text(item.name))}>恢复演练</button>}
                  </div>
                </article>
              ))}
            </div>
          )}
        </section>
      )}
    </div>
  )
}
