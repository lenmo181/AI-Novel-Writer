import { create } from 'zustand'

/**
 * 思考过程的实时展示状态。隐藏推理不进工作流历史，也不进生成产物。
 *
 * 推理按模型分块到达（每秒可达几十块）。若每块都 set 一次：
 * ① 订阅步骤列表的组件每块都重渲染整份步骤；② 已累积文本每次全量重拼重绘，
 * 整条流是 O(n²)，生成期间渲染进程主线程被打满——自绘标题栏的窗口拖动也走这条路，会跟着卡。
 * 这里把分块攒进缓冲，约每 160ms 合并提交一次，并把单条文本封顶为保留尾部
 * （界面只展示思考的末尾，超出开头用省略号标注），长推理的渲染成本有界。
 */
const FLUSH_INTERVAL_MS = 160
/** 单条推理保留的字符上限（UTF-16 code units）。 */
const MAX_TEXT_LENGTH = 12_000

export const useWorkflowReasoningStore = create<{
  entries: Record<string, { attemptId: string; text: string }>
  append: (workflowRunId: string, attemptId: string, chunk: string) => void
  clear: (workflowRunId: string) => void
}>(set => {
  let pending: Record<string, { attemptId: string; text: string }> = {}
  let flushTimer: ReturnType<typeof setTimeout> | null = null

  const flush = () => {
    flushTimer = null
    const batch = pending
    pending = {}
    if (Object.keys(batch).length === 0) return
    set(state => {
      const entries = { ...state.entries }
      for (const [workflowRunId, item] of Object.entries(batch)) {
        const previous = state.entries[workflowRunId]
        let text = (previous?.attemptId === item.attemptId ? previous.text : '') + item.text
        if (text.length > MAX_TEXT_LENGTH) text = '…' + text.slice(-MAX_TEXT_LENGTH)
        entries[workflowRunId] = { attemptId: item.attemptId, text }
      }
      return { entries }
    })
  }

  return {
    entries: {},
    append: (workflowRunId, attemptId, chunk) => {
      const previous = pending[workflowRunId]
      pending[workflowRunId] = {
        attemptId,
        text: (previous?.attemptId === attemptId ? previous.text : '') + chunk,
      }
      if (flushTimer === null) flushTimer = setTimeout(flush, FLUSH_INTERVAL_MS)
    },
    clear: workflowRunId => {
      delete pending[workflowRunId]
      set(state => {
        if (!(workflowRunId in state.entries)) return state
        const entries = { ...state.entries }
        delete entries[workflowRunId]
        return { entries }
      })
    },
  }
})
