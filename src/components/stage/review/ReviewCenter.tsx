import { useMemo, useState } from 'react'
import { CircleAlert, FileSearch, ListChecks, RefreshCw, TriangleAlert } from 'lucide-react'

import { navigate } from '../../../stores/navigation/navigation-intents'
import { useLocaleStore } from '../../../stores/locale-store'
import type { ReviewCenterChapter, ReviewCenterIssue } from '../../../shared/review-center'
import { chapterLabel } from '../../../shared/chapter-label'
import { Button } from '../../ui/Button'
import { EmptyState } from '../../ui/EmptyState'
import { pluralEn } from '../../../shared/plural-en'
import { shelfTime } from '../library/shelf-format'
import { useReviewOverview } from './use-review-overview'

type Text = (zh: string, en: string) => string

const SEVERITY_ORDER: readonly ReviewCenterIssue['severity'][] = ['error', 'warning', 'unknown', 'pass']

const SEVERITY_LABELS: Record<ReviewCenterIssue['severity'], [string, string]> = {
  error: ['严重', 'Critical'],
  warning: ['建议', 'Suggestion'],
  unknown: ['待核实', 'Needs verification'],
  pass: ['通过', 'Pass'],
}

const STATUS_LABELS: Record<NonNullable<ReviewCenterIssue['status']>, [string, string]> = {
  unverified: ['未复核', 'Unverified'],
  unresolved: ['未处理', 'Unresolved'],
  unknown: ['证据不足', 'Inconclusive'],
  resolved: ['已解决', 'Resolved'],
  'author-waived': ['作者豁免', 'Waived'],
}

function now(): Date {
  return new Date()
}

/** 时间线与事件顺序这类英文类别翻成界面语言；不认识的类别原样展示。 */
function categoryLabel(category: string, text: Text): string {
  const categories: Record<string, [string, string]> = {
    '剧情逻辑': ['剧情逻辑', 'Plot logic'],
    '人物设定与行为一致性': ['人物一致性', 'Character consistency'],
    '时间线与事件顺序': ['时间线', 'Timeline'],
    '伏笔及回收': ['伏笔与回收', 'Setup & payoff'],
    '场景衔接': ['场景衔接', 'Scene transitions'],
    '章节重复与矛盾': ['重复与矛盾', 'Repetition & contradictions'],
    '设定冲突': ['设定冲突', 'Setting conflicts'],
    '叙事节奏': ['叙事节奏', 'Pacing'],
    '语言及表达': ['语言表达', 'Language'],
  }
  const copy = categories[category]
  return copy ? text(copy[0], copy[1]) : category
}

function totalIssues(chapters: readonly ReviewCenterChapter[]): { critical: number; suggest: number; verify: number; pass: number } {
  const totals = { critical: 0, suggest: 0, verify: 0, pass: 0 }
  for (const chapter of chapters) {
    totals.critical += chapter.counts.critical
    totals.suggest += chapter.counts.suggest
    totals.verify += chapter.counts.verify
    totals.pass += chapter.counts.pass
  }
  return totals
}

function IssueRow({ issue, chapterNumber, text }: { issue: ReviewCenterIssue; chapterNumber: number; text: Text }) {
  const severityLabel = text(...SEVERITY_LABELS[issue.severity])
  return (
    <li className="stage-review-issue" data-severity={issue.severity}>
      <div className="stage-review-issue-head">
        <span className="stage-review-severity">{severityLabel}</span>
        <span className="stage-review-category">{categoryLabel(issue.category, text)}</span>
        {issue.status && <span className="stage-review-status" data-status={issue.status}>{text(...STATUS_LABELS[issue.status])}</span>}
        <button
          type="button"
          className="stage-review-locate"
          onClick={() => navigate({ kind: 'chapter', chapterNumber })}
        >
          {text(`打开第 ${chapterNumber} 章`, `Open ${chapterLabel(chapterNumber, text)}`)}
        </button>
      </div>
      <p className="stage-review-description">{issue.description}</p>
      {issue.quote && <blockquote className="stage-review-quote">{issue.quote}</blockquote>}
      {issue.sourceChapter !== undefined && issue.sourceChapter !== chapterNumber && (
        <p className="stage-review-source">{text(`跨章核对 · 涉及第 ${issue.sourceChapter} 章`, `Cross-chapter check · chapter ${issue.sourceChapter}`)}</p>
      )}
    </li>
  )
}

