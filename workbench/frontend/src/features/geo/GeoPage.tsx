import {useEffect, useState} from 'react'
import {apiGet, ApiError} from '../../api/client'
import type {GeoBootstrapData} from '../../types'

type IslandState = 'loading' | 'ready' | 'error'

function sourceStatus(data: GeoBootstrapData | null): string {
  const value = data?.source_status
  return value && typeof value === 'object' && 'status' in value
    ? String(value.status ?? 'unknown')
    : 'unknown'
}

function statusLabel(value: unknown): string {
  const normalized = String(value || 'unknown')
  if (normalized === 'healthy') return '健康'
  if (normalized === 'degraded' || normalized === 'partial') return '降级'
  if (normalized === 'offline') return '离线'
  if (normalized === 'not_checked') return '未检查'
  return normalized
}

export default function GeoPage({onSourceStatus}: {onSourceStatus: (status: string) => void}) {
  const [state, setState] = useState<IslandState>('loading')
  const [error, setError] = useState('')
  const [bootstrap, setBootstrap] = useState<GeoBootstrapData | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    apiGet<{data?: GeoBootstrapData}>('/api/v1/geo/bootstrap', controller.signal)
      .then((response) => {
        const data = response.data ?? null
        setBootstrap(data)
        const hubStatus = data?.hub_import_status?.status
        onSourceStatus(String(hubStatus || sourceStatus(data)))
        setState('ready')
      })
      .catch((reason) => {
        if (reason instanceof DOMException && reason.name === 'AbortError') return
        setState('error')
        setError(reason instanceof ApiError || reason instanceof Error ? reason.message : 'GEO 状态读取失败')
        onSourceStatus('offline')
      })
    return () => controller.abort()
  }, [onSourceStatus])

  return (
    <div className="wechat-island-page geo-island-page">
      <div className="wechat-island-caption">
        <div>
          <strong>GEO 观察 · 原系统业务岛屿</strong>
          <span>原版问题视角、引用源视角、平台/作者透视、时间快照和本地 JSON 导入保持不变；页面先经工作台同源承载。</span>
        </div>
        <span className={`wechat-island-state ${state}`}>
          {state === 'loading' ? '正在检查真实来源' : state === 'error' ? '来源检查失败' : '原系统已载入'}
        </span>
      </div>
      {error && <div className="wechat-island-error" role="alert">{error}</div>}
      {bootstrap && (
        <div className="module-status-banner amber geo-status-grid" role="status">
          <div>
            <strong>原始 GEO 来源：{statusLabel(sourceStatus(bootstrap))}</strong>
            <span>{String((bootstrap.source_status && typeof bootstrap.source_status === 'object' && 'path' in bootstrap.source_status) ? bootstrap.source_status.path : '原始 SQLite 只读连接')}</span>
          </div>
          <div>
            <strong>Hub 数据底座：{statusLabel(bootstrap.hub_import_status?.status)}</strong>
            <span>{bootstrap.hub_import_status?.message || '尚未获得 Hub 导入回执。'}</span>
          </div>
          <div>
            <strong>最近导入</strong>
            <span>
              已写入 {bootstrap.hub_import_status?.records_written ?? 0} 条，
              失败 {bootstrap.hub_import_status?.records_failed ?? 0} 条
            </span>
          </div>
        </div>
      )}
      <iframe
        className="wechat-legacy-frame"
        title="GEO 观察原系统业务岛屿"
        src="/legacy/geo/index.html"
      />
    </div>
  )
}
