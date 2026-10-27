/**
 * 写作统计 — 从项目数据库聚合的进度与用量概览。
 * 只读：数字全部来自已有事实（草稿、定稿、LLM 调用记录），不做预测也不补假数据。
 */

/** 一章的最新一稿状态；none 表示这一章还没写过。 */
export type WritingStatsChapterStatus = 'none' | 'draft' | 'revised' | 'finalized'

export interface WritingStatsChapter {
  chapterNumber: number
  title: string
  status: WritingStatsChapterStatus
  /** 最新一稿的字数缓存；没写过为 0。 */
  wordCount: number
  /** 最新一稿的更新时间（SQLite UTC 时间串）。 */
  updatedAt: string
}

export interface WritingStatsDay {
  /** 本机时区日期（YYYY-MM-DD）。 */
  date: string
  llmCalls: number
  failedCalls: number
  completionTokens: number
  /** 当日第一次成稿的章数（version 1 的草稿）。 */
  newChapters: number
  /** 当日各稿产出的字数（含修稿改写，每次生成各计一次）。 */
  generatedWords: number
}

export interface WritingStatsUsageRow {
  /** 模型 id 或用途标识。 */
  key: string
  /** 模型显示名或用途原文；主进程没有本地化，界面原样展示。 */
  label: string
  calls: number
  /** 记到 tokens 的调用才有值。 */
  totalTokens: number | null
}

export interface WritingStatsOverview {
  /** 设定里的预计总章数。 */
  plannedChapters: number
  /** 设定里的单章基准字数。 */
  wordsPerChapter: number
  chapters: WritingStatsChapter[]
  /** 全部最新稿的字数合计。 */
  totalWords: number
  finalizedChapters: number
  /** 已定稿各章最新稿的字数合计。 */
  finalizedWords: number
  llmCalls: { total: number; successful: number; failed: number }
  /** 最近 30 天，按日期升序；没有活动的日期也占一行，值全为 0。 */
  daily: WritingStatsDay[]
  byModel: WritingStatsUsageRow[]
  byPurpose: WritingStatsUsageRow[]
}

/** 每日活动统计的天数窗口。 */
export const WRITING_STATS_DAY_WINDOW = 30
