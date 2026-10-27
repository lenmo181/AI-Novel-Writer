import { useEffect, useState } from 'react'

import { displayedErrorMessage } from '../../i18n/app-errors'
import { ipc } from '../../services/ipc-client'
import { useLocaleStore } from '../../stores/locale-store'

/**
 * 硬件加速开关（设置 · 外观）。默认开启；关闭后写入 config.json，
 * 下次启动时主进程在 app ready 之前读取并走软件渲染——用于机器 GPU 进程
 * 异常导致整体卡顿的场景。更改必须重启应用才生效。
 */
export function useHardwareAcceleration() {
  const text = useLocaleStore(s => s.text)
  const [enabled, setEnabled] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [pendingRestart, setPendingRestart] = useState(false)

  useEffect(() => {
    let active = true
    void ipc.invoke('config:get').then(config => {
      if (active) setEnabled(config.hardwareAcceleration !== false)
    }).catch(() => { /* 读不到按默认开启 */ })
    return () => { active = false }
  }, [])

  const set = async (checked: boolean) => {
    const previous = enabled
    setEnabled(checked)
    setSaving(true)
    setError(null)
    try {
      const result = await ipc.invoke('config:set', { hardwareAcceleration: checked })
      if (!result.success) throw new Error(result.error || text('未知错误', 'Unknown error'))
      setPendingRestart(true)
    } catch (cause) {
      setEnabled(previous)
      setError(displayedErrorMessage(useLocaleStore.getState().locale, cause))
    } finally {
      setSaving(false)
    }
  }

  return { enabled, saving, error, pendingRestart, set }
}
