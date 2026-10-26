/**
 * 全文审稿中心的聚合数据：把按章存放的审稿报告（reviews + contents）收敛成
 * 「每章最新一次审稿」的问题清单，并带上审稿周期里的问题处理状态。
 *
 * 解析刻意宽容：报告 JSON 由旧版本或外部工具写坏时，该章标记 parseError 而不是让整页失败。
 */
import type BetterSqlite3 from 'better-sqlite3'
import { ReviewCycleRepository } from '../repositories/review-cycle-repository'
import type { ReviewCenterChapter, ReviewCenterIssue, ReviewCenterSeverity, ReviewFindingStatus } from '../../src/shared/review-center'

interface ReviewRow {
  id: number
  chapter_number: number
  created_at: string
  body: string
}

const SEVERITIES: readonly ReviewCenterSeverity[] = ['error', 'warning', 'pass', 'unknown']

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function nonempty(value: unknown): value is string {
  return typeof value === 'string' && Boolean(value.trim())
}

/** 宽容解析：认识 severity/category/description 的条目才收，其余跳过。 */
function parseReportIssues(body: string): { summary: string; items: ReviewCenterIssue[]; parseError: boolean } {
  let raw: unknown
  try { raw = JSON.parse(body) } catch {
    return { summary: '', items: [], parseError: true }
  }
  if (!isRecord(raw)) return { summary: '', items: [], parseError: true }
  const summary = nonempty(raw.summary) ? raw.summary : ''
  const items: ReviewCenterIssue[] = []
  if (Array.isArray(raw.items)) {
    for (const entry of raw.items) {
      if (!isRecord(entry)) continue
      const severity = SEVERITIES.includes(entry.severity as ReviewCenterSeverity)
        ? entry.severity as ReviewCenterSeverity : 'unknown'
      if (!nonempty(entry.category) || !nonempty(entry.description)) continue
      items.push({
        index: items.length,
        category: entry.category,
        severity,
        description: entry.description,
        ...(nonempty(entry.quote) ? { quote: entry.quote } : {}),
        ...(Number.isSafeInteger(entry.sourceChapter) && (entry.sourceChapter as number) >= 1
          ? { sourceChapter: entry.sourceChapter as number }
          : {}),
      })
    }
  }
  return { summary, items, parseError: items.length === 0 && !summary }
}

/** 审稿周期里的问题处理状态，按报告条目序号对回问题。 */
function findingStatuses(reviewId: number, db: BetterSqlite3.Database): Map<number, ReviewFindingStatus> {
  const statuses = new Map<number, ReviewFindingStatus>()
  try {
    const projection = ReviewCycleRepository.getByReviewId(reviewId, db)
    if (!projection) return statuses
    for (const finding of projection.findings) {
      if (finding.reviewItemIndex === null) continue
      statuses.set(finding.reviewItemIndex, finding.status)
    }
  } catch {
    // 周期数据缺失或报告是旧格式：没有状态也照常展示问题本身。
  }
  return statuses
}

/** 每章取最新一次审稿（按审稿序号），章节号取草稿当前归属。 */
export function reviewCenterOverview(db: BetterSqlite3.Database): ReviewCenterChapter[] {
  const rows = db.prepare(`
    SELECT r.id AS id, d.chapter_number AS chapter_number, r.created_at AS created_at, c.body AS body,
           r.review_index AS review_index
    FROM reviews r
    JOIN drafts d ON d.id = r.base_draft_id
    JOIN contents c ON c.id = r.content_id
    WHERE d.chapter_number IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM reviews newer
        JOIN drafts newerDraft ON newerDraft.id = newer.base_draft_id
        WHERE newerDraft.chapter_number = d.chapter_number
          AND (newer.review_index > r.review_index OR newer.review_index = r.review_index AND newer.id > r.id)
      )
    ORDER BY d.chapter_number ASC
  `).all() as unknown as ReviewRow[]

  return rows.map(row => {
    const parsed = parseReportIssues(row.body)
    const statuses = findingStatuses(row.id, db)
    const items = parsed.items.map(item => ({
      ...item,
      ...(statuses.has(item.index) ? { status: statuses.get(item.index)! } : {}),
    }))
    const counts = { critical: 0, suggest: 0, pass: 0, verify: 0 }
    for (const item of items) {
      if (item.severity === 'error') counts.critical += 1
      else if (item.severity === 'warning') counts.suggest += 1
      else if (item.severity === 'unknown') counts.verify += 1
      else counts.pass += 1
    }
    return {
      chapterNumber: row.chapter_number,
      reviewId: row.id,
      reviewedAt: row.created_at,
      summary: parsed.summary,
      counts,
      items,
      ...(parsed.parseError ? { parseError: true } : {}),
    }
  })
}
