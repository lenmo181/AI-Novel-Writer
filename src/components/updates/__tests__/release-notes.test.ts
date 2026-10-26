import { describe, expect, it } from 'vitest'

import { plainReleaseNotes } from '../release-notes'

describe('plainReleaseNotes', () => {
  it('strips markdown decorations and keeps line structure', () => {
    expect(plainReleaseNotes('### 新增\n- **A** 功能，见 [主页](https://example.com)\n`x` 修复'))
      .toBe('新增\n· A 功能，见 主页\nx 修复')
  })

  it('converts GitHub Atom HTML notes into readable plain text', () => {
    const html = '<h1>1.6.0</h1>\n<h2>中文</h2>\n<p>1.6.0 是一次产品级升级。</p>\n<ul>\n<li>全局导航重构</li>\n<li>油画风主题 &amp; 启动动画</li>\n</ul>'
    expect(plainReleaseNotes(html)).toBe(
      '1.6.0\n中文\n1.6.0 是一次产品级升级。\n· 全局导航重构\n· 油画风主题 & 启动动画',
    )
  })

  it('handles block tags, inline tags and entities inside paragraphs', () => {
    const html = '<p>修复了 <code>integrity_check</code> 的性能问题，详见 &lt;docs&gt; &amp; "说明"。</p>\n<p>第二段。</p>'
    expect(plainReleaseNotes(html)).toBe(
      '修复了 integrity_check 的性能问题，详见 <docs> & "说明"。\n第二段。',
    )
  })

  it('leaves plain markdown input untouched by the HTML path', () => {
    expect(plainReleaseNotes('第一行\n第二行')).toBe('第一行\n第二行')
  })
})
