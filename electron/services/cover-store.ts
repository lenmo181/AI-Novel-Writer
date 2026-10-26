import fs from 'node:fs'
import path from 'node:path'
import { createHash } from 'node:crypto'

import { nativeImage } from 'electron'
import { VELA_HOME } from './app-data-locator'

import type { CoverImagePayload } from '../../src/shared/ipc-channels'

/** 封面按作品存放在应用数据目录：书架预览不需要打开作品，也不侵入作品存储格式。 */
const MAX_STORED_BYTES = 12 * 1024 * 1024
/** 书架卡片最大的显示宽度，读出时缩小，避免整页加载多张原图。 */
const DISPLAY_WIDTH = 640

function coversDir(): string {
  return path.join(VELA_HOME, 'covers')
}

function coverFile(projectId: string): string {
  // projectId 来自项目清单（UUID 形态）；防御性收敛成安全文件名，异常值退回哈希。
  const safe = /^[A-Za-z0-9._-]{1,120}$/u.test(projectId) ? projectId : createHash('sha256').update(projectId).digest('hex').slice(0, 32)
  return path.join(coversDir(), `${safe}.png`)
}

export function saveCover(projectId: string, image: CoverImagePayload): void {
  const bytes = Buffer.from(image.dataBase64, 'base64')
  if (!bytes.length || bytes.length > MAX_STORED_BYTES) throw new Error('COVER_IMAGE_INVALID')
  const dir = coversDir()
  fs.mkdirSync(dir, { recursive: true })
  const target = coverFile(projectId)
  const temporary = `${target}.tmp`
  fs.writeFileSync(temporary, bytes, { flag: 'w' })
  fs.renameSync(temporary, target)
}

export function readCover(projectId: string): CoverImagePayload | null {
  const file = coverFile(projectId)
  if (!fs.existsSync(file)) return null
  const bytes = fs.readFileSync(file)
  if (!bytes.length) return null
  const original = nativeImage.createFromBuffer(bytes)
  if (original.isEmpty()) {
    return { dataBase64: bytes.toString('base64'), mimeType: 'image/png' }
  }
  const width = original.getSize().width
  const display = width > DISPLAY_WIDTH
    ? original.resize({ width: DISPLAY_WIDTH })
    : original
  const compressed = display.toJPEG(86)
  return {
    dataBase64: (compressed.length ? compressed : bytes).toString('base64'),
    mimeType: compressed.length ? 'image/jpeg' : 'image/png',
  }
}

export function removeCover(projectId: string): void {
  const file = coverFile(projectId)
  if (fs.existsSync(file)) fs.rmSync(file, { force: true })
}
