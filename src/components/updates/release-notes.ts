/** 把 GitHub Release 的 markdown 说明拍成可读纯文本：保留行结构，去掉标题符号与装饰。 */
export function plainReleaseNotes(notes: string): string {
  return notes
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
