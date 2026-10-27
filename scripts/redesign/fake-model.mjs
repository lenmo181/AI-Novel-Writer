import { Buffer } from 'node:buffer'
import { createServer } from 'node:http'

// 与 f05-u05、f05-u12 旅程相同的做法：主进程 fetch 改道到本机假 OpenAI 兼容服务，零真实模型调用。
export const FAKE_MODEL = Object.freeze({ id: 'redesign-drive-fixture', name: '驱动夹具模型', provider: 'openai', protocol: 'openai',
  modelName: 'gpt-4.1', baseUrl: 'https://api.openai.com/v1', apiKey: 'redesign-drive-offline-key',
  maxTokens: 4096, temperature: 0.7, purposes: ['generation'] })

export const FIXTURE_ENDPOINT_MODELS = Object.freeze(['gpt-5.5-mini', 'gpt-4.1', 'gateway/novel-writer-pro', 'text-embedding-3-small',
  'gateway/very-long-model-identifier-for-layout-checks-2026-10-24-preview', ...Array.from({ length: 12 }, (_, index) => `gateway/model-${String(index + 1).padStart(2, '0')}`)])

export const FIXTURE_PROSE = [
  '林岚沿着旧港的石阶往下走，潮水刚退，青石上还留着一层薄薄的盐霜。她把父亲留下的地图摊在膝头，对照远处灯塔的位置，一格一格数过去。守塔人站在塔下朝她招手，说昨夜的钟声比往常早了一刻。',
  '她没有立刻回答，只把潮位表压在地图上。两张纸的边缘都被海风吹得发卷，刻痕却刚好重合。林岚记得父亲说过，旧港的钟从不报错时辰，除非有人提前动过塔里的齿轮。',
  '守塔人把一把生锈的钥匙放进她掌心，指向灯塔背面封死的铁门。门缝里透出冷白的光，光线随着雾气一明一暗，像有人在里面用手电示意。林岚问他昨夜看见了谁，他只说听见了第三声钟。',
  '第三声钟并不在记录里。林岚沿着墙根找到一串湿脚印，脚印从海边来，却在铁门前突然消失。她蹲下摸了摸石缝，指尖沾到细碎的黑色煤渣，那是沉船货舱才会留下的东西。',
  '她用父亲的测绘笔在地图上标出铁门的位置，又把钟声提前的刻度记进日志。守塔人终于承认，十年前那艘失踪的货船曾在灯塔下卸过一只没有编号的木箱，港务局长要求所有人忘掉这件事。',
  '远处传来汽笛，港务局的巡逻船正绕过北堤。守塔人催她离开，说局长已经知道有人重新查档。林岚却推开铁门，里面没有人，只有一张被潮水浸透的航海图，图上圈着父亲失踪的日期。',
  '灯塔的齿轮在头顶缓慢转动，钟摆每摆一下，墙上的影子便向北移动一寸。林岚沿着影子找到一块松动的砖，砖后藏着一枚铜制船牌，背面刻着同一串她在父亲地图上见过的数字。',
  '她把船牌收进衣袋，听见铁门外有人敲了三下。守塔人的声音隔着雾传来，提醒她不要开门。林岚关掉手电，贴在门边屏住呼吸，等第四声钟响起时，门外的人说出了只有父亲才知道的暗号。',
  '暗号最后一个字落下，灯塔的钟却停在半空。林岚看向窗外，北堤尽头亮起一盏不属于港务局的红灯。她知道今晚查到的不是旧案的尾巴，而是有人正在用父亲留下的线索，把她引向海上。',
  '她没有去碰门闩，而是把地图摊在地上。灯塔的影子从窗格斜斜压过来，正好盖住父亲留下的那道裂口。她用测绘笔沿着影子的边缘描了一圈，笔尖在一处旧墨迹上停住，那里原本没有任何标记。',
  '潮声从地下传来，像有水在石墙另一侧缓慢退去。林岚撬开松动的砖，砖后不是空腔，而是一条只容一人侧身通过的窄缝。缝里塞着油布包，包上的绳结是父亲教她打的，三圈，最后一圈反扣。',
  '油布里裹着半截航海日志。纸页被盐水泡得发脆，仍能看清十年前的日期。那一夜船没有按北堤航标转向，而是被一盏红灯引进了禁航水道。记录写到第三声钟便戛然而止，后面只剩一道被指甲划破的长痕。',
  '守塔人隔着门又敲了一下，声音比刚才轻。林岚把日志塞进衣襟，问他为什么知道暗号。他沉默了很久，才说那不是暗号，是她父亲在出海前留给一个活人的最后一句话。说完，门外的脚步忽然向楼梯口退去。',
  '林岚打开一条门缝，雾里没有守塔人，只有一盏倒扣在地上的煤油灯。灯罩内壁沾着新鲜的黑灰，和她刚才在石缝里摸到的煤渣一模一样。她俯身拾灯，远处巡逻船的汽笛已经绕过北堤，正朝灯塔直冲过来。',
  '她把航海日志藏进地图夹层，转身走向灯室。齿轮仍在转，却有一枚铜齿比别处新。林岚用钥匙拨开护罩，里面露出一根细线，细线通向墙后的暗槽。槽中嵌着第二块船牌，刻的不是船号，而是父亲失踪的潮期。',
  '第四声钟终于响了。钟声落下的一刻，墙上的影子全部向北偏移，红灯也在雾中熄灭。林岚握住两块船牌，听见楼下有人推开灯塔大门。她没有逃，反而把灯室的主灯重新点亮，让整座旧港看见她已经找到了不该找到的东西。',
].join('\n\n')
export const FIXTURE_PROSE_NEXT = [
  '第二天清晨，林岚没有回灯塔，而是带着铜制船牌去了北堤的旧档案仓。雨水从屋檐滴下来，仓门上的编号被人刮去一半，她只能按父亲地图上的坐标寻找对应的铁柜。',
  '港务局长派来的文员拦住她，说所有沉船资料都已封存。林岚把船牌放在桌上，指出牌背的数字与十年前的货运登记相同。文员脸色变了，却坚持说那艘船从未靠过旧港。',
  '她转身离开，在走廊尽头听见纸张翻动的声音。墙上的值班表少了一页，留下的钉孔里卡着一小片蓝色蜡纸。林岚用指甲挑出蜡纸，发现上面写着父亲当年最后一次出海的潮期。',
  '北堤的潮水正在上涨。林岚租了一条窄船，顺着标记驶向外海的暗礁。船底撞到木板时，她以为遇见了漂流物，俯身却看见一只锁着的铁盒从泥沙里露出来。',
  '铁盒里没有账本，只有三张被盐水粘在一起的照片。照片上的灯塔还没有翻修，父亲站在门口，旁边的人正是如今的港务局长。林岚把照片按时间排好，发现每张背面都多了一道红色划痕。',
  '海面突然起雾，远处传来发动机声。林岚熄灭船灯，把船横在礁石后面。另一艘船没有挂旗，船头有人用红灯照向她刚才打捞的位置，像是在寻找被遗漏的东西。',
  '她等到那艘船绕过暗礁，才把铁盒藏进桨座。回到北堤时，守塔人已经在码头等候。他说昨夜的钟声不是提前，而是有人把整座灯塔的时间往回拨了十七分钟。',
  '林岚把新线索写进地图，发现父亲留下的数字并非坐标，而是一串仓库钥匙的编号。最后一把钥匙对应港务局长办公室，锁孔旁还留着新鲜的海盐。',
  '她站在办公室门外听见里面有人打电话，提到“第三次转运”。电话挂断后，门缝下滑出一张船期单，最下方写着今晚的出港时间。林岚握紧钥匙，决定在潮水退去前打开这扇门。',
  '她没有马上插钥匙，而是先蹲下查看船期单的纸纹。纸张来自港务局旧档案仓，边缘有一条只有测绘员才会使用的蓝线。蓝线在门缝下被截断，另一端应该还在办公室里。',
  '林岚绕到窗后，从半掩的百叶缝里看见港务局长正在拆一只木箱。箱内没有货物，只有一排按潮期编号的铜片。局长把其中一枚放在桌上，铜片背面正是她从灯塔带走的数字。',
  '门内传来椅脚摩擦声。她退到走廊的阴影里，听见局长吩咐文员把北堤的旧船全部改成“无记录”。文员问起守塔人，局长只回了一句：只要第四声钟没有响，他就还算活着。',
  '这句话让林岚想起灯塔里的钟。她把船牌贴在墙上，发现两处刻痕可以拼成一张潮汐路线图。路线的终点不在外海，而在旧港下方一座早已封死的闸门。',
  '外面忽然传来急促的脚步，守塔人从楼梯口跌跌撞撞赶来，手里攥着一张被撕掉半边的值班表。他说港务局长已经知道她拿到了日志，今夜的红灯会在涨潮前再亮一次。',
  '林岚把钥匙插进办公室的锁孔。锁芯里有新鲜海盐，说明有人刚从海上回来。她推开门，看见墙上挂满十年前的潮位图，每一张图上都画着同一个没有名字的孩子。',
  '孩子的脸被红笔划去，旁边写着父亲的字：如果有人找到这里，先别相信钟声。窗外潮水正在退，闸门的铁链一节一节露出水面。林岚收起船期单，决定在所有人赶到之前下到港底。',
].join('\n\n')
const FIXTURE_REVISED = `${FIXTURE_PROSE}林岚把这句话记进日志，又核对了一遍潮位表。`

