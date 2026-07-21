import {useCallback, useEffect, useMemo, useState, type ReactNode} from 'react'
import {apiGet} from './api/client'
import HomePage from './features/overview/HomePage'
import MpPage from './features/mp/MpPage'
import GeoPage from './features/geo/GeoPage'
import './styles/app.css'
import './styles/demo-shell.css'
import WechatIslandPage from './features/wechat/WechatIslandPage'
import XhsIslandPage from './features/xhs/XhsIslandPage'
import {useWorkbenchData} from './hooks/useWorkbenchData'

type NavKey = 'overview' | 'wechat' | 'xhs' | 'geo' | 'mp'
type NavGroup = {label: string; items: ReadonlyArray<readonly [NavKey, string]>}

const NAV_GROUPS: ReadonlyArray<NavGroup> = [
  {label: '主页', items: [['overview', '统一首页']]},
  {label: '选题发现', items: [['wechat', '微信关键词'], ['xhs', '小红书关键词'], ['geo', 'GEO 观察']]},
  {label: '内容采集', items: [['mp', '公众号监控']]},
]

const NAV_LABELS: Record<NavKey, string> = {
  overview: '统一首页',
  wechat: '微信关键词',
  xhs: '小红书关键词',
  geo: 'GEO 观察',
  mp: '公众号监控',
}

const STATUS_SYSTEM_KEYS: Partial<Record<NavKey, string>> = {
  wechat: 'wechat-search',
  mp: 'wechat-mp',
  xhs: 'xhs-search',
  geo: 'geo',
}

