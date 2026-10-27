/**
 * EPUB 导出构建器 — 纯函数，把定稿章节打包成一份 EPUB 3 电子书。
 *
 * ZIP 用无压缩（stored）条目手写：EPUB 要求 mimetype 是第一个条目且不压缩，
 * 正文体量在几 MB 量级，压缩收益可以忽略，换来的是零依赖与完全确定的输出。
 */

export interface EpubChapterInput {
  chapterNumber: number
  title: string
  content: string
}

export interface EpubBuildInput {
  title: string
  author?: string
  /** BCP 47 语言标签，如 zh-CN / en-US。 */
  language: string
  chapters: readonly EpubChapterInput[]
  /** dcterms:modified 需要的 UTC 时间（ISO 8601）。 */
  modifiedIso: string
}

/** mimetype 必须原样作为第一个条目，不得有换行。 */
const MIMETYPE = 'application/epub+zip'

const ZIP_LOCAL_SIGNATURE = 0x04034b50
const ZIP_CENTRAL_SIGNATURE = 0x02014b50
const ZIP_END_SIGNATURE = 0x06054b50

/** 固定的打包时间：同一份内容导出两次得到字节相同的文件，便于测试与去重。 */
const DOS_TIME = 0
const DOS_DATE = (1 << 5) | 1 // 1980-01-01，DOS 纪元起点

export function crc32(bytes: Uint8Array): number {
  let crc = 0xffffffff
  for (const byte of bytes) {
    crc ^= byte
    for (let bit = 0; bit < 8; bit++) {
      crc = (crc >>> 1) ^ ((crc & 1) === 1 ? 0xedb88320 : 0)
    }
  }
  return (crc ^ 0xffffffff) >>> 0
}

interface ZipEntry {
  name: string
  bytes: Uint8Array
}

/** 按给定顺序写一个全 stored 的 ZIP；第一个条目必须由调用方保证是 mimetype。 */
export function buildStoredZip(entries: readonly ZipEntry[]): Uint8Array {
  const encoder = new TextEncoder()
  const chunks: Uint8Array[] = []
  const central: Uint8Array[] = []
  let offset = 0

  const u16 = (value: number): Uint8Array => {
    const out = new Uint8Array(2)
    new DataView(out.buffer).setUint16(0, value, true)
    return out
  }
  const u32 = (value: number): Uint8Array => {
    const out = new Uint8Array(4)
    new DataView(out.buffer).setUint32(0, value, true)
    return out
  }
  const push = (...parts: Uint8Array[]): void => {
    chunks.push(...parts)
  }

  for (const entry of entries) {
    const name = encoder.encode(entry.name)
    const checksum = crc32(entry.bytes)
    push(
      u32(ZIP_LOCAL_SIGNATURE),
      u16(20), u16(0x0800), u16(0),
      u16(DOS_TIME), u16(DOS_DATE),
      u32(checksum), u32(entry.bytes.length), u32(entry.bytes.length),
      u16(name.length), u16(0),
      name, entry.bytes,
    )

    central.push(
      u32(ZIP_CENTRAL_SIGNATURE),
      u16(20), u16(20), u16(0x0800), u16(0),
      u16(DOS_TIME), u16(DOS_DATE),
      u32(checksum), u32(entry.bytes.length), u32(entry.bytes.length),
      u16(name.length), u16(0), u16(0), u16(0), u16(0),
      u32(0), u32(offset),
      name,
    )
    offset += 30 + name.length + entry.bytes.length
  }

  const centralBytes = central.flat()
  const centralSize = centralBytes.reduce((total, part) => total + part.length, 0)
  push(
    ...centralBytes,
    u32(ZIP_END_SIGNATURE),
    u16(0), u16(0),
    u16(entries.length), u16(entries.length),
    u32(centralSize), u32(offset),
    u16(0),
  )
  return concat(chunks)
}

function concat(chunks: readonly Uint8Array[]): Uint8Array {
  const total = chunks.reduce((sum, chunk) => sum + chunk.length, 0)
  const out = new Uint8Array(total)
  let offset = 0
  for (const chunk of chunks) {
    out.set(chunk, offset)
    offset += chunk.length
  }
  return out
}