function fixtureContinuation(prompt) {
  const marker = prompt.match(/(?:【本章已写正文全文】|\[Full existing manuscript for this chapter\])\s*([\s\S]*)$/u)
  const currentLength = marker?.[1]?.trim().length ?? 0
  return `林岚把第${currentLength}个字记在地图边缘，沿着北堤的湿木栈道继续向前。雾里传来第二声钟响，她没有回头，而是把灯塔的影子与潮位刻痕重新对齐，确认有人在昨夜改动过档案。守塔人远远喊出一个警告，林岚仍将门锁撬开，准备看清里面藏着什么。`
}
const REVIEW = JSON.stringify({ summary: '时间线需要核对。', items: [
  { category: '连续性', severity: 'warning', description: '核对钟声提前一刻与潮位的关系。', quote: '昨夜的钟声比往常早了一刻' },
], goalReviews: [] })

/** 创作资料角色提取：每个资料块回一张守塔人与一张林岚（示例作品已有林岚，可以对应已有角色）。 */
function characterExtraction(prompt, cards) {
  const sourceIds = [...prompt.matchAll(/【资料 ([^｜】]+)｜/gu)].map(match => match[1])
  return JSON.stringify({ results: sourceIds.map(sourceId => ({ sourceId, characterCards: [
    { name: '守塔人', role: 'supporting', background: '灯塔看守，记得每一次钟声。' },
    { name: '林岚', role: 'protagonist', background: '旧港潮汐记录的测绘师。' },
  ].slice(0, cards) })) })
}

