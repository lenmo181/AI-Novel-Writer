import { describe, expect, it } from 'vitest'
import {
  BOOK_SEARCH_MAX_TERMS,
  buildSnippet,
  contentMatchesAllTerms,
  findTermMatches,
  parseSearchTerms,
  searchTextField,
} from '../book-search'

describe('parseSearchTerms', () => {
  it('splits on whitespace, lowercases and dedupes', () => {
    expect(parseSearchTerms('  灯塔 FOG  fog  雾钟 ')).toEqual(['灯塔', 'fog', '雾钟'])
  })

  it('caps the number of terms', () => {
    const terms = parseSearchTerms('a b c d e f g')
    expect(terms).toHaveLength(BOOK_SEARCH_MAX_TERMS)
    expect(terms).toEqual(['a', 'b', 'c', 'd', 'e'])
  })

  it('returns empty for blank queries', () => {
    expect(parseSearchTerms('   ')).toEqual([])
  })
})

describe('findTermMatches', () => {
  it('finds repeated positions up to the cap', () => {
    expect(findTermMatches('ababab', 'ab', 2)).toEqual([
      { index: 0, length: 2 },
      { index: 2, length: 2 },
    ])
  })

  it('returns empty for empty terms', () => {
    expect(findTermMatches('abc', '', 3)).toEqual([])
  })
})

describe('contentMatchesAllTerms', () => {
  it('requires every term', () => {
    expect(contentMatchesAllTerms('灯塔与雾钟 fog bell', ['灯塔', 'fog'])).toBe(true)
    expect(contentMatchesAllTerms('灯塔与雾钟', ['灯塔', 'fog'])).toBe(false)
  })
})

describe('searchTextField', () => {
  it('matches CJK keywords and builds a snippet', () => {
    const content = '林岚推开灯塔的门，风铃在门梁上响了起来。'.repeat(4)
    const found = searchTextField(content, ['风铃'])
    expect(found).not.toBeNull()
    expect(found?.snippet).toContain('风铃')
    expect(found?.matches).toBe(4)
  })

  it('is case-insensitive for ASCII', () => {
    expect(searchTextField('The Fog Bell rang', ['fog bell'])?.matches).toBe(1)
    expect(searchTextField('the FOG bell rang', ['Fog BELL'])?.matches).toBe(1)
  })

  it('misses when any term is absent', () => {
    expect(searchTextField('只有灯塔没有雾', ['灯塔', '海'])).toBeNull()
  })

  it('caps matches per field', () => {
    const content = '重复重复重复重复重复'
    const found = searchTextField(content, ['重复'], 3)
    expect(found?.matches).toBe(3)
    expect(found?.matchesTruncated).toBe(true)
  })

  it('returns null for empty content or empty terms', () => {
    expect(searchTextField('', ['词'])).toBeNull()
    expect(searchTextField('正文', [])).toBeNull()
  })
})

describe('buildSnippet', () => {
  it('marks clipped edges and collapses whitespace', () => {
    const content = `前文${'很'.repeat(80)}\n命中词在这里\n后文${'长'.repeat(80)}`
    const snippet = buildSnippet(content, { index: content.indexOf('命中词'), length: 3 }, 10)
    expect(snippet.startsWith('…')).toBe(true)
    expect(snippet.endsWith('…')).toBe(true)
    expect(snippet).toContain('命中词在这里')
    expect(snippet).not.toContain('\n')
  })

  it('omits ellipses when the match is at the edge', () => {
    expect(buildSnippet('命中词开头', { index: 0, length: 3 }, 10)).toBe('命中词开头')
  })
})
