/**
 * 章节事实的批量聚合：一次调用算出一批稿各自的审稿与修稿状态。
 *
 * 逐稿发 4~7 个 IPC 的旧路径在大部头项目（几千章）下会变成上万次 IPC 往返，
 * 这里把同样的判定收进主进程：只有「有待合并修稿」的稿才读正文，
 * 其余全部是带索引的小查询。判定语义与渲染端旧实现逐条对齐。
 */
import type BetterSqlite3 from 'better-sqlite3'

import { confirmationSourceReviewId, parseHumanConfirmedReviewSnapshot } from '../../src/shared/human-confirmed-review'
import type { ChapterFacts } from '../../src/shared/chapter-facts'
import type { ExpectedDraftSource } from '../../src/shared/ipc-channels'
import type { ReviewCycleProjection } from '../../src/shared/review-cycle'

export interface ChapterFactsEntry {
  draftId: number
  facts: ChapterFacts
}

interface RevisionRow {
  id: number
  base_draft_id: number
  status: string
  updated_at: string
  source_draft_chapter_number: number | null
  source_draft_version: number | null
  source_draft_status: string | null
  source_content: string | null
  content: string | null
}

interface DraftContentRow {
  id: number
  chapter_number: number
  version: number
  status: string
  body: string
}

interface ReviewRow {
  id: number
  base_draft_id: number
  review_index: number
  created_at: string
  content_id: number
}

const CHUNK = 400

function chunked<T, R>(values: readonly T[], query: (chunk: readonly T[]) => R[]): R[] {
  const result: R[] = []
  for (let offset = 0; offset < values.length;) {
    const chunk = values.slice(offset, offset + CHUNK)
    offset += chunk.length
    if (chunk.length === 0) continue
    result.push(...query(chunk))
  }
  return result
}

function placeholders(count: number): string {
  return Array.from({ length: count }, () => '?').join(', ')
}

/** 与渲染端旧 rowToSourceDraft 一致：冻结源列不齐就没有源稿，只能按 unsourced 处理。 */
function frozenSource(row: Record<string, unknown>): ExpectedDraftSource | null {
  if (
    typeof row.source_draft_chapter_number !== 'number'
    || typeof row.source_draft_version !== 'number'
    || typeof row.source_draft_status !== 'string'
    || typeof row.source_content !== 'string'
  ) return null
  return {
    id: row.base_draft_id as number,
    chapterNumber: row.source_draft_chapter_number,
    version: row.source_draft_version,
    status: row.source_draft_status as ExpectedDraftSource['status'],
    content: row.source_content,
  }
}

/** 修稿生成时冻结的源稿与这一稿现在保存的样子相同，才能合并；与渲染端 isRevisionSourceCurrent 一致。 */
function revisionSourceCurrent(source: ExpectedDraftSource | null | undefined, draft: DraftContentRow): boolean {
  if (!source) return false
  return source.id === draft.id
    && source.chapterNumber === draft.chapter_number
    && source.version === draft.version
    && source.status === draft.status
    && source.content === draft.body
}

function frozenSourceOf(row: RevisionRow): ExpectedDraftSource | null {
  return frozenSource(row as unknown as Record<string, unknown>)
}

/**
 * 聚合一批稿的章节事实。draftIds 传哪些就算哪些；单稿未出现在结果里表示它没有任何记录。
 * 单条查询失败会整体抛出，由调用方决定是否保留旧值。
 */
