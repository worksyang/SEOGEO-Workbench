import {useState} from 'react'
import {useWorkbenchData} from './hooks/useWorkbenchData'
import './styles/app.css'

const NAV_ITEMS = [
  ['overview', '总览', '⌂'],
  ['wechat', '微信搜一搜', '微'],
  ['mp', '公众号监控', '公'],
  ['xhs', '小红书', '红'],
  ['geo', 'GEO', 'G'],
  ['wiki', 'Wiki 母文章库', 'W'],
  ['writing', 'WritingMoney', '写'],
  ['publish', '发布中心', '发'],
] as const

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

export default function App() {
  const [active, setActive] = useState('overview')
  const {overview, status, loading, error, reload} = useWorkbenchData()

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">全</span>
          <span>
            <strong>全域内容工作台</strong>
            <small>CONTENT OPERATIONS</small>
          </span>
        </div>
        <nav aria-label="主导航">
          {NAV_ITEMS.map(([key, label, icon]) => (
            <button
              className={active === key ? 'nav-item active' : 'nav-item'}
              key={key}
              onClick={() => setActive(key)}
              type="button"
            >
              <span className="nav-icon" aria-hidden="true">{icon}</span>
              <span>{label}</span>
              {key !== 'overview' && <span className="nav-state">待接入</span>}
            </button>
          ))}
        </nav>
        <div className="sidebar-footer">
          <span className={`status-dot ${status?.database.status ?? 'unknown'}`} />
          <span>Hub {status?.database.status === 'healthy' ? '运行正常' : '等待检查'}</span>
        </div>
      </aside>

      <main className="workspace">
        <header className="topbar">
          <div>
            <p className="eyebrow">LOCAL-FIRST · 127.0.0.1</p>
            <h1>{NAV_ITEMS.find(([key]) => key === active)?.[1]}</h1>
          </div>
          <div className="topbar-actions">
            <span className="schema-pill">Schema v{status?.database.schema_version ?? '—'}</span>
            <button className="secondary-button" type="button" onClick={reload}>重新检查</button>
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
                <span className={`status-dot large ${status?.database.status ?? 'unknown'}`} />
                <div>
                  <strong>{loading ? '正在检查' : status?.database.status === 'healthy' ? '数据底座正常' : '数据底座异常'}</strong>
                  <small>{status?.database.integrity ? `integrity: ${status.database.integrity}` : '等待数据库回执'}</small>
                </div>
              </div>
            </section>

            <section className="metric-grid" aria-label="统一数据统计">
              {COUNT_CARDS.map(([key, label]) => (
                <article className="metric-card" key={key}>
                  <span>{label}</span>
                  <strong>{loading ? '—' : formatNumber(overview?.counts[key] ?? 0)}</strong>
                  <small>{overview?.data_state === 'empty' ? '等待历史导入' : '来自 Hub 实时查询'}</small>
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
                  <span className="count-pill">{status?.connections.length ?? 0} 已登记</span>
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
