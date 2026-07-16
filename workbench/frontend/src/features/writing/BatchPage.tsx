import {useRef, useState} from 'react'

export default function BatchPage() {
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  const iframeRef = useRef<HTMLIFrameElement>(null)

  const handleLoad = () => {
    setState('ready')
    const iframe = iframeRef.current
    if (!iframe) return
    try {
      const doc = iframe.contentDocument
      const win = iframe.contentWindow
      if (!doc || !win) return
      // 注入 CSS：隐藏 WritingMoney 内部的 system-nav 侧边栏（与外层主导航重复）
      const style = doc.createElement('style')
      style.textContent = `
        .layout { grid-template-columns: 0 minmax(0,1fr) !important; }
        .system-nav { display: none !important; }
        .work-surface { grid-column: 1 / -1 !important; margin-left: 14px !important; border-left: 1px solid var(--border-color) !important; border-radius: 16px !important; }
      `
      doc.head.appendChild(style)
      // 调用内部 setMode 确保处于批量成稿模式
      const wmWin = win as unknown as {setMode?: (mode: string) => void}
      if (typeof wmWin.setMode === 'function') wmWin.setMode('batch')
    } catch {
      // 跨域时忽略
    }
  }

  return (
    <div className="wechat-island-page legacy-writing-island">
      <div className="wechat-island-caption">
        <div>
          <strong>WritingMoney · 批量成稿原版业务岛屿</strong>
          <span>保留原版批次管理、关键词×母文章匹配、缺口回跳、成稿队列、抽屉和弹窗。</span>
        </div>
        <span className={`wechat-island-state ${state}`}>
          {state === 'loading' ? '正在读取真实任务' : state === 'error' ? '原版界面载入失败' : '原版界面已载入'}
        </span>
      </div>
      <iframe
        className="wechat-legacy-frame"
        ref={iframeRef}
        title="批量成稿原版业务岛屿"
        src="/legacy/writing/index.html?mode=batch"
        onLoad={handleLoad}
        onError={() => setState('error')}
      />
    </div>
  )
}
