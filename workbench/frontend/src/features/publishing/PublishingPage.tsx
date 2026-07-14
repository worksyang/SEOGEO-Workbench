import {useEffect, useMemo, useState} from 'react'
import {apiGet, apiRequest, ApiError} from '../../api/client'

type Account = {
  account_id: string
  display_name: string
  enabled: boolean
  last_attempt_at?: string
}

type PageState = 'loading' | 'ready' | 'offline' | 'error'

function record(value: unknown, fallback: Record<string, unknown> = {}): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : fallback
}
function text(value: unknown, fallback = ''): string {
  return typeof value === 'string' || typeof value === 'number' ? String(value) : fallback
}

const SAMPLE_BODY = `# 香港储蓄险的三个常见盲点

## 流动性锁定
储蓄险前期流动性差，短于 5 年退保可能产生本金损失。

## 分红演示 ≠ 保证
非保证分红受保司投资策略影响，需要看多年的分红实现率。

## 跨境成本
汇率、缴费路径和后续资金调拨都会影响实际收益。
`

export default function PublishingPage() {
  const [state, setState] = useState<PageState>('loading')
  const [error, setError] = useState('')
  const [accounts, setAccounts] = useState<Account[]>([])
  const [selected, setSelected] = useState('')
  const [contentId, setContentId] = useState('')
  const [body, setBody] = useState(SAMPLE_BODY)
  const [tab, setTab] = useState<'preview' | 'dry-run' | 'publish'>('preview')
  const [previewHtml, setPreviewHtml] = useState('')
  const [sensitive, setSensitive] = useState<Array<Record<string, unknown>>>([])
  const [warnings, setWarnings] = useState<string[]>([])
  const [result, setResult] = useState<Record<string, unknown> | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const refresh = async () => {
    setState('loading')
    try {
      const res = await apiGet<{ok: boolean; data: {items: Account[]}}>('/api/v1/publishing/accounts')
      const items = res?.data?.items || []
      setAccounts(items)
      if (items[0]) setSelected((prev) => prev || items[0].account_id)
      setState('ready')
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message)
        setState('offline')
      } else {
        setError(String(err))
        setState('error')
      }
    }
  }

  useEffect(() => {
    refresh()
  }, [])

  const requestPreview = async () => {
    setSubmitting(true)
    setResult(null)
    try {
      const res = await apiRequest<{ok: boolean; data: Record<string, unknown>}>(
        '/api/v1/publishing/preview',
        'POST',
        {content_id: contentId || 'demo', body},
      )
      const data = record(res?.data)
      setPreviewHtml(text(data.html))
      setSensitive((Array.isArray(data.sensitive_matches) ? data.sensitive_matches : []) as Array<Record<string, unknown>>)
      setWarnings(Array.isArray(data.warnings) ? (data.warnings as string[]) : [])
    } catch (err) {
      setResult({error: err instanceof Error ? err.message : String(err)})
    } finally {
      setSubmitting(false)
    }
  }

  const dryRun = async () => {
    if (!selected) return
    setSubmitting(true)
    setResult(null)
    try {
      const res = await apiRequest<{ok: boolean; data: Record<string, unknown>}>(
        '/api/v1/publishing/dry-run',
        'POST',
        {account_id: selected, content_id: contentId || 'demo', body},
      )
      setResult(record(res?.data))
    } catch (err) {
      setResult({error: err instanceof Error ? err.message : String(err)})
    } finally {
      setSubmitting(false)
    }
  }

  const publishReal = async () => {
    if (!selected) return
    setSubmitting(true)
    setResult(null)
    try {
      const res = await apiRequest<{ok: boolean; data: Record<string, unknown>}>(
        '/api/v1/publishing/publish',
        'POST',
        {account_id: selected, content_id: contentId || 'demo', body, confirm: true},
      )
      setResult(record(res?.data))
    } catch (err) {
      setResult({error: err instanceof Error ? err.message : String(err)})
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="module-frame demo-module-page publishing-page">
      <header className="module-top">
        <strong className="module-logo">发布中心</strong>
        <span className="sep" />
        <span className="module-meta">
          可用账号 <b>{accounts.filter((a) => a.enabled).length}</b> / 全部 <b>{accounts.length}</b>
        </span>
        <div className="module-switch">
          <button className={`pill ${tab === 'preview' ? 'active' : ''}`} onClick={() => setTab('preview')}>
            预览
          </button>
          <button className={`pill ${tab === 'dry-run' ? 'active' : ''}`} onClick={() => setTab('dry-run')}>
            Dry-run
          </button>
          <button className={`pill ${tab === 'publish' ? 'active' : ''}`} onClick={() => setTab('publish')}>
            真发布
          </button>
        </div>
        <span className="module-right">Cookie 不进入前端；真发布需 confirm=true</span>
      </header>

      {state === 'offline' && (
        <div className="module-placeholder">
          <strong>Hub 暂时不可达</strong>
          <span>{error}</span>
        </div>
      )}

      <section className="publishing-form">
        <div className="publishing-fields">
          <label>
            <span>账号</span>
            <select value={selected} onChange={(e) => setSelected(e.target.value)}>
              {accounts.map((acct) => (
                <option key={acct.account_id} value={acct.account_id}>
                  {acct.display_name} ({acct.account_id})
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>content_id</span>
            <input value={contentId} onChange={(e) => setContentId(e.target.value)} placeholder="可选 · 用于幂等键" />
          </label>
        </div>
        <label className="publishing-body">
          <span>正文 Markdown</span>
          <textarea value={body} onChange={(e) => setBody(e.target.value)} rows={12} />
        </label>
        <div className="publishing-actions">
          {tab === 'preview' && (
            <button type="button" className="mini-btn primary" onClick={requestPreview} disabled={submitting}>
              {submitting ? '生成中…' : '生成预览 HTML'}
            </button>
          )}
          {tab === 'dry-run' && (
            <button type="button" className="mini-btn primary" onClick={dryRun} disabled={submitting || !selected}>
              {submitting ? '执行中…' : '执行 dry-run'}
            </button>
          )}
          {tab === 'publish' && (
            <button type="button" className="mini-btn primary danger" onClick={publishReal} disabled={submitting || !selected}>
              {submitting ? '执行中…' : '确认并真发布'}
            </button>
          )}
        </div>
      </section>

      {tab === 'preview' && (
        <section className="publishing-preview">
          <article className="publishing-html">
            <h3>微信编辑器预览</h3>
            <pre dangerouslySetInnerHTML={{__html: previewHtml || '<p>请先生成预览</p>'}} />
          </article>
          <aside className="publishing-checks">
            <h3>敏感词</h3>
            {sensitive.length === 0 ? (
              <p className="subtle">未发现敏感词</p>
            ) : (
              <ul>
                {sensitive.map((match, idx) => (
                  <li key={idx}>
                    {text(match.word)} · 第 {text(match.line)} 行
                  </li>
                ))}
              </ul>
            )}
            <h3>提示</h3>
            {warnings.length === 0 ? (
              <p className="subtle">无</p>
            ) : (
              <ul>
                {warnings.map((warn, idx) => (
                  <li key={idx}>{warn}</li>
                ))}
              </ul>
            )}
          </aside>
        </section>
      )}

      {(tab === 'dry-run' || tab === 'publish') && result && (
        <section className="publishing-result">
          <h3>{tab === 'dry-run' ? 'Dry-run 报告' : '真发布回执'}</h3>
          <pre>{JSON.stringify(result, null, 2)}</pre>
        </section>
      )}
    </div>
  )
}
