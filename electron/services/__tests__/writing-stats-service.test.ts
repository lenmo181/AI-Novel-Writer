import { createRequire } from 'node:module'
import type BetterSqlite3 from 'better-sqlite3'
import { afterEach, describe, expect, it } from 'vitest'
import { WRITING_STATS_DAY_WINDOW, type WritingStatsOverview } from '../../../src/shared/writing-stats'
import { searchBook } from '../book-search-service'
import { writingStatsOverview } from '../writing-stats-service'

const require = createRequire(import.meta.url)
const Database = require('better-sqlite3') as typeof import('better-sqlite3')

const databases: BetterSqlite3.Database[] = []

function openStatsDatabase(): BetterSqlite3.Database {
  const db = new Database(':memory:')
  databases.push(db)
  db.exec(`
    CREATE TABLE project_core (
      id TEXT PRIMARY KEY,
      total_chapters INTEGER DEFAULT 100,
      words_per_chapter INTEGER DEFAULT 3000,
      premise TEXT DEFAULT '',
      worldbuilding TEXT DEFAULT '',
      synopsis TEXT DEFAULT '',
      core_outline TEXT DEFAULT '',
      world_setting TEXT DEFAULT '',
      golden_finger TEXT DEFAULT '',
      protagonist_profile TEXT DEFAULT '',
      writing_style TEXT DEFAULT '',
      global_guidance TEXT DEFAULT ''
    );
    CREATE TABLE blueprints (
      chapter_number INTEGER PRIMARY KEY,
      title TEXT NOT NULL DEFAULT '',
      purpose TEXT DEFAULT '',
      key_events TEXT DEFAULT '',
      suspense_hook TEXT DEFAULT '',
      user_guidance TEXT DEFAULT '',
      notes TEXT DEFAULT ''
    );
    CREATE TABLE characters (
      name TEXT PRIMARY KEY,
      role TEXT DEFAULT '',
      appearance TEXT DEFAULT '',
      personality TEXT DEFAULT '',
      background TEXT DEFAULT '',
      abilities TEXT DEFAULT '',
      motivation TEXT DEFAULT '',
      relationships TEXT DEFAULT '',
      arc TEXT DEFAULT '',
      notes TEXT DEFAULT ''
    );
    CREATE TABLE contents (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      body TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE drafts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      chapter_number INTEGER NOT NULL,
      version INTEGER NOT NULL,
      status TEXT DEFAULT 'draft',
      content_id INTEGER NOT NULL REFERENCES contents(id),
      word_count INTEGER DEFAULT 0,
      created_at TEXT DEFAULT (datetime('now')),
      updated_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE llm_calls (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      model_id TEXT NOT NULL,
      model_name TEXT DEFAULT '',
      purpose TEXT DEFAULT '',
      prompt_tokens INTEGER DEFAULT 0,
      completion_tokens INTEGER DEFAULT 0,
      total_tokens INTEGER DEFAULT 0,
      duration_ms INTEGER DEFAULT 0,
      success INTEGER DEFAULT 1,
      error_message TEXT DEFAULT '',
      created_at TEXT DEFAULT (datetime('now'))
    );
  `)
  return db
}

afterEach(() => {
  for (const db of databases.splice(0)) db.close()
})

function seedWritingDatabase(): BetterSqlite3.Database {
  const db = openStatsDatabase()
  db.prepare(`INSERT INTO project_core (id, total_chapters, words_per_chapter) VALUES ('main', 24, 2000)`).run()

  const insertContent = db.prepare('INSERT INTO contents (body) VALUES (?)')
  const insertDraft = db.prepare(`
    INSERT INTO drafts (chapter_number, version, status, content_id, word_count)
    VALUES (?, ?, ?, ?, ?)
  `)
  const insertCall = db.prepare(`
    INSERT INTO llm_calls (model_id, model_name, purpose, total_tokens, completion_tokens, success, created_at)
    VALUES (?, ?, ?, ?, ?, ?, datetime('now', ?))
  `)

  const chapter1v1 = insertContent.run('第一章初稿，提到灯塔和雾钟。').lastInsertRowid as number
  const chapter1v2 = insertContent.run('第一章修稿，仍提到灯塔和雾钟。').lastInsertRowid as number
  const chapter2v1 = insertContent.run('第二章定稿。').lastInsertRowid as number
  const chapter5v1 = insertContent.run('很久以前写的一章。').lastInsertRowid as number
  // created_at 走表默认 datetime('now')，只有 40 天前的旧章单独回拨
  insertDraft.run(1, 1, 'draft', chapter1v1, 800)
  insertDraft.run(1, 2, 'revised', chapter1v2, 850)
  insertDraft.run(2, 1, 'finalized', chapter2v1, 1200)
  insertDraft.run(5, 1, 'draft', chapter5v1, 100)
  db.prepare(`UPDATE drafts SET created_at = datetime('now', '-40 days') WHERE chapter_number = 5`).run()

  db.prepare(`INSERT INTO blueprints (chapter_number, title, purpose, key_events) VALUES (1, '雾钟', '立悬念', '雾中钟声')`).run()
  db.prepare(`INSERT INTO blueprints (chapter_number, title) VALUES (2, '灯语')`).run()
  db.prepare(`
    INSERT INTO characters (name, role, background)
    VALUES ('林岚', 'protagonist', '守灯塔的人，怕雾钟声')
  `).run()

  insertCall.run('m-a', '模型甲', 'draft', 900, 700, 1, '-1 day')
  insertCall.run('m-a', '模型甲', 'review', 500, 300, 1, '0 days')
  insertCall.run('m-b', '模型乙', 'draft', 0, 0, 0, '0 days')
  insertCall.run('m-c', '旧模型', 'draft', 100, 50, 1, '-60 days')

  return db
}

