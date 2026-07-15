import {useState} from 'react'
import {useWorkbenchData} from './hooks/useWorkbenchData'
import WechatPage from './features/wechat/WechatPage'
import MpPage from './features/mp/MpPage'
import XhsPage from './features/xhs/XhsPage'
import GeoPage from './features/geo/GeoPage'
import WikiPage from './features/wiki/WikiPage'
import WritingPage from './features/writing/WritingPage'
import PublishingPage from './features/publishing/PublishingPage'
import SystemsPage from './features/systems/SystemsPage'
import GovernancePage from './features/governance/GovernancePage'
import './styles/app.css'

type NavKey = 'overview' | 'wechat' | 'mp' | 'xhs' | 'geo' | 'wiki' | 'writing' | 'publish' | 'systems' | 'governance'
type NavGroup = {label: string; items: ReadonlyArray<readonly [NavKey, string]>}

const NAV_GROUPS: ReadonlyArray<NavGroup> = [
  {label: '主页', items: [['overview', '统一首页']]},
  {label: '选题发现', items: [['wechat', '微信关键词'], ['xhs', '小红书关键词'], ['geo', 'GEO 观察']]},
  {label: '内容采集', items: [['mp', '公众号监控']]},
  {label: '内容资产', items: [['wiki', '母文章 Wiki']]},
  {label: '内容生产', items: [['writing', 'WritingMoney']]},
  {label: '内容发布', items: [['publish', '写作与发布']]},
  {label: '运维', items: [['systems', '系统状态'], ['governance', '数据治理']]},
]
const NAV_ITEMS = NAV_GROUPS.flatMap((group) => group.items)

const COUNT_CARDS = [
  ['contents', '统一内容'],
  ['creators', '创作者'],
  ['snapshots', '搜索快照'],
  ['observations', '指标观测'],
  ['geo_answers', 'GEO 回答'],
  ['jobs', '生产任务'],
] as const

function formatNumber(value: number): string {
  return new Intl.NumberFormat('zh-CN').format(value)
}

function statusLabel(value: string | undefined, loading = false): string {
  if (loading) return '检查中'
  if (!value) return '未检查'
  return ({
    healthy: '健康', ready: '健康', online: '健康',
    degraded: '降级', partial: '降级', blocked: '受阻',
    offline: '离线', unavailable: '离线', error: '错误', unknown: '未知',
  }[value.toLowerCase()] ?? value)
}

function statusTone(value: string | undefined): string {
  if (!value) return 'unknown'
  if (['healthy', 'ready', 'online'].includes(value.toLowerCase())) return 'healthy'
  if (['degraded', 'partial', 'blocked'].includes(value.toLowerCase())) return 'degraded'
  if (['offline', 'unavailable', 'error'].includes(value.toLowerCase())) return 'offline'
  return 'unknown'
}

function NavIcon({name}: {name: NavKey}) {
  const paths: Record<string, React.ReactNode> = {
    overview: <><rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="7" /><rect x="3" y="14" width="7" height="7" /><rect x="14" y="14" width="7" height="7" /></>,
    wechat: <><path d="M4 17V7h16v10" /><path d="M7 11h10M7 14h7" /></>,
    xhs: <><circle cx="12" cy="12" r="8" /><path d="M8 12h8M12 8v8" /></>,
    geo: <><circle cx="12" cy="12" r="8" /><path d="M4 12h16M12 4a13 13 0 0 1 0 16M12 4a13 13 0 0 0 0 16" /></>,
    mp: <><path d="M5 4h14v16H5z" /><path d="M8 8h8M8 12h8M8 16h5" /></>,
    wiki: <><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v16H6.5A2.5 2.5 0 0 0 4 21.5z" /><path d="M4 5.5v16" /></>,
    writing: <><path d="m4 20 4.5-1 10-10-3.5-3.5-10 10z" /><path d="m13.5 7 3.5 3.5" /></>,
    publish: <><path d="M12 16V3" /><path d="m7 8 5-5 5 5" /><path d="M5 13v7h14v-7" /></>,
    systems: <><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" /></>,
    governance: <><path d="M3 12h18" /><path d="M3 6h18" /><path d="M3 18h12" /></>,
  }
  return <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">{paths[name]}</svg>
}