export function chapterFactsOverview(db: BetterSqlite3.Database, draftIds: readonly number[]): ChapterFactsEntry[] {
  const ids = [...new Set(draftIds.filter(id => Number.isSafeInteger(id) && id > 0))]
  if (ids.length === 0) return []

  const revisionRows = chunked(ids, chunk => db.prepare(`
    SELECT id, base_draft_id, status, updated_at,
           source_draft_chapter_number, source_draft_version, source_draft_status, source_content,
           NULL AS content
    FROM revisions WHERE base_draft_id IN (${placeholders(chunk.length)})
    ORDER BY base_draft_id ASC, revision_index ASC
  `).all(...chunk) as unknown as RevisionRow[])

  const reviewRows = chunked(ids, chunk => db.prepare(`
    SELECT id, base_draft_id, review_index, created_at, content_id
    FROM reviews WHERE base_draft_id IN (${placeholders(chunk.length)})
    ORDER BY base_draft_id ASC, review_index ASC
  `).all(...chunk) as unknown as ReviewRow[])

  const pendingRevisions = revisionRows.filter(row => row.status === 'pending')
  const pendingDraftIds = [...new Set(pendingRevisions.map(row => row.base_draft_id))]

  // 只有有待合并修稿时才读正文：判断修稿生成之后这一稿的保存内容变没变。
  const draftContents = pendingDraftIds.length === 0
    ? []
    : chunked(pendingDraftIds, chunk => db.prepare(`
        SELECT d.id, d.chapter_number, d.version, d.status, c.body AS body
        FROM drafts d JOIN contents c ON c.id = d.content_id
        WHERE d.id IN (${placeholders(chunk.length)})
      `).all(...chunk) as unknown as DraftContentRow[])
  const draftById = new Map(draftContents.map(row => [row.id, row]))

  // 待合并修稿要和冻结源稿比对，冻结源与修稿正文一起在这里读，正文不再跨 IPC。
  const pendingWithContent = pendingRevisions.length === 0
    ? []
    : chunked(pendingRevisions.map(row => row.id), chunk => db.prepare(`
        SELECT r.id, r.base_draft_id, r.status, r.updated_at,
               r.source_draft_chapter_number, r.source_draft_version, r.source_draft_status, r.source_content,
               c.body AS content
        FROM revisions r JOIN contents c ON c.id = r.content_id
        WHERE r.id IN (${placeholders(chunk.length)})
      `).all(...chunk) as unknown as RevisionRow[])
  const pendingById = new Map(pendingWithContent.map(row => [row.id, row]))

  // 每稿最新的审稿（同一稿按审稿序号取最大），复验周期按「作者确认快照」指向的那条审稿找。
  const latestReviews = new Map<number, ReviewRow>()
  for (const row of reviewRows) {
    const current = latestReviews.get(row.base_draft_id)
    if (!current || row.review_index > current.review_index) latestReviews.set(row.base_draft_id, row)
  }
  const reviewBodies = latestReviews.size === 0
    ? []
    : chunked([...latestReviews.values()].map(row => row.content_id), chunk => db.prepare(`
        SELECT id, body FROM contents WHERE id IN (${placeholders(chunk.length)})
      `).all(...chunk) as unknown as Array<{ id: number; body: string }>)
  const bodyById = new Map(reviewBodies.map(row => [row.id, row.body]))

  const cycleStatusByReview = new Map<number, ReviewCycleProjection['revisionStatus']>()
  if (latestReviews.size > 0) {
    const cycleReviewIds = [...latestReviews.values()].map(review => {
      const snapshot = parseHumanConfirmedReviewSnapshot(bodyById.get(review.content_id) ?? '')
      return confirmationSourceReviewId(snapshot, review.id)
    })
    const cycleRows = chunked([...new Set(cycleReviewIds)], chunk => db.prepare(`
      SELECT review_id, revision_status FROM review_cycles WHERE review_id IN (${placeholders(chunk.length)})
    `).all(...chunk) as unknown as Array<{ review_id: number; revision_status: ReviewCycleProjection['revisionStatus'] }>)
    for (const row of cycleRows) cycleStatusByReview.set(row.review_id, row.revision_status)
  }

  const revisionsByDraft = new Map<number, RevisionRow[]>()
  for (const row of revisionRows) {
    const list = revisionsByDraft.get(row.base_draft_id) ?? []
    list.push(pendingById.get(row.id) ?? row)
    revisionsByDraft.set(row.base_draft_id, list)
  }

  const entries: ChapterFactsEntry[] = []
  for (const draftId of ids) {
    const revisions = revisionsByDraft.get(draftId) ?? []
    const pending = revisions.filter(row => row.status === 'pending')
    const draft = draftById.get(draftId)
    const current = draft ? pending.filter(row => revisionSourceCurrent(frozenSourceOf(row), draft)) : []
    const mergedDates = revisions.filter(row => row.status === 'merged').map(row => row.updated_at).sort()
    const latest = latestReviews.get(draftId)
    let cycleStatus: ReviewCycleProjection['revisionStatus'] | null = null
    if (latest) {
      const snapshot = parseHumanConfirmedReviewSnapshot(bodyById.get(latest.content_id) ?? '')
      const cycleReviewId = confirmationSourceReviewId(snapshot, latest.id)
      cycleStatus = cycleStatusByReview.get(cycleReviewId) ?? null
    }
    entries.push({
      draftId,
      facts: {
        mergeable: current.map(row => row.id),
        stale: pending.flatMap(row => current.includes(row)
          ? []
          : [{ id: row.id, reason: frozenSourceOf(row) ? 'edited' as const : 'unsourced' as const }]),
        latestReview: latest ? { createdAt: latest.created_at, cycleStatus } : null,
        lastMergedAt: mergedDates.at(-1) ?? null,
      },
    })
  }
  return entries
}
