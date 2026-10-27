import type Database from 'better-sqlite3'
import { getProjectDb } from '../database'
import {
  BOOK_SEARCH_MAX_HITS,
  parseSearchTerms,
  searchTextField,
  type BookSearchHit,
  type BookSearchResult,
  type BookSearchScope,
} from '../../src/shared/book-search'

/** 一个待检索的字段：正文、蓝图、角色或架构设定里的一段文字。 */
interface SearchFieldSource {
  scope: BookSearchScope
  chapterNumber?: number
  title: string
  field: string
  content: string
}

interface LatestDraftRow {
  chapterNumber: number
  title: string | null
  content: string
}

interface BlueprintRow {
  chapterNumber: number
  title: string
  purpose: string
  keyEvents: string
  suspenseHook: string
  userGuidance: string
  notes: string
}

interface CharacterRow {
  name: string
  role: string
  appearance: string
  personality: string
  background: string
  abilities: string
  motivation: string
  relationships: string
  arc: string
  notes: string
}

interface ProjectCoreRow {
  premise: string
  worldbuilding: string
  synopsis: string
  coreOutline: string
  worldSetting: string
  goldenFinger: string
  protagonistProfile: string
  writingStyle: string
  globalGuidance: string
}

function collectChapterFields(db: Database.Database): SearchFieldSource[] {
  const drafts = db.prepare(`
    SELECT d.chapter_number AS chapterNumber, b.title AS title, c.body AS content
    FROM drafts d
    JOIN contents c ON c.id = d.content_id
    LEFT JOIN blueprints b ON b.chapter_number = d.chapter_number
    WHERE d.version = (SELECT MAX(d2.version) FROM drafts d2 WHERE d2.chapter_number = d.chapter_number)
    ORDER BY d.chapter_number
  `).all() as LatestDraftRow[]
  return drafts.map(row => ({
    scope: 'chapter' as const,
    chapterNumber: row.chapterNumber,
    title: row.title ?? '',
    field: 'content',
    content: row.content,
  }))
}

function collectBlueprintFields(db: Database.Database): SearchFieldSource[] {
  const rows = db.prepare(`
    SELECT chapter_number AS chapterNumber, title, purpose, key_events AS keyEvents,
      suspense_hook AS suspenseHook, user_guidance AS userGuidance, notes
    FROM blueprints ORDER BY chapter_number
  `).all() as BlueprintRow[]
  const fields: Array<[Exclude<keyof BlueprintRow, 'chapterNumber'>, string]> = [
    ['title', 'title'],
    ['purpose', 'purpose'],
    ['keyEvents', 'keyEvents'],
    ['suspenseHook', 'suspenseHook'],
    ['userGuidance', 'userGuidance'],
    ['notes', 'notes'],
  ]
  return rows.flatMap(row => fields
    .map(([property, field]) => ({ scope: 'blueprint' as const, chapterNumber: row.chapterNumber, title: row.title, field, content: row[property] ?? '' }))
    .filter(source => source.content !== ''))
}

const CHARACTER_FIELDS: Array<[keyof CharacterRow, string]> = [
  ['role', 'role'],
  ['appearance', 'appearance'],
  ['personality', 'personality'],
  ['background', 'background'],
  ['abilities', 'abilities'],
  ['motivation', 'motivation'],
  ['relationships', 'relationships'],
  ['arc', 'arc'],
  ['notes', 'notes'],
]

function collectCharacterFields(db: Database.Database): SearchFieldSource[] {
  let rows: CharacterRow[]
  try {
    rows = db.prepare(`
      SELECT name, role, appearance, personality, background, abilities, motivation, relationships, arc, notes
      FROM characters ORDER BY name
    `).all() as CharacterRow[]
  } catch {
    // 角色名单的迁移版式在极旧项目里可能缺列；角色检索缺席不该让正文检索失败。
    return []
  }
  return rows.flatMap(row => [
    { scope: 'character' as const, title: row.name, field: 'name', content: row.name },
    ...CHARACTER_FIELDS
      .map(([property, field]) => ({ scope: 'character' as const, title: row.name, field, content: row[property] ?? '' }))
      .filter(source => source.content !== ''),
  ])
}

const OUTLINE_FIELDS: Array<[keyof ProjectCoreRow, string]> = [
  ['premise', 'premise'],
  ['worldbuilding', 'worldbuilding'],
  ['synopsis', 'synopsis'],
  ['coreOutline', 'coreOutline'],
  ['worldSetting', 'worldSetting'],
  ['goldenFinger', 'goldenFinger'],
  ['protagonistProfile', 'protagonistProfile'],
  ['writingStyle', 'writingStyle'],
  ['globalGuidance', 'globalGuidance'],
]

function collectOutlineFields(db: Database.Database): SearchFieldSource[] {
  const row = db.prepare(`
    SELECT premise, worldbuilding, synopsis, core_outline AS coreOutline, world_setting AS worldSetting,
      golden_finger AS goldenFinger, protagonist_profile AS protagonistProfile,
      writing_style AS writingStyle, global_guidance AS globalGuidance
    FROM project_core WHERE id = 'main'
  `).get() as ProjectCoreRow | undefined
  if (!row) return []
  return OUTLINE_FIELDS
    .map(([property, field]) => ({ scope: 'outline' as const, title: '', field, content: row[property] ?? '' }))
    .filter(source => source.content !== '')
}

/**
 * 全书检索：关键词按空白拆词，全部词都出现的字段才算命中；
 * 章节命中排最前（按章号），其余按蓝图、角色、设定分组。
 */
export function searchBook(query: string, db: Database.Database | null = getProjectDb()): BookSearchResult {
  const terms = parseSearchTerms(query)
  if (!db || terms.length === 0) {
    return { query, totalMatches: 0, hits: [], truncated: false }
  }

  const sources = [
    ...collectChapterFields(db),
    ...collectBlueprintFields(db),
    ...collectCharacterFields(db),
    ...collectOutlineFields(db),
  ]

  const hits: BookSearchHit[] = []
  let totalMatches = 0
  let truncated = false
  for (const source of sources) {
    const found = searchTextField(source.content, terms)
    if (!found) continue
    totalMatches += found.matches
    if (hits.length >= BOOK_SEARCH_MAX_HITS) {
      truncated = true
      continue
    }
    hits.push({
      scope: source.scope,
      ...(source.chapterNumber !== undefined ? { chapterNumber: source.chapterNumber } : {}),
      title: source.title,
      field: source.field,
      snippet: found.snippet,
      matches: found.matches,
    })
    if (found.matchesTruncated) truncated = true
  }

  return { query, totalMatches, hits, truncated }
}