export default function App() {
  const [active, setActive] = useState<NavKey>('overview')
  const [wechatSourceStatus, setWechatSourceStatus] = useState('unknown')
  const [mpSourceStatus, setMpSourceStatus] = useState('unknown')
  const [xhsSourceStatus, setXhsSourceStatus] = useState('unknown')
  const [geoSourceStatus, setGeoSourceStatus] = useState('unknown')
  const {overview, status, loading, error, reload} = useWorkbenchData()
  const navStatuses: Partial<Record<NavKey, string>> = {
    wechat: wechatSourceStatus,
    mp: mpSourceStatus,
    xhs: xhsSourceStatus,
    geo: geoSourceStatus,
    systems: status?.database.status,
    governance: status?.database.integrity === 'ok' ? 'healthy' : status?.database.integrity ? 'degraded' : undefined,
  }
  const dataState = overview?.data_state ?? 'unknown'
  const migrationLabel = status?.database.schema_version !== undefined
    ? `架构 v3.2 · 迁移 v${status.database.schema_version}`
    : loading
      ? '架构 v3.2 · 迁移检查中'
      : '架构 v3.2 · 迁移等待回执'

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <strong>内容工作台</strong>
          <small>监控、资产、生产与发布</small>
        </div>
        <nav aria-label="主导航">
          {NAV_GROUPS.map((group) => (
            <div className="nav-group" key={group.label}>
              <div className="nav-label">{group.label}</div>
              {group.items.map(([key, label]) => (
                <button
                  className={active === key ? 'nav-item active' : 'nav-item'}
                  key={key}
                  onClick={() => setActive(key)}
                  type="button"
                >
                  <span className="nav-icon"><NavIcon name={key} /></span>
                  <span>{label}</span>
                  {key !== 'overview' && <span className={`nav-state nav-state-${statusTone(navStatuses[key])}`}>{statusLabel(navStatuses[key], key === 'wechat' && active === 'wechat' && wechatSourceStatus === 'unknown')}</span>}
                </button>
              ))}
            </div>
          ))}
        </nav>
        <div className="sidebar-footer">
          <div><span>运行状态</span><span className={`status-dot ${status?.database.status ?? 'unknown'}`} /></div>
          <small>七套系统入口已统一</small>
          <small>灰色外壳 · 本地工作台</small>
        </div>
      </aside>

      <main className="workspace">
        <header className="topbar">
          <div className="topbar-title">
            <strong>{NAV_ITEMS.find(([key]) => key === active)?.[1]}</strong>
            <small>从这里进入原来的每一套系统</small>
          </div>
          <div className="topbar-actions">
            <span className="schema-pill">{migrationLabel}</span>
            <button className="secondary-button primary" type="button" onClick={reload}>重新检查</button>
          </div>
        </header>

        {error && (
          <section className="error-banner" role="alert">
            <div>
              <strong>后端暂时无法连接</strong>
              <p>{error}</p>
            </div>
            <button type="button" onClick={reload}>重试</button>
          </section>
        )}

        {active === 'overview' ? (
          <div className="overview-page">
            <section className="hero-panel">
              <div>
                <p className="eyebrow">统一事实底座</p>
                <h2>七套旧系统，一个真实工作台</h2>
                <p className="hero-copy">
                  Markdown 保存正文，SQLite 保存身份、关系、时间切片、指标、任务与审计。
                  当前界面只展示后端真实返回的数据，不使用 Demo 常量冒充接入结果。
                </p>
              </div>
              <div className="hero-status">
                <span className={`status-dot large ${statusTone(status?.database.status)}`} />
                <div>
                  <strong>{loading ? '正在检查' : status ? statusLabel(status.database.status) : error ? '离线' : '等待回执'}</strong>
                  <small>{status?.database.integrity ? `integrity: ${status.database.integrity}` : error ?? '等待数据库回执'}</small>
                </div>
              </div>
            </section>

            <section className="metric-grid" aria-label="统一数据统计">
              {COUNT_CARDS.map(([key, label]) => (
                <article className="metric-card" key={key}>
                  <span>{label}</span>
                  <strong>{loading ? '—' : overview ? (dataState === 'empty' ? '暂无' : formatNumber(overview.counts[key])) : '—'}</strong>
                  <small>{error ? '读取失败' : dataState === 'empty' ? 'API 已返回空数据' : overview ? '来自 Hub 实时查询' : '等待 API 回执'}</small>
                </article>
              ))}
            </section>

            <section className="two-column">
              <article className="panel">
                <div className="panel-heading">
                  <div>
                    <p className="eyebrow">连接状态</p>
                    <h3>七套系统适配器</h3>
                  </div>
                  <span className="count-pill">{status ? `${status.connections.length} 已登记` : '未返回连接数据'}</span>
                </div>
                {status?.connections.length ? (
                  <div className="connection-list">
                    {status.connections.map((connection) => (
                      <div className="connection-row" key={connection.system_key}>
                        <span className={`status-dot ${connection.status}`} />
                        <strong>{connection.display_name}</strong>
                        <span>{connection.status}</span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="empty-state">
                    <strong>尚未写入连接探测结果</strong>
                    <p>下一阶段将按真实端口和 API 逐一登记，不会预填“正常”。</p>
                  </div>
                )}
              </article>

              <article className="panel">
                <div className="panel-heading">
                  <div>
                    <p className="eyebrow">不可变边界</p>
                    <h3>原始资产保护</h3>
                  </div>
                </div>
                <div className="guard-list">
                  <div>
                    <span className={status?.readonly_contract.source ? 'guard-ok' : 'guard-warn'}>SOURCE</span>
                    <div><strong>源码参考层</strong><small>只读，不在其中运行新代码</small></div>
                  </div>
                  <div>
                    <span className={status?.readonly_contract.demo ? 'guard-ok' : 'guard-warn'}>DEMO</span>
                    <div><strong>视觉参考文件</strong><small>保留原始哈希，不直接修改</small></div>
                  </div>
                </div>
              </article>
            </section>
          </div>
        ) : active === 'wechat' ? (
          <WechatPage onSourceStatus={setWechatSourceStatus} />
        ) : active === 'mp' ? (
          <MpPage onSourceStatus={setMpSourceStatus} />
        ) : active === 'xhs' ? (
          <XhsPage onSourceStatus={setXhsSourceStatus} />
        ) : active === 'geo' ? (
          <GeoPage onSourceStatus={setGeoSourceStatus} />
        ) : active === 'wiki' ? (
          <WikiPage />
        ) : active === 'writing' ? (
          <WritingPage />
        ) : active === 'publish' ? (
          <PublishingPage />
        ) : active === 'systems' ? (
          <SystemsPage />
        ) : active === 'governance' ? (
          <GovernancePage />
        ) : (
          <section className="module-placeholder">
            <p className="eyebrow">真实接入进行中</p>
            <h2>{NAV_ITEMS.find(([key]) => key === active)?.[1]}</h2>
            <p>此模块尚未连接 Hub 适配器，因此不展示虚构列表或虚构成功状态。</p>
          </section>
        )}
      </main>
    </div>
  )
}
