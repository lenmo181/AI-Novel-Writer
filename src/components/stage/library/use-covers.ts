import { useEffect, useState } from 'react'

import { ipc } from '../../../services/ipc-client'
import type { CoverImagePayload } from '../../../shared/ipc-channels'

const cache = new Map<string, string>()
const pending = new Set<string>()
const listeners = new Set<() => void>()

function notify(): void {
  for (const listener of [...listeners]) listener()
}

export function toCoverDataUrl(image: CoverImagePayload): string {
  return `data:${image.mimeType};base64,${image.dataBase64}`
}

/** 生成/保存后写入缓存，书架卡片立即换新封面。 */
export function setCachedCover(projectId: string, image: CoverImagePayload): void {
  cache.set(projectId, toCoverDataUrl(image))
  notify()
}

async function loadCover(projectId: string): Promise<void> {
  if (pending.has(projectId) || cache.has(projectId)) return
  pending.add(projectId)
  try {
    const result = await ipc.invoke('cover:get', projectId)
    if (result?.success && result.image) cache.set(projectId, toCoverDataUrl(result.image))
  } catch {
    // 没有封面或读取失败只是回到默认纸面封面，不需要打扰作者。
  } finally {
    pending.delete(projectId)
    notify()
  }
}

/** 书架封面的会话级缓存：预览与书架间切换不重复拉取。 */
export function useCover(projectId?: string): string | undefined {
  const [, bump] = useState(0)
  useEffect(() => {
    const listener = () => bump(value => value + 1)
    listeners.add(listener)
    return () => { listeners.delete(listener) }
  }, [])
  useEffect(() => {
    if (projectId && !cache.has(projectId) && !pending.has(projectId)) void loadCover(projectId)
  }, [projectId])
  return projectId ? cache.get(projectId) : undefined
}
