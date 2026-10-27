import { describe, expect, it } from 'vitest'
import {
  buildEpub,
  buildStoredZip,
  chapterDisplayTitle,
  crc32,
  epubBytesToBase64,
  escapeXmlText,
  proseParagraphsToXhtml,
} from '../epub-builder'

function decodeText(bytes: Uint8Array): string {
  return new TextDecoder('utf-8').decode(bytes)
}

/** 从 ZIP 字节里解出 EOCD、中央目录与各条目，供断言结构。 */
function parseZip(bytes: Uint8Array) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  const u16 = (offset: number) => view.getUint16(offset, true)
  const u32 = (offset: number) => view.getUint32(offset, true)
  let eocd = -1
  for (let offset = bytes.length - 22; offset >= 0; offset--) {
    if (u32(offset) === 0x06054b50) {
      eocd = offset
      break
    }
  }
  expect(eocd).toBeGreaterThanOrEqual(0)
  const entryCount = u16(eocd + 10)
  let centralOffset = u32(eocd + 16)
  const entries: Array<{ name: string; localOffset: number; method: number; size: number; crc: number }> = []
  for (let index = 0; index < entryCount; index++) {
    expect(u32(centralOffset)).toBe(0x02014b50)
    const method = u16(centralOffset + 10)
    const crc = u32(centralOffset + 16)
    const size = u32(centralOffset + 24)
    const nameLength = u16(centralOffset + 28)
    const localOffset = u32(centralOffset + 42)
    const name = decodeText(bytes.subarray(centralOffset + 46, centralOffset + 46 + nameLength))
    entries.push({ name, localOffset, method, size, crc })
    centralOffset += 46 + nameLength
  }
  return { entryCount, entries, u16, u32 }
}

describe('crc32', () => {
  it('matches the IEEE 802.3 check value', () => {
    expect(crc32(new TextEncoder().encode('123456789'))).toBe(0xcbf43926)
  })

  it('handles multibyte UTF-8 bytes deterministically', () => {
    const bytes = new TextEncoder().encode('第1章 雾钟')
    expect(crc32(bytes)).toBe(crc32(bytes))
  })
})

describe('buildStoredZip', () => {
  it('writes stored entries with a correct central directory', () => {
    const first = new TextEncoder().encode('hello')
    const second = new TextEncoder().encode('世界')
    const zip = buildStoredZip([
      { name: 'a.txt', bytes: first },
      { name: 'b.txt', bytes: second },
    ])
    const parsed = parseZip(zip)
    expect(parsed.entryCount).toBe(2)
    expect(parsed.entries.map(entry => entry.name)).toEqual(['a.txt', 'b.txt'])
    expect(parsed.entries.every(entry => entry.method === 0)).toBe(true)
    expect(parsed.entries[0].size).toBe(5)
    expect(parsed.entries[0].crc).toBe(crc32(first))
    // 第二个条目的本地头恰好紧跟在第一个条目数据之后
    expect(parsed.u32(parsed.entries[1].localOffset)).toBe(0x04034b50)
  })
})

describe('proseParagraphsToXhtml', () => {
  it('escapes XML and splits blank-line paragraphs', () => {
    const xhtml = proseParagraphsToXhtml('第一段 <引号> & 符号\n第二行\n\n第二段')
    expect(xhtml).toContain('<p>第一段 &lt;引号&gt; &amp; 符号<br/>第二行</p>')
    expect(xhtml).toContain('<p>第二段</p>')
  })

  it('strips leading markdown heading marks inside paragraphs', () => {
    const xhtml = proseParagraphsToXhtml('# 一级标题\n\n正文继续')
    expect(xhtml).toContain('<p>一级标题</p>')
    expect(xhtml).toContain('<p>正文继续</p>')
  })

  it('keeps an empty paragraph for empty content', () => {
    expect(proseParagraphsToXhtml('')).toBe('<p></p>')
  })
})

