// 全程走查：像真实作者一样，从空书房新建一部小说，走完
// 新建 → 设定（AI 补全）→ 架构（生成四节）→ 蓝图（生成 1～6 章）→ 正文（写第 1 章 → 审稿 → 修稿合并 → 定稿）
// → 写第 2 章 → 知识库（导入资料并提取角色卡）→ 全文审稿 → 回书房，最后冷启动重启核对状态还在。
// 只按真实路径走，并在正文定稿处保留最小字数门槛；任何一步走不下去都会以截图现场失败。
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'

import { FAKE_MODEL, FIXTURE_CONFIG, FIXTURE_PROSE, FIXTURE_PROSE_NEXT } from './fake-model.mjs'
import * as arch from './pages/architecture.mjs'
import * as blueprint from './pages/blueprint.mjs'
import * as chapterView from './pages/chapter-view.mjs'
import * as runSheet from './pages/run-sheet.mjs'
import * as stage from './pages/stage.mjs'
import { dismissCompletionToasts } from './pages/shell.mjs'

const BOOK = '雾港谜案'
const TARGET_WORDS = 1000
const firstLine = text => text.split('\n')[0]

async function readDisplayedWordCount(page) {
  const label = await chapterView.versionButton(page).innerText()
  const match = label.match(/([\d,]+)\s*字/u)
  assert.ok(match, `定稿按钮缺少正文计数：${label}`)
  return Number(match[1].replace(/,/gu, ''))
}

/** 系统打开对话框直接返回 selected（文件与文件夹通用）。 */
async function stubOpenDialog(app, selected) {
  await app.evaluate(({ dialog }, selected) => {
    dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [selected] })
  }, selected)
}

/** 等一次模型运行结束：运行侧页开着就等上一次运行完成，收着就等顶栏没有进行中的胶囊。 */
async function waitRunDone(page) {
  if (await runSheet.sheet(page).isVisible()) {
    await runSheet.waitForRunDone(page)
  } else {
    await stage.root(page).locator('.stage-capsule:not([data-tone="ok"])').waitFor({ state: 'detached', timeout: 90_000 })
  }
  // 完成事件和侧页的打开状态是两次 React 更新，完成提示先出现时侧页可能仍在屏幕上。
  const runPanel = page.locator('.stage-sheet-slot .pi-sheet:visible').last()
  if (await runPanel.count() > 0) {
    await runPanel.press('Escape').catch(() => {})
    await runPanel.waitFor({ state: 'detached', timeout: 2_000 }).catch(() => {})
    if (await runPanel.count() === 0) return
    // 让流程脊自己的浮层关闭处理先执行；它不依赖焦点是否还停在运行卡片里。
    await page.locator('.stage-body').dispatchEvent('keydown', { key: 'Escape', code: 'Escape', bubbles: true })
    const doneCapsule = stage.root(page).locator('.stage-capsule[data-tone="ok"]')
    if (await doneCapsule.count() > 0) await doneCapsule.click()
    const closeButton = runPanel.locator('button[aria-label="收起运行侧页"]')
    if (await runPanel.count() > 0 && await closeButton.count() > 0) await closeButton.evaluate(element => element.click())
    await runPanel.waitFor({ state: 'detached', timeout: 10_000 })
  }
  // 完成卡片只用于查看历史，走查后续的按钮动作需要露出内容区。
  await page.locator('.stage-sheet-slot').evaluate(element => { element.style.display = 'none' }).catch(() => {})
}

async function showRunSheet(page) {
  await page.locator('.stage-sheet-slot').evaluate(element => { element.style.display = '' }).catch(() => {})
}

async function hideRunSheet(page) {
  await page.locator('.stage-sheet-slot').evaluate(element => { element.style.display = 'none' }).catch(() => {})
}

async function waitRunStart(page) {
  await page.locator('.stage-capsule:not([data-tone="ok"])').waitFor({ state: 'visible', timeout: 10_000 }).catch(() => {})
}

async function waitCurrentRunDone(page) {
  await waitRunStart(page)
  await page.locator('.stage-capsule:not([data-tone="ok"])').waitFor({ state: 'detached', timeout: 120_000 })
}

