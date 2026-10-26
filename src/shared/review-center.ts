/**
 * 全文审稿中心的跨进程数据契约。主进程聚合每章最新一次审稿，渲染端按章展示。
 * 此文件不能依赖 Electron 或主进程实现。
 */

export type ReviewCenterSeverity = 'error' | 'warning' | 'pass' | 'unknown'

export type ReviewFindingStatus = 'unverified' | 'unresolved' | 'unknown' | 'resolved' | 'author-waived'

export interface ReviewCenterIssue {
  index: number
  category: string
  severity: ReviewCenterSeverity
  description: string
  quote?: string
  /** 跨章核对来源章：这条问题对照的是第几章的事实。 */
  sourceChapter?: number
  /** 审稿周期里的处理状态；报告没有进入周期时缺省。 */
  status?: ReviewFindingStatus
}

export interface ReviewCenterChapter {
  chapterNumber: number
  reviewId: number
  reviewedAt: string
  summary: string
  counts: { critical: number; suggest: number; verify: number; pass: number }
  items: ReviewCenterIssue[]
  /** 报告无法解析：界面提示该章报告可能来自旧版本。 */
  parseError?: boolean
}
