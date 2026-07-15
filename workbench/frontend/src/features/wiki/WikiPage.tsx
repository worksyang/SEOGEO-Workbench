import {useState} from 'react'

type FrameState = 'loading' | 'ready' | 'error'

export default function WikiPage() {
  const [state, setState] = useState<FrameState>('loading')
  return (
    <div className="wechat-island-page legacy-wiki-island">
      <div className="wechat-island-caption">
        <div>
          <strong>Wiki / 母文章库 · 原版业务岛屿</strong>
          <span>原版目录懒加载、全文搜索、Markdown 渲染、图片 OCR、灯箱、编辑保存和批量图片操作保持不变；数据经 Hub 兼容 API 接入。</span>
        </div>
        <span className={`wechat-island-state ${state}`}>
          {state === 'loading' ? '正在载入原版界面' : state === 'error' ? '原版界面载入失败' : '原版界面已载入'}
        </span>
      </div>
      <iframe
        className="wechat-legacy-frame"
        title="Wiki 母文章库原版业务岛屿"
        src="/legacy/wiki/wiki.html"
        onLoad={() => setState('ready')}
        onError={() => setState('error')}
      />
    </div>
  )
}