function navFromHash(): NavKey {
  const key = window.location.hash.replace(/^#\/?/, '') as NavKey
  return key in NAV_LABELS ? key : 'overview'
}

function NavIcon({name}: {name: NavKey}) {
  const paths: Record<NavKey, ReactNode> = {
    overview: <><rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="7" /><rect x="3" y="14" width="7" height="7" /><rect x="14" y="14" width="7" height="7" /></>,
    wechat: <><path d="M4 17V7h16v10" /><path d="M7 11h10M7 14h7" /></>,
    xhs: <><circle cx="12" cy="12" r="8" /><path d="M8 12h8M12 8v8" /></>,
    geo: <><circle cx="12" cy="12" r="8" /><path d="M4 12h16M12 4a13 13 0 0 1 0 16M12 4a13 13 0 0 0 0 16" /></>,
    mp: <><path d="M5 4h14v16H5z" /><path d="M8 8h8M8 12h8M8 16h5" /></>,
  }
  return <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">{paths[name]}</svg>
}

function statusText(value?: string): string {
  const normalized = value?.toLowerCase()
  if (normalized === 'healthy' || normalized === 'ready' || normalized === 'online') return '健康'
  if (normalized === 'degraded' || normalized === 'partial' || normalized === 'blocked') return '降级'
  if (normalized === 'offline' || normalized === 'unavailable' || normalized === 'error') return '离线'
  return '未检查'
}

function statusClass(value?: string): string {
  const normalized = value?.toLowerCase()
  if (normalized === 'healthy' || normalized === 'ready' || normalized === 'online') return 'healthy'
  if (normalized === 'degraded' || normalized === 'partial' || normalized === 'blocked') return 'degraded'
  if (normalized === 'offline' || normalized === 'unavailable' || normalized === 'error') return 'offline'
  return 'unknown'
}

export default function App() {
  const [active, setActive] = useState<NavKey>(() => navFromHash())
  const [globalMessage, setGlobalMessage] = useState('')
  const [sourceStatuses, setSourceStatuses] = useState<Record<string, string>>({})
  const {overview, status, loading, error, reload} = useWorkbenchData()

  useEffect(() => {
    const handleHash = () => setActive(navFromHash())
    window.addEventListener('hashchange', handleHash)
    window.addEventListener('popstate', handleHash)
    return () => {
      window.removeEventListener('hashchange', handleHash)
      window.removeEventListener('popstate', handleHash)
    }
  }, [])

  const navigate = (key: NavKey) => {
    if (window.location.hash !== `#/${key}`) window.history.pushState({}, '', `#/${key}`)
    setActive(key)
    setGlobalMessage('')
  }

  const setModuleSourceStatus = useCallback((key: NavKey, value: string) => {
    setSourceStatuses((current) => (
      current[key] === value ? current : {...current, [key]: value}
    ))
  }, [])
  const handleWechatSourceStatus = useCallback(
    (value: string) => setModuleSourceStatus('wechat', value),
    [setModuleSourceStatus],
  )
  const handleMpSourceStatus = useCallback(
    (value: string) => setModuleSourceStatus('mp', value),
    [setModuleSourceStatus],
  )
  const handleXhsSourceStatus = useCallback(
    (value: string) => setModuleSourceStatus('xhs', value),
    [setModuleSourceStatus],
  )
  const handleGeoSourceStatus = useCallback(
    (value: string) => setModuleSourceStatus('geo', value),
    [setModuleSourceStatus],
  )

  const connectionStatuses = useMemo(
    () => new Map((status?.connections ?? []).map((item) => [item.system_key, item.status])),
    [status],
  )

  const navStatus = (key: NavKey) => sourceStatuses[key] || connectionStatuses.get(STATUS_SYSTEM_KEYS[key] || '')

  const renderPage = () => {
    if (active === 'overview') return <HomePage overview={overview} status={status} onNavigate={(key) => navigate(key)} />
    if (active === 'wechat') return <WechatIslandPage onSourceStatus={handleWechatSourceStatus} />
    if (active === 'mp') return <MpPage onSourceStatus={handleMpSourceStatus} />
    if (active === 'xhs') return <XhsIslandPage onSourceStatus={handleXhsSourceStatus} />
    return <GeoPage onSourceStatus={handleGeoSourceStatus} />
  }

  return (
    <div className="demo-app">
      <aside className="demo-rail">
        <div className="demo-brand"><b>Content OS</b><span>选题发现与内容采集工作台</span></div>
        <nav aria-label="主导航">
          {NAV_GROUPS.map((group) => (
            <div className="demo-nav-group" key={group.label}>
              <div className="demo-label">{group.label}</div>
              <div className="demo-nav">
                {group.items.map(([key, label]) => (
                  <button className={`demo-nav-button${active === key ? ' active' : ''}`} key={key} type="button" onClick={() => navigate(key)} aria-current={active === key ? 'page' : undefined}>
                    <NavIcon name={key} />
                    <span>{label}</span>
                    {key !== 'overview' && <small className={`demo-nav-state ${statusClass(navStatus(key))}`}>{statusText(navStatus(key))}</small>}
                  </button>
                ))}
              </div>
            </div>
          ))}
        </nav>
        <div className="demo-foot">
          <div className="status-line"><span>运行状态</span><i className={`demo-status-dot ${statusClass(status?.database.status)}`} /></div>
          <div>选题发现 · 内容采集</div>
          <div>本地工作台 · 8799</div>
        </div>
      </aside>
      <section className="demo-shell">
        {active !== 'overview' && (
          <header className="demo-top">
            <div className="demo-title"><b>{NAV_LABELS[active]}</b><small>Content OS · 选题发现与内容采集</small></div>
            <div className="demo-search" aria-hidden="true" />
            <button className="demo-button primary" type="button" onClick={() => setGlobalMessage('Content OS 负责发现、观测和采集；内容资产、生产和发布已迁移到独立 Production OS。')}>边界说明</button>
          </header>
        )}
        {globalMessage && <div className="demo-global-message" role="status">{globalMessage}<button type="button" onClick={() => setGlobalMessage('')}>关闭</button></div>}
        {error && active === 'overview' && <div className="demo-global-error" role="alert"><b>Hub 暂时无法连接</b><span>{error}</span><button type="button" onClick={reload}>重试</button></div>}
        <main className="demo-stage">{renderPage()}</main>
      </section>
      {loading && <div className="demo-loading-indicator" aria-label="工作台正在读取">读取中</div>}
    </div>
  )
}
