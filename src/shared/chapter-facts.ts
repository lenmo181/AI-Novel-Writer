/**
 * 一稿走到哪一步要看的审稿与修稿记录。由主进程按稿批量聚合（chapterFactsOverview），
 * 流程脊与单章视图读同一份。此文件不能依赖 Electron 或主进程实现。
 */
import type { ReviewCycleProjection } from './review-cycle'

export interface ChapterFacts {
  /** 可以合并的待合并修稿 id，按生成先后。 */
  mergeable: readonly number[]
  /** 不能再合并、只能查看或丢弃的待合并修稿：edited 是生成后正文或稿状态变了，unsourced 是旧修稿没有记下生成时的源稿。 */
  stale: readonly { id: number; reason: 'edited' | 'unsourced' }[]
  /** 这一稿最新的审稿；cycleStatus 是它所属复验周期的修稿进度，旧审稿与合并后的自动复验没有周期。 */
  latestReview: { createdAt: string; cycleStatus: ReviewCycleProjection['revisionStatus'] | null } | null
  /** 最近一次合并修稿的时间；没合并过为 null。 */
  lastMergedAt: string | null
}

import type { ExpectedDraftSource } from './ipc-channels'

/** 修稿生成时冻结的源稿与这一稿现在保存的样子相同，才能合并；旧修稿没有冻结源，只能查看。 */
export function isRevisionSourceCurrent(
  source: ExpectedDraftSource | null | undefined,
  draft: { id: number; chapterNumber: number; version: number; status: string; content: string },
): boolean {
  if (!source) return false
  return source.id === draft.id
    && source.chapterNumber === draft.chapterNumber
    && source.version === draft.version
    && source.status === draft.status
    && source.content === draft.content
}
