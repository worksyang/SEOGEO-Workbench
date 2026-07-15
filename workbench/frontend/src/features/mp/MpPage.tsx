import {useEffect, useState} from 'react'
import {apiGet, ApiError} from '../../api/client'
import type {MpBootstrapData, MpBootstrapResponse} from '../../types'

type IslandState = 'loading' | 'ready' | 'error'

function sourceStatus(data: MpBootstrapData | null): string {
  const value = data?.source_status
  return value && typeof value === 'object' && 'status' in value
    ? String(value.status ?? 'unknown')
    : 'unknown'
}

export default function MpPage({onSourceStatus}: {onSourceStatus: (status: string) => void}) {
  const [state, setState] = useState<IslandState>('loading')
  const [error, setError] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    apiGet<MpBootstrapResponse>('/api/v1/mp/bootstrap', controller.signal)
      .then((response) => {
        const data = response.data ?? null
        onSourceStatus(sourceStatus(data))
        setState('ready')
      })
      .catch((reason) => {
        if (reason instanceof DOMException && reason.name === 'AbortError') return
        setState('error')
        setError(reason instanceof ApiError || reason instanceof Error ? reason.message : '公众号监控状态读取失败')
        onSourceStatus('offline')
      })
    return () => controller.abort()
  }, [onSourceStatus])

  return (
    <div className="wechat-island-page">
      <div className="wechat-island-caption">
        <div>
          <strong>公众号监控 · 原系统业务岛屿</strong>
          <span>原版账号、分类、执行任务、登录授权、AI 设置和任务日志保持不变；接口先经工作台白名单代理。</span>
        </div>
        <span className={`wechat-island-state ${state}`}>
          {state === 'loading' ? '正在检查真实来源' : state === 'error' ? '来源检查失败' : '原系统已载入'}
        </span>
      </div>
      {error && <div className="wechat-island-error" role="alert">{error}</div>}
      <iframe
        className="wechat-legacy-frame"
        title="公众号监控原系统业务岛屿"
        src="/legacy/mp/index.html"
      />
    </div>
  )
}
