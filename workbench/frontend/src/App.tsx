import {useEffect, useMemo, useState, type ReactNode} from 'react'
import {apiGet} from './api/client'
import HomePage from './features/overview/HomePage'
import MpPage from './features/mp/MpPage'
import XhsPage from './features/xhs/XhsPage'
import GeoPage from './features/geo/GeoPage'
import WikiPage from './features/wiki/WikiPage'
import WritingPage from './features/writing/WritingPage'
import PublishingPage from './features/publishing/PublishingPage'
import SystemsPage from './features/systems/SystemsPage'
import GovernancePage from './features/governance/GovernancePage'
import {useWorkbenchData} from './hooks/useWorkbenchData'
import './styles/app.css'
import './styles/demo-shell.css'
import WechatIslandPage from './features/wechat/WechatIslandPage'

type NavKey = 'overview' | 'wechat' | 'xhs' | 'geo' | 'mp' | 'wiki' | 'mother' | 'batch' | 'publish' | 'systems' | 'governance'
type BusinessNavKey = Exclude<NavKey, 'overview' | 'systems' | 'governance'>
type NavGroup = {label: string; items: ReadonlyArray<readonly [NavKey, string]>}

const NAV_GROUPS: ReadonlyArray<NavGroup> = [
  {label: '主页', items: [['overview', '统一首页']]},
  {label: '选题发现', items: [['wechat', '微信关键词'], ['xhs', '小红书关键词'], ['geo', 'GEO 观察']]},
  {label: '内容采集', items: [['mp', '公众号监控']]},
  {label: '内容资产', items: [['wiki', '母文章 Wiki']]},
  {label: '内容生产', items: [['mother', '母文章铸造'], ['batch', '批量成稿']]},
  {label: '内容发布', items: [['publish', '写作与发布']]},
]

const NAV_LABELS: Record<NavKey, string> = {
  overview: '统一首页',
  wechat: '微信关键词',
  xhs: '小红书关键词',
  geo: 'GEO 观察',
  mp: '公众号监控',
  wiki: '母文章 Wiki',
  mother: '母文章铸造',
  batch: '批量成稿',
  publish: '写作与发布',
  systems: '系统状态',
  governance: '数据治理',
}

const STATUS_SYSTEM_KEYS: Partial<Record<BusinessNavKey, string>> = {
  wechat: 'wechat-search',
  mp: 'wechat-mp',
  xhs: 'xhs-search',
  geo: 'geo',
  wiki: 'wiki',
  publish: 'publishing',
}

