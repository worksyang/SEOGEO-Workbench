import {useEffect, useMemo, useState} from 'react'
import {apiGet, apiRequest, ApiError} from '../../api/client'

type Job = {
  job_id: string
  job_type: 'mother_forge' | 'batch_production' | string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'blocked' | string
  created_at: string
  updated_at: string
  scheduled_at: string
}
type PageState = 'loading' | 'ready' | 'offline' | 'error'

function record(value: unknown, fallback: Record<string, unknown> = {}): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : fallback
}
function text(value: unknown, fallback = ''): string {
  return typeof value === 'string' || typeof value === 'number' ? String(value) : fallback
}

const STATUS_LABELS: Record<string, string> = {
  queued: '等待中',
  running: '运行中',
  succeeded: '已完成',
  failed: '失败',
  cancelled: '已取消',
  blocked: '受阻',
}

export default function WritingPage() {
  const [state, setState] = useState<PageState>('loading')
  const [error, setError] = useState('')
  const [jobs, setJobs] = useState<Job[]>([])
  const [tab, setTab] = useState<'overview' | 'forge' | 'batch'>('overview')
  const [forgeTopic, setForgeTopic] = useState('')
  const [forgePurpose, setForgePurpose] = useState('')
  const [batchTopic, setBatchTopic] = useState('')
  const [batchKeywords, setBatchKeywords] = useState('')
  const [batchCount, setBatchCount] = useState(3)
  const [submitting, setSubmitting] = useState(false)
  const [lastMessage, setLastMessage] = useState('')

  const refresh = async () => {
    setState('loading')
    try {
      const res = await apiGet<{ok: boolean; data: {items: Job[]}}>('/api/v1/writing/jobs?limit=30')
      setJobs(res?.data?.items || [])
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

  const counts = useMemo(() => {
    const out: Record<string, number> = {}
    for (const job of jobs) {
      out[job.status] = (out[job.status] || 0) + 1
    }
    return out
  }, [jobs])

  const submit = async (mode: 'mother_forge' | 'batch_production') => {
    setSubmitting(true)
    setLastMessage('')
    try {
      const payload =
        mode === 'mother_forge'
          ? {mode, topic: forgeTopic || '未命名母文章', purpose: forgePurpose}
          : {
              mode: 'batch_production',
              topic: batchTopic || '未命名批次',
              keywords: batchKeywords.split(/[,，\s]+/).filter(Boolean),
              target_article_count: Math.max(1, batchCount),
              source: 'manual',
              requirements: {},
            }
      const res = await apiRequest<{ok: boolean; data: Record<string, unknown>}>(
        '/api/v1/writing/jobs',
        'POST',
        payload,
      )
      const jobId = text(record(res.data).job_id)
      setLastMessage(`已创建任务 ${jobId}，开始执行…`)
      if (jobId) {
        await apiRequest(`/api/v1/writing/jobs/${encodeURIComponent(jobId)}/run`, 'POST')
        await refresh()
      }
    } catch (err) {
      setLastMessage(`创建失败：${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="module-frame demo-module-page writing-page">
      <header className="module-top">
        <strong className="module-logo">WritingMoney · 母文章铸造 / 批量成稿</strong>
        <span className="sep" />
        <span className="module-meta">
          任务 <b>{jobs.length}</b> 个 · 运行 <b>{counts.running || 0}</b> · 完成 <b>{counts.succeeded || 0}</b>
        </span>
        <div className="module-switch">
          <button className={`pill ${tab === 'overview' ? 'active' : ''}`} onClick={() => setTab('overview')}>
            任务总览
          </button>
          <button className={`pill ${tab === 'forge' ? 'active' : ''}`} onClick={() => setTab('forge')}>
            母文章铸造
          </button>
          <button className={`pill ${tab === 'batch' ? 'active' : ''}`} onClick={() => setTab('batch')}>
            批量成稿
          </button>
        </div>
        <span className="module-right">
          <button className="mini-btn" onClick={refresh}>刷新</button>
        </span>
      </header>

      {state === 'offline' && <div className="module-placeholder"><strong>Hub 暂时不可达</strong><span>{error}</span></div>}
      {state === 'error' && <div className="module-placeholder"><strong>读取失败</strong><span>{error}</span></div>}

      {(state === 'ready' || state === 'loading') && tab === 'overview' && (
        <div className="writing-jobs">
          {jobs.length === 0 ? (
            <div className="module-placeholder">
              <strong>暂无任务</strong>
              <p>请到「母文章铸造」或「批量成稿」创建第一个任务。</p>
            </div>
          ) : (
            jobs.map((job) => (
              <article key={job.job_id} className={`job-card status-${job.status}`}>
                <div className="job-head">
                  <strong>{job.job_type === 'mother_forge' ? '母文章铸造' : '批量成稿'}</strong>
                  <span className={`tag ${job.status === 'succeeded' ? 'green' : job.status === 'failed' ? 'red' : 'amber'}`}>
                    {STATUS_LABELS[job.status] || job.status}
                  </span>
                </div>
                <p className="job-meta">
                  {job.job_id} · 更新于 {new Date(job.updated_at).toLocaleString('zh-CN')}
                </p>
              </article>
            ))
          )}
        </div>
      )}

      {tab === 'forge' && (
        <div className="writing-form">
          <h3>新建母文章铸造项目</h3>
          <label>
            <span>选题</span>
            <input value={forgeTopic} onChange={(e) => setForgeTopic(e.target.value)} placeholder="例如：分红实现率判断框架" />
          </label>
          <label>
            <span>写作目的</span>
            <textarea value={forgePurpose} onChange={(e) => setForgePurpose(e.target.value)} placeholder="给 Fake Provider 描述本篇文章将回答的核心问题" />
          </label>
          <div className="form-actions">
            <button type="button" className="mini-btn primary" disabled={submitting} onClick={() => submit('mother_forge')}>
              {submitting ? '提交中…' : '铸造母文章'}
            </button>
          </div>
          {lastMessage && <p className="last-message">{lastMessage}</p>}
        </div>
      )}

      {tab === 'batch' && (
        <div className="writing-form">
          <h3>新建批量成稿批次</h3>
          <label>
            <span>主选题</span>
            <input value={batchTopic} onChange={(e) => setBatchTopic(e.target.value)} placeholder="例如：香港储蓄险" />
          </label>
          <label>
            <span>关键词（空格 / 逗号分隔）</span>
            <input value={batchKeywords} onChange={(e) => setBatchKeywords(e.target.value)} placeholder="友邦财富盈活 保诚信守明天 安盛盛利2" />
          </label>
          <label>
            <span>成稿篇数</span>
            <input
              type="number"
              min={1}
              max={20}
              value={batchCount}
              onChange={(e) => setBatchCount(Number(e.target.value || 1))}
            />
          </label>
          <div className="form-actions">
            <button type="button" className="mini-btn primary" disabled={submitting} onClick={() => submit('batch_production')}>
              {submitting ? '提交中…' : '创建并执行批次'}
            </button>
          </div>
          {lastMessage && <p className="last-message">{lastMessage}</p>}
        </div>
      )}
    </div>
  )
}
