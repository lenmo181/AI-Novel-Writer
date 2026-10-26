import type { WritingLanguage } from './writing-language'
import { inspectWritingSkillMarkdown, type WritingSkillInspection } from './writing-skills'

export interface BuiltinSkillMetadata {
  /** Skill 唯一名称 */
  name: string
  /** 显示名称 */
  displayName?: string
  /** 功能描述 */
  description: string
  /** 使用场景（用于 Agent 自动匹配） */
  whenToUse?: string
  /** 版本 */
  version?: string
  /** 允许的工具列表（白名单） */
  allowedTools?: string[]
  /** 参数提示 */
  argumentHint?: string
  /** 是否可由模型自动调用 */
  userInvocable?: boolean
}

/** Fresh data keeps registry mutations out of the main-process source authority. */
export function getBuiltinWritingSkills() {
  const builtins: Array<{
    metadata: BuiltinSkillMetadata
    content: string
    writingSkill?: WritingSkillInspection
    localizedContent?: Partial<Record<WritingLanguage, string>>
  }> = [
    {
      metadata: {
        name: 'long-form-continuity',
        displayName: '长篇连续性与场景推进',
        description: '在规划和正文阶段守住作者事实、因果链、角色状态与伏笔进度，并把每一批生成落到可验证的状态变化。',
        version: '1.1.0',
      },
      content: 'Treat established author facts as authoritative. Track causality, character state, and unresolved narrative threads. Build each scene around a character choice, its cost, and a concrete change in the story state.',
      localizedContent: {
        'zh-CN': '以作者已经确认的事实为最高依据。每次生成前先确认当前章节、角色状态和未回收线索；每批生成后记录发生了什么、改变了什么、留下了什么。每个场景围绕角色的主动选择、选择的代价，以及故事状态发生的具体变化展开。上下文缺失时保守停在已知边界，不擅自补设定。',
        'en-US': 'Treat established author facts as authoritative. Before each generation, confirm the current chapter, character state, and unresolved threads; after each batch, record what happened, what changed, and what remains open. Build every scene around a character choice, its cost, and a concrete change in story state. When context is missing, stop at the known boundary instead of inventing facts.',
      },
      writingSkill: inspectWritingSkillMarkdown(`---\nname: long-form-continuity\ndisplay_name: Long-form Continuity and Scene Progression\ndescription: Long-form continuity and scene progression\nversion: 1.1.0\nlanguage: bilingual\nstage: planning\n---\nTreat established author facts as authoritative. Before each generation, confirm the current chapter, character state, and unresolved threads. After each batch, record what changed and what remains open. When context is missing, stop at the known boundary instead of inventing facts.`),
    },
    {
      metadata: {
        name: 'natural-prose-refinement',
        displayName: '自然语言润色',
        description: '在修稿阶段减少模板化表达，让动作、感官、对话潜台词和句式节奏服务于人物与场景。',
        version: '1.1.0',
      },
      content: 'Revise the prose without changing author facts or plot outcomes. Replace generic summaries with selective concrete action, vary sentence rhythm, preserve the viewpoint voice, and remove meta commentary and repetitive transitions.',
      localizedContent: {
        'zh-CN': '在不改变作者事实和情节结果的前提下润色正文。用有选择的具体动作替代空泛概述，让对话带着人物目的和潜台词；调整句式节奏，保持视角人物的语言质感，并删除元话术、套话和重复的过渡表达。只修正文，不为了“更像小说”擅自新增事件。',
        'en-US': 'Revise the prose without changing author facts or plot outcomes. Replace generic summaries with selective concrete action, give dialogue a character goal and subtext, vary sentence rhythm, preserve the viewpoint voice, and remove meta commentary, stock phrases, and repetitive transitions. Revise the prose only; do not add events just to make it feel more novelistic.',
      },
      writingSkill: inspectWritingSkillMarkdown(`---\nname: natural-prose-refinement\ndisplay_name: Natural Prose Refinement\ndescription: Natural prose refinement\nversion: 1.1.0\nlanguage: bilingual\nstage: refinement\n---\nRevise prose without changing author facts or plot outcomes. Give dialogue a character goal and subtext, vary sentence rhythm, and remove stock phrases. Do not add events just to make the prose feel more novelistic.`),
    },
    {
      metadata: {
        name: 'outline-continuation-guard',
        displayName: '大纲分批接力守卫',
        description: '把情节大纲按副篇章数连续生成，逐批核对章节范围、前后衔接和断点，避免跳章、重复或悄悄停止。',
        version: '1.0.0',
        whenToUse: '用户要求自动生成完整情节大纲、继续下一批或从断点恢复时',
      },
      content: `# 大纲分批接力守卫

情节大纲按副篇章数分批生成时，必须把每一批当作一个可恢复的事务。

## 生成前
- 读取当前断点：已完成的最后一章、下一批起始章、目标总章数和副篇章数。
- 确认下一批范围连续且不超过总章数；如果起始章已经完成，先跳到下一章，不要重复覆盖。
- 读取最近一批的结尾状态、未回收伏笔和角色状态。缺少关键上下文时先标记缺口，不凭空补写。

## 生成中
- 每章都必须有具体事件、角色选择、阻力、结果和下一步钩子。
- 只生成请求范围内的章节，不提前写后续批次，也不把“继续生成”写进结果。
- 输出前逐项核对 chapterNumber，必须从起始章连续递增到结束章。

## 生成后
- 验证批次数量、首尾章节号、JSON 结构和每章必填字段。
- 只有保存成功并写入新的断点后，才把这一批视为完成；失败时保留原断点并从原范围重试。
- 返回下一批起始章和未解决线索，供下一次接力使用。`,
      localizedContent: {
        'zh-CN': `# 大纲分批接力守卫

情节大纲按副篇章数分批生成时，必须把每一批当作一个可恢复的事务。

## 生成前
- 读取当前断点：已完成的最后一章、下一批起始章、目标总章数和副篇章数。
- 确认下一批范围连续且不超过总章数；如果起始章已经完成，先跳到下一章，不要重复覆盖。
- 读取最近一批的结尾状态、未回收伏笔和角色状态。缺少关键上下文时先标记缺口，不凭空补写。

## 生成中
- 每章都必须有具体事件、角色选择、阻力、结果和下一步钩子。
- 只生成请求范围内的章节，不提前写后续批次，也不把“继续生成”写进结果。
- 输出前逐项核对 chapterNumber，必须从起始章连续递增到结束章。

## 生成后
- 验证批次数量、首尾章节号、JSON 结构和每章必填字段。
- 只有保存成功并写入新的断点后，才把这一批视为完成；失败时保留原断点并从原范围重试。
- 返回下一批起始章和未解决线索，供下一次接力使用。`,
        'en-US': `# Outline Continuation Guard

When a plot outline is generated in batches, treat every batch as a resumable transaction.

## Before generation
- Read the checkpoint: the last completed chapter, the next start chapter, the total target, and the batch size.
- Ensure the requested range is contiguous and does not exceed the target. If the start chapter is already complete, advance instead of overwriting it.
- Read the latest ending state, unresolved threads, and character state. Mark missing context instead of inventing it.

## During generation
- Every chapter needs a concrete event, character choice, resistance, result, and next hook.
- Every chapter advances the main line: new information, new goals, new conflict, or new cost. No treading water and no repeating plot patterns already written.
- Introduce new characters as the plot moves (state their identity and role) and extend the world with new settings; never let one small cast dominate several chapters in a row, and keep new settings consistent with author facts.
- Generate only the requested range. Do not prewrite later batches or emit “continue generating” text.
- Check chapterNumber before returning: it must increase without gaps from start to end.

## After generation
- Validate the batch count, boundary chapter numbers, JSON shape, and required fields.
- Treat a batch as complete only after it is saved and its checkpoint advances. On failure, keep the old checkpoint and retry the same range.
- Return the next start chapter and unresolved threads for the next batch.`,
      },
      writingSkill: inspectWritingSkillMarkdown(`---
name: outline-continuation-guard
display_name: Outline Continuation Guard
description: Keeps batched plot-outline generation contiguous and resumable.
version: 1.0.0
language: bilingual
stage: planning
---
Treat every outline batch as a resumable transaction. Read the checkpoint before generation, validate contiguous chapter numbers and required fields, and advance the checkpoint only after a successful save. On failure, keep the old checkpoint and retry the same range.`),
    },
    {
      metadata: {
        name: 'chapter-momentum',
        displayName: '章节推进与断点恢复',
        description: '让章节正文从上一章状态继续推进，失败后从已知断点恢复，不重复、不跳跃、不擅自扩写。',
        version: '1.0.0',
        whenToUse: '用户要求连续写章、批量写作或恢复中断的正文生成时',
      },
      content: `# 章节推进与断点恢复

正文生成必须从上一章的最终状态继续，任何中断都只能从最近一次已保存的草稿或章节边界恢复。

- 先确认上一章结尾、当前章节蓝图、角色状态和本章目标；未知内容保持未知。
- 第一段推进新动作，不复述上一章结尾，不把蓝图说明写进正文。
- 每个场景至少产生一个可验证的状态变化：关系、资源、位置、线索、风险或目标。
- 失败重试时复用同一批输入和断点，不重复提交已保存正文，也不把错误信息写进小说。
- 接近目标字数时按蓝图的结束状态收束；不要用总结句、界面提示或凭空新增高潮填充篇幅。`,
      localizedContent: {
        'zh-CN': `# 章节推进与断点恢复

正文生成必须从上一章的最终状态继续，任何中断都只能从最近一次已保存的草稿或章节边界恢复。

- 先确认上一章结尾、当前章节蓝图、角色状态和本章目标；未知内容保持未知。
- 第一段推进新动作，不复述上一章结尾，不把蓝图说明写进正文。
- 每个场景至少产生一个可验证的状态变化：关系、资源、位置、线索、风险或目标。
- 失败重试时复用同一批输入和断点，不重复提交已保存正文，也不把错误信息写进小说。
- 接近目标字数时按蓝图的结束状态收束；不要用总结句、界面提示或凭空新增高潮填充篇幅。`,
        'en-US': `# Chapter Momentum and Recovery

Drafting must continue from the previous chapter's final state. An interruption may resume only from the most recently saved draft or chapter boundary.

- Confirm the previous ending, current blueprint, character state, and chapter goal; keep unknowns unknown.
- Start with a new action. Do not recap the previous ending or put blueprint instructions into the prose.
- Every scene must create a verifiable state change in a relationship, resource, location, clue, risk, or goal.
- On retry, reuse the same inputs and checkpoint. Do not resubmit saved prose or place error messages in the novel.
- Close at the blueprint's ending state near the target length. Do not pad with summaries, UI prompts, or invented climaxes.`,
      },
      writingSkill: inspectWritingSkillMarkdown(`---
name: chapter-momentum
display_name: Chapter Momentum and Recovery
description: Continues chapter prose from saved state and recovers without duplication.
version: 1.0.0
language: bilingual
stage: drafting
---
Continue from the previous chapter's final state. On retry, reuse the same checkpoint and inputs, do not duplicate saved prose, and ensure every scene creates a verifiable state change.`),
    },
    {
      metadata: {
        name: 'review-chapter',
        displayName: '章节审阅',
        description: '对指定章节进行全面的质量审阅，包括剧情逻辑、角色一致性、节奏感、伏笔呼应等多个维度。',
        whenToUse: '用户要求审阅、检查、评估某个章节时',
        version: '1.1.0',
      },
      content: `# 章节审阅

请对目标章节进行专业的小说审阅。依次检查以下维度：

## 1. 剧情逻辑
- 情节是否连贯，有无逻辑矛盾
- 因果关系是否成立

## 2. 角色一致性
- 角色行为是否符合既定性格
- 对话风格是否一致

## 3. 节奏感
- 张弛是否有度
- 是否有不必要的拖沓或过于仓促的转折

## 4. 伏笔与呼应
- 已有伏笔是否得到了回应
- 新埋的伏笔是否自然

## 5. 文笔与风格
- 描写是否生动
- 是否符合整体文风设定

请先使用 read_drafts 工具读取目标章节，再使用 read_architecture 读取故事架构进行对比评估。
输出格式：每个维度评分（1-5星）+ 详细说明 + 修改建议。`,
      localizedContent: {
        'en-US': `# Chapter Review

Review the target chapter as a fiction editor. Check each dimension in order:

## 1. Plot logic
- Is the plot coherent and free of logical contradictions?
- Are cause and effect convincing?

## 2. Character consistency
- Do actions match established characterization?
- Does each character keep a consistent voice?

## 3. Pacing
- Is tension balanced with release?
- Are any passages needlessly slow or any turns too abrupt?

## 4. Foreshadowing and payoff
- Are established clues paid off where appropriate?
- Do new clues arise naturally?

## 5. Prose and style
- Is the description vivid and purposeful?
- Does it match the project's established style?

Use read_drafts to load the target chapter and read_architecture to compare it with the story plan.
For each dimension, provide a 1-5 rating, a concise explanation, and actionable revision suggestions.`,
      },
      writingSkill: inspectWritingSkillMarkdown(`---\nname: review-chapter\ndisplay_name: Chapter Review\ndescription: Reviews a chapter for plot logic, character consistency, pacing, foreshadowing, and prose quality.\nlanguage: bilingual\nstage: review\n---\nUse the read_drafts and read_architecture tools before reviewing the chapter.`),
    },
    {
      metadata: {
        name: 'brainstorm',
        displayName: '脑暴创意',
        description: '针对指定话题进行创意脑暴，生成多个创意方向和灵感。',
        whenToUse: '用户要求头脑风暴、找灵感、想创意时',
        version: '1.1.0',
      },
      content: `# 创意脑暴

请围绕用户给出的话题进行专业的创意脑暴。

## 输出格式
为每个创意方向提供：
1. **创意概念**（一句话）
2. **详细展开**（100-200 字）
3. **可行性评估**（高/中/低）
4. **与已有剧情的融合度**

请先使用 read_architecture 和 read_project_state 了解项目背景，确保创意与现有设定不矛盾。
至少提供 5 个不同方向的创意。`,
      localizedContent: {
        'en-US': `# Creative Brainstorming

Brainstorm professionally around the user's topic.

## Output format
For every direction, provide:
1. **Concept** in one sentence
2. **Development** in 100-200 words
3. **Feasibility** as high, medium, or low
4. **Fit with the existing plot**

Use read_architecture and read_project_state first so the ideas do not contradict established project facts.
Provide at least five distinct directions.`,
      },
      writingSkill: inspectWritingSkillMarkdown(`---\nname: brainstorm\ndisplay_name: Creative Brainstorming\ndescription: Generates multiple creative directions and ideas for a chosen topic.\nlanguage: bilingual\nstage: planning\n---\nUse the read_architecture and read_project_state tools before brainstorming.`),
    },
    {
      metadata: {
        name: 'character-analysis',
        displayName: '角色分析',
        description: '深入分析指定角色的性格、动机、角色弧、人物关系等。',
        whenToUse: '用户想深入了解或调整角色设定时',
        version: '1.1.0',
      },
      content: `# 角色深度分析

请对目标角色进行全方位的深度分析。

## 分析维度
1. **核心性格特质** — MBTI、大五人格倾向
2. **深层动机** — 驱动角色行动的核心诉求
3. **角色弧预测** — 基于当前设定推演角色成长轨迹
4. **关系网络** — 与其他角色的关系图谱
5. **冲突点** — 角色面临的核心矛盾和困境
6. **独特标识** — 口头禅、习惯动作、标志性特征

请先使用 read_characters 读取角色卡，以及 read_architecture 了解故事结构。`,
      localizedContent: {
        'en-US': `# Character Analysis

Analyze the target character in depth.

## Dimensions
1. **Core traits** — including useful personality-framework tendencies
2. **Deep motivation** — the need that drives action
3. **Character arc** — likely development based on established facts
4. **Relationship network** — ties to other characters
5. **Sources of conflict** — central pressures and dilemmas
6. **Distinctive markers** — speech patterns, habits, and recognizable traits

Use read_characters for the character cards and read_architecture for the story structure before analyzing.`,
      },
      writingSkill: inspectWritingSkillMarkdown(`---\nname: character-analysis\ndisplay_name: Character Analysis\ndescription: Analyzes a character's personality, motivation, arc, and relationships in depth.\nlanguage: bilingual\nstage: planning\n---\nUse the read_characters and read_architecture tools before analyzing the character.`),
    },
    {
      metadata: {
        name: 'continuity-check',
        displayName: '连续性检查',
        description: '检查小说中的设定一致性和连续性问题，发现矛盾和遗漏。',
        whenToUse: '用户想检查设定有没有矛盾、是否有不一致的地方时',
        version: '1.1.0',
      },
      content: `# 连续性与一致性检查

请对项目进行全面的连续性检查。

## 检查项
1. **时间线一致性** — 事件发生顺序是否合理
2. **地理一致性** — 地点描述是否前后一致
3. **角色状态** — 角色的伤病、装备、能力等是否正确追踪
4. **设定遵守** — 是否与世界观设定产生矛盾
5. **伏笔追踪** — 哪些伏笔已回收，哪些待回收

请使用 list_chapters 了解进度，使用 read_architecture 获取设定，逐章检查关键节点。
输出为表格形式，标注问题严重程度（🔴严重 / 🟡注意 / 🟢正常）。`,
      localizedContent: {
        'en-US': `# Continuity Check

Run a comprehensive continuity check on the project.

## Checks
1. **Timeline** — whether events occur in a plausible order
2. **Geography** — whether locations remain consistent
3. **Character state** — injuries, equipment, abilities, and other tracked state
4. **Setting rules** — conflicts with established worldbuilding
5. **Foreshadowing** — clues already resolved and clues still open

Use list_chapters to understand progress and read_architecture for established facts, then inspect the key points chapter by chapter.
Return a table and label each finding as critical, warning, or clear.`,
      },
      writingSkill: inspectWritingSkillMarkdown(`---\nname: continuity-check\ndisplay_name: Continuity Check\ndescription: Checks the novel for continuity and setting inconsistencies, contradictions, and omissions.\nlanguage: bilingual\nstage: review\n---\nUse the list_chapters and read_architecture tools for a chapter-by-chapter continuity check.`),
    },
    {
      metadata: {
        name: 'writing-coach',
        displayName: '写作教练',
        description: '提供专业的写作技巧指导和文笔改善建议。',
        whenToUse: '用户想提高写作水平、求教写作技巧时',
        version: '1.1.0',
      },
      content: `# 写作教练

作为专业的写作教练，为用户提供针对性的指导。

## 指导范围
- 叙述技巧（视角运用、时间线处理）
- 描写技法（环境渲染、人物刻画）
- 对话写作（个性化对话、潜台词运用）
- 节奏控制（场景切换、留白技巧）
- 悬念设置（钩子、反转、暗线）

请先使用 read_project_state 了解项目的写作风格设定，
再根据用户的具体问题提供定制化建议，并附上示例对比。`,
      localizedContent: {
        'en-US': `# Writing Coach

Act as a professional writing coach and give guidance tailored to the user's question.

## Areas
- Narrative technique, including viewpoint and timeline
- Description, including atmosphere and characterization
- Dialogue, including individual voice and subtext
- Pacing, including scene transitions and deliberate omission
- Suspense, including hooks, reversals, and hidden threads

Use read_project_state first to understand the project's established style. Then give focused advice with a short before-and-after example.`,
      },
      writingSkill: inspectWritingSkillMarkdown(`---\nname: writing-coach\ndisplay_name: Writing Coach\ndescription: Provides professional writing guidance and suggestions for improving prose.\nlanguage: bilingual\nstage: refinement\n---\nUse the read_project_state tool before giving tailored writing advice.`),
    },
  ]
  return builtins
}

/** The selected language body is exactly the body used by renderer skill bindings. */
export function readBuiltinWritingSkill(name: string, language: WritingLanguage): string | undefined {
  const skill = getBuiltinWritingSkills().find(entry => entry.metadata.name === name)
  if (!skill) return undefined
  const metadata = skill.writingSkill!.metadata
  const fields = { name: metadata.name, display_name: metadata.displayName, description: metadata.description, version: metadata.version, language: metadata.language, stage: metadata.stage }
  const frontmatter = Object.entries(fields).filter(([, value]) => value !== undefined).map(([key, value]) => `${key}: ${JSON.stringify(value)}`).join('\n')
  return `---\n${frontmatter}\n---\n${skill.localizedContent?.[language] ?? skill.content}`
}
