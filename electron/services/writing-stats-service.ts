import type Database from 'better-sqlite3'
import { getProjectDb } from '../database'
import {
  WRITING_STATS_DAY_WINDOW,
  type WritingStatsChapter,
  type WritingStatsDay,
  type WritingStatsOverview,
  type WritingStatsUsageRow,
  type WritingStatsChapterStatus,
} from '../../src/shared/writing-stats'

interface ChapterRow {
  chapterNumber: number
  title: string | null
  status: string
  wordCount: number
  updatedAt: string
}

function toChapterStatus(status: string): WritingStatsChapterStatus {
  return status === 'finalized' || status === 'revised' ? status : 'draft'
}

/** SQLite 的 UTC 时间串转本机日期（YYYY-MM-DD）。 */
function localDate(sqliteTime: string): string {
  const parsed = new Date(`${sqliteTime.replace(' ', 'T')}Z`)
  if (Number.isNaN(parsed.getTime())) return ''
  const year = parsed.getFullYear()
  const month = String(parsed.getMonth() + 1).padStart(2, '0')
  const day = String(parsed.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function dayKey(date: Date): string {
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${date.getFullYear()}-${month}-${day}`
}

function collectDailyActivity(db: Database.Database): WritingStatsDay[] {
  const days = new Map<string, WritingStatsDay>()
  const windowStart = new Date()
  windowStart.setDate(windowStart.getDate() - (WRITING_STATS_DAY_WINDOW - 1))
  for (let index = 0; index < WRITING_STATS_DAY_WINDOW; index++) {
    const date = new Date(windowStart)
    date.setDate(windowStart.getDate() + index)
    days.set(dayKey(date), { date: dayKey(date), llmCalls: 0, failedCalls: 0, completionTokens: 0, newChapters: 0, generatedWords: 0 })
  }

  const calls = db.prepare(`
    SELECT created_at AS createdAt, success AS success, total_tokens AS totalTokens, completion_tokens AS completionTokens
    FROM llm_calls
    WHERE created_at >= datetime('now', ?)
  `).all(`-${WRITING_STATS_DAY_WINDOW} days`) as Array<{ createdAt: string; success: number; totalTokens: number | null; completionTokens: number | null }>
  for (const call of calls) {
    const bucket = days.get(localDate(call.createdAt))
    if (!bucket) continue
    bucket.llmCalls += 1
    if (!call.success) bucket.failedCalls += 1
    bucket.completionTokens += call.completionTokens ?? call.totalTokens ?? 0
  }

  const drafts = db.prepare(`
    SELECT created_at AS createdAt, version, word_count AS wordCount
    FROM drafts
    WHERE created_at >= datetime('now', ?)
  `).all(`-${WRITING_STATS_DAY_WINDOW} days`) as Array<{ createdAt: string; version: number; wordCount: number }>
  for (const draft of drafts) {
    const bucket = days.get(localDate(draft.createdAt))
    if (!bucket) continue
    if (draft.version === 1) bucket.newChapters += 1
    bucket.generatedWords += draft.wordCount
  }

  return [...days.values()].sort((left, right) => left.date.localeCompare(right.date))
}

function collectModelUsage(db: Database.Database): WritingStatsUsageRow[] {
  const rows = db.prepare(`
    SELECT model_id AS modelId, model_name AS modelName, COUNT(*) AS calls, SUM(total_tokens) AS totalTokens
    FROM llm_calls
    GROUP BY model_id, model_name
    ORDER BY calls DESC
    LIMIT 10
  `).all() as Array<{ modelId: string; modelName: string; calls: number; totalTokens: number | null }>
  return rows.map(row => ({
    key: row.modelId,
    label: row.modelName || row.modelId,
    calls: row.calls,
    totalTokens: row.totalTokens,
  }))
}

function collectPurposeUsage(db: Database.Database): WritingStatsUsageRow[] {
  const rows = db.prepare(`
    SELECT purpose AS purposeKey, COUNT(*) AS calls, SUM(total_tokens) AS totalTokens
    FROM llm_calls
    GROUP BY purpose
    ORDER BY calls DESC
    LIMIT 10
  `).all() as Array<{ purposeKey: string; calls: number; totalTokens: number | null }>
  return rows.map(row => ({
    key: row.purposeKey,
    label: row.purposeKey,
    calls: row.calls,
    totalTokens: row.totalTokens,
  }))
}

/** 聚合当前作品的写作统计；只读，不改任何表。 */
export function writingStatsOverview(db: Database.Database | null = getProjectDb()): WritingStatsOverview {
  if (!db) {
    return {
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
  }

  const core = db.prepare(`
    SELECT total_chapters AS totalChapters, words_per_chapter AS wordsPerChapter
    FROM project_core WHERE id = 'main'
  `).get() as { totalChapters: number | null; wordsPerChapter: number | null } | undefined

  const rows = db.prepare(`
    SELECT d.chapter_number AS chapterNumber, b.title AS title, d.status AS status,
      d.word_count AS wordCount, d.updated_at AS updatedAt
    FROM drafts d
    LEFT JOIN blueprints b ON b.chapter_number = d.chapter_number
    WHERE d.version = (SELECT MAX(d2.version) FROM drafts d2 WHERE d2.chapter_number = d.chapter_number)
    ORDER BY d.chapter_number
  `).all() as ChapterRow[]

  const chapters: WritingStatsChapter[] = rows.map(row => ({
    chapterNumber: row.chapterNumber,
    title: row.title ?? '',
    status: toChapterStatus(row.status),
    wordCount: row.wordCount ?? 0,
    updatedAt: row.updatedAt ?? '',
  }))

  const totals = db.prepare(`
    SELECT COUNT(*) AS total,
      SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS successful,
      SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failed
    FROM llm_calls
  `).get() as { total: number; successful: number | null; failed: number | null }

  return {
    plannedChapters: core?.totalChapters ?? 0,
    wordsPerChapter: core?.wordsPerChapter ?? 0,
    chapters,
    totalWords: chapters.reduce((sum, chapter) => sum + chapter.wordCount, 0),
    finalizedChapters: chapters.filter(chapter => chapter.status === 'finalized').length,
    finalizedWords: chapters.filter(chapter => chapter.status === 'finalized').reduce((sum, chapter) => sum + chapter.wordCount, 0),
    llmCalls: {
      total: totals.total ?? 0,
      successful: totals.successful ?? 0,
      failed: totals.failed ?? 0,
    },
    daily: collectDailyActivity(db),
    byModel: collectModelUsage(db),
    byPurpose: collectPurposeUsage(db),
  }
}
