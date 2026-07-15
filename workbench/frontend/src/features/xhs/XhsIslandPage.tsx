import {useEffect, useState} from 'react'
import {apiGet, ApiError} from '../../api/client'
import type {XhsApiEnvelope, XhsBootstrapData} from '../../types'

type IslandState = 'loading' | 'ready' | 'error'

export default function XhsIslandPage({onSourceStatus}: {onSourceStatus: (status: string) => void}) {
  const [state, setState] = useState<IslandState>('loading')
  const [error, setError] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    apiGet<XhsApiEnvelope<XhsBootstrapData>>('/api/v1/xhs/bootstrap?summary=1', controller.signal)
      .then((response) => {
        onSourceStatus(response.data?.source_status?.status ?? 'unknown')
        setState('ready')
      })
      .catch((reason) => {
        if (reason instanceof DOMException && reason.name === 'AbortError') return
        setState('error')
        setError(reason instanceof ApiError || reason instanceof Error ? reason.message : '小红书状态读取失败')
        onSourceStatus('offline')
      })
    return () => controller.abort()
  }, [onSourceStatus])

  return (
    <div className="wechat-island-page">
      <div className="wechat-island-caption">
        <div>
          <strong>小红书关键词 · 原系统业务岛屿</strong>
          <span>原版关键词视角、博主透视、关键词管理与笔记 List 保持不变；接口先经工作台白名单代理。</span>
        </div>
        <span className={`wechat-island-state ${state}`}>
          {state === 'loading' ? '正在检查真实来源' : state === 'error' ? '来源检查失败' : '原系统已载入'}
        </span>
      </div>
      {error && <div className="wechat-island-error" role="alert">{error}</div>}
      <iframe
        className="wechat-legacy-frame"
        title="小红书关键词原系统业务岛屿"
        src="/legacy/xhs/monitor.html"
      />
    </div>
  )
}
