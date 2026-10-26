import { useCallback, useEffect, useState } from 'react'

import { ipc } from '../../../services/ipc-client'
import { captureProjectSession, isProjectSessionCurrent } from '../../project-session-gate'
import { useProjectStore } from '../../../stores/project-store'
import type { ReviewCenterChapter } from '../../../shared/review-center'

export interface ReviewOverview {
  chapters: ReviewCenterChapter[]
  loading: boolean
  /** 汇总读取失败；具体原因不打给界面，界面只按布尔给统一文案。 */
  error: boolean
  refresh: () => void
}

interface LoadedOverview {
  /** 这份数据属于哪个「作品#会话#请求」；对不上当前键就算在途。 */
  key: string
  chapters: ReviewCenterChapter[]
  error: boolean
}

const NOT_LOADED: LoadedOverview = { key: '', chapters: [], error: false }

/** 全文审稿中心的聚合数据：每章最新一次审稿的问题清单，按当前打开的作品读取。 */
export function useReviewOverview(): ReviewOverview {
  const currentProject = useProjectStore(s => s.currentProject)
  const hasProject = currentProject !== null
  const [requestId, setRequestId] = useState(0)
  const refresh = useCallback(() => setRequestId(id => id + 1), [])
  const key = `${currentProject?.path ?? ''}#${currentProject?.sessionLease ?? ''}#${requestId}`
  const [loaded, setLoaded] = useState<LoadedOverview>(NOT_LOADED)

  useEffect(() => {
    const session = captureProjectSession(useProjectStore.getState().currentProject)
    if (!session) return
    void ipc.invokeWithProjectSession(session, 'db:review-overview', session.projectPath).then(result => {
      if (!isProjectSessionCurrent(session)) return
      if (result.success && result.chapters) setLoaded({ key, chapters: result.chapters, error: false })
      else setLoaded({ key, chapters: [], error: true })
    }).catch(() => {
      if (isProjectSessionCurrent(session)) setLoaded({ key, chapters: [], error: true })
    })
  }, [key])

  if (!hasProject) return { chapters: [], loading: false, error: false, refresh }
  const current = loaded.key === key
  return {
    chapters: current ? loaded.chapters : [],
    loading: !current,
    error: current && loaded.error,
    refresh,
  }
}
