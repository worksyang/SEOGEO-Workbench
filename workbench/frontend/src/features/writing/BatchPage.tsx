import {useState} from 'react'

export default function BatchPage() {
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')

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
        title="批量成稿原版业务岛屿"
        src="/legacy/writing/index.html?mode=batch"
        onLoad={() => setState('ready')}
        onError={() => setState('error')}
      />
    </div>
  )
}
