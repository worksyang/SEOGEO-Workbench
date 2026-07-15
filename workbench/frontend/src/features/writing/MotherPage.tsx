import {useState} from 'react'

export default function MotherPage() {
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')

  return (
    <div className="wechat-island-page legacy-writing-island">
      <div className="wechat-island-caption">
        <div>
          <strong>WritingMoney · 母文章铸造原版业务岛屿</strong>
          <span>保留原版项目列表、三阶段工作区、素材三态、模板/方案、URL 临时素材、队列、抽屉和弹窗。</span>
        </div>
        <span className={`wechat-island-state ${state}`}>
          {state === 'loading' ? '正在读取真实任务' : state === 'error' ? '原版界面载入失败' : '原版界面已载入'}
        </span>
      </div>
      <iframe
        className="wechat-legacy-frame"
        title="母文章铸造原版业务岛屿"
        src="/legacy/writing/index.html?mode=mother"
        onLoad={() => setState('ready')}
        onError={() => setState('error')}
      />
    </div>
  )
}
