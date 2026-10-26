import { Sparkles } from 'lucide-react'

import { navigate } from '../../../stores/navigation/navigation-intents'
import { useLocaleStore } from '../../../stores/locale-store'
import { useWorkflowStore } from '../../../stores/workflow-store'
import { pluralEn } from '../../../shared/plural-en'
import { Button } from '../../ui/Button'
import { useReviewOverview } from '../review/use-review-overview'

/**
 * 书房右侧的工作状态卡：全文审稿的待处理问题与进行中的 AI 任务。
 * 只在有真实数据时出现——空状态交给空书房与更新卡，不堆无意义的卡片。
 */
export function WorkStatusCards() {
  const text = useLocaleStore(s => s.text)
  const { chapters } = useReviewOverview()
  const activeRuns = useWorkflowStore(s => s.activeRuns)
  const activeHere = activeRuns.length

  const openIssues = chapters.reduce((total, chapter) => total
    + chapter.counts.critical + chapter.counts.suggest + chapter.counts.verify, 0)
  const openChapters = chapters.filter(chapter => chapter.counts.critical + chapter.counts.suggest + chapter.counts.verify > 0).length

  if (openIssues === 0 && activeHere === 0) return null

  return (
    <div className="stage-work-status">
      {openIssues > 0 && (
        <section className="stage-work-card" aria-label={text('全文审稿待处理问题', 'Full review open issues')}>
          <p className="stage-work-card-title">{text('全文审稿', 'Full review')}</p>
          <p className="stage-work-card-text">
            {text(
              `${openChapters} 章还有 ${openIssues} 个待处理问题`,
              `${openChapters} ${pluralEn(openChapters, 'chapter')} still ${openChapters === 1 ? 'has' : 'have'} ${openIssues} open ${pluralEn(openIssues, 'issue')}`,
            )}
          </p>
          <Button variant="text" size="sm" onClick={() => navigate({ kind: 'resource', page: 'review-center' })}>
            {text('打开审稿中心', 'Open review center')}
          </Button>
        </section>
      )}
      {activeHere > 0 && (
        <section className="stage-work-card" aria-label={text('进行中的 AI 任务', 'Running AI tasks')}>
          <p className="stage-work-card-title"><Sparkles size={12} aria-hidden="true" /> {text('AI 正在工作', 'AI is working')}</p>
          <p className="stage-work-card-text">
            {text(`${activeHere} 个任务进行中`, `${activeHere} ${pluralEn(activeHere, 'task')} running`)}
          </p>
          <Button variant="text" size="sm" onClick={() => navigate({ kind: 'run', facet: 'output' })}>
            {text('查看运行', 'View runs')}
          </Button>
        </section>
      )}
    </div>
  )
}