function navFromHash(): NavKey {
  const key = window.location.hash.replace(/^#\/?/, '') as NavKey
  return key in NAV_LABELS ? key : 'overview'
}

function NavIcon({name}: {name: NavKey}) {
  const paths: Record<string, ReactNode> = {
    overview: <><rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="7" /><rect x="3" y="14" width="7" height="7" /><rect x="14" y="14" width="7" height="7" /></>,
    wechat: <><path d="M4 17V7h16v10" /><path d="M7 11h10M7 14h7" /></>,
    xhs: <><circle cx="12" cy="12" r="8" /><path d="M8 12h8M12 8v8" /></>,
    geo: <><circle cx="12" cy="12" r="8" /><path d="M4 12h16M12 4a13 13 0 0 1 0 16M12 4a13 13 0 0 0 0 16" /></>,
    mp: <><path d="M5 4h14v16H5z" /><path d="M8 8h8M8 12h8M8 16h5" /></>,
    wiki: <><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v16H6.5A2.5 2.5 0 0 0 4 21.5z" /><path d="M4 5.5v16" /></>,
    mother: <><path d="m4 20 4.5-1 10-10-3.5-3.5-10 10z" /><path d="m13.5 7 3.5 3.5" /></>,
    batch: <><rect x="4" y="4" width="6" height="6" /><rect x="14" y="4" width="6" height="6" /><rect x="4" y="14" width="6" height="6" /><path d="M14 17h6M17 14v6" /></>,
    publish: <><path d="M12 16V3" /><path d="m7 8 5-5 5 5" /><path d="M5 13v7h14v-7" /></>,
    systems: <><circle cx="12" cy="12" r="3" /><path d="M19 12h2M3 12h2M12 3v2M12 19v2" /></>,
    governance: <><path d="M3 12h18M3 6h18M3 18h12" /></>,
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
  const [globalQuery, setGlobalQuery] = useState('')
  const [globalMessage, setGlobalMessage] = useState('')
  const [wechatSourceStatus, setWechatSourceStatus] = useState('unknown')
  const [mpSourceStatus, setMpSourceStatus] = useState('unknown')
  const [xhsSourceStatus, setXhsSourceStatus] = useState('unknown')
  const [geoSourceStatus, setGeoSourceStatus] = useState('unknown')
  const {overview, status, loading, error, reload} = useWorkbenchData()

  useEffect(() => {
    const handleHash = () => setActive(navFromHash())
    window.addEventListener('hashchange', handleHash)
    return () => window.removeEventListener('hashchange', handleHash)
  }, [])

  const navigate = (key: NavKey) => {
    if (window.location.hash !== `#/${key}`) window.history.pushState({}, '', `#/${key}`)
    setActive(key)
    setGlobalMessage('')
  }

  useEffect(() => {
    const onPopState = () => setActive(navFromHash())
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])

  const connectionStatuses = useMemo(() => new Map((status?.connections ?? []).map((item) => [item.system_key, item.status])), [status])
  const sourceStatuses: Partial<Record<BusinessNavKey, string>> = {
    wechat: wechatSourceStatus,
    mp: mpSourceStatus,
    xhs: xhsSourceStatus,
    geo: geoSourceStatus,
  }

  const search = async () => {
    const query = globalQuery.trim()
    if (!query) {
      setGlobalMessage('请输入关键词、文章、母文章或批次。')
      return
    }
    try {
      const response = await apiGet<{data?: {items?: unknown[]; total?: number}}>(`/api/v1/contents?query=${encodeURIComponent(query)}&limit=20`)
      const total = response.data?.total ?? response.data?.items?.length ?? 0
      setGlobalMessage(`跨系统搜索已返回 ${total} 条 Hub 事实。`)
      navigate('overview')
    } catch (reason) {
      setGlobalMessage(reason instanceof Error ? reason.message : '跨系统搜索未获得真实回执。')
    }
  }

  const navStatus = (key: BusinessNavKey): string | undefined => {
    const source = sourceStatuses[key]
    if (source && source !== 'unknown') return source
    const systemKey = STATUS_SYSTEM_KEYS[key]
    return systemKey ? connectionStatuses.get(systemKey) : undefined
  }

  const renderPage = () => {
    if (active === 'overview') return <HomePage overview={overview} status={status} onNavigate={(key) => navigate(key)} />
    if (active === 'wechat') return <WechatIslandPage onSourceStatus={setWechatSourceStatus} />
    if (active === 'mp') return <MpPage onSourceStatus={setMpSourceStatus} />
    if (active === 'xhs') return <XhsPage onSourceStatus={setXhsSourceStatus} />
    if (active === 'geo') return <GeoPage onSourceStatus={setGeoSourceStatus} />
    if (active === 'wiki') return <WikiPage />
    if (active === 'mother') return <WritingPage initialTab="forge" />
    if (active === 'batch') return <WritingPage initialTab="batch" />
    if (active === 'publish') return <PublishingPage />
    if (active === 'systems') return <SystemsPage />
    return <GovernancePage />
  }

  return (
    <div className="demo-app">
      <aside className="demo-rail">
        <div className="demo-brand"><b>ContentOS</b><span>你的内容资产总工作台</span></div>
        <div className="demo-label">Observe · 观察层</div>
        <nav className="demo-nav" aria-label="主导航">
          {NAV_GROUPS.flatMap((group) => group.items).map(([key, label]) => (
            <button className={`demo-nav-button${active === key ? ' active' : ''}`} key={key} type="button" onClick={() => navigate(key)} aria-current={active === key ? 'page' : undefined}>
              <NavIcon name={key} />
              <span>{label}</span>
              {key !== 'overview' && <small className={`demo-nav-state ${statusClass(navStatus(key as BusinessNavKey))}`}>{statusText(navStatus(key as BusinessNavKey))}</small>}
            </button>
          ))}
        </nav>
        <div className="demo-rail-secondary" aria-label="运维入口">
          <button type="button" onClick={() => navigate('systems')}>系统状态（运维）</button>
          <button type="button" onClick={() => navigate('governance')}>数据治理（运维）</button>
        </div>
        <div className="demo-foot">
          <div className="status-line"><span>运行状态</span><i className={`demo-status-dot ${statusClass(status?.database.status)}`} /></div>
          <div>原系统结构保持不变</div>
          <div>统一灰色外壳 · 本地工作台</div>
        </div>
      </aside>

      <section className="demo-shell">
        <header className="demo-top">
          <div className="demo-title"><b>{NAV_LABELS[active]}</b><small>从这里进入原来的每一套系统</small></div>
          <div className="demo-search">
            <NavIcon name="overview" />
            <input value={globalQuery} onChange={(event) => setGlobalQuery(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') void search() }} placeholder="跨系统搜索关键词、文章、母文章、批次" aria-label="跨系统搜索" />
          </div>
          <button className="demo-button" type="button" onClick={() => setGlobalMessage('原系统入口将在对应模块完成 provenance 后开放。')}>打开原系统</button>
          <button className="demo-button primary" type="button" onClick={() => { navigate('overview'); setGlobalMessage('今日流程仅显示已返回的真实任务事件。') }}>查看今日流程</button>
        </header>
        {globalMessage && <div className="demo-global-message" role="status">{globalMessage}<button type="button" onClick={() => setGlobalMessage('')}>关闭</button></div>}
        {error && active === 'overview' && <div className="demo-global-error" role="alert"><b>Hub 暂时无法连接</b><span>{error}</span><button type="button" onClick={reload}>重试</button></div>}
        <main className="demo-stage">{renderPage()}</main>
      </section>
      {loading && <div className="demo-loading-indicator" aria-label="工作台正在读取">读取中</div>}
    </div>
  )
}