/** 从 Ctrl K 打开一个单例资料页（知识库、全文审稿）。 */
async function openSingletonPage(page, name) {
  await page.keyboard.press('Control+K')
  await stage.paletteInput(page).waitFor({ state: 'visible' })
  await stage.paletteInput(page).fill(name)
  await page.keyboard.press('Enter')
  await stage.palette(page).waitFor({ state: 'hidden' })
  await stage.tab(page, name).waitFor({ state: 'visible', timeout: 20_000 })
}

/** 在设置页添加一个模型：点添加按钮，按占位符填四个字段，保存。 */
async function addModelViaSettings(page, { name, baseUrl, apiKey, modelName }, listName, addButton) {
  await page.getByRole('button', { name: addButton, exact: true }).click()
  await page.getByPlaceholder('如：DeepSeek 主力 / GPT-4o 备用').fill(name)
  await page.getByPlaceholder('https://api.openai.com').fill(baseUrl)
  await page.getByPlaceholder('sk-...').fill(apiKey)
  await page.getByPlaceholder('从列表选择，或直接输入模型名称').fill(modelName)
  await page.getByRole('button', { name: '保存配置', exact: true }).click()
  await page.getByRole('list', { name: listName }).getByText(name, { exact: true }).waitFor({ state: 'visible', timeout: 15_000 })
}

