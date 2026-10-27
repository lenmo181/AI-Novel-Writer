import { useState } from 'react'
import { BookOpen, CheckCircle2, Download, FileText, Files, Type, XCircle } from 'lucide-react'
import { useProjectStore } from '../../stores/project-store'
import {
  exportNovel,
  type ExportChapterRange,
  type ExportFormat,
  type ExportProjectSnapshot,
} from '../../services/export-service'
import { ipc } from '../../services/ipc-client'
import {
  Dialog, DialogContent, DialogHeader, DialogFooter, DialogTitle, DialogDescription,
} from '../ui/Dialog'
import { Button } from '../ui/Button'
import { cn } from '../../lib/utils'
import { useLocaleStore } from '../../stores/locale-store'
import {
  captureProjectSession,
  isProjectSessionCurrent,
} from '../project-session-gate'
import type { ExternalDirectoryGrant, ProjectSessionContext } from '../../shared/ipc-channels'
import { resolveWritingLanguage } from '../../shared/writing-language'

interface Props {
  isOpen: boolean
  onClose: () => void
}

interface ExportTaskState {
  session: ProjectSessionContext
  exporting: boolean
  result: { success: boolean; path?: string; error?: string } | null
}

/** 导出对话框 — 使用 shadcn/ui */
export default function ExportDialog({ isOpen, onClose }: Props) {
  const currentProject = useProjectStore(s => s.currentProject)
  const [format, setFormat] = useState<ExportFormat>('merged-md')
  const [includeOutline, setIncludeOutline] = useState(true)
  const [rangeEnabled, setRangeEnabled] = useState(false)
  const [rangeFrom, setRangeFrom] = useState('')
  const [rangeTo, setRangeTo] = useState('')
  const [taskState, setTaskState] = useState<ExportTaskState | null>(null)
  const text = useLocaleStore(s => s.text)
  const activeTask = taskState && isProjectSessionCurrent(taskState.session) ? taskState : null
  const exporting = activeTask?.exporting ?? false
  const result = activeTask?.result ?? null

  const parsedRange: ExportChapterRange | null = (() => {
    if (!rangeEnabled) return null
    const from = Math.floor(Number(rangeFrom))
    const to = Math.floor(Number(rangeTo))
    if (!Number.isSafeInteger(from) || !Number.isSafeInteger(to) || from < 1 || from > to) return null
    return { from, to }
  })()
  const rangeInvalid = rangeEnabled && parsedRange === null

  const handleExport = async () => {
    const projectSession = captureProjectSession(currentProject)
    if (!currentProject || !projectSession) return
    const projectSnapshot: ExportProjectSnapshot = Object.freeze({
      id: projectSession.projectId,
      sessionLease: projectSession.leaseId,
      path: projectSession.projectPath,
      name: currentProject.name,
      novelConfig: Object.freeze({
        genre: currentProject.novelConfig.genre,
        targetAudience: currentProject.novelConfig.targetAudience,
        writingLanguage: resolveWritingLanguage(currentProject.novelConfig.writingLanguage),
      }),
    })
    let destination: ExternalDirectoryGrant | null
    try {
      destination = await ipc.invoke('dialog:select-export-directory')
    } catch {
      if (!isProjectSessionCurrent(projectSession)) return
      setTaskState({
        session: projectSession,
        exporting: false,
        result: { success: false, error: text('选择导出目录失败，请重试。', 'Could not choose an export folder. Try again.') },
      })
      return
    }
    if (!destination || !isProjectSessionCurrent(projectSession)) return

    setTaskState({ session: projectSession, exporting: true, result: null })
    let res: Awaited<ReturnType<typeof exportNovel>>
    try {
      res = await exportNovel(
        { format, grantId: destination.grantId, includeOutline, ...(parsedRange ? { chapterRange: parsedRange } : {}) },
        projectSnapshot,
        projectSession,
      )
    } catch {
      if (!isProjectSessionCurrent(projectSession)) return
      setTaskState({
        session: projectSession,
        exporting: false,
        result: { success: false, error: text('导出失败，请重试。', 'Export failed. Try again.') },
      })
      return
    }
    if (!isProjectSessionCurrent(projectSession)) return
    setTaskState({
      session: projectSession,
      exporting: false,
      result: res.success && res.path
        ? { ...res, path: `${destination.displayName}/${res.path}` }
        : res,
    })
  }

  const FORMAT_OPTIONS: Array<{ value: ExportFormat; label: string; desc: string; icon: React.ReactNode }> = [
    { value: 'merged-md', label: text('合并 Markdown', 'Merged Markdown'), desc: text('全书合并为单个 .md 文件', 'Combine the novel into one .md file'), icon: <FileText size={18} /> },
    { value: 'split-md', label: text('分章 Markdown', 'Chapter Markdown'), desc: text('每章一个独立 .md 文件', 'Create one .md file per chapter'), icon: <Files size={18} /> },
    { value: 'txt', label: text('纯文本 TXT', 'Plain text'), desc: text('去除格式标记的纯文本', 'Export plain text without formatting'), icon: <Type size={18} /> },
    { value: 'epub', label: text('EPUB 电子书', 'EPUB e-book'), desc: text('可在阅读器里打开的 .epub，带目录', 'An .epub with a table of contents for e-readers'), icon: <BookOpen size={18} /> },
  ]

  return (
    <Dialog open={isOpen} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-[480px]">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Download size={16} className="text-[var(--color-accent)]" />
            {text('导出项目', 'Export project')}
          </DialogTitle>
          <DialogDescription>{text('选择导出格式和目标目录', 'Choose a format and destination folder.')}</DialogDescription>
        </DialogHeader>

        <div className="px-5 py-4 space-y-3">
          {/* 格式选择 */}
          <div className="space-y-2">
            {FORMAT_OPTIONS.map((opt) => (
              <div
                key={opt.value}
                onClick={() => setFormat(opt.value)}
                className={cn(
                  'flex items-center gap-3 p-3 rounded-lg cursor-pointer transition-colors border',
                  format === opt.value
                    ? 'bg-[var(--color-active)] border-[var(--color-accent)]'
                    : 'bg-[var(--color-panel)] border-[var(--color-border)] hover:bg-[var(--color-hover)]'
                )}
              >
                <div className={cn(
                  'transition-colors',
                  format === opt.value ? 'text-[var(--color-accent)]' : 'text-[var(--color-text-secondary)]'
                )}>
                  {opt.icon}
                </div>
                <div>
                  <div className="text-xs font-medium text-[var(--color-text)]">{opt.label}</div>
                  <div className="text-xs text-[var(--color-text-muted)]">{opt.desc}</div>
                </div>
              </div>
            ))}
          </div>

          {/* 选项 */}
          <label className="flex items-center gap-2 text-xs cursor-pointer text-[var(--color-text-secondary)]">
            <input type="checkbox" checked={includeOutline} onChange={(e) => setIncludeOutline(e.target.checked)} />
            {text('包含故事大纲', 'Include story outline')}
          </label>

          {/* 章节范围 */}
          <label className="flex items-center gap-2 text-xs cursor-pointer text-[var(--color-text-secondary)]">
            <input type="checkbox" checked={rangeEnabled} onChange={(e) => setRangeEnabled(e.target.checked)} />
            {text('只导出部分章节', 'Export only part of the book')}
          </label>
          {rangeEnabled && (
            <div className="flex items-center gap-2 text-xs text-[var(--color-text-secondary)]">
              <span>{text('第', 'From chapter')}</span>
              <input
                type="number"
                min={1}
                value={rangeFrom}
                onChange={(e) => setRangeFrom(e.target.value)}
                aria-label={text('起始章号', 'First chapter')}
                className="w-16 rounded border border-[var(--color-border)] bg-[var(--color-panel)] px-2 py-1 text-xs text-[var(--color-text)]"
              />
              <span>{text('到', 'to')}</span>
              <input
                type="number"
                min={1}
                value={rangeTo}
                onChange={(e) => setRangeTo(e.target.value)}
                aria-label={text('结束章号', 'Last chapter')}
                className="w-16 rounded border border-[var(--color-border)] bg-[var(--color-panel)] px-2 py-1 text-xs text-[var(--color-text)]"
              />
              {rangeInvalid && (
                <span className="text-[var(--color-error-text)]">{text('请填有效的章号范围', 'Enter a valid chapter range')}</span>
              )}
            </div>
          )}

          {/* 结果 */}
          {result && (
            <div className={cn(
              'p-3 rounded-lg text-xs',
              result.success ? 'bg-[var(--color-success-soft)] text-[var(--color-success-text)]' : 'bg-[var(--color-error-soft)] text-[var(--color-error-text)]'
            )}>
              {result.success ? <CheckCircle2 size={14} className="inline mr-1" /> : <XCircle size={14} className="inline mr-1" />}
              {result.success ? text(`已导出到：${result.path}`, `Exported to: ${result.path}`) : result.error}
            </div>
          )}
        </div>

        <DialogFooter className="justify-end">
          <Button variant="default" onClick={handleExport} disabled={exporting || rangeInvalid}>
            <Download size={13} />
            {exporting ? text('导出中...', 'Exporting...') : text('选择目录并导出', 'Choose folder and export')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