export function escapeXmlText(value: string): string {
  return value
    .replace(/&/gu, '&amp;')
    .replace(/</gu, '&lt;')
    .replace(/>/gu, '&gt;')
    .replace(/"/gu, '&quot;')
    .replace(/'/gu, '&apos;')
}

/** 章节显示名：与 Markdown 导出同一套规则，写作语言决定「第 N 章」还是「Chapter N」。 */
export function chapterDisplayTitle(chapterNumber: number, title: string, language: string): string {
  const clean = title.trim()
  return language.startsWith('zh')
    ? `第${chapterNumber}章${clean ? ` ${clean}` : ''}`
    : `Chapter ${chapterNumber}${clean ? ` ${clean}` : ''}`
}

/**
 * 正文转 XHTML 段落：空行分段，段内换行折成 <br/>，去掉行首 Markdown 标题记号
 * （章名已由 <h2> 承担，正文里残留的 # 会被原样印出来）。
 */
export function proseParagraphsToXhtml(content: string): string {
  const blocks = content.split(/\r?\n\s*\r?\n/u)
    .map(block => block
      .split(/\r?\n/u)
      .map(line => line.replace(/^ {0,3}#{1,6}[ \t]+/u, '').trimEnd())
      .join('\n')
      .trim())
    .filter(block => block !== '')
  if (blocks.length === 0) return '<p></p>'
  return blocks
    .map(block => `<p>${escapeXmlText(block).replace(/\r?\n/gu, '<br/>')}</p>`)
    .join('\n')
}

function xhtmlDocument(title: string, language: string, body: string, stylesheet: boolean): string {
  return [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<!DOCTYPE html>',
    `<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="${language}" lang="${language}">`,
    '<head>',
    '<meta charset="utf-8"/>',
    `<title>${escapeXmlText(title)}</title>`,
    ...(stylesheet ? ['<link rel="stylesheet" type="text/css" href="style.css"/>'] : []),
    '</head>',
    `<body>\n${body}\n</body>`,
    '</html>',
  ].join('\n')
}

const STYLE_CSS = `body { margin: 5%; line-height: 1.8; }
h2 { line-height: 1.4; }
p { text-indent: 2em; margin: 0 0 0.6em; }
p.no-indent { text-indent: 0; }`

const NAV_HEADING = { zh: '目录', en: 'Contents' } as const

/** 由书名派生的稳定标识；EPUB 只要求非空且在同一本书内稳定。 */
function stableIdentifier(title: string): string {
  let hash = 0x811c9dc5
  for (let index = 0; index < title.length; index++) {
    hash ^= title.charCodeAt(index)
    hash = Math.imul(hash, 0x01000193) >>> 0
  }
  return `urn:pagekin:${hash.toString(16).padStart(8, '0')}`
}

function chapterFileName(chapterNumber: number): string {
  return `chapter-${String(chapterNumber).padStart(3, '0')}.xhtml`
}

export function buildEpub(input: EpubBuildInput): Uint8Array {
  const encoder = new TextEncoder()
  const text = (value: string): Uint8Array => encoder.encode(value)
  const language = input.language || 'zh-CN'
  const navHeading = language.startsWith('zh') ? NAV_HEADING.zh : NAV_HEADING.en

  const chapterFiles = input.chapters.map(chapter => {
    const displayTitle = chapterDisplayTitle(chapter.chapterNumber, chapter.title, language)
    const body = `<h2 class="no-indent">${escapeXmlText(displayTitle)}</h2>\n${proseParagraphsToXhtml(chapter.content)}`
    return {
      chapterNumber: chapter.chapterNumber,
      displayTitle,
      name: `OEBPS/${chapterFileName(chapter.chapterNumber)}`,
      bytes: text(xhtmlDocument(displayTitle, language, body, true)),
    }
  })

  const tocItems = chapterFiles
    .map(file => `<li><a href="${chapterFileName(file.chapterNumber)}">${escapeXmlText(file.displayTitle)}</a></li>`)
    .join('\n')
  const nav = xhtmlDocument(
    navHeading,
    language,
    [
      `<nav epub:type="toc" id="toc" xmlns:epub="http://www.idpf.org/2007/ops">`,
      `<h1>${escapeXmlText(navHeading)}</h1>`,
      '<ol>',
      tocItems,
      '</ol>',
      '</nav>',
    ].join('\n'),
    false,
  )

  const manifestItems = [
    '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
    '<item id="css" href="style.css" media-type="text/css"/>',
    ...chapterFiles.map(file => `<item id="ch${file.chapterNumber}" href="${chapterFileName(file.chapterNumber)}" media-type="application/xhtml+xml"/>`),
  ].join('\n')
  const spineItems = chapterFiles.map(file => `<itemref idref="ch${file.chapterNumber}"/>`).join('\n')
  const authorLine = input.author && input.author.trim() !== ''
    ? `\n    <dc:creator>${escapeXmlText(input.author.trim())}</dc:creator>`
    : ''
  const opf = [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">',
    '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">',
    `    <dc:identifier id="book-id">${stableIdentifier(input.title)}</dc:identifier>`,
    `    <dc:title>${escapeXmlText(input.title)}</dc:title>`,
    `    <dc:language>${escapeXmlText(language)}</dc:language>${authorLine}`,
    `    <meta property="dcterms:modified">${escapeXmlText(input.modifiedIso)}</meta>`,
    '  </metadata>',
    '  <manifest>',
    manifestItems,
    '  </manifest>',
    '  <spine>',
    spineItems,
    '  </spine>',
    '</package>',
  ].join('\n')

  const container = [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">',
    '  <rootfiles>',
    '    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>',
    '  </rootfiles>',
    '</container>',
  ].join('\n')

  return buildStoredZip([
    { name: 'mimetype', bytes: text(MIMETYPE) },
    { name: 'META-INF/container.xml', bytes: text(container) },
    { name: 'OEBPS/content.opf', bytes: text(opf) },
    { name: 'OEBPS/nav.xhtml', bytes: text(nav) },
    { name: 'OEBPS/style.css', bytes: text(STYLE_CSS) },
    ...chapterFiles.map(file => ({ name: file.name, bytes: file.bytes })),
  ])
}

/** 大文件分块 btoa，避免把整本书塞进一次 String.fromCharCode。 */
export function epubBytesToBase64(bytes: Uint8Array): string {
  const chunkSize = 0x8000
  let encoded = ''
  for (let start = 0; start < bytes.length; start += chunkSize) {
    encoded += String.fromCharCode(...bytes.subarray(start, start + chunkSize))
  }
  return btoa(encoded)
}
