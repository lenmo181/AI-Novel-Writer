/**
 * 把更新说明拍成可读纯文本。来源有两种：
 * - GitHub Atom 摘要流（electron-updater 的 GitHub provider）给的是渲染后的 HTML；
 * - 其他后端给原始 markdown。
 * HTML 先转行结构（块级标签换行、列表项加圆点、实体解码），再走同一套 markdown 清理，两条路都出干净文本。
 */
const HTML_TAG_RE = /<\/?(?:h[1-6]|p|ul|ol|li|br|div|strong|em|b|i|code|pre|a|hr|span)\b/i

function htmlToLines(html: string): string {
  return html
    .replace(/<br\s*\/?>/gi, '\n')
    .replace(/<\/(?:p|h[1-6]|li|div|ul|ol|pre|hr)>/gi, '\n')
    .replace(/<li\b[^>]*>/gi, '· ')
    .replace(/<[^>]+>/g, '')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;|&apos;/g, "'")
    .replace(/&nbsp;/g, ' ')
}

export function plainReleaseNotes(notes: string): string {
  const source = HTML_TAG_RE.test(notes) ? htmlToLines(notes) : notes
  return source
    .split(/\r?\n/)
    .map(line => {
      const trimmed = line.trim()
      if (!trimmed) return ''
      const bullet = /^[-*]\s+(.*)$/.exec(trimmed)
      const content = (bullet ? bullet[1] : trimmed)
        .replace(/^#{1,6}\s+/, '')
        .replace(/`([^`]+)`/g, '$1')
        .replace(/\*\*([^*]+)\*\*/g, '$1')
        .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
        .trim()
      if (!content) return ''
      return bullet ? '· ' + content : content
    })
    .filter(Boolean)
    .join('\n')
}
