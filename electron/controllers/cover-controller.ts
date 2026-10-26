import { ipcMain } from 'electron'

import type { CoverGenerateRequest, ModelProfile } from '../../src/shared/ipc-channels'
import { LLMFactory } from '../llm/llm-factory'
import { generateCoverImage } from '../llm/image-generation'
import { readCover, removeCover, saveCover } from '../services/cover-store'
import { applyProxyConfig, getModelConfig } from './llm-controller'

const OUTLINE_EXCERPT_LIMIT = 600

interface CoverPromptContext {
  name: string
  hint?: string
  showTitle: boolean
}

function coverPromptInstructions({ name, hint, showTitle }: CoverPromptContext): string {
  return [
    `书名：《${name}》`,
    hint?.trim() ? `作者补充的风格描述：${hint.trim()}` : '',
    showTitle
      ? `封面上需要出现书名「${name}」的美术字，位置与排版由你决定。`
      : '封面上不要出现任何文字。',
  ].filter(Boolean).join('\n')
}

function fallbackImagePrompt(context: CoverPromptContext): string {
  const style = context.hint?.trim() || ' atmospheric lighting, rich color palette'
  return [
    `Professional novel cover illustration for the Chinese novel 《${context.name}》.`,
    style + '.',
    'Vertical portrait composition (2:3), cinematic mood, high detail, elegant book cover art.',
    context.showTitle
      ? `Render the Chinese title 「${context.name}」 as elegant typography on the cover.`
      : 'Do not include any text on the cover.',
  ].join(' ')
}

/** 先用文本模型把书名、类型与作者补充写成一段英文生图提示词；文本模型不可用时退回拼装提示词。 */
async function composeImagePrompt(
  context: CoverPromptContext,
  model: ModelProfile | null,
  extra: { genre?: string; outline?: string } = {},
): Promise<string> {
  if (!model) return fallbackImagePrompt(context)
  const provider = LLMFactory.getProvider(model)
  const response = await provider.generate(model, [
    {
      role: 'system',
      content: 'You write concise English prompts for text-to-image models that design novel covers. Reply with the prompt only, no explanations.',
    },
    {
      role: 'user',
      content: [
        '为下面这部小说设计一个竖版封面，输出一段英文生图提示词（80 词以内），描述构图、风格、色调与氛围。',
        coverPromptInstructions(context),
        extra.genre?.trim() ? `类型：${extra.genre.trim()}` : '',
        extra.outline?.trim()
          ? `故事梗概（供参考，截取）：${[...extra.outline.trim()].slice(0, OUTLINE_EXCERPT_LIMIT).join('')}`
          : '',
      ].filter(Boolean).join('\n'),
    },
  ], {
    temperature: 0.8,
    maxTokens: 400,
    signal: undefined,
  })
  const prompt = response.success ? response.content.trim() : ''
  if (!prompt) return fallbackImagePrompt(context)

  const titleDirective = context.showTitle
    ? ` Include the Chinese title 「${context.name}」 as elegant cover typography.`
    : ' Do not include any text on the cover.'
  const compositionDirective = ' Vertical portrait book cover composition (2:3).'
  return `${prompt.replace(/\s+$/u, '')}${titleDirective}${compositionDirective}`
}

/** 书架上的 AI 封面：生成只调远程模型，保存/读取只碰应用数据目录，都不要求作品处于打开状态。 */
export function registerCoverController() {
  ipcMain.handle('cover:generate', async (_event, request: CoverGenerateRequest) => {
    try {
      applyProxyConfig()
      if (!request?.imageModel?.trim()) {
        return { success: false, error: 'IMAGE_MODEL_REQUIRED' }
      }
      const context: CoverPromptContext = {
        name: request.name,
        hint: request.hint,
        showTitle: request.showTitle,
      }

      const textModel = request.modelId ? getModelConfig(request.modelId) : null
      // 图像模型复用文本模型的网关配置，只替换模型名：作者不用再配一份 key。
      const baseModel = textModel ?? null
      const imageModel: ModelProfile | null = baseModel
        ? { ...baseModel, id: `${baseModel.id}:cover`, name: request.imageModel, modelName: request.imageModel.trim() }
        : null
      if (!imageModel) return { success: false, error: 'MODEL_NOT_FOUND' }

      const prompt = await composeImagePrompt(context, textModel)
      const image = await generateCoverImage(imageModel, prompt)
      return { success: true, image }
    } catch (error) {
      const code = error instanceof Error && /^[A-Z_]+$/.test(error.message) ? error.message : 'COVER_GENERATE_FAILED'
      return { success: false, error: code }
    }
  })

  ipcMain.handle('cover:save', async (_event, projectId: string, image: { dataBase64: string; mimeType: string }) => {
    try {
      if (typeof projectId !== 'string' || !projectId) return { success: false, error: 'COVER_PROJECT_ID_REQUIRED' }
      saveCover(projectId, image)
      return { success: true }
    } catch (error) {
      return { success: false, error: String(error) }
    }
  })

  ipcMain.handle('cover:get', async (_event, projectId: string) => {
    try {
      if (typeof projectId !== 'string' || !projectId) return { success: false, error: 'COVER_PROJECT_ID_REQUIRED' }
      const image = readCover(projectId)
      return image ? { success: true, image } : { success: true }
    } catch (error) {
      return { success: false, error: String(error) }
    }
  })

  ipcMain.handle('cover:remove', async (_event, projectId: string) => {
    try {
      if (typeof projectId !== 'string' || !projectId) return { success: false, error: 'COVER_PROJECT_ID_REQUIRED' }
      removeCover(projectId)
      return { success: true }
    } catch (error) {
      return { success: false, error: String(error) }
    }
  })
}