/** AI 填充小说配置：按配置 JSON 合同给出 9 个必填字段与两个枚举；规模字段省略，由作者设置决定。 */
export const FIXTURE_CONFIG = Object.freeze({
  genre: '悬疑', targetAudience: '全龄', subGenre: '海港悬疑',
  coreOutline: '测绘师在停用的灯塔里追查一条沉船。',
  worldSetting: '旧港以潮汐计时，港务局的档案按潮期装订。',
  goldenFinger: '林岚能从潮位刻痕里读出年份。',
  protagonistProfile: '林岚，测绘师，冷静而执拗，相信一切都能量出来。',
  globalGuidance: ['对白克制，不替人物解释动机。', '每章至少写到一次潮水或灯光。', '线索先于解释出现。', '不使用倒叙开篇。'].join('\n'),
  writingStyle: '短句为主，多用潮汐与灯光的意象，叙述距离贴近林岚。',
  plotStructure: 'three_act', narrativePOV: 'third_limited',
})

// 架构阶段现在把角色生成拆成「身份清单 → 冻结身份详情」两次结构化请求。
// 走查夹具也按生产合同返回，才能把后面的世界观、大纲和正文问题真正暴露出来。
const FIXTURE_CHARACTER_MANIFEST = Object.freeze({ slots: [
  { slotId: 'cast-lan', name: '林岚', role: 'protagonist', narrativeDuty: '追查沉船与父亲失踪的主角', relations: [
    { targetSlotId: 'cast-keeper', relation: '向守塔人追问钟声异常' },
    { targetSlotId: 'cast-chief', relation: '与港务局长对立并交换档案' },
  ] },
  { slotId: 'cast-keeper', name: '守塔人', role: 'supporting', narrativeDuty: '掌握旧港钟声秘密的证人', relations: [
    { targetSlotId: 'cast-lan', relation: '向林岚提供关键线索' },
  ] },
  { slotId: 'cast-chief', name: '港务局长', role: 'antagonist', narrativeDuty: '掩盖沉船档案的阻力来源', relations: [
    { targetSlotId: 'cast-lan', relation: '阻止林岚查阅封存档案' },
  ] },
] })

