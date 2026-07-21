import {useCallback, useEffect, useRef, useState} from 'react'

type IslandState = 'loading' | 'ready' | 'error'

export default function WechatIslandPage({onSourceStatus}: {onSourceStatus: (status: string) => void}) {
  const [state, setState] = useState<IslandState>('loading')
  const [error, setError] = useState('')
  const iframeRef = useRef<HTMLIFrameElement>(null)

  const handleLoad = useCallback(() => {
    setError('')
    setState('ready')
    onSourceStatus('online')
  }, [onSourceStatus])

  const handleError = useCallback(() => {
    setState('error')
    setError('微信搜一搜原系统载入失败')
    onSourceStatus('offline')
  }, [onSourceStatus])

  useEffect(() => {
    setState('loading')
    setError('')
    return () => {
      iframeRef.current?.contentWindow?.postMessage(
        {type: 'wechat-island:teardown', wbv: 'wechat-v2'},
        window.location.origin,
      )
    }
  }, [])

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
        ref={iframeRef}
        className="wechat-legacy-frame"
        title="微信关键词原系统业务岛屿"
        src="/legacy/wechat/monitor.html?wbv=wechat-v2"
        onLoad={handleLoad}
        onError={handleError}
      />
    </div>
  )
}
