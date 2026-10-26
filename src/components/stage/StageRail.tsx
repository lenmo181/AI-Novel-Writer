import { BookOpen, FilePen, GitBranch, Library, ListChecks, Settings, Sparkles, Workflow, type LucideIcon } from 'lucide-react'
import { useLocaleStore } from '../../stores/locale-store'
import type { WorkStage } from '../../stores/navigation/navigation-intents'
import type { StageLocation } from '../../stores/navigation/stage-progress'

interface StageRailProps {
  library: boolean
  location: StageLocation | null
  settingsOpen?: boolean
  onLibrary: () => void
  onStage: (stage: WorkStage) => void
  onRun: () => void
  onSettings: () => void
}

type RailItem = {
  id: string
  label: [string, string]
  icon: LucideIcon
  onClick: () => void
  active: boolean
}

/**
 * The application-level rail. StageSpine remains the detailed, project-local
 * navigation; this rail keeps the global destinations visible at a glance.
 */
export default function StageRail({ library, location, settingsOpen, onLibrary, onStage, onRun, onSettings }: StageRailProps) {
  const text = useLocaleStore(s => s.text)
  const activeStage = location?.kind === 'stage' ? location.stage : null

  const primary: RailItem[] = [
    { id: 'library', label: ['书房', 'Library'], icon: Library, onClick: onLibrary, active: library },
    ...(!library ? [
      { id: 'setup', label: ['设定', 'Setup'] as [string, string], icon: Workflow, onClick: () => onStage('setup'), active: activeStage === 'setup' },
      { id: 'architecture', label: ['架构', 'Architecture'] as [string, string], icon: GitBranch, onClick: () => onStage('architecture'), active: activeStage === 'architecture' },
      { id: 'blueprint', label: ['蓝图', 'Blueprint'] as [string, string], icon: ListChecks, onClick: () => onStage('blueprint'), active: activeStage === 'blueprint' },
      { id: 'manuscript', label: ['正文', 'Manuscript'] as [string, string], icon: FilePen, onClick: () => onStage('manuscript'), active: activeStage === 'manuscript' },
      { id: 'run', label: ['AI 运行', 'AI runs'] as [string, string], icon: Sparkles, onClick: onRun, active: false },
    ] : []),
  ]

  const item = (entry: RailItem) => {
    const Icon = entry.icon
    return (
      <button
        key={entry.id}
        type="button"
        className="stage-rail-item"
        aria-current={entry.active ? 'page' : undefined}
        aria-label={text(...entry.label)}
        title={text(...entry.label)}
        onClick={entry.onClick}
      >
        <Icon size={18} strokeWidth={1.75} aria-hidden="true" />
        <span>{text(...entry.label)}</span>
      </button>
    )
  }

  return (
    <nav className="stage-rail" aria-label={text('全局导航', 'Global navigation')}>
      <button
        type="button"
        className="stage-rail-mark"
        aria-label={text('打开书房', 'Open library')}
        title={text('打开书房', 'Open library')}
        onClick={onLibrary}
      >
        <span aria-hidden="true" />
      </button>
      <div className="stage-rail-primary">
        {primary.map(item)}
      </div>
      <div className="stage-rail-footer">
        <button
          type="button"
          className="stage-rail-item"
          aria-current={settingsOpen ? 'page' : undefined}
          aria-label={text('设置', 'Settings')}
          title={text('设置', 'Settings')}
          onClick={onSettings}
        >
          <Settings size={18} strokeWidth={1.75} aria-hidden="true" />
          <span>{text('设置', 'Settings')}</span>
        </button>
        <span className="stage-rail-version" aria-hidden="true"><BookOpen size={12} /></span>
      </div>
    </nav>
  )
}
