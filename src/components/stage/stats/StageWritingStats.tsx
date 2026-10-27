import { useEffect, useRef, useState } from 'react'
import { ipc } from '../../../services/ipc-client'
import { displayedErrorMessage } from '../../../i18n/app-errors'
import { useLocaleStore } from '../../../stores/locale-store'
import { useProjectStore } from '../../../stores/project-store'
import { projectSessionContextFromProject } from '../../../shared/project-session-context'
import type { WritingStatsChapter, WritingStatsDay, WritingStatsOverview } from '../../../shared/writing-stats'
import { pluralEn } from '../../../shared/plural-en'

type Text = (zh: string, en: string) => string

const EMPTY: WritingStatsOverview = {
  plannedChapters: 0,
  wordsPerChapter: 0,
  chapters: [],
  totalWords: 0,
  finalizedChapters: 0,
  finalizedWords: 0,
  llmCalls: { total: 0, successful: 0, failed: 0 },
  daily: [],
  byModel: [],
  byPurpose: [],
}

const STATUS_NAMES: Record<WritingStatsChapter['status'], [string, string]> = {
  none: ['未写', 'Not written'],
  draft: ['草稿', 'Draft'],
  revised: ['已修稿', 'Revised'],
  finalized: ['定稿', 'Final'],
}

/** 每日活动一行的说明：日期、调用、产出。 */
function dayTitle(day: WritingStatsDay, text: Text): string {
  return text(
    `${day.date}：AI 调用 ${day.llmCalls} 次（失败 ${day.failedCalls}），成稿 ${day.newChapters} 章，产出 ${day.generatedWords} 字`,
    `${day.date}: ${day.llmCalls} ${pluralEn(day.llmCalls, 'call')} (${day.failedCalls} ${pluralEn(day.failedCalls, 'failed')}), ${day.newChapters} new ${pluralEn(day.newChapters, 'chapter')}, ${day.generatedWords} ${pluralEn(day.generatedWords, 'word')} generated`,
  )
}

function formatCount(value: number, locale: string): string {
  return value.toLocaleString(locale)
}

function formatTokens(value: number | null, locale: string, text: Text): string {
  return value === null ? text('未记录', 'Not recorded') : formatCount(value, locale)
}

/** 每日两根柱：调用次数（浅）与产出字数（深），各自按窗口内最大值等比。 */
function DailyBars({ daily, text, locale }: { daily: WritingStatsDay[]; text: Text; locale: string }) {
  const maxCalls = Math.max(1, ...daily.map(day => day.llmCalls))
  const maxWords = Math.max(1, ...daily.map(day => day.generatedWords))
  return (
    <>
      <div className="stage-stats-daily" role="img" aria-label={text('最近 30 天活动柱状图', 'Bar chart of the last 30 days')}>
        {daily.map(day => (
          <span key={day.date} className="stage-stats-day" title={dayTitle(day, text)}>
            <span
              data-kind="words"
              style={{ height: `${Math.round((day.generatedWords / maxWords) * 60)}%` }}
            />
            <span
              data-kind="calls"
              style={{ height: `${Math.round((day.llmCalls / maxCalls) * 38)}%` }}
            />
          </span>
        ))}
      </div>
      <p className="stage-stats-day-axis" aria-hidden="true">
        <span>{daily[0]?.date.slice(5)}</span>
        <span>{text('浅色 = AI 调用，深色 = 产出字数', 'Light = AI calls, dark = words generated')}</span>
        <span>{daily[daily.length - 1]?.date.slice(5)}</span>
      </p>
      <p className="stage-stats-help">
        {text(
          `30 天内共 ${formatCount(daily.reduce((sum, day) => sum + day.llmCalls, 0), locale)} 次调用、${formatCount(daily.reduce((sum, day) => sum + day.generatedWords, 0), locale)} 字产出。`,
          `${formatCount(daily.reduce((sum, day) => sum + day.llmCalls, 0), locale)} ${pluralEn(daily.reduce((sum, day) => sum + day.llmCalls, 0), 'call')} and ${formatCount(daily.reduce((sum, day) => sum + day.generatedWords, 0), locale)} ${pluralEn(daily.reduce((sum, day) => sum + day.generatedWords, 0), 'word')} generated in 30 days.`,
        )}
      </p>
    </>
  )
}

