import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createRequire } from 'node:module'
import type BetterSqlite3 from 'better-sqlite3'

import { reviewCenterOverview } from '../review-overview'
import { ReviewCycleRepository } from '../../repositories/review-cycle-repository'

vi.mock('../../repositories/review-cycle-repository', () => ({
  ReviewCycleRepository: { getByReviewId: vi.fn(() => null) },
}))

const require = createRequire(import.meta.url)
const Database = require('better-sqlite3') as typeof import('better-sqlite3')

function createDb(): BetterSqlite3.Database {
  const db = new Database(':memory:')
  db.exec(`
    CREATE TABLE contents (id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL);
    CREATE TABLE drafts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      chapter_number INTEGER NOT NULL,
      version INTEGER NOT NULL DEFAULT 1,
      status TEXT NOT NULL DEFAULT 'draft'
    );
    CREATE TABLE reviews (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      base_draft_id INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
      review_index INTEGER NOT NULL,
      source_draft_chapter_number INTEGER,
      source_draft_version INTEGER,
      source_draft_status TEXT,
      source_content TEXT,
      content_id INTEGER NOT NULL REFERENCES contents(id),
      created_at TEXT DEFAULT (datetime('now'))
    );
  `)
  return db
}

function report(summary: string, items: unknown[]): string {
  return JSON.stringify({
    summary,
    items,
    goalReview: { version: 1, chapterNumber: 1, coverage: 'not_configured', items: [] },
  })
}

function insertReview(
  db: BetterSqlite3.Database,
  options: { chapter: number; draftId?: number; reviewIndex?: number; body: string; createdAt?: string },
): number {
  const draftId = options.draftId ?? db.prepare('INSERT INTO drafts (chapter_number) VALUES (?)').run(options.chapter).lastInsertRowid as number
  const contentId = db.prepare('INSERT INTO contents (body) VALUES (?)').run(options.body).lastInsertRowid as number
  const reviewIndex = options.reviewIndex ?? 1
  const result = db.prepare(`
    INSERT INTO reviews (base_draft_id, review_index, content_id, created_at) VALUES (?, ?, ?, ?)
  `).run(draftId, reviewIndex, contentId, options.createdAt ?? '2026-01-01 08:00:00')
  return Number(result.lastInsertRowid)
}

beforeEach(() => {
  vi.mocked(ReviewCycleRepository.getByReviewId).mockClear()
  vi.mocked(ReviewCycleRepository.getByReviewId).mockImplementation(() => null)
})

describe('reviewCenterOverview', () => {
  it('aggregates the latest review of each chapter with severity counts', () => {
    const db = createDb()
    insertReview(db, {
      chapter: 1, body: report('早稿', [
        { category: '剧情逻辑', severity: 'error', description: '动机断裂' },
        { category: '叙事节奏', severity: 'warning', description: '开场过慢' },
      ]), createdAt: '2026-01-01 08:00:00',
    })
    insertReview(db, {
      chapter: 1, reviewIndex: 2, body: report('修订后', [
        { category: '剧情逻辑', severity: 'error', description: '动机断裂' },
        { category: '人物设定与行为一致性', severity: 'pass', description: '人物行为一致' },
        { category: '时间线与事件顺序', severity: 'unknown', description: '日期待核实' },
      ]), createdAt: '2026-01-02 09:30:00',
    })
    insertReview(db, { chapter: 2, body: report('第二章', [
      { category: '场景衔接', severity: 'warning', description: '转场生硬' },
    ]) })

    const chapters = reviewCenterOverview(db)
    expect(chapters.map(chapter => chapter.chapterNumber)).toEqual([1, 2])
    expect(chapters[0]).toMatchObject({
      chapterNumber: 1,
      summary: '修订后',
      counts: { critical: 1, suggest: 0, pass: 1, verify: 1 },
    })
    expect(chapters[0]?.items).toHaveLength(3)
    expect(chapters[0]?.items.every(item => item.index >= 0)).toBe(true)
    expect(chapters[1]).toMatchObject({ chapterNumber: 2, counts: { critical: 0, suggest: 1, pass: 0, verify: 0 } })
  })

  it('keeps cross-chapter source references on issues', () => {
    const db = createDb()
    insertReview(db, {
      chapter: 3, body: report('跨章', [
        { category: '设定冲突', severity: 'warning', description: '与第 1 章的规则冲突', quote: '铃力代价', sourceChapter: 1 },
      ]),
    })
    const [chapter] = reviewCenterOverview(db)
    expect(chapter?.items[0]).toMatchObject({ sourceChapter: 1, quote: '铃力代价' })
  })

  it('treats a summary-only report as valid instead of a parse error', () => {
    const db = createDb()
    insertReview(db, { chapter: 1, body: '这不是 JSON' })
    insertReview(db, { chapter: 2, body: report('只有总结', []) })

    const chapters = reviewCenterOverview(db)
    expect(chapters.find(chapter => chapter.chapterNumber === 1)).toMatchObject({ parseError: true, items: [] })
    expect(chapters.find(chapter => chapter.chapterNumber === 2)).toMatchObject({ summary: '只有总结', items: [] })
    expect(chapters.find(chapter => chapter.chapterNumber === 2)?.parseError).toBeFalsy()
  })

  it('attaches review-cycle finding statuses to matching report items', () => {
    const db = createDb()
    const reviewId = insertReview(db, {
      chapter: 1, body: report('带状态', [
        { category: '剧情逻辑', severity: 'error', description: '已修的问题' },
        { category: '叙事节奏', severity: 'warning', description: '节奏问题' },
      ]),
    })
    vi.mocked(ReviewCycleRepository.getByReviewId).mockImplementation((_reviewId: number) => {
      expect(_reviewId).toBe(reviewId)
      return {
        version: 2,
        cycleId: 'cycle-1',
        reviewId,
        comparisonVersion: 1,
        mergedHash: 'a'.repeat(64),
        findingSetHash: 'b'.repeat(64),
        revisionStatus: 'merge-committed',
        recheckCount: 0,
        findings: [
          { findingId: 'f1', reviewItemIndex: 0, category: '剧情逻辑', kind: 'literary', status: 'resolved' },
          { findingId: 'f2', reviewItemIndex: 1, category: '叙事节奏', kind: 'literary', status: 'unresolved' },
        ],
      }
    })

    const [chapter] = reviewCenterOverview(db)
    expect(chapter?.items[0]).toMatchObject({ status: 'resolved' })
    expect(chapter?.items[1]).toMatchObject({ status: 'unresolved' })
  })

  it('still lists issues when the review-cycle repository rejects legacy reports', () => {
    const db = createDb()
    insertReview(db, { chapter: 1, body: report('旧格式', [
      { category: '剧情逻辑', severity: 'error', description: '老报告的问题' },
    ]) })
    vi.mocked(ReviewCycleRepository.getByReviewId).mockImplementation(() => {
      throw new Error('REVIEW_CYCLE_INPUT_INVALID')
    })

    const [chapter] = reviewCenterOverview(db)
    expect(chapter?.items).toHaveLength(1)
    expect(chapter?.items[0]).toMatchObject({ description: '老报告的问题', severity: 'error' })
    expect(chapter?.items[0]?.status).toBeUndefined()
  })
})
