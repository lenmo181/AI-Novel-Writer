/**
 * Keep-a-Changelog 形态更新日志的解析：`## [版本] - 日期` 起一条，`### 分组` 带列表。
 * 独立成文件以便单测覆盖。
 */
export interface ChangelogSection {
  title: string
  items: string[]
}

export interface ChangelogEntry {
  version: string
  date?: string
  sections: ChangelogSection[]
}

/** 去掉行内 markdown 装饰：粗体、行内代码与链接只留文字。 */
function stripInlineMarkdown(value: string): string {
  return value
    .replace(/`([^`]+)`/g, '$1')
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .trim()
}

/**
 * 解析 Keep-a-Changelog 形态的更新日志：`## [版本] - 日期` 起一条，`### 分组` 带列表。
 * 解析不了的行直接跳过；一页里只出现认识的内容。
 */
export function parseChangelog(markdown: string): ChangelogEntry[] {
  const entries: ChangelogEntry[] = []
  let current: ChangelogEntry | null = null

  for (const rawLine of markdown.split(/\r?\n/)) {
    const line = rawLine.trimEnd()
    const heading = /^##\s+(.+)$/.exec(line)
    if (heading) {
      const header = heading[1].trim()
      const match = /^\[([^\]]+)\](?:\s*-\s*(.+))?$/.exec(header) ?? /^([\w.-]+)(?:\s*-\s*(.+))?$/.exec(header)
      if (match) {
        current = {
          version: match[1].trim(),
          ...(match[2]?.trim() ? { date: match[2].trim() } : {}),
          sections: [],
        }
        entries.push(current)
      } else {
        current = null
      }
      continue
    }
    if (!current) continue

    const section = /^###\s+(.+)$/.exec(line)
    if (section) {
      current.sections.push({ title: stripInlineMarkdown(section[1]), items: [] })
      continue
    }
    const item = /^[-*]\s+(.+)$/.exec(line.trim())
    if (item && current.sections.length) {
      current.sections[current.sections.length - 1].items.push(stripInlineMarkdown(item[1]))
    }
  }
  return entries.filter(entry => entry.sections.some(section => section.items.length))
}
