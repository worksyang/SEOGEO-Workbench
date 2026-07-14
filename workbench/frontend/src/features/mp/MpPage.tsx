import {useEffect, useMemo, useState} from 'react'
import {apiGet, apiRequest, ApiError} from '../../api/client'
import type {MpArticlesResponse, MpBootstrapData, MpBootstrapResponse} from '../../types'

type RecordValue = Record<string, unknown>
type PageState = 'loading' | 'ready' | 'empty' | 'offline' | 'error'

const STATUS_LABELS: Record<string, string> = {
  healthy: '健康', degraded: '降级', offline: '离线', unknown: '未知',
}

function record(value: unknown): RecordValue {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as RecordValue : {}
}
function text(value: unknown, fallback = ''): string {
  return typeof value === 'string' || typeof value === 'number' ? String(value) : fallback
}
function bool(value: unknown): boolean {
  return value === true || value === 1 || value === 'true'
}
function first(row: RecordValue, keys: string[], fallback = ''): string {
  for (const key of keys) {
    const value = text(row[key])
    if (value) return value
  }
  return fallback
}
function rows(value: unknown, keys: string[] = ['items', 'data', 'results', 'rows', 'list']): RecordValue[] {
  if (Array.isArray(value)) return value.map(record)
  const object = record(value)
  for (const key of keys) if (Array.isArray(object[key])) return (object[key] as unknown[]).map(record)
  return []
}
function dateText(value: unknown): string {
  const raw = text(value)
  if (!raw) return '时间不可用'
  const date = new Date(raw)
  return Number.isNaN(date.getTime()) ? raw : new Intl.DateTimeFormat('zh-CN', {dateStyle: 'short', timeStyle: 'short'}).format(date)
}
function errorText(reason: unknown, fallback: string): string {
  return reason instanceof ApiError || reason instanceof Error ? reason.message : fallback
}
function statusTone(value: unknown): string {
  const status = text(record(value).status || value).toLowerCase()
  if (status === 'healthy' || status === 'ready' || status === 'online') return 'healthy'
  if (status === 'degraded' || status === 'partial' || status === 'blocked') return 'degraded'
  if (status === 'offline' || status === 'unavailable' || status === 'error') return 'offline'
  return 'unknown'
}
function statusLabel(value: unknown): string {
  const status = text(record(value).status || value).toLowerCase()
  return STATUS_LABELS[status] ?? (status || '未知')
}
function enabledLabel(value: unknown): {label: string; tone: string} {
  if (value === true || value === 1 || value === '1' || value === 'true' || value === 'enabled' || value === 'online') return {label: '启用', tone: 'healthy'}
  if (value === false || value === 0 || value === '0' || value === 'false' || value === 'disabled' || value === 'offline') return {label: '停用', tone: 'offline'}
  return {label: '未知', tone: 'unknown'}
}
function jobStatus(job: RecordValue | null): string {
  const value = job ?? {}
  return first(value, ['status', 'state'], first(record(value.job), ['status', 'state'], 'unknown')).toLowerCase()
}
function isActiveJob(status: string): boolean {
  return ['queued', 'running', 'cancelling'].includes(status)
}

