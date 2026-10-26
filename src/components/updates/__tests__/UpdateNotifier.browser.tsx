import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useLocaleStore } from '../../../stores/locale-store'

;(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

let container: HTMLDivElement
let root: Root

const availableState = (version: string) => ({
  status: 'available',
  currentVersion: '0.9.2',
  availableVersion: version,
  updateAction: 'download',
  isReminderDeferred: false,
  releaseNotes: '### 新增\n- **AI 封面**：一键生成\n- 更新弹窗',
})

beforeEach(() => {
  useLocaleStore.setState({ locale: 'zh-CN', initialized: true })
  Object.assign(window, {
    aiNovelAPI: {
      invoke: vi.fn(async (channel: string) => channel === 'update:get-state' ? availableState('9.8.7') : null),
      on: vi.fn(() => () => {}),
      once: vi.fn(),
      send: vi.fn(),
      setZoomLevel: vi.fn(),
      setZoomFactor: vi.fn(),
      getZoomLevel: vi.fn(() => 0),
    },
  })
  container = document.createElement('div')
  container.className = 'paper-ink'
  document.body.append(container)
  root = createRoot(container)
})

afterEach(async () => {
  await act(async () => root.unmount())
  container.remove()
  Reflect.deleteProperty(window, 'aiNovelAPI')
  vi.restoreAllMocks()
})

async function renderNotifier() {
  const { UpdateNotifier } = await import('../UpdateNotifier')
  await act(async () => root.render(<UpdateNotifier />))
  return await vi.waitFor(() => {
    const dialog = document.querySelector('.pi-dialog')
    expect(dialog).toBeTruthy()
    return dialog as HTMLElement
  })
}

function buttonIn(dialog: HTMLElement, label: string): HTMLButtonElement {
  const buttons = [...dialog.querySelectorAll('button')]
  const button = buttons.find(candidate => candidate.textContent?.includes(label))
  expect(button, `button ${label}`).toBeTruthy()
  return button as HTMLButtonElement
}

describe('UpdateNotifier 更新弹窗', () => {
  it('展示版本号与更新内容，附稍后与立即更新按钮', async () => {
    const dialog = await renderNotifier()
    expect(dialog.textContent).toContain('发现新版本 v9.8.7')
    expect(dialog.textContent).toContain('· AI 封面：一键生成')
    expect(dialog.textContent).toContain('· 更新弹窗')
    expect(buttonIn(dialog, '稍后更新')).toBeTruthy()
    expect(buttonIn(dialog, '立即更新')).toBeTruthy()
  })

  it('稍后更新收起弹窗，重新挂载后同一个版本也不再弹', async () => {
    const dialog = await renderNotifier()
    await act(async () => buttonIn(dialog, '稍后更新').click())
    await vi.waitFor(() => expect(document.querySelector('.pi-dialog')).toBeNull())
    await act(async () => root.unmount())
    const { UpdateNotifier } = await import('../UpdateNotifier')
    await act(async () => root.render(<div className="paper-ink"><UpdateNotifier /></div>))
    await act(async () => { await Promise.resolve() })
    expect(document.querySelector('.pi-dialog')).toBeNull()
  })

  it('立即更新请求下载并收起弹窗', async () => {
    const dialog = await renderNotifier()
    await act(async () => buttonIn(dialog, '立即更新').click())
    await vi.waitFor(() => {
      expect(vi.mocked(window.aiNovelAPI!.invoke)).toHaveBeenCalledWith('update:download')
      expect(document.querySelector('.pi-dialog')).toBeNull()
    })
  })

  it('延后提醒生效时不弹窗', async () => {
    vi.mocked(window.aiNovelAPI!.invoke).mockImplementation(async (channel: string) => channel === 'update:get-state'
      ? { ...availableState('9.8.8'), isReminderDeferred: true }
      : null)
    const { UpdateNotifier } = await import('../UpdateNotifier')
    await act(async () => root.render(<UpdateNotifier />))
    await act(async () => { await Promise.resolve() })
    expect(document.querySelector('.pi-dialog')).toBeNull()
  })
})

describe('plainReleaseNotes', () => {
  it('去掉 markdown 装饰，保留行结构', async () => {
    const { plainReleaseNotes } = await import('../release-notes')
    expect(plainReleaseNotes('### 新增\n- **A** 功能，见 [主页](https://example.com)\n`x` 修复'))
      .toBe('新增\n· A 功能，见 主页\n· x 修复')
  })
})
