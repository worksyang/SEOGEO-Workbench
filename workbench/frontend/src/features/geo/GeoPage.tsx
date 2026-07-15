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

export default function GeoPage({onSourceStatus}: {onSourceStatus: (status: string) => void}) {
  const [state, setState] = useState<IslandState>('loading')
  const [error, setError] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    apiGet<{data?: GeoBootstrapData}>('/api/v1/geo/bootstrap', controller.signal)
      .then((response) => {
        const data = response.data ?? null
        onSourceStatus(sourceStatus(data))
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
    <div className="wechat-island-page">
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
      <iframe
        className="wechat-legacy-frame"
        title="GEO 观察原系统业务岛屿"
        src="/legacy/geo/index.html"
      />
    </div>
  )
}
