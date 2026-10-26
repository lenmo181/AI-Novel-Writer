import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createRequire } from 'node:module'
import type BetterSqlite3 from 'better-sqlite3'

import { chapterFactsOverview } from '../chapter-facts-overview'

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
      status TEXT NOT NULL DEFAULT 'draft',
      content_id INTEGER NOT NULL REFERENCES contents(id)
    );
    CREATE TABLE revisions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      base_draft_id INTEGER NOT NULL REFERENCES drafts(id),
      revision_index INTEGER NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending',
      updated_at TEXT DEFAULT (datetime('now')),
      source_draft_chapter_number INTEGER,
      source_draft_version INTEGER,
      source_draft_status TEXT,
      source_content TEXT,
      content_id INTEGER NOT NULL REFERENCES contents(id)
    );
    CREATE TABLE reviews (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      base_draft_id INTEGER NOT NULL REFERENCES drafts(id),
      review_index INTEGER NOT NULL,
      created_at TEXT DEFAULT (datetime('now')),
      content_id INTEGER NOT NULL REFERENCES contents(id)
    );
    CREATE TABLE review_cycles (
      cycle_id TEXT PRIMARY KEY,
      review_id INTEGER UNIQUE,
      revision_status TEXT NOT NULL DEFAULT 'not-generated'
    );
  `)
  return db
}

function insertContent(db: BetterSqlite3.Database, body: string): number {
  return db.prepare('INSERT INTO contents (body) VALUES (?)').run(body).lastInsertRowid as number
}

function insertDraft(db: BetterSqlite3.Database, options: { chapter: number; status?: string; body: string }): number {
  const contentId = insertContent(db, options.body)
  return db.prepare('INSERT INTO drafts (chapter_number, status, content_id) VALUES (?, ?, ?)')
    .run(options.chapter, options.status ?? 'draft', contentId).lastInsertRowid as number
}

function insertRevision(db: BetterSqlite3.Database, options: {
  draftId: number
  status: string
  body: string
  source?: { chapter: number; version: number; status: string; content: string } | null
  updatedAt?: string
}): number {
  const contentId = insertContent(db, options.body)
  return db.prepare(`
    INSERT INTO revisions (base_draft_id, revision_index, status, updated_at,
      source_draft_chapter_number, source_draft_version, source_draft_status, source_content, content_id)
    VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)
  `).run(
    options.draftId,
    options.status,
    options.updatedAt ?? '2026-01-01 08:00:00',
    options.source?.chapter ?? null,
    options.source?.version ?? null,
    options.source?.status ?? null,
    options.source?.content ?? null,
    contentId,
  ).lastInsertRowid as number
}

function insertReview(db: BetterSqlite3.Database, draftId: number, reviewIndex: number, body: string): number {
  const contentId = insertContent(db, body)
  return db.prepare('INSERT INTO reviews (base_draft_id, review_index, content_id) VALUES (?, ?, ?)')
    .run(draftId, reviewIndex, contentId).lastInsertRowid as number
}

function reviewReport(items: Array<{ category: string; severity: string; description: string }>): string {
  return JSON.stringify({
    summary: '汇总',
    items: items.map((item, index) => ({ index, ...item })),
    goalReview: { version: 1, chapterNumber: 1, coverage: 'not_configured', items: [] },
  })
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('chapterFactsOverview', () => {
  it('splits pending revisions into mergeable and stale by frozen source', () => {
    const db = createDb()
    const draftBody = '当前正文第二版'
    const draftId = insertDraft(db, { chapter: 1, body: draftBody })
    const mergeableId = insertRevision(db, {
      draftId, status: 'pending', body: '修稿一',
      source: { chapter: 1, version: 1, status: 'draft', content: draftBody },
    })
    const editedId = insertRevision(db, {
      draftId, status: 'pending', body: '修稿二',
      source: { chapter: 1, version: 1, status: 'draft', content: '生成时的旧正文' },
    })
    const unsourcedId = insertRevision(db, { draftId, status: 'pending', body: '旧修稿', source: null })

    const [entry] = chapterFactsOverview(db, [draftId])
    expect(entry.draftId).toBe(draftId)
    expect(entry.facts.mergeable).toEqual([mergeableId])
    expect(entry.facts.stale).toEqual([
      { id: editedId, reason: 'edited' },
      { id: unsourcedId, reason: 'unsourced' },
    ])
    expect(entry.facts.latestReview).toBeNull()
    expect(entry.facts.lastMergedAt).toBeNull()
  })

  it('reads the latest review and its cycle status without shipping content over IPC', () => {
    const db = createDb()
    const draftId = insertDraft(db, { chapter: 2, body: '正文' })
    insertReview(db, draftId, 1, reviewReport([{ category: '剧情逻辑', severity: 'error', description: '旧' }]))
    const latestId = insertReview(db, draftId, 2, reviewReport([{ category: '叙事节奏', severity: 'warning', description: '新' }]))
    db.prepare("INSERT INTO review_cycles (cycle_id, review_id, revision_status) VALUES ('c1', ?, 'merge-committed')")
      .run(latestId)

    const [entry] = chapterFactsOverview(db, [draftId])
    expect(entry.facts.latestReview).toMatchObject({ cycleStatus: 'merge-committed' })
    // 修稿与正文都没读：没有 pending 时不碰 contents 的正文查询
    expect(entry.facts.mergeable).toEqual([])
  })

  it('falls back to the latest review itself when no confirmation snapshot exists', () => {
    const db = createDb()
    const draftId = insertDraft(db, { chapter: 1, body: '正文' })
    const latestId = insertReview(db, draftId, 1, reviewReport([
      { category: '剧情逻辑', severity: 'error', description: '普通 AI 审稿' },
    ]))
    db.prepare("INSERT INTO review_cycles (cycle_id, review_id, revision_status) VALUES ('c-latest', ?, 'merge-committed')")
      .run(latestId)

    const [entry] = chapterFactsOverview(db, [draftId])
    expect(entry.facts.latestReview).toMatchObject({ cycleStatus: 'merge-committed' })
  })

  it('reports lastMergedAt from the newest merged revision', () => {
    const db = createDb()
    const draftId = insertDraft(db, { chapter: 1, status: 'revised', body: '正文' })
    insertRevision(db, { draftId, status: 'merged', body: '修稿', updatedAt: '2026-01-01 08:00:00' })
    insertRevision(db, { draftId, status: 'merged', body: '修稿二', updatedAt: '2026-01-03 10:00:00' })

    const [entry] = chapterFactsOverview(db, [draftId])
    expect(entry.facts.lastMergedAt).toBe('2026-01-03 10:00:00')
    expect(entry.facts.mergeable).toEqual([])
    expect(entry.facts.stale).toEqual([])
  })

  it('returns entries for every requested id and nothing for unknown ids', () => {
    const db = createDb()
    const draftId = insertDraft(db, { chapter: 1, body: '正文' })
    const entries = chapterFactsOverview(db, [draftId, 99999])
    expect(entries.map(entry => entry.draftId).sort()).toEqual([draftId, 99999].sort())
    expect(entries.find(entry => entry.draftId === 99999)?.facts.latestReview).toBeNull()
    expect(chapterFactsOverview(db, [])).toEqual([])
  })
})