function fixtureCharacterDetails(prompt) {
  const manifestMatch = prompt.match(/(?:【冻结身份与关系清单】|\[Frozen identities and relationships\])\s*([\s\S]*?)(?:\n\s*(?:【本批必须完整生成的 slotId】|\[slotId values required in this batch\]))/u)
  let slots = FIXTURE_CHARACTER_MANIFEST.slots
  if (manifestMatch) {
    try {
      const parsed = JSON.parse(manifestMatch[1].trim())
      if (Array.isArray(parsed.slots)) slots = parsed.slots
    } catch { /* 下游会报告结构错误；夹具默认清单仍保持可用 */ }
  }
  const requested = prompt.match(/(?:【本批必须完整生成的 slotId】|\[slotId values required in this batch\])\s*([^\n]+)/u)?.[1]
  const ids = requested ? requested.split(/[,，\s]+/u).filter(Boolean) : slots.map(slot => slot.slotId)
  const entries = slots.filter(slot => ids.includes(slot.slotId)).map((slot, index) => ({
    slotId: slot.slotId,
    name: slot.name,
    role: slot.role,
    gender: slot.name === '林岚' ? '女' : '男',
    age: index === 0 ? 28 : 45,
    appearance: `${slot.name}有清晰、可识别的外貌特征。`,
    personality: index === 0 ? '冷静执拗，习惯用测量确认判断。' : '谨慎克制，知道太多旧港秘密。',
    background: `${slot.name}长期生活在旧港，与沉船档案有直接关联。`,
    abilities: index === 0 ? '能从潮位刻痕读出年份。' : '熟悉灯塔、潮汐和港务档案。',
    motivation: index === 0 ? '查清父亲留下的地图和沉船真相。' : '保护自己知道的秘密。',
    arc: index === 0 ? '从只相信测量到接受无法量化的人心。' : '在隐瞒与坦白之间做出选择。',
    notes: '每次出场都推动钟声或沉船线索。',
    currentState: {
      location: '旧港灯塔',
      powerLevel: '普通人',
      physicalState: '健康',
      mentalState: '保持警惕',
      keyItems: ['父亲的地图'],
      recentEvents: ['听见提前一刻的钟声'],
      updatedAtChapter: 0,
    },
  }))
  return JSON.stringify({ entries })
}

