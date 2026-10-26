import fs from 'node:fs'
import path from 'node:path'

/** 打包版从 resources 根读，开发态从仓库根读； locations 依序尝试，读到非空内容即返回。 */
export function createChangelogReader(locations: readonly string[]): () => string | null {
  return function readChangelog(): string | null {
    for (const location of locations) {
      if (!location) continue
      try {
        if (!fs.existsSync(location)) continue
        const markdown = fs.readFileSync(location, 'utf8')
        if (markdown.trim()) return markdown
      } catch {
        // 单个位置读失败继续尝试下一个；都没有就返回 null，界面显示不可用说明。
      }
    }
    return null
  }
}

export function changelogLocations(isPackaged: boolean, appRoot: string, resourcesPath: string): string[] {
  return isPackaged
    ? [path.join(resourcesPath, 'CHANGELOG.md'), path.join(path.dirname(resourcesPath), 'CHANGELOG.md')]
    : [path.join(appRoot, 'CHANGELOG.md')]
}