/** 写作统计：全部数字来自本地数据库已有事实，只读展示。载入结果按租约归属，换作品自动失效。 */
export default function StageWritingStats() {
  const text = useLocaleStore(s => s.text)
  const locale = useLocaleStore(s => s.locale)
  const project = useProjectStore(s => s.currentProject)
  const [loaded, setLoaded] = useState<{ lease: string | undefined; stats: WritingStatsOverview } | null>(null)
  const [failure, setFailure] = useState<{ lease: string | undefined; message: string } | null>(null)
  const loadRequest = useRef(0)

  const sessionLease = project?.sessionLease
  useEffect(() => {
    const request = ++loadRequest.current
    const session = projectSessionContextFromProject(useProjectStore.getState().currentProject)
    if (!session) return
    ipc.invokeWithProjectSession(session, 'db:writing-stats', session.projectPath)
      .then((value: WritingStatsOverview) => {
        if (request !== loadRequest.current) return
        setLoaded({ lease: sessionLease, stats: value })
        setFailure(null)
      })
      .catch((error: unknown) => {
        if (request !== loadRequest.current) return
        setFailure({ lease: sessionLease, message: displayedErrorMessage(useLocaleStore.getState().locale, error) })
      })
  }, [sessionLease])

  const loading = loaded === null && failure === null
  const failed = failure !== null && failure.lease === sessionLease ? failure.message : null
  const stats = loaded !== null && loaded.lease === sessionLease ? loaded.stats : EMPTY

  const written = stats.chapters.length
  const maxChapterWords = Math.max(1, ...stats.chapters.map(chapter => chapter.wordCount))
  const averageWords = stats.finalizedChapters > 0
    ? Math.round(stats.finalizedWords / stats.finalizedChapters)
    : 0
  const isEmpty = !loading && written === 0 && stats.llmCalls.total === 0

  return (
    <div className="stage-stats">
      <h2 className="stage-stats-title">{text('写作统计', 'Writing stats')}</h2>
      <p className="stage-stats-help">
        {text('数字全部来自这部作品的本地记录：草稿、定稿与 AI 调用日志。', 'Every number comes from this book’s local records: drafts, finalizations and AI call logs.')}
      </p>

      {failed && <p className="stage-stats-failed" role="alert">{text('读取统计失败：', 'Could not load stats: ')}{failed}</p>}
      {loading && <p className="stage-stats-help" role="status">{text('正在读取…', 'Loading…')}</p>}
      {isEmpty && (
        <p className="stage-stats-empty">
          {text('还没有任何草稿或 AI 调用。写出第一章后，这里会显示进度、字数与用量。', 'No drafts or AI calls yet. Write the first chapter and progress, word counts and usage will appear here.')}
        </p>
      )}

      {!loading && !isEmpty && (
        <>
          <div className="stage-stats-grid">
            <div className="stage-stats-cell">
              <p className="stage-stats-cell-label">{text('已动笔章节', 'Chapters written')}</p>
              <p className="stage-stats-cell-value">
                {formatCount(written, locale)}
                {stats.plannedChapters > 0 && <span className="stage-stats-cell-meta"> / {formatCount(stats.plannedChapters, locale)} {text('预计', 'planned')}</span>}
              </p>
            </div>
            <div className="stage-stats-cell">
              <p className="stage-stats-cell-label">{text('全部字数', 'Total words')}</p>
              <p className="stage-stats-cell-value">{formatCount(stats.totalWords, locale)}</p>
              {stats.wordsPerChapter > 0 && (
                <p className="stage-stats-cell-meta">{text(`基准 ${formatCount(stats.wordsPerChapter, locale)} 字/章`, `${formatCount(stats.wordsPerChapter, locale)} per chapter`)}</p>
              )}
            </div>
            <div className="stage-stats-cell">
              <p className="stage-stats-cell-label">{text('定稿', 'Finalized')}</p>
              <p className="stage-stats-cell-value">
                {formatCount(stats.finalizedChapters, locale)}
                <span className="stage-stats-cell-meta"> · {formatCount(stats.finalizedWords, locale)} {text('字', 'words')}</span>
              </p>
              {averageWords > 0 && (
                <p className="stage-stats-cell-meta">{text(`均 ${formatCount(averageWords, locale)} 字/章`, `avg ${formatCount(averageWords, locale)} per chapter`)}</p>
              )}
            </div>
            <div className="stage-stats-cell">
              <p className="stage-stats-cell-label">{text('AI 调用', 'AI calls')}</p>
              <p className="stage-stats-cell-value">{formatCount(stats.llmCalls.total, locale)}</p>
              <p className="stage-stats-cell-meta">
                {text(`成功 ${formatCount(stats.llmCalls.successful, locale)} · 失败 ${formatCount(stats.llmCalls.failed, locale)}`, `${formatCount(stats.llmCalls.successful, locale)} ok · ${formatCount(stats.llmCalls.failed, locale)} failed`)}
              </p>
            </div>
          </div>

          {stats.chapters.length > 0 && (
            <section className="stage-stats-section" aria-label={text('每章字数', 'Words per chapter')}>
              <h3 className="stage-stats-section-name">{text('每章字数', 'Words per chapter')}</h3>
              <ul className="stage-stats-chapters">
                {stats.chapters.map(chapter => (
                  <li
                    key={chapter.chapterNumber}
                    className="stage-stats-chapter"
                    data-status={chapter.status}
                    title={text(`${STATUS_NAMES[chapter.status][0]} · 更新于 ${chapter.updatedAt}`, `${STATUS_NAMES[chapter.status][1]} · updated ${chapter.updatedAt}`)}
                  >
                    <span className="stage-stats-chapter-name">
                      {text(`第 ${chapter.chapterNumber} 章`, `Chapter ${chapter.chapterNumber}`)}
                      {chapter.title.trim() !== '' && ` · ${chapter.title.trim()}`}
                    </span>
                    <span className="stage-stats-chapter-bar" aria-hidden="true">
                      <span style={{ width: `${Math.max(2, Math.round((chapter.wordCount / maxChapterWords) * 100))}%` }} />
                    </span>
                    <span className="stage-stats-chapter-count">{formatCount(chapter.wordCount, locale)}</span>
                  </li>
                ))}
              </ul>
            </section>
          )}

          {stats.daily.length > 0 && (
            <section className="stage-stats-section" aria-label={text('最近 30 天', 'Last 30 days')}>
              <h3 className="stage-stats-section-name">{text('最近 30 天', 'Last 30 days')}</h3>
              <DailyBars daily={stats.daily} text={text} locale={locale} />
            </section>
          )}

          {stats.byModel.length > 0 && (
            <section className="stage-stats-section" aria-label={text('按模型', 'By model')}>
              <h3 className="stage-stats-section-name">{text('按模型', 'By model')}</h3>
              <ul className="stage-stats-usage">
                {stats.byModel.map(row => (
                  <li key={row.key}>
                    <span className="stage-stats-usage-label">{row.label}</span>
                    <span className="stage-stats-usage-value">{text(`${formatCount(row.calls, locale)} 次`, `${formatCount(row.calls, locale)} ${pluralEn(row.calls, 'call')}`)}</span>
                    <span className="stage-stats-usage-value">{formatTokens(row.totalTokens, locale, text)} tokens</span>
                  </li>
                ))}
              </ul>
            </section>
          )}

          {stats.byPurpose.length > 0 && (
            <section className="stage-stats-section" aria-label={text('按用途', 'By purpose')}>
              <h3 className="stage-stats-section-name">{text('按用途', 'By purpose')}</h3>
              <ul className="stage-stats-usage">
                {stats.byPurpose.map(row => (
                  <li key={row.key}>
                    <span className="stage-stats-usage-label">{row.label}</span>
                    <span className="stage-stats-usage-value">{text(`${formatCount(row.calls, locale)} 次`, `${formatCount(row.calls, locale)} ${pluralEn(row.calls, 'call')}`)}</span>
                    <span className="stage-stats-usage-value">{formatTokens(row.totalTokens, locale, text)} tokens</span>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </>
      )}
    </div>
  )
}