describe('chapterDisplayTitle', () => {
  it('follows the writing language', () => {
    expect(chapterDisplayTitle(3, '雾钟', 'zh-CN')).toBe('第3章 雾钟')
    expect(chapterDisplayTitle(3, '', 'zh-CN')).toBe('第3章')
    expect(chapterDisplayTitle(3, 'Fog bell', 'en-US')).toBe('Chapter 3 Fog bell')
  })
})

describe('escapeXmlText', () => {
  it('escapes the five XML entities', () => {
    expect(escapeXmlText(`<a href="x">&'`)).toBe('&lt;a href=&quot;x&quot;&gt;&amp;&apos;')
  })
})

describe('buildEpub', () => {
  const input = {
    title: '灯塔 <试炼>',
    language: 'zh-CN',
    modifiedIso: '2026-10-28T00:00:00.000Z',
    chapters: [
      { chapterNumber: 1, title: '雾钟', content: '开篇第一段。\n\n第二段 begins here.' },
      { chapterNumber: 2, title: '', content: '第二章只有一段。' },
    ],
  }

  it('places mimetype first, stored and uncompressed', () => {
    const zip = buildEpub(input)
    const parsed = parseZip(zip)
    expect(parsed.entries[0].name).toBe('mimetype')
    expect(parsed.entries[0].method).toBe(0)
    const localNameLength = parsed.u16(parsed.entries[0].localOffset + 26)
    expect(decodeText(zip.subarray(parsed.entries[0].localOffset + 30, parsed.entries[0].localOffset + 30 + localNameLength))).toBe('mimetype')
  })

  it('contains container, package, nav and per-chapter documents', () => {
    const zip = buildEpub(input)
    const parsed = parseZip(zip)
    const byName = new Map(parsed.entries.map(entry => [entry.name, entry]))
    expect(byName.get('META-INF/container.xml')).toBeDefined()
    expect(byName.get('OEBPS/content.opf')).toBeDefined()
    expect(byName.get('OEBPS/nav.xhtml')).toBeDefined()
    expect(byName.get('OEBPS/chapter-001.xhtml')).toBeDefined()
    expect(byName.get('OEBPS/chapter-002.xhtml')).toBeDefined()

    const read = (name: string): string => {
      const entry = byName.get(name)
      if (!entry) throw new Error(`missing ${name}`)
      const nameLength = parsed.u16(entry.localOffset + 26)
      const extraLength = parsed.u16(entry.localOffset + 28)
      const start = entry.localOffset + 30 + nameLength + extraLength
      return decodeText(zip.subarray(start, start + entry.size))
    }

    const opf = read('OEBPS/content.opf')
    expect(opf).toContain('<dc:title>灯塔 &lt;试炼&gt;</dc:title>')
    expect(opf).toContain('<dc:language>zh-CN</dc:language>')
    expect(opf).toContain('href="chapter-001.xhtml"')
    expect(opf).toContain('href="chapter-002.xhtml"')
    expect(opf).toContain('properties="nav"')

    const nav = read('OEBPS/nav.xhtml')
    expect(nav).toContain('href="chapter-001.xhtml"')
    expect(nav).toContain('第1章 雾钟')
    expect(nav).toContain('第2章')

    const chapter = read('OEBPS/chapter-001.xhtml')
    expect(chapter).toContain('<h2 class="no-indent">第1章 雾钟</h2>')
    expect(chapter).toContain('<p>开篇第一段。</p>')
    expect(chapter).toContain('<p>第二段 begins here.</p>')
    expect(chapter).toContain('href="style.css"')
  })

  it('is byte-stable for identical input', () => {
    const first = buildEpub(input)
    const second = buildEpub(input)
    expect(Buffer.from(first).equals(Buffer.from(second))).toBe(true)
  })
})

describe('epubBytesToBase64', () => {
  it('round-trips through base64', () => {
    const zip = buildEpub({
      title: '书名',
      language: 'zh-CN',
      modifiedIso: '2026-10-28T00:00:00.000Z',
      chapters: [{ chapterNumber: 1, title: '一', content: '正文' }],
    })
    const decoded = Buffer.from(epubBytesToBase64(zip), 'base64')
    expect(decoded.equals(Buffer.from(zip))).toBe(true)
  })
})
