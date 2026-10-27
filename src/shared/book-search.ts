/**
 * 全书检索 — 在正文、蓝图、角色与架构设定里查找关键词。
 * 匹配、计数与摘录都在这里完成；主进程负责取数并只把命中的摘录传回渲染层。
 */

export type BookSearchScope = 'chapter' | 'blueprint' | 'character' | 'outline'

export interface BookSearchHit {
  scope: BookSearchScope
  /** 正文与蓝图命中时的章节号；用于点击后跳转。 */
  chapterNumber?: number
  /** 命中对象的展示标题（章名 / 角色名 / 设定名），主进程用数据库里的名字。 */
  title: string
  /** 命中字段标识，界面用它查出本地化的字段名。 */
  field: string
  snippet: string
  matches: number
}

export interface BookSearchResult {
  query: string
  totalMatches: number
  hits: BookSearchHit[]
  /** 命中超过上限被截断时为真；界面提示作者换更具体的关键词。 */
  truncated: boolean
}

/** 单次检索最多返回的命中数。超过只截断，不掩盖「还有更多」。 */
export const BOOK_SEARCH_MAX_HITS = 240
/** 一个字段里最多摘出几条命中。 */
export const BOOK_SEARCH_MAX_PER_FIELD = 4
/** 检索词数上限；更多说明作者在贴整段文字而不是查词。 */
export const BOOK_SEARCH_MAX_TERMS = 5
/** 摘录在命中位置前后各保留的字符数。 */
export const BOOK_SEARCH_SNIPPET_RADIUS = 48

/** 拆词：按空白拆开、去空、去重、转小写（对中文无影响），超出的词丢弃。 */
export function parseSearchTerms(query: string): string[] {
  const terms: string[] = []
  for (const raw of query.split(/\s+/u)) {
    const term = raw.toLowerCase()
    if (term === '' || terms.includes(term)) continue
    terms.push(term)
    if (terms.length >= BOOK_SEARCH_MAX_TERMS) break
  }
  return terms
}

export interface SearchMatch {
  index: number
  length: number
}

/** 在已转小写的文本里找出一个词的前几处位置。 */
export function findTermMatches(lowerContent: string, lowerTerm: string, max: number): SearchMatch[] {
  if (lowerTerm === '' || max <= 0) return []
  const matches: SearchMatch[] = []
  let from = 0
  for (;;) {
    const index = lowerContent.indexOf(lowerTerm, from)
    if (index === -1) break
    matches.push({ index, length: lowerTerm.length })
    if (matches.length >= max) break
    from = index + Math.max(lowerTerm.length, 1)
  }
  return matches
}

/** 所有词都出现才算命中；摘录用第一个词的位置。词条内部再小写一次，调用方传什么大小写都能查。 */
export function contentMatchesAllTerms(lowerContent: string, terms: readonly string[]): boolean {
  return terms.every(term => lowerContent.includes(term.toLowerCase()))
}

/** 把换行折成空格，命中词前后各留 radius 个字符，越界处补省略号。 */
export function buildSnippet(content: string, match: SearchMatch, radius = BOOK_SEARCH_SNIPPET_RADIUS): string {
  const start = Math.max(0, match.index - radius)
  const end = Math.min(content.length, match.index + match.length + radius)
  const before = start > 0 ? '…' : ''
  const after = end < content.length ? '…' : ''
  const body = content
    .slice(start, end)
    .replace(/\s+/gu, ' ')
    .trim()
  return `${before}${body}${after}`
}

/** 一个字段的全部命中摘录；terms 为空或文本不含全部词时返回空数组。 */
export function searchTextField(
  content: string,
  terms: readonly string[],
  max = BOOK_SEARCH_MAX_PER_FIELD,
): { snippet: string; matches: number; matchesTruncated: boolean } | null {
  if (terms.length === 0 || content === '') return null
  const lower = content.toLowerCase()
  const lowered = terms.map(term => term.toLowerCase())
  if (!contentMatchesAllTerms(lower, lowered)) return null
  const positions = findTermMatches(lower, lowered[0], max + 1)
  const matchesTruncated = positions.length > max
  const usable = positions.slice(0, max)
  if (usable.length === 0) return null
  return {
    snippet: buildSnippet(content, usable[0]),
    matches: usable.length,
    matchesTruncated,
  }
}
