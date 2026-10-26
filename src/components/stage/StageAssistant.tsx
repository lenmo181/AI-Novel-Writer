import { PanelRightClose, Sparkles } from 'lucide-react'
import { useLocaleStore } from '../../stores/locale-store'
import AIPanel from '../panels/AIPanel'

export default function StageAssistant({ onClose }: { onClose: () => void }) {
  const text = useLocaleStore(s => s.text)

  return (
    <aside className="stage-assistant" aria-label={text('AI 写作助手', 'AI writing assistant')}>
      <div className="stage-assistant-toolbar">
        <div className="stage-assistant-heading">
          <span className="stage-assistant-signal" aria-hidden="true"><Sparkles size={13} /></span>
          <span>{text('AI 协作', 'AI workspace')}</span>
          <span className="stage-assistant-state">{text('就绪', 'Ready')}</span>
        </div>
        <button
          type="button"
          className="stage-assistant-close"
          aria-label={text('关闭 AI 助手', 'Close AI assistant')}
          title={text('关闭 AI 助手', 'Close AI assistant')}
          onClick={onClose}
        >
          <PanelRightClose size={16} aria-hidden="true" />
        </button>
      </div>
      <div className="stage-assistant-body">
        <AIPanel />
      </div>
    </aside>
  )
}
