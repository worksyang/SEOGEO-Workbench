import {useState} from 'react'

type WritingPageProps = {
  initialTab?: 'overview' | 'forge' | 'batch'
}

export default function WritingPage({initialTab = 'overview'}: WritingPageProps) {
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  const mode = initialTab === 'batch' ? '?mode=batch' : '?mode=mother'
  return (
    <div className="wechat-island-page legacy-writing-island">
      <div className="wechat-island-caption">
        <div>
          <strong>WritingMoney · 原版业务岛屿</strong>
          <span>母文章铸造与批量成稿保留原版工作流、步骤条、素材三态、模板/方案、URL 临时素材、队列、抽屉和弹窗；任务状态由 Hub 提供。</span>
        </div>
        <span className={`wechat-island-state ${state}`}>
          {state === 'loading' ? '正在读取真实任务' : state === 'error' ? '原版界面载入失败' : '原版界面已载入'}
        </span>
      </div>
      <iframe
        className="wechat-legacy-frame"
        title={initialTab === 'batch' ? '批量成稿原版业务岛屿' : '母文章铸造原版业务岛屿'}
        src={`/legacy/writing/index.html${mode}`}
        onLoad={() => setState('ready')}
        onError={() => setState('error')}
      />
    </div>
  )
}