describe('writingStatsOverview', () => {
  it('aggregates latest drafts, finalizations and calls', () => {
    const stats: WritingStatsOverview = writingStatsOverview(seedWritingDatabase())

    expect(stats.plannedChapters).toBe(24)
    expect(stats.wordsPerChapter).toBe(2000)
    expect(stats.chapters.map(chapter => chapter.chapterNumber)).toEqual([1, 2, 5])
    expect(stats.chapters[0]).toMatchObject({ wordCount: 850, status: 'revised', title: '雾钟' })
    expect(stats.chapters[1]).toMatchObject({ status: 'finalized', wordCount: 1200 })
    expect(stats.chapters[2].title).toBe('')
    expect(stats.totalWords).toBe(850 + 1200 + 100)
    expect(stats.finalizedChapters).toBe(1)
    expect(stats.finalizedWords).toBe(1200)
    expect(stats.llmCalls).toEqual({ total: 4, successful: 3, failed: 1 })
  })

  it('builds a 30-day window with calls and draft output', () => {
    const stats = writingStatsOverview(seedWritingDatabase())
    expect(stats.daily).toHaveLength(WRITING_STATS_DAY_WINDOW)
    expect(stats.daily.every(day => /^\d{4}-\d{2}-\d{2}$/u.test(day.date))).toBe(true)
    // 60 天前的调用不出现在窗口里
    const windowCalls = stats.daily.reduce((sum, day) => sum + day.llmCalls, 0)
    expect(windowCalls).toBe(3)
    const windowFailures = stats.daily.reduce((sum, day) => sum + day.failedCalls, 0)
    expect(windowFailures).toBe(1)
    // 窗口内 v1 草稿两章（第 1、2 章），40 天前的第 5 章不计
    const newChapters = stats.daily.reduce((sum, day) => sum + day.newChapters, 0)
    expect(newChapters).toBe(2)
    const generatedWords = stats.daily.reduce((sum, day) => sum + day.generatedWords, 0)
    expect(generatedWords).toBe(800 + 850 + 1200)
  })

  it('groups usage by model and purpose', () => {
    const stats = writingStatsOverview(seedWritingDatabase())
    const byModel = stats.byModel.find(row => row.key === 'm-a')
    expect(byModel).toMatchObject({ label: '模型甲', calls: 2, totalTokens: 1400 })
    const byPurpose = stats.byPurpose.find(row => row.key === 'review')
    expect(byPurpose?.calls).toBe(1)
    expect(stats.byPurpose.find(row => row.key === '')).toBeUndefined()
  })

  it('returns an empty overview without a database', () => {
    const stats = writingStatsOverview(null)
    expect(stats.chapters).toEqual([])
    expect(stats.totalWords).toBe(0)
    expect(stats.daily).toEqual([])
  })
})

describe('searchBook', () => {
  it('finds hits in chapters, blueprints and characters', () => {
    const result = searchBook('雾钟', seedWritingDatabase())
    expect(result.query).toBe('雾钟')
    expect(result.totalMatches).toBeGreaterThan(0)
    const scopes = new Set(result.hits.map(hit => hit.scope))
    expect(scopes.has('chapter')).toBe(true)
    expect(scopes.has('blueprint')).toBe(true)
    expect(scopes.has('character')).toBe(true)
    const chapterHit = result.hits.find(hit => hit.scope === 'chapter')
    expect(chapterHit?.chapterNumber).toBe(1)
    expect(chapterHit?.snippet).toContain('雾钟')
  })

  it('requires every term and returns nothing for blank queries', () => {
    const db = seedWritingDatabase()
    expect(searchBook('灯塔 海鸥', db).totalMatches).toBe(0)
    expect(searchBook('   ', db)).toMatchObject({ totalMatches: 0, hits: [], truncated: false })
  })

  it('returns an empty result without a database', () => {
    expect(searchBook('灯塔', null).totalMatches).toBe(0)
  })
})