export default function MpPage({onSourceStatus}: {onSourceStatus: (status: string) => void}) {
  const [bootstrap, setBootstrap] = useState<MpBootstrapData | null>(null)
  const [state, setState] = useState<PageState>('loading')
  const [error, setError] = useState('')
  const [query, setQuery] = useState('')
  const [categoryFilter, setCategoryFilter] = useState('all')
  const [actionMessage, setActionMessage] = useState('')
  const [actionError, setActionError] = useState('')
  const [busyKey, setBusyKey] = useState('')
  const [authState, setAuthState] = useState<RecordValue | null>(null)
  const [qrImage, setQrImage] = useState('')
  const [refreshBeforeRun, setRefreshBeforeRun] = useState(true)
  const [useAiFilter, setUseAiFilter] = useState(true)
  const [daysToFetch, setDaysToFetch] = useState('15')
  const [selectedMpIds, setSelectedMpIds] = useState<string[]>([])
  const [startPage, setStartPage] = useState('0')
  const [endPage, setEndPage] = useState('20')
  const [selectedJobId, setSelectedJobId] = useState('')
  const [jobDetail, setJobDetail] = useState<RecordValue | null>(null)
  const [dryRunResult, setDryRunResult] = useState<RecordValue | null>(null)
  const [articleData, setArticleData] = useState<RecordValue | null>(null)
  const [articleState, setArticleState] = useState<PageState>('loading')

  const load = async (signal?: AbortSignal) => {
    setState('loading'); setError(''); setActionError('')
    try {
      const response = await apiGet<MpBootstrapResponse>('/api/v1/mp/bootstrap', signal)
      const data = response.data ?? null
      setBootstrap(data)
      const source = data?.source_status ?? {}
      const sourceStatus = text(source.status, 'unknown')
      onSourceStatus(sourceStatus)
      setState(statusTone(sourceStatus) === 'offline' ? 'offline' : data ? 'ready' : 'empty')
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === 'AbortError') return
      setState('offline'); setError(errorText(reason, '公众号监控服务暂时无法连接')); onSourceStatus('offline')
    }
  }
  const loadArticles = async (signal?: AbortSignal) => {
    setArticleState('loading')
    try {
      const response = await apiGet<MpArticlesResponse>('/api/v1/mp/articles', signal)
      const data = response.data ?? null
      setArticleData(data)
      setArticleState(rows(data?.articles).length ? 'ready' : 'empty')
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === 'AbortError') return
      setArticleState('error'); setActionError(errorText(reason, 'Hub 文章读取失败'))
    }
  }
  useEffect(() => {
    const controller = new AbortController()
    void Promise.all([load(controller.signal), loadArticles(controller.signal)])
    return () => controller.abort()
  }, [])

  const source = bootstrap?.source_status ?? {}
  const accounts = useMemo(() => rows(bootstrap?.accounts, ['accounts', 'items']), [bootstrap])
  const categories = useMemo(() => rows(bootstrap?.categories, ['categories', 'items']), [bootstrap])
  const jobs = useMemo(() => rows(bootstrap?.jobs, ['jobs', 'items']), [bootstrap])
  const categoryNames = useMemo(() => {
    const raw = bootstrap?.categories
    const values = Array.isArray(raw)
      ? raw.flatMap((item) => typeof item === 'string' ? [item] : [first(record(item), ['name', 'category_name', 'title'])])
      : categories.map((item) => first(item, ['name', 'category_name', 'title']))
    return Array.from(new Set(values.filter(Boolean)))
  }, [bootstrap, categories])
  const visibleAccounts = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return accounts.filter((account) => {
      const category = first(account, ['category_name', 'category', 'group'], '未分类')
      return (!needle || JSON.stringify(account).toLowerCase().includes(needle)) && (categoryFilter === 'all' || category === categoryFilter)
    })
  }, [accounts, categoryFilter, query])
  const selectableJobAccounts = useMemo(() => accounts.filter((account) => !(bool(account.is_system) && first(account, ['mp_id', 'account_id', 'id']) === 'MP_WXS_FEATURED_ARTICLES')), [accounts])
  const summary = bootstrap?.summary ?? {}
  const articleRows = rows(articleData?.articles)
  const importedRows = rows(bootstrap?.hub_articles)

  const refresh = async () => {
    const controller = new AbortController()
    await Promise.all([load(controller.signal), loadArticles(controller.signal)])
  }
  useEffect(() => {
    if (!selectedJobId || !isActiveJob(jobStatus(jobDetail))) return
    const timer = window.setInterval(() => {
      void apiGet<{data?: RecordValue}>(`/api/v1/mp/jobs/${encodeURIComponent(selectedJobId)}`)
        .then((response) => setJobDetail(response.data ?? null))
        .catch((reason) => setActionError(errorText(reason, '任务详情轮询失败')))
    }, 2000)
    return () => window.clearInterval(timer)
  }, [jobDetail, selectedJobId])
  const runAction = async (key: string, fn: () => Promise<unknown>, success: string | ((result: unknown) => string)) => {
    setBusyKey(key); setActionError(''); setActionMessage('')
    try {
      const result = await fn()
      setActionMessage(typeof success === 'function' ? success(result) : success)
      await refresh()
    }
    catch (reason) { setActionError(errorText(reason, '操作失败，上游未确认成功')) }
    finally { setBusyKey('') }
  }
  const updateAccount = (account: RecordValue, patch: RecordValue) => {
    const id = first(account, ['mp_id', 'account_id', 'id'])
    if (!id) { setActionError('该账号缺少 mp_id，无法安全更新。'); return }
    void runAction(`account-${id}`, () => apiRequest(`/api/v1/mp/accounts/${encodeURIComponent(id)}`, 'PATCH', {...patch, confirm: true}), '账号设置已由上游确认并已重新读取。')
  }
  const checkAuth = () => void runAction('auth-check', async () => {
    const response = await apiRequest<{data?: RecordValue}>('/api/v1/mp/auth/check', 'POST')
    setAuthState(response.data ?? null)
  }, '登录状态已重新检查。')
  const requestQr = () => void runAction('auth-qr', async () => {
    const response = await apiRequest<{data?: RecordValue}>('/api/v1/mp/auth/qrcode', 'POST')
    const data = response.data ?? {}
    setAuthState(data); setQrImage(text(data.image_url))
    if (!text(data.image_url)) throw new Error(text(data.message, '上游未返回二维码。'))
  }, '二维码已由上游返回，请扫码授权。')
  const finishAuth = () => void runAction('auth-finish', async () => {
    const response = await apiRequest<{data?: RecordValue}>('/api/v1/mp/auth/qrcode/finish', 'POST')
    const data = response.data ?? {}
    setAuthState(data)
    const loggedIn = data.logged_in === true || data.authenticated === true
    const finished = data.finished === true || data.status === 'finished' || data.auth_status === 'finished'
    if (loggedIn || finished) setQrImage('')
    else throw new Error(first(data, ['message', 'display_status', 'auth_status', 'status'], '上游尚未确认授权完成。'))
  }, '授权完成状态已由上游返回。')
  const createJob = () => {
    if (source.inconsistent || source.logged_in === false) {
      setActionError('未登录，不能启动采集任务。请先重新扫码授权，并等待上游确认登录状态。')
      return
    }
    const days = Number(daysToFetch)
    const start = Number(startPage)
    const end = Number(endPage)
    if (!Number.isInteger(days) || days < 1 || days > 365) {
      setActionError('抓取天数必须是 1–365 的整数。')
      return
    }
    if (!Number.isInteger(start) || start < 0 || start > 999 || !Number.isInteger(end) || end < 0 || end > 999) {
      setActionError('开始页和结束页必须是 0–999 的整数。')
      return
    }
    if (end < start) {
      setActionError('结束页不能小于开始页，任务未提交。')
      return
    }
    if (!window.confirm('确认创建公众号采集任务？此操作会提交到真实上游。')) return
    const payload = {
      refresh_before_run: refreshBeforeRun,
      use_ai_filter: useAiFilter,
      days_to_fetch: days,
      selected_mp_ids: selectedMpIds,
      start_page: start,
      end_page: end,
      confirm: true,
    }
    void runAction('job-create', () => apiRequest('/api/v1/mp/jobs', 'POST', payload), '已收到任务回执；页面已重拉真实任务状态。')
  }
  const cancelJob = (job: RecordValue) => {
    const id = first(job, ['job_id', 'id', 'task_id'])
    if (!id || !window.confirm('确认取消这个真实采集任务？')) return
    void runAction(`job-cancel-${id}`, () => apiRequest<{data?: RecordValue}>(`/api/v1/mp/jobs/${encodeURIComponent(id)}/cancel`, 'POST', {confirm: true}), (result) => `取消请求已返回，任务当前状态：${first(record((result as {data?: RecordValue}).data), ['status', 'state'], 'unknown')}。`)
  }
  const showJobDetail = (job: RecordValue) => {
    const id = first(job, ['job_id', 'id', 'task_id'])
    if (!id) return
    setSelectedJobId(id); setJobDetail(null)
    void runAction(`job-detail-${id}`, async () => {
      const response = await apiGet<{data?: RecordValue}>(`/api/v1/mp/jobs/${encodeURIComponent(id)}`)
      setJobDetail(response.data ?? null)
    }, '任务详情已读取。')
  }
  const dryRunImport = () => void runAction('import-dry-run', async () => {
    const response = await apiRequest<{data?: RecordValue}>('/api/v1/mp/import', 'POST', {dry_run: true})
    setDryRunResult(response.data ?? null)
  }, '历史导入 dry-run 已返回真实统计。')

  return (
    <div className="mp-page">
      <section className="mp-source-banner">
        <div>
          <p className="eyebrow">WECHAT MP · LIVE SOURCE</p>
          <h2>公众号监控</h2>
          <p className="subtle-copy">{text(source.message) || '账号、任务和 Hub 文章均来自真实公众号监控上游与本地数据库。'}</p>
        </div>
        <div className="source-status-block">
          <span className={`status-dot large ${statusTone(source.status)}`} />
          <div><strong>{state === 'loading' ? '检查中' : statusLabel(source.status)}</strong><small>{source.inconsistent ? '状态不一致' : 'live_http'}</small></div>
        </div>
      </section>
      {error && <div className="module-notice error" role="alert"><strong>连接失败</strong><span>{error}</span><button type="button" onClick={() => void refresh()}>重试</button></div>}
      {source.inconsistent && source.logged_in === false && (
        <section className="mp-auth-card" aria-labelledby="mp-auth-title">
          <div><p className="eyebrow">AUTHORIZATION REQUIRED</p><h3 id="mp-auth-title">未登录 / 重新扫码授权</h3><p className="subtle-copy">{text(source.message) || '上游报告当前微信登录状态不可用，不能把账号监控显示为成功。'}</p></div>
          <div className="mp-auth-actions">
            <button className="secondary-button" type="button" onClick={checkAuth} disabled={busyKey !== ''}>重新检查登录</button>
            <button className="primary-button" type="button" onClick={requestQr} disabled={busyKey !== ''}>获取二维码</button>
            {qrImage && <button className="secondary-button" type="button" onClick={finishAuth} disabled={busyKey !== ''}>我已扫码，检查授权</button>}
            {qrImage && <img className="mp-qr-image" src={qrImage} alt="公众号监控登录二维码" />}
          </div>
          {authState && <p className="mp-auth-receipt" aria-live="polite">上游登录回执：{first(authState, ['display_status', 'auth_status', 'status', 'message'], '已返回')}</p>}
        </section>
      )}
      {(actionError || actionMessage) && <div className={`module-notice ${actionError ? 'error' : ''}`} role={actionError ? 'alert' : 'status'} aria-live="polite"><strong>{actionError ? '操作未完成' : '已收到回执'}</strong><span>{actionError || actionMessage}</span></div>}
      <section className="mp-summary-grid" aria-label="公众号监控统计">
        {([['account_count', '账号'], ['category_count', '分类'], ['job_count', '任务'], ['imported_article_count', 'Hub 已入库文章']] as const).map(([key, label]) => <article className="metric-card" key={key}><span>{label}</span><strong>{state === 'loading' ? '—' : new Intl.NumberFormat('zh-CN').format(Number(summary[key] ?? 0))}</strong><small>来自 bootstrap 实时返回</small></article>)}
      </section>
      <div className="mp-grid">
        <section className="panel mp-accounts-panel">
          <div className="panel-heading"><div><p className="eyebrow">ACCOUNT FLAGS</p><h3>监控账号</h3></div><span className="count-pill">{visibleAccounts.length}/{accounts.length}</span></div>
          <div className="mp-filters"><label>搜索账号<input aria-label="搜索公众号账号" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="名称或 mp_id" /></label><label>分类<select aria-label="按分类筛选" value={categoryFilter} onChange={(event) => setCategoryFilter(event.target.value)}><option value="all">全部分类</option>{categoryNames.map((name) => <option key={name} value={name}>{name}</option>)}</select></label></div>
          <div className="mp-account-list" role="list">
            {visibleAccounts.length ? visibleAccounts.map((account, index) => {
              const id = first(account, ['mp_id', 'account_id', 'id'], `account-${index}`)
              const name = first(account, ['mp_name', 'name', 'canonical_name'], '未命名账号')
              const category = first(account, ['category_name', 'category', 'group'], '未分类')
              const monitor = bool(account.monitor_enabled), run = bool(account.run_enabled)
              const systemAccount = bool(account.is_system)
              const server = enabledLabel(account.server_status ?? account.status)
              return <article className="mp-account-row" key={id} role="listitem"><div className="mp-account-main"><strong>{name}{systemAccount && <span className="system-account-label">系统账号</span>}</strong><small>{id} · {category}</small></div><span className={`status-badge ${server.tone}`}>{server.label}</span><label className="switch-label"><input type="checkbox" aria-label={`${name} 监控开关`} checked={monitor} onChange={(event) => updateAccount(account, {monitor_enabled: event.target.checked})} disabled={busyKey !== '' || systemAccount} /><span>监控</span></label><label className="switch-label"><input type="checkbox" aria-label={`${name} 执行开关`} checked={run} onChange={(event) => updateAccount(account, {run_enabled: event.target.checked})} disabled={busyKey !== '' || systemAccount} /><span>执行</span></label><select aria-label={`${name} 分类`} value={categoryNames.includes(category) ? category : ''} onChange={(event) => updateAccount(account, {category_name: event.target.value})} disabled={busyKey !== '' || systemAccount}><option value="" disabled>选择分类</option>{categoryNames.map((nameValue) => <option key={nameValue} value={nameValue}>{nameValue}</option>)}</select></article>
            }) : <div className="empty-state"><strong>{state === 'loading' ? '账号加载中…' : '暂无符合条件的账号'}</strong><p>不使用本地占位数据；请检查上游账号列表或筛选条件。</p></div>}
          </div>
        </section>
        <section className="panel mp-jobs-panel">
          <div className="panel-heading"><div><p className="eyebrow">TASKS</p><h3>任务</h3></div><button className="secondary-button" type="button" onClick={() => void refresh()} disabled={busyKey !== ''}>刷新</button></div>
          <div className="mp-job-form">
            <label className="checkbox-field"><input type="checkbox" checked={refreshBeforeRun} onChange={(event) => setRefreshBeforeRun(event.target.checked)} />运行前刷新</label>
            <label className="checkbox-field"><input type="checkbox" checked={useAiFilter} onChange={(event) => setUseAiFilter(event.target.checked)} />使用 AI 筛选</label>
            <label>抓取天数<input inputMode="numeric" value={daysToFetch} onChange={(event) => setDaysToFetch(event.target.value.replace(/[^\d]/g, ''))} aria-label="抓取天数" /></label>
            <label>开始页<input inputMode="numeric" value={startPage} onChange={(event) => setStartPage(event.target.value.replace(/[^\d]/g, ''))} aria-label="开始页" /></label>
            <label>结束页<input inputMode="numeric" value={endPage} onChange={(event) => setEndPage(event.target.value.replace(/[^\d]/g, ''))} aria-label="结束页" /></label>
            <fieldset className="mp-account-picker"><legend>选择账号（空数组使用上游 run_enabled 清单）</legend>{selectableJobAccounts.length ? selectableJobAccounts.map((account) => { const id = first(account, ['mp_id', 'account_id', 'id']); const name = first(account, ['mp_name', 'name'], id); return <label key={id} className="checkbox-field"><input type="checkbox" checked={selectedMpIds.includes(id)} onChange={(event) => setSelectedMpIds((current) => event.target.checked ? [...current, id] : current.filter((value) => value !== id))} />{name}</label> }) : <small>暂无可选采集账号</small>}</fieldset>
            <button className="primary-button" type="button" onClick={createJob} disabled={busyKey !== '' || Boolean(source.inconsistent) || source.logged_in === false}>创建采集任务（需确认）</button>
            {(source.inconsistent || source.logged_in === false) && <small className="mp-job-blocked">未登录，不能启动采集任务。</small>}
          </div>
          <div className="mp-job-list">{jobs.length ? jobs.map((job, index) => { const id = first(job, ['job_id', 'id', 'task_id'], `job-${index}`); return <article className="mp-job-row" key={id}><div><strong>{first(job, ['name', 'type', 'job_type'], '未命名任务')}</strong><small>{id} · {first(job, ['status', 'state'], 'unknown')} · {dateText(job.created_at || job.updated_at)}</small></div><div className="mp-job-actions"><button className="secondary-button" type="button" onClick={() => showJobDetail(job)} disabled={busyKey !== ''}>详情</button><button className="secondary-button" type="button" onClick={() => cancelJob(job)} disabled={busyKey !== '' || ['cancelled', 'completed', 'success', 'failed'].includes(first(job, ['status', 'state']).toLowerCase())}>取消</button></div></article> }) : <div className="empty-state"><strong>{state === 'loading' ? '任务加载中…' : '暂无任务'}</strong><p>创建任务会显式发送 confirm=true，并在完成后重新读取上游状态。</p></div>}</div>
          {jobDetail && <div className="mp-job-detail" role="status" aria-live="polite"><strong>任务详情 · {jobStatus(jobDetail)}</strong><div className="mp-job-detail-grid">{(['status', 'progress', 'request', 'result', 'error', 'logs'] as const).map((key) => <div key={key}><span>{key}</span><pre>{JSON.stringify(jobDetail[key] ?? record(jobDetail.job)[key] ?? '', null, 2)}</pre></div>)}</div></div>}
        </section>
      </div>
      <section className="panel mp-articles-panel">
        <div className="panel-heading"><div><p className="eyebrow">HUB ARTICLES</p><h3>Hub 已入库文章</h3></div><div className="panel-heading-actions"><span className="count-pill">{articleRows.length || importedRows.length} 篇</span><button className="secondary-button" type="button" onClick={dryRunImport} disabled={busyKey !== ''}>历史导入 dry-run</button></div></div>
        {dryRunResult && <div className="mp-dry-run" role="status" aria-live="polite"><strong>dry-run 真实回执</strong><span>文章 {text(record(dryRunResult.counts).articles, '—')} · Markdown {text(record(dryRunResult.counts).markdown_articles, '—')} · CSV-only {text(record(dryRunResult.counts).csv_only, '—')}</span><span>对账：{JSON.stringify(record(record(dryRunResult.audit).reconcile))}</span><span>拒绝文章行数：{text(record(record(dryRunResult.audit).rejected_articles).rows, '0')}</span></div>}
        {articleState === 'loading' ? <div className="empty-state"><strong>文章加载中…</strong><p>正在读取 Hub 真实文章。</p></div> : articleState === 'error' ? <div className="empty-state"><strong>文章读取失败</strong><p>请查看上方错误回执后重试。</p></div> : (articleRows.length || importedRows.length) ? <div className="mp-article-list">{(articleRows.length ? articleRows : importedRows).map((article, index) => { const payload = record(article.payload); const warnings = payload.integrity_warnings; const metadataStatus = text(payload.metadata_match_status, 'unknown'); const metadataTone = ['matched', 'csv_only'].includes(metadataStatus) ? 'healthy' : ['ambiguous', 'unmatched'].includes(metadataStatus) ? 'degraded' : 'unknown'; return <article className="mp-article-row" key={first(article, ['content_id', 'id'], `article-${index}`)}><div><strong>{first(article, ['title', 'name'], '未命名文章')}</strong><small>{first(article, ['author_name', 'author', 'mp_name'], '作者不可用')} · {dateText(article.published_at)}</small></div><span className={`status-badge ${metadataTone}`}>metadata {metadataStatus}</span><span className={`status-badge ${Array.isArray(warnings) && warnings.length ? 'degraded' : 'healthy'}`}>{Array.isArray(warnings) && warnings.length ? `${warnings.length} integrity warning` : 'integrity ok'}</span></article> })}</div> : <div className="empty-state"><strong>Hub 暂无公众号文章</strong><p>当前没有已入库的真实文章；可先执行 dry-run 检查历史资产，正式导入仍需显式操作。</p></div>}
      </section>
    </div>
  )
}