function fixtureSynopsis(prompt) {
  const range = prompt.match(/第\s*(\d+)\s*[–—-]\s*(\d+)\s*章/u)
    ?? prompt.match(/chapters?\s*(\d+)\s*[–—-]\s*(\d+)/iu)
  const from = range ? Number(range[1]) : 1
  const to = range ? Number(range[2]) : 10
  return Array.from({ length: Math.max(0, to - from + 1) }, (_, offset) => {
    const number = from + offset
    return `## 第${number}章：灯下第${number}夜\n林岚在第${number}夜核对雾钟与潮位，找到一条新的线索，并决定下一步去问守塔人。`
  }).join('\n\n')
}

/** 情节大纲逐章生成：每次只要一章，回这一章的二级标题与一段正文，章号取自请求。 */
export const outlineChapter = number => `## 第${number}章：灯下第${number}夜
林岚在第${number}夜核对雾钟与潮位，找到一条新的线索，并决定下一步去问守塔人。她把这一夜的发现记进父亲的地图边上。`

/** 章节蓝图合同：按请求中的范围返回完整、可校验的结构化蓝图。 */
function fixtureBlueprints(prompt) {
  const range = prompt.match(/(?:第|chapters?\s*)(\d+)\s*(?:[–—-]|到|至)\s*(?:第\s*)?(\d+)/iu)
  const compact = prompt.match(/targetChapterNumber["']?\s*:\s*(\d+)/u)
  const from = range ? Number(range[1]) : compact ? Number(compact[1]) : 1
  const to = range ? Number(range[2]) : compact ? from : 6
  const blueprints = Array.from({ length: Math.max(0, to - from + 1) }, (_, offset) => {
    const chapterNumber = from + offset
    const characters = chapterNumber % 2 === 0 ? ['林岚', '守塔人'] : ['林岚', '港务局长']
    return {
      chapterNumber,
      title: `灯下第${chapterNumber}夜`,
      role: chapterNumber === 1 ? '建置' : chapterNumber % 3 === 0 ? '转折' : '发展',
      purpose: '追查雾钟提前一刻的原因并确认沉船线索。',
      keyEvents: `林岚在旧港灯塔核对第${chapterNumber}夜的潮位与钟声，发现一条指向沉船档案的新线索；她与${characters[1]}交换信息，决定前往北堤继续调查。`,
      characters,
      relationships: [{ from: characters[0], to: characters[1], relation: chapterNumber % 2 === 0 ? '向守塔人追问钟声异常' : '与港务局长交换并核对封存档案' }],
      suspenseHook: `钟声再次提前一刻，地图上浮出一个尚未标记的坐标。`,
    }
  })
  return JSON.stringify({ blueprints })
}

/**
 * 剧情树夹具：定稿章与单数位的蓝图章进主线，双数位的蓝图章进一条支线。来源全部取自提示里的资料，能通过主进程的校验；
 * 不开时假模型回正文，剧情树落到按资料整理的单轨快照。
 */
export function fixturePlotTree(facts) {
  const finals = new Set(facts.finalizedChapters.map(chapter => chapter.chapterNumber))
  const planned = facts.blueprints.filter(blueprint => !finals.has(blueprint.chapterNumber))
  const plan = blueprint => ({ status: 'planned', chapterNumber: blueprint.chapterNumber, summary: blueprint.title,
    sources: [{ type: 'blueprint', chapterNumber: blueprint.chapterNumber }] })
  const main = [
    ...facts.finalizedChapters.map(chapter => ({ status: 'occurred', chapterNumber: chapter.chapterNumber, summary: chapter.title,
      sources: [{ type: 'finalized-chapter', draftId: chapter.draftId, chapterNumber: chapter.chapterNumber }] })),
    ...planned.filter((_, index) => index % 2 === 0).map(plan),
  ].sort((left, right) => left.chapterNumber - right.chapterNumber)
  const side = planned.filter((_, index) => index % 2 === 1).map(plan)
  const span = events => ({ startChapter: Math.min(...events.map(event => event.chapterNumber)), endChapter: Math.max(...events.map(event => event.chapterNumber)) })
  return { tracks: [
    { id: 'main', title: '雾钟与沉船', role: 'main', ...span([...main, ...side]), summary: '林岚追查钟声提前的原因。', events: main },
    ...(side.length > 0 ? [{ id: 'side', title: '父亲的地图', role: 'subplot', parentTrackId: 'main', ...span(side), summary: '父亲留下的地图指向北堤外。', events: side }] : []),
  ] }
}

function responseFor(prompt, { extraction, plotTree, review, characterState }) {
  if (prompt.includes('不可变小说配置 JSON 合同') || prompt.includes('Immutable novel-configuration JSON contract')) {
    return { kind: 'config', content: JSON.stringify(FIXTURE_CONFIG) }
  }
  if (prompt.includes('身份清单合同') || prompt.includes('[Identity manifest contract]')) {
    return { kind: 'character-manifest', content: JSON.stringify(FIXTURE_CHARACTER_MANIFEST) }
  }
  if (prompt.includes('角色详情上下文') || prompt.includes('[Character-detail context]')) {
    return { kind: 'character-details', content: fixtureCharacterDetails(prompt) }
  }
  if (typeof review === 'function') {
    const custom = review(prompt)
    if (custom !== null && custom !== undefined && typeof custom !== 'string') throw new Error('replyReview 的函数只能返回文本或空值')
    if (custom) return { kind: 'review', content: custom }
  }
  if (prompt.includes('本次生成范围（重要）') || prompt.includes('[Generation scope for this batch (important)]')) {
    return { kind: 'architecture-synopsis', content: fixtureSynopsis(prompt) }
  }
  if (prompt.includes('不可变蓝图 JSON 合同') || prompt.includes('[Immutable blueprint JSON contract]')
    || prompt.includes('【精确 JSON 形状】{"blueprints"') || prompt.includes('[Exact JSON shape] {"blueprints"')) {
    return { kind: 'chapter-blueprint', content: fixtureBlueprints(prompt) }
  }
  if (prompt.includes('【本章已写正文全文】') || prompt.includes('[Full existing manuscript for this chapter]')) {
    return { kind: 'prose-continuation', content: fixtureContinuation(prompt) }
  }
  const outline = prompt.match(/本次只写第 (\d+) 章/)
  if (outline) return { kind: 'outline-chapter', content: outlineChapter(Number(outline[1])) }
  if (prompt.includes('【已确认纳入本次修稿的审稿项】')) return { kind: 'revision', content: FIXTURE_REVISED }
  if (prompt.includes('goalReviews')) return { kind: 'review', content: typeof review === 'string' ? review : REVIEW }
  if (prompt.includes('{"updates"')) return { kind: 'character-state', content: characterState?.(prompt) ?? '{"updates":[]}' }
  if (extraction && prompt.includes('【资料 ')) return { kind: 'character-extraction', content: characterExtraction(prompt, extraction) }
  if (plotTree && prompt.includes('小说剧情结构编辑')) return { kind: 'plot-tree', content: JSON.stringify(fixturePlotTree(JSON.parse(prompt.split('\n').find(line => line.startsWith('{"synopsis"'))))) }
  if (prompt.includes('只写短细纲')) return { kind: 'outline', content: FIXTURE_PROSE }
  if (prompt.includes('你是一位经验丰富的小说作者') && /"chapterNumber"\s*:\s*2/u.test(prompt)) {
    return { kind: 'prose', content: FIXTURE_PROSE_NEXT }
  }
  return { kind: 'prose', content: FIXTURE_PROSE }
}

/** 按固定节奏流出的正文：每块一句、以「。」结尾，界面里数「。」就知道哪一块已经显示出来。 */
export function streamingProse(chunks, chunkChars) {
  const filler = '林岚沿着旧港的石阶往下走潮水刚退青石上还留着一层薄薄的盐霜她把父亲留下的地图摊在膝头对照远处灯塔的位置一格一格数过去'
  return Array.from({ length: chunks }, (_, index) => {
    const head = `第${index + 1}句`
    return `${head}${filler.repeat(2).slice(0, chunkChars - head.length - 1)}。`
  })
}

export async function startFakeModel(diagnostics) {
  const requests = []
  /** 对话请求的模型与消息原文，按到达顺序；场景用它核对两边发给模型的参数是否一致。 */
  const chats = []
  let held = null
  let holdEmbeddings = false
  let waiting = 0
  let stream = null
  let extraction = 0
  let brokenCharacterState = false
  let droppedCharacterStates = 0
  let plotTree = false
  let failedProse = false
  let review = null
  let characterState = null
  let keylessEmbeddings = false
  /** 每次被接受的嵌入请求带的 Authorization 头（没有时为 null），场景据此确认本机检索模型真的没带 Key。 */
  const embeddingAuthorizations = []
  const streamLog = []
  const gate = {
    /** 卡住之后的模型调用；embeddings 为 true 时连嵌入也卡住（参考文本入库这类只走嵌入的步骤）。 */
    hold: ({ embeddings = false } = {}) => { held ??= Promise.withResolvers(); holdEmbeddings = embeddings },
    release: () => { held?.resolve(); held = null; holdEmbeddings = false },
    /** 正卡在门上的请求数，场景据此确认运行已停在要截的那一步。 */
    get waiting() { return waiting },
  }
  const pass = async () => {
    if (!held) return
    waiting++
    try { await held.promise } finally { waiting-- }
  }
  /** 之后的正文回复按 chunks 逐块发出，块间隔 intervalMs；每块发出的时刻记进 streamLog（epoch 毫秒与累计块数）。 */
  const streamProse = plan => { stream = plan; streamLog.length = 0 }
  /** 之后的创作资料角色提取回一份合法 JSON（每份资料默认两张卡，cards 限定张数，夹具只有 1–2 张）；不开时与以前一样回正文，提取失败，导入类场景不会停在等你确认。 */
  const replyCharacterCards = (cards = 2) => {
    if (cards !== 1 && cards !== 2) throw new Error(`replyCharacterCards: cards 只能是 1 或 2，收到 ${cards}`)
    extraction = cards
  }
  /** 定稿后处理的角色状态更新收到不是 JSON 的回复，这一步失败；传 false 恢复，用来演示只重试失败的步骤。 */
  const breakCharacterState = (broken = true) => { brokenCharacterState = broken }
  /** 之后 times 次角色状态请求回 200 后不发任何内容就断开连接（#420 的断流），用来演示重试真的重新请求。 */
  const dropCharacterState = (times = 1) => { droppedCharacterStates = times }
  /** 之后的剧情树整理回一份两轨（主线加支线）的合法 JSON。 */
  const replyPlotTree = () => { plotTree = true }
  /** 之后的正文回复以 content_filter 结束：写稿在这次打开里失败，留下可恢复的候选。传 false 恢复。 */
  const failProse = (failed = true) => { failedProse = failed }
  /** 之后的审稿（含合并后的复验）回这份 JSON；不调用时回默认审稿。也可传函数：它对任意提示词返回非空文本时就回这段文本（含修稿请求），返回空值时照常作答；它给出的回复一律记作 review。 */
  const replyReview = content => { review = content }
  /** 之后的定稿后处理角色状态更新回 build(提示词) 给出的 JSON 文本；不调用时回空的 updates。 */
  const replyCharacterState = build => { characterState = build }
  /** 之后不带 Key 的嵌入请求也照常回向量，模拟不需要 Key 的本机检索服务；传 false 恢复拒绝。 */
  const acceptKeylessEmbeddings = (accepted = true) => { keylessEmbeddings = accepted }
  const record = (kind, detail) => {
    requests.push(kind)
    diagnostics.push(`[fake-model] ${kind} <- ${detail}`)
  }
  const server = createServer(async (request, response) => {
    if (request.method === 'GET' && request.url === '/v1/models' && request.headers.authorization === `Bearer ${FAKE_MODEL.apiKey}`) {
      record('models', 'GET /v1/models')
      response.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify({ data: FIXTURE_ENDPOINT_MODELS.map(id => ({ id })) }))
      return
    }
    const keyless = keylessEmbeddings && request.url === '/v1/embeddings' && !(request.headers.authorization ?? '').replace(/^Bearer\s*/u, '')
    if (request.method !== 'POST' || (request.headers.authorization !== `Bearer ${FAKE_MODEL.apiKey}` && !keyless)) {
      response.writeHead(403).end(); return
    }
    const body = JSON.parse(Buffer.concat(await Array.fromAsync(request)).toString('utf8'))
    if (request.url === '/v1/embeddings') {
      if (holdEmbeddings) await pass()
      const input = Array.isArray(body.input) ? body.input : [body.input]
      embeddingAuthorizations.push(request.headers.authorization ?? null)
      record('embedding', `${input.length} 条${keyless ? '（无 Key）' : ''}`)
      response.writeHead(200, { 'Content-Type': 'application/json' })
        .end(JSON.stringify({ data: input.map((_, index) => ({ index, embedding: [0.1, 0.2, 0.3] })) }))
      return
    }
    if (request.url !== '/v1/chat/completions') { response.writeHead(404).end(); return }
    await pass()
    const prompt = (body.messages ?? []).map(message => typeof message.content === 'string' ? message.content : JSON.stringify(message.content)).join('\n')
    const reply = responseFor(prompt, { extraction, plotTree, review, characterState })
    const { kind } = reply
    const content = kind === 'character-state' && brokenCharacterState ? '角色状态暂时无法整理。' : reply.content
    chats.push({ kind, model: body.model, messages: body.messages })
    record(kind, prompt.slice(0, 120).replace(/\s+/g, ' '))
    response.writeHead(200, { 'Content-Type': 'text/event-stream' })
    if (kind === 'character-state' && droppedCharacterStates > 0) {
      droppedCharacterStates--
      response.flushHeaders()
      response.destroy()
      return
    }
    const finish = kind === 'prose' && failedProse ? 'content_filter' : 'stop'
    if (kind === 'prose' && stream) {
      for (const [index, chunk] of stream.chunks.entries()) {
        response.write(`data: ${JSON.stringify({ choices: [{ delta: { content: chunk }, finish_reason: null }] })}\n\n`)
        streamLog.push({ at: Date.now(), count: index + 1 })
        await new Promise(resolve => setTimeout(resolve, stream.intervalMs))
      }
      response.write(`data: ${JSON.stringify({ choices: [{ delta: {}, finish_reason: finish }] })}\n\n`)
    } else {
      response.write(`data: ${JSON.stringify({ choices: [{ delta: { content }, finish_reason: finish }] })}\n\n`)
    }
    response.write('data: {"choices":[],"usage":{"prompt_tokens":400,"completion_tokens":200,"total_tokens":600}}\n\n')
    response.end('data: [DONE]\n\n')
  })
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
  server.unref()
  return { port: server.address().port, requests, chats, gate, streamProse, streamLog, replyCharacterCards, breakCharacterState, dropCharacterState, replyPlotTree, failProse, replyReview, replyCharacterState, acceptKeylessEmbeddings, embeddingAuthorizations,
    close: () => new Promise(resolve => { server.closeAllConnections(); server.close(() => resolve()) }) }
}