function ChapterGroup({ chapter, text }: { chapter: ReviewCenterChapter; text: Text }) {
  const [open, setOpen] = useState(true)
  const openIssues = chapter.items.filter(issue => issue.severity !== 'pass')
  const ariaId = `stage-review-ch-${chapter.chapterNumber}`
  return (
    <section className="stage-review-chapter" aria-labelledby={ariaId}>
      <header className="stage-review-chapter-head">
        <button type="button" className="stage-review-chapter-toggle" aria-expanded={open} aria-controls={ariaId} onClick={() => setOpen(value => !value)}>
          <h3 id={ariaId} className="stage-review-chapter-title">{chapterLabel(chapter.chapterNumber, text)}</h3>
          <span className="stage-review-chapter-meta">
            {chapter.parseError
              ? text('报告无法解析，可能来自旧版本', 'Report unreadable; it may come from an older version')
              : text(
                  `审于 ${shelfTime(chapter.reviewedAt, now(), text)} · ${chapter.counts.critical} 严重 · ${chapter.counts.suggest} 建议 · ${chapter.counts.verify} 待核实`,
                  `Reviewed ${shelfTime(chapter.reviewedAt, now(), text)} · ${chapter.counts.critical} ${pluralEn(chapter.counts.critical, 'critical')} · ${chapter.counts.suggest} ${pluralEn(chapter.counts.suggest, 'suggestion')} · ${chapter.counts.verify} ${pluralEn(chapter.counts.verify, 'item')} to verify`,
                )}
          </span>
          <span className="stage-review-chapter-counts">
            {openIssues.length > 0
              ? text(`${openIssues.length} 个待看`, `${openIssues.length} to look at`)
              : text('无待处理问题', 'No open issues')}
          </span>
        </button>
        <div className="stage-review-chapter-actions">
          <Button variant="text" size="sm" onClick={() => navigate({ kind: 'chapter', chapterNumber: chapter.chapterNumber })}>
            {text('打开本章', 'Open chapter')}
          </Button>
        </div>
      </header>
      {open && (
        <div id={ariaId} className="stage-review-chapter-body">
          {chapter.parseError
            ? (
              <p className="stage-review-parse-error">
                <CircleAlert size={13} aria-hidden="true" />
                {text('这一章的审稿报告读不出来；重新审稿一次会生成新的报告。', 'This chapter’s review report cannot be read; reviewing it again produces a fresh one.')}
              </p>
            )
            : chapter.items.length === 0
              ? <p className="stage-review-none">{text('这次审稿没有留下条目。', 'This review left no findings.')}</p>
              : (
                <ul className="stage-review-issues">
                  {[...chapter.items]
                    .sort((left, right) => SEVERITY_ORDER.indexOf(left.severity) - SEVERITY_ORDER.indexOf(right.severity))
                    .map(issue => <IssueRow key={issue.index} issue={issue} chapterNumber={chapter.chapterNumber} text={text} />)}
                </ul>
              )}
        </div>
      )}
    </section>
  )
}

function StatCard({ kind, count, label }: { kind: ReviewCenterIssue['severity'] | 'chapters'; count: number; label: string }) {
  return (
    <div className="stage-review-stat" data-kind={kind}>
      <b className="pi-num">{count}</b>
      <span>{label}</span>
    </div>
  )
}

