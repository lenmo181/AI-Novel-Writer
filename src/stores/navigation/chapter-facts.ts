import { ipc } from '../../services/ipc-client'
import type { ProjectSessionContext } from '../../shared/ipc-channels'
import { getActiveProjectSessionContext, sameProjectSessionContext } from '../../shared/project-session-context'
export { isRevisionSourceCurrent } from '../../shared/chapter-facts'
import type { ChapterFacts } from './stage-progress'
import { useStageStore } from './stage-store'

let sequence = 0
const latestRequest = new Map<number, number>()

/**
 * 重读这几稿的审稿与修稿记录，写进 stage-store 的 chapterFacts。流程脊（打开作品与每次刷新）和单章视图（打开、合并、
 * 丢弃修稿、保存了有待合并修稿的稿）都调它，所以两边看到的是同一份。
 *
 * 聚合在主进程一次完成（db:chapter-facts-overview）：逐稿 4~7 个 IPC 的旧路径在几千章的项目里
 * 会变成上万次往返，把打开作品拖成转圈。同一稿只认最后一次请求，但还没有记录的稿先收下
 * 较早请求的结果：打开作品时单章视图对落点那一章的读取会取代流程脊那批里的同一稿，流程脊不能因此缺这一章。
 * 聚合失败时全部保留旧值，与旧版逐稿读失败的行为一致。
 */
export async function refreshChapterFacts(drafts: readonly { id: number }[], session: ProjectSessionContext): Promise<void> {
  const ids = [...new Set(drafts.map(draft => draft.id).filter(id => Number.isSafeInteger(id) && id > 0))]
  const request = ++sequence
  for (const id of ids) latestRequest.set(id, request)
  if (ids.length === 0) {
    // 没有可读的稿也要把会话印记写对：换项目后旧项目的 facts 不能残留。
    if (!sameProjectSessionContext(session, getActiveProjectSessionContext())) return
    useStageStore.setState(state => ({
      chapterFacts: { session, byDraft: state.chapterFacts && sameProjectSessionContext(state.chapterFacts.session, session) ? state.chapterFacts.byDraft : {} },
    }))
    return
  }
  let entries: Array<{ draftId: number; facts: ChapterFacts }>
  try {
    entries = await ipc.invokeWithProjectSession(session, 'db:chapter-facts-overview', ids, session.projectPath)
  } catch {
    return
  }
  if (!sameProjectSessionContext(session, getActiveProjectSessionContext())) return
  useStageStore.setState(state => {
    const byDraft = state.chapterFacts && sameProjectSessionContext(state.chapterFacts.session, session) ? { ...state.chapterFacts.byDraft } : {}
    for (const { draftId, facts } of entries) {
      if (latestRequest.get(draftId) === request || !(draftId in byDraft)) byDraft[draftId] = facts
    }
    return { chapterFacts: { session, byDraft } }
  })
}