export const JOURNEY_SCENARIOS = {
  'journey-full-flow': { model: true, shell: 'stage', run: async context => {
    const { app, page, roots, invoke, fake } = context
    const measured = { steps: [] }
    const mark = name => { measured.steps.push(name) }

    // 1. 新建作品：书房 → 新建作品对话框 → 落到设定页
    await stubOpenDialog(app, roots.projects)
    await page.getByRole('button', { name: '新建作品', exact: true }).click()
    const createDialog = page.getByRole('dialog', { name: '新建作品' })
    await createDialog.getByRole('textbox').first().fill(BOOK)
    await createDialog.getByRole('button', { name: '选择', exact: true }).click()
    await createDialog.getByRole('button', { name: '创建作品', exact: true }).click()
    await createDialog.waitFor({ state: 'hidden' })
    await stage.setupOutline(page).waitFor({ state: 'visible', timeout: 30_000 })
    mark('新建作品')

    // 2. 设置里添加 AI 模型与检索模型（真实用户路径），生成模型设为默认
    await stage.openSettings(page)
    await stage.showSettingsPage(page, 'AI 模型')
    await addModelViaSettings(page, { name: FAKE_MODEL.name, baseUrl: FAKE_MODEL.baseUrl, apiKey: FAKE_MODEL.apiKey, modelName: 'gpt-4.1' }, 'AI 模型', '添加模型')
    await stage.root(page).getByRole('list', { name: 'AI 模型' }).getByRole('button', { name: `「${FAKE_MODEL.name}」更多操作` }).click()
    const setDefault = page.getByRole('menuitem', { name: '设为默认' })
    if (await setDefault.count() > 0) await setDefault.click()
    else await page.keyboard.press('Escape')
    await stage.root(page).getByRole('list', { name: 'AI 模型' }).getByText(/默认/).first().waitFor({ state: 'visible', timeout: 10_000 })
    await stage.showSettingsPage(page, '检索模型')
    await addModelViaSettings(page, { name: '走查检索模型', baseUrl: FAKE_MODEL.baseUrl, apiKey: FAKE_MODEL.apiKey, modelName: 'text-embedding-3-small' }, '检索模型', '添加检索模型')
    await page.keyboard.press('Escape')
    await stage.settingsNav(page).waitFor({ state: 'detached' })
    mark('设置里添加模型')

    // 3. 设定页：手写核心大纲与两个下拉，再让 AI 补全其余设定
    await stage.setupOutline(page).fill(FIXTURE_CONFIG.coreOutline)
    await stage.setupField(page, '类型').selectOption(FIXTURE_CONFIG.genre)
    await stage.setupField(page, '目标读者').selectOption(FIXTURE_CONFIG.targetAudience)
    // 走查用一部 6 章小书，足以覆盖架构、蓝图、正文与导出，同时让架构批次一次完成。
    await stage.root(page).getByRole('spinbutton', { name: '总章数' }).fill('6')
    await stage.root(page).getByRole('spinbutton', { name: '每章字数' }).fill('1000')
    fake.replyCharacterCards()
    await stage.setupFill(page).click()
    const confirm = page.getByRole('dialog', { name: '扩写已有的设定？' })
    if (await confirm.count() > 0) await confirm.getByRole('button', { name: '继续扩写', exact: true }).click()
    await stage.setupChip(page, '世界观').and(page.locator('[data-filled]')).waitFor({ state: 'visible', timeout: 30_000 })
    await waitRunDone(page)
    mark('设定 AI 补全')

    // 4. 下一步：故事架构（先保存设定）
    await stage.setupNext(page).click()
    await page.getByRole('navigation', { name: '位置' }).getByText('架构', { exact: true }).waitFor({ state: 'visible', timeout: 20_000 })
    await arch.waitReady(page)
    assert.equal(await stage.saveStatus(page).innerText(), '已保存', '下一步先保存设定')
    mark('进入架构')

    // 5. 生成架构：四节一起生成，等运行结束、四节都变成已确认
    await arch.primary(page).click()
    await arch.dialog(page).waitFor({ state: 'visible' })
    await arch.startGenerate(page)
    // 角色身份和详情生成后，生产流程会停在明确的采用确认点；走查按作者默认选择继续。
    const characterApproval = runSheet.anyCard(page).filter({ hasText: '等你确认' })
    await characterApproval.getByRole('button', { name: '按当前选择确认', disabled: false }).waitFor({ state: 'visible', timeout: 60_000 })
    await characterApproval.getByRole('button', { name: '按当前选择确认' }).click()
    await waitRunDone(page)
    await arch.waitReady(page)
    await arch.badge(page, 'synopsis').filter({ hasText: '已确认' }).waitFor({ state: 'visible', timeout: 30_000 })
    for (const key of Object.keys(arch.SECTION_NAMES)) {
      await arch.badge(page, key).filter({ hasText: '已确认' }).waitFor({ state: 'visible', timeout: 10_000 })
    }
    measured.archSummaries = {}
    for (const key of Object.keys(arch.SECTION_NAMES)) measured.archSummaries[key] = await arch.summary(page, key).innerText()
    mark('生成架构')

    // 6. 下一步：章节蓝图
    await stage.stageButton(page, '蓝图').click({ force: true })
    await blueprint.plan(page).or(blueprint.emptyHeading(page)).waitFor({ state: 'visible', timeout: 20_000 })
    mark('进入蓝图')

    // 7. 生成 1～6 章蓝图
    await blueprint.generateRange(page, 1, 6)
    await waitRunDone(page)
    await blueprint.plan(page).getByRole('listitem').first().waitFor({ state: 'visible', timeout: 30_000 })
    const blueprintCount = await blueprint.plan(page).getByRole('listitem').count()
    assert.equal(blueprintCount, 6, `应生成 6 章蓝图，实际 ${blueprintCount}`)
    mark('生成蓝图')

    // 8. 写第 1 章
    await hideRunSheet(page)
    await blueprint.writeNext(page)
    await waitCurrentRunDone(page)
    await stage.stageButton(page, '正文').click()
    await stage.chapterButton(page, 1).click()
    await chapterView.view(page).waitFor({ state: 'visible', timeout: 20_000 })
    await chapterView.prose(page).getByText(firstLine(FIXTURE_PROSE)).first().waitFor({ state: 'visible', timeout: 30_000 })
    measured.chapter1DraftWords = await readDisplayedWordCount(page)
    assert.ok(measured.chapter1DraftWords >= TARGET_WORDS,
      `第 1 章正文低于目标：${measured.chapter1DraftWords}/${TARGET_WORDS}`)
    mark('写第 1 章')

    // 9. 审稿第 1 章
    await chapterView.review(page)
    await chapterView.reviseButton(page).waitFor({ state: 'visible', timeout: 90_000 })
    measured.reviewItems = await chapterView.reviewSheet(page).locator('article, li').count()
    mark('审稿')

    // 10. 按意见修稿，全部用修稿，完成合并
    await chapterView.reviseButton(page).click()
    await chapterView.merge(page).waitFor({ state: 'visible', timeout: 60_000 })
    await chapterView.merge(page).getByRole('button', { name: '全部用修稿' }).click()
    await chapterView.merge(page).getByRole('button', { name: '完成合并' }).click()
    await chapterView.view(page).getByRole('button', { name: '定稿', exact: true }).waitFor({ state: 'visible', timeout: 30_000 })
    await waitRunDone(page)
    mark('修稿合并')

    // 11. 定稿第 1 章（定稿后处理等跑完）
    await chapterView.finalize(page, 1)
    await chapterView.postProcess(page).waitFor({ state: 'visible', timeout: 60_000 })
    await waitRunDone(page)
    measured.postSteps = await chapterView.postProcessSteps(page)
    measured.chapter1FinalWords = await readDisplayedWordCount(page)
    assert.ok(measured.chapter1FinalWords >= TARGET_WORDS,
      `第 1 章定稿低于目标：${measured.chapter1FinalWords}/${TARGET_WORDS}`)
    await dismissCompletionToasts(page)
    mark('定稿第 1 章')

    // 12. 写第 2 章（接在定稿章后面，考连续性）
    await stage.stageButton(page, '蓝图').click()
    await blueprint.plan(page).waitFor({ state: 'visible' })
    await hideRunSheet(page)
    await blueprint.writeNext(page)
    await waitCurrentRunDone(page)
    await stage.stageButton(page, '正文').click()
    await stage.chapterButton(page, 2).click()
    await chapterView.view(page).waitFor({ state: 'visible', timeout: 20_000 })
    await chapterView.prose(page).getByText(firstLine(FIXTURE_PROSE_NEXT)).first().waitFor({ state: 'visible', timeout: 30_000 })
    mark('写第 2 章')

    // 13. 知识库：导入创作资料，确认发给模型提取角色卡，预览确认写入名单
    const material = path.join(roots.temp, '雾港资料.txt')
    fs.writeFileSync(material, '守塔人：灯塔看守，记得每一次钟声。\n港务局长：管着按潮期装订的档案。')
    await stubOpenDialog(app, material)
    await openSingletonPage(page, '知识库')
    fake.replyCharacterCards()
    await page.getByRole('button', { name: '导入创作资料', exact: true }).click()
    const consent = page.getByRole('dialog').filter({ has: page.getByRole('button', { name: '发送并提取', exact: true }) })
    await consent.waitFor({ state: 'visible', timeout: 20_000 })
    await consent.getByRole('button', { name: '发送并提取', exact: true }).click()
    await showRunSheet(page)
    const waiting = runSheet.anyCard(page).filter({ hasText: '等你确认' })
    await waiting.getByRole('button', { name: '按当前选择确认', disabled: false }).waitFor({ state: 'visible', timeout: 60_000 })
    for (const name of ['守塔人', '港务局长']) {
      const combo = waiting.getByRole('combobox', { name: new RegExp(`^采用方式：${name}`) })
      if (await combo.count() > 0) {
        const options = await combo.locator('option').allTextContents()
        const create = options.find(option => /新建|create/.test(option)) ?? options[0]
        await combo.selectOption(await combo.locator('option').filter({ hasText: create.split('·')[0].trim() }).first().getAttribute('value'))
      }
    }
    await waiting.getByRole('button', { name: '按当前选择确认' }).click()
    await runSheet.waitForRunDone(page)
    mark('知识库导入并提取角色卡')

    // 14. 全文审稿中心能打开
    await openSingletonPage(page, '全文审稿')
    mark('打开全文审稿')

    // 15. 回书房看概览
    await stage.backToLibrary(page, BOOK)
    measured.overviewBars = await stage.overviewBars(page, BOOK)
    mark('回书房')

    // 16. 冷启动重启，重新打开作品核对状态还在
    await context.restart()
    const restartedPage = context.page
    await stage.openFromLibrary(restartedPage, BOOK)
    await stage.stageButton(restartedPage, '正文').click()
    await stage.chapterButton(restartedPage, 1).click()
    await chapterView.view(restartedPage).waitFor({ state: 'visible', timeout: 20_000 })
    await chapterView.view(restartedPage).getByText('已定稿，只读', { exact: true }).waitFor({ state: 'visible', timeout: 30_000 })
    await stage.chapterButton(restartedPage, 2).click()
    await chapterView.view(restartedPage).waitFor({ state: 'visible', timeout: 20_000 })
    await chapterView.prose(restartedPage).getByText(firstLine(FIXTURE_PROSE_NEXT)).first().waitFor({ state: 'visible', timeout: 30_000 })
    mark('重启后状态完好')

    return measured
  } },
}