/** 筛选生效时的章组：只列出命中的条目，头部保留该章汇总。 */
function FilteredChapterGroup({ chapter, items, text }: { chapter: ReviewCenterChapter; items: ReviewCenterIssue[]; text: Text }) {
  const ariaId = `stage-review-ch-${chapter.chapterNumber}`
  return (
    <section className="stage-review-chapter" aria-labelledby={ariaId}>
      <header className="stage-review-chapter-head">
        <div className="stage-review-chapter-toggle">
          <h3 id={ariaId} className="stage-review-chapter-title">{chapterLabel(chapter.chapterNumber, text)}</h3>
          <span className="stage-review-chapter-meta">
            {text(
              `审于 ${shelfTime(chapter.reviewedAt, now(), text)} · ${chapter.counts.critical} 严重 · ${chapter.counts.suggest} 建议`,
              `Reviewed ${shelfTime(chapter.reviewedAt, now(), text)} · ${chapter.counts.critical} ${pluralEn(chapter.counts.critical, 'critical')} · ${chapter.counts.suggest} ${pluralEn(chapter.counts.suggest, 'suggestion')}`,
            )}
          </span>
          <span className="stage-review-chapter-counts">
            {text(`${items.length} 条命中`, `${items.length} ${pluralEn(items.length, 'match')}`)}
          </span>
        </div>
        <div className="stage-review-chapter-actions">
          <Button variant="text" size="sm" onClick={() => navigate({ kind: 'chapter', chapterNumber: chapter.chapterNumber })}>
            {text('打开本章', 'Open chapter')}
          </Button>
        </div>
      </header>
      <div id={ariaId} className="stage-review-chapter-body">
        <ul className="stage-review-issues">
          {items.map(issue => <IssueRow key={issue.index} issue={issue} chapterNumber={chapter.chapterNumber} text={text} />)}
        </ul>
      </div>
    </section>
  )
}

/**
 * 全文审稿中心：聚合每章最新一次审稿，按严重程度与处理状态筛选，逐条定位到章节。
 * 审稿与修稿仍在单章视图里进行；这里是全书视角的看板与入口。
 */
