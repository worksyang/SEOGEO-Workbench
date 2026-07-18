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
        .layout { grid-template-columns: 0 minmax(0,1fr) !important; height: 100% !important; background: #fff !important; }
        .system-nav { display: none !important; }
        .work-surface { grid-column: 1 / -1 !important; width: auto !important; height: 100% !important; margin: 0 !important; border: 0 !important; border-radius: 0 !important; box-shadow: none !important; }
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
      <iframe
        className="wechat-legacy-frame"
        ref={iframeRef}
        title="批量成稿原版业务岛屿"
        src="/legacy/writing/index.html?mode=batch&demo=1&v=5"
        onLoad={handleLoad}
        onError={() => setState('error')}
      />
    </div>
  )
}
