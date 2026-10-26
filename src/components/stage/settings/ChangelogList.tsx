import { useEffect, useState } from 'react'

import { ipc } from '../../../services/ipc-client'
import { useLocaleStore } from '../../../stores/locale-store'
import { parseChangelog, type ChangelogEntry } from './changelog-parse'

/** 「关于与更新」下方的更新日志：每个版本一条，带新增/改进/修复分组。 */
export default function ChangelogList() {
  const text = useLocaleStore(s => s.text)
  const [entries, setEntries] = useState<ChangelogEntry[] | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let mounted = true
    void ipc.invoke('update:get-changelog').then(result => {
      if (!mounted) return
      if (result?.success && typeof result.markdown === 'string') {
        setEntries(parseChangelog(result.markdown))
      } else {
        setFailed(true)
      }
    }).catch(() => {
      if (mounted) setFailed(true)
    })
    return () => { mounted = false }
  }, [])

  if (failed || (entries !== null && entries.length === 0)) return null

  return (
    <section className="stage-set-changelog" aria-label={text('更新日志', 'Changelog')}>
      <div className="stage-set-row">
        <div>
          <p className="stage-set-row-title">{text('更新日志', 'Changelog')}</p>
          <p className="stage-set-row-desc">{text('每个版本更新与修复了什么，都在这里。', 'What each version added and fixed.')}</p>
        </div>
      </div>
      {entries === null
        ? <p className="stage-set-changelog-empty">{text('正在读取更新日志…', 'Loading the changelog…')}</p>
        : (
          <div className="stage-set-changelog-list">
            {entries.map(entry => (
              <article key={entry.version} className="stage-set-changelog-entry">
                <header className="stage-set-changelog-head">
                  <h3 className="stage-set-changelog-version">
                    {text('v', 'v')}{entry.version}
                    {entry.date && <time className="stage-set-changelog-date">{entry.date}</time>}
                  </h3>
                </header>
                {entry.sections.map(section => (
                  <div key={section.title} className="stage-set-changelog-group">
                    <p className="stage-set-changelog-group-title">{section.title}</p>
                    <ul className="stage-set-changelog-items">
                      {section.items.map((item, index) => <li key={index}>{item}</li>)}
                    </ul>
                  </div>
                ))}
              </article>
            ))}
          </div>
        )}
    </section>
  )
}
