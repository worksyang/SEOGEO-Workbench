import {useEffect, useState} from 'react'
import {apiGet, ApiError} from '../../api/client'
import type {WechatBootstrapResponse} from '../../types'

type IslandState = 'loading' | 'ready' | 'error'

export default function WechatIslandPage({onSourceStatus}: {onSourceStatus: (status: string) => void}) {
  const [state, setState] = useState<IslandState>('loading')
  const [error, setError] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    apiGet<WechatBootstrapResponse>('/api/v1/wechat/bootstrap', controller.signal)
      .then((response) => {
        const sourceStatus: unknown = response.data?.source_status
        const status = typeof sourceStatus === 'string'
          ? sourceStatus
          : sourceStatus && typeof sourceStatus === 'object' && 'status' in sourceStatus
            ? String(sourceStatus.status ?? 'unknown')
            : 'unknown'
        onSourceStatus(status)
        setState('ready')
      })
      .catch((reason) => {
        if (reason instanceof DOMException && reason.name === 'AbortError') return
        setState('error')
        setError(reason instanceof ApiError || reason instanceof Error ? reason.message : '微信搜一搜状态读取失败')
        onSourceStatus('offline')
      })
    return () => controller.abort()
  }, [onSourceStatus])

  return (
    <div className="wechat-island-page">
      <div className="wechat-island-caption">
        <div>
          <strong>微信关键词 · 原系统业务岛屿</strong>
          <span>原版四视图、筛选、刷新队列、抽屉与文章详情保持不变；接口先经工作台白名单代理。</span>
        </div>
        <span className={`wechat-island-state ${state}`}>
          {state === 'loading' ? '正在检查真实来源' : state === 'error' ? '来源检查失败' : '原系统已载入'}
        </span>
      </div>
      {error && <div className="wechat-island-error" role="alert">{error}</div>}
      <iframe
        className="wechat-legacy-frame"
        title="微信关键词原系统业务岛屿"
        src="/legacy/wechat/monitor.html?wbv=wechat-v1"
      />
    </div>
  )
}