export default function ReviewCenter() {
  const text = useLocaleStore(s => s.text)
  const { chapters, loading, error, refresh } = useReviewOverview()
  const [severityFilter, setSeverityFilter] = useState<'open' | ReviewCenterIssue['severity']>('open')
  const [statusFilter, setStatusFilter] = useState<'open' | 'resolved-ish' | NonNullable<ReviewCenterIssue['status']>>('open')

  const filteredChapters = useMemo(() => {
    const matches = (issue: ReviewCenterIssue) => {
      const severityOk = severityFilter === 'open' ? issue.severity !== 'pass' : issue.severity === severityFilter
      const statusOk = statusFilter === 'open'
        ? issue.status === undefined || (issue.status !== 'resolved' && issue.status !== 'author-waived')
        : statusFilter === 'resolved-ish'
          ? issue.status === 'resolved' || issue.status === 'author-waived'
          : issue.status === statusFilter
      return severityOk && statusOk
    }
    return chapters
      .map(chapter => ({ chapter, items: chapter.items.filter(matches) }))
      .filter(entry => entry.items.length > 0)
  }, [chapters, severityFilter, statusFilter])

  const totals = useMemo(() => totalIssues(chapters), [chapters])
  const hasAny = chapters.length > 0

  return (
    <div className="stage-review-center" aria-label={text('全文审稿中心', 'Full review center')}>
      <header className="stage-review-head">
        <div>
          <h2 className="stage-review-title">{text('全文审稿', 'Full review')}</h2>
          <p className="stage-review-lede">
            {text(
              '全书视角的问题看板：每章显示最新一次审稿；审稿与修稿在单章视图里进行。',
              'A book-wide issue board: each chapter shows its latest review; reviewing and revising happen in the chapter view.',
            )}
          </p>
        </div>
        <div className="stage-review-head-actions">
          <Button variant="text" size="sm" onClick={refresh} disabled={loading}>
            <RefreshCw size={13} className={loading ? 'stage-update-spin' : undefined} aria-hidden="true" />
            {text('刷新', 'Refresh')}
          </Button>
          <Button variant="secondary" size="sm" onClick={() => navigate({ kind: 'stage', stage: 'manuscript' })}>
            {text('去审稿', 'Go review')}
          </Button>
        </div>
      </header>

      {error && (
        <p className="stage-review-error" role="alert">
          <TriangleAlert size={13} aria-hidden="true" />
          {text('审稿汇总读取失败；作品数据可能暂时不可用。', 'Failed to read the review overview; the book data may be unavailable right now.')}
        </p>
      )}

      {loading && !hasAny
        ? (
          <div className="stage-review-empty" aria-busy="true">
            <p>{text('正在读取审稿记录…', 'Loading review records…')}</p>
          </div>
        )
        : !hasAny
          ? (
            <EmptyState
              icon={<FileSearch size={20} aria-hidden="true" />}
              heading={text('还没有审稿记录', 'No reviews yet')}
              description={text(
                '先在单章视图里对草稿发起审稿；审过的章会自动汇总到这里，按章查看问题、跳回原文。',
                'Start a review from a chapter’s draft; reviewed chapters gather here automatically so you can inspect issues and jump back to the text.',
              )}
              action={
                <div className="stage-review-empty-actions">
                  <Button variant="primary" onClick={() => navigate({ kind: 'stage', stage: 'manuscript' })}>{text('去审稿第一章', 'Review the first chapter')}</Button>
                  <Button variant="text" onClick={() => navigate({ kind: 'stage', stage: 'blueprint' })}>{text('先看章节蓝图', 'Open blueprints')}</Button>
                </div>
              }
            />
          )
          : (
            <>
              <div className="stage-review-stats">
                <StatCard kind="chapters" count={chapters.length} label={text('已审章数', 'Chapters reviewed')} />
                <StatCard kind="error" count={totals.critical} label={text('严重', 'Critical')} />
                <StatCard kind="warning" count={totals.suggest} label={text('建议', 'Suggestions')} />
                <StatCard kind="unknown" count={totals.verify} label={text('待核实', 'To verify')} />
                <StatCard kind="pass" count={totals.pass} label={text('通过', 'Passed')} />
              </div>

              <div className="stage-review-filters" role="group" aria-label={text('筛选问题', 'Filter issues')}>
                <div className="pi-seg" role="radiogroup" aria-label={text('按严重程度', 'By severity')}>
                  {([
                    ['open', text('待处理', 'Open')],
                    ['error', text('严重', 'Critical')],
                    ['warning', text('建议', 'Suggestions')],
                    ['unknown', text('待核实', 'To verify')],
                    ['pass', text('通过', 'Passed')],
                  ] as const).map(([value, label]) => (
                    <button
                      key={value}
                      type="button"
                      role="radio"
                      aria-checked={severityFilter === value}
                      className="pi-seg-item"
                      data-active={severityFilter === value || undefined}
                      onClick={() => setSeverityFilter(value)}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                <div className="pi-seg" role="radiogroup" aria-label={text('按处理状态', 'By status')}>
                  {([
                    ['open', text('未解决', 'Unresolved')],
                    ['unresolved', text('未处理', 'Unresolved items')],
                    ['unknown', text('证据不足', 'Inconclusive')],
                    ['resolved-ish', text('已解决/豁免', 'Resolved / waived')],
                  ] as const).map(([value, label]) => (
                    <button
                      key={value}
                      type="button"
                      role="radio"
                      aria-checked={statusFilter === value}
                      className="pi-seg-item"
                      data-active={statusFilter === value || undefined}
                      onClick={() => setStatusFilter(value)}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </div>

              <div className="stage-review-list">
                {filteredChapters.length === 0
                  ? (
                    <EmptyState
                      icon={<ListChecks size={20} aria-hidden="true" />}
                      heading={text('当前筛选没有问题', 'No issues match this filter')}
                      description={text('换个筛选条件，或回到单章继续处理。', 'Try a different filter, or keep working in the chapter view.')}
                      action={<Button variant="secondary" onClick={() => { setSeverityFilter('open'); setStatusFilter('open') }}>{text('重置筛选', 'Reset filters')}</Button>}
                    />
                  )
                  : filteredChapters.map(({ chapter, items }) => (
                    items.length === chapter.items.length
                      ? <ChapterGroup key={chapter.chapterNumber} chapter={chapter} text={text} />
                      : <FilteredChapterGroup key={chapter.chapterNumber} chapter={chapter} items={items} text={text} />
                  ))}
              </div>
            </>
          )}
    </div>
  )
}
