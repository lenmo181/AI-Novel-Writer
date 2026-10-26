import type { ModelProfile } from '../../src/shared/ipc-channels'
import { resolveOpenAIChatCompletionsUrl } from './openai-compatible-endpoint'

export interface CoverImageResult {
  dataBase64: string
  mimeType: string
}

export type ImageGenerationErrorCode =
  | 'IMAGE_MODEL_REQUIRED'
  | 'IMAGE_REQUEST_FAILED'
  | 'IMAGE_RESPONSE_INVALID'

export class ImageGenerationError extends Error {
  constructor(readonly code: ImageGenerationErrorCode) {
    super(code)
    this.name = 'ImageGenerationError'
  }
}

const REQUEST_TIMEOUT_MS = 120_000
const MAX_IMAGE_BYTES = 12 * 1024 * 1024

/** 封面是竖版书封；各家对竖版尺寸支持不一，先用最常见的 OpenAI 竖版尺寸，不支持时去掉重试。 */
const PORTRAIT_SIZE = '1024x1792'

function imagesUrl(model: ModelProfile): string {
  // 与 chat/completions 同一根：按 chat 解析出完整地址后把路径换成 images/generations。
  const chatUrl = resolveOpenAIChatCompletionsUrl(model.baseUrl, model.provider)
  const endpoint = new URL(chatUrl)
  endpoint.pathname = endpoint.pathname.replace(/\/chat\/completions$/u, '/images/generations')
  return endpoint.toString()
}

function isGeminiProtocol(model: ModelProfile): boolean {
  return model.provider === 'gemini'
}

function normalizeBase64(value: string): string {
  return value.replace(/^data:[^;,]+;base64,/i, '').trim()
}

async function readImageResponse(response: Response): Promise<CoverImageResult> {
  const contentType = response.headers.get('content-type') ?? ''
  if (contentType.startsWith('image/')) {
    const buffer = Buffer.from(await response.arrayBuffer())
    return { dataBase64: buffer.toString('base64'), mimeType: contentType.split(';')[0] }
  }
  const text = await response.text()
  const dataUrl = /^data:(image\/[\w.+-]+);base64,([A-Za-z0-9+/=]+)/i.exec(text)
  if (dataUrl) return { dataBase64: normalizeBase64(dataUrl[2]), mimeType: dataUrl[1] }
  throw new ImageGenerationError('IMAGE_RESPONSE_INVALID')
}

/** 图片可能以 data URL 直接内联；跨网关时也可能是 http(s) 链接，需要再取一次。 */
async function fetchRemoteImage(url: string, headers: Record<string, string>): Promise<CoverImageResult> {
  const response = await fetch(url, { headers, signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS) })
  if (!response.ok) throw new ImageGenerationError('IMAGE_RESPONSE_INVALID')
  return readImageResponse(response)
}

function parseDataUrlImage(value: unknown): CoverImageResult | null {
  if (typeof value !== 'string') return null
  const match = /^data:(image\/[\w.+-]+);base64,([A-Za-z0-9+/=]+)/i.exec(value.trim())
  if (!match) return null
  return { dataBase64: normalizeBase64(match[2]), mimeType: match[1] }
}

interface OpenAIImagePayload {
  data?: Array<{ b64_json?: unknown; url?: unknown }>
  error?: unknown
}

async function generateWithImagesEndpoint(
  model: ModelProfile,
  prompt: string,
  headers: Record<string, string>,
  withSize: boolean,
): Promise<CoverImageResult> {
  const body: Record<string, unknown> = {
    model: model.modelName,
    prompt,
    n: 1,
    response_format: 'b64_json',
  }
  if (withSize) body.size = PORTRAIT_SIZE

  const response = await fetch(imagesUrl(model), {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  })
  if (!response.ok) {
    // 尺寸或 response_format 不被网关支持时值得原样再试一次；其余失败直接上抛。
    if (withSize && (response.status === 400 || response.status === 422)) {
      return generateWithImagesEndpoint(model, prompt, headers, false)
    }
    throw new ImageGenerationError('IMAGE_REQUEST_FAILED')
  }

  const value = await response.json() as OpenAIImagePayload
  if (value.error) throw new ImageGenerationError('IMAGE_REQUEST_FAILED')
  const first = value.data?.[0]
  if (typeof first?.b64_json === 'string' && first.b64_json.trim()) {
    return { dataBase64: normalizeBase64(first.b64_json), mimeType: 'image/png' }
  }
  if (typeof first?.url === 'string' && /^https?:/iu.test(first.url)) {
    return fetchRemoteImage(first.url, {})
  }
  throw new ImageGenerationError('IMAGE_RESPONSE_INVALID')
}

/**
 * 部分网关不给图像模型开 images 端点，而是走 chat/completions：图片以 data URL 出现在
 * message.images 或正文 markdown 里。两类都识别。
 */
interface OpenAIChatImagePayload {
  choices?: Array<{ message?: { images?: Array<{ image_url?: { url?: unknown } }>; content?: unknown } }>
}

async function generateWithChatEndpoint(
  model: ModelProfile,
  prompt: string,
  headers: Record<string, string>,
): Promise<CoverImageResult> {
  const response = await fetch(resolveOpenAIChatCompletionsUrl(model.baseUrl, model.provider), {
    method: 'POST',
    headers,
    body: JSON.stringify({
      model: model.modelName,
      messages: [{ role: 'user', content: prompt }],
      stream: false,
    }),
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  })
  if (!response.ok) throw new ImageGenerationError('IMAGE_REQUEST_FAILED')

  const value = await response.json() as OpenAIChatImagePayload
  const message = value.choices?.[0]?.message
  const inlineUrl = message?.images?.[0]?.image_url?.url
  const inline = parseDataUrlImage(inlineUrl)
  if (inline) return inline
  if (typeof inlineUrl === 'string' && /^https?:/iu.test(inlineUrl)) {
    return fetchRemoteImage(inlineUrl, {})
  }
  if (typeof message?.content === 'string') {
    const markdown = /!\[[^\]]*\]\((data:image\/[\w.+-]+;base64,[A-Za-z0-9+/=]+)\)/iu.exec(message.content)
      ?? /!\[[^\]]*\]\((https?:\/\/[^)\s]+)\)/iu.exec(message.content)
    if (markdown) {
      const fromMarkdown = parseDataUrlImage(markdown[1])
        ?? await fetchRemoteImage(markdown[1], {}).catch(() => null)
      if (fromMarkdown) return fromMarkdown
    }
  }
  throw new ImageGenerationError('IMAGE_RESPONSE_INVALID')
}

interface GeminiImagePayload {
  predictions?: Array<{ bytesBase64Encoded?: unknown; mimeType?: unknown }>
  candidates?: Array<{ content?: { parts?: Array<{ inlineData?: { mimeType?: unknown; data?: unknown } }> } }>
  error?: unknown
}

async function generateWithGemini(model: ModelProfile, prompt: string): Promise<CoverImageResult> {
  const base = model.baseUrl.replace(/\/$/u, '')
  const isImagen = /imagen/iu.test(model.modelName)
  const url = isImagen
    ? `${base}/v1beta/models/${model.modelName}:predict`
    : `${base}/v1beta/models/${model.modelName}:generateContent`
  const body = isImagen
    ? {
        instances: [{ prompt }],
        parameters: { sampleCount: 1, aspectRatio: '2:3' },
      }
    : {
        contents: [{ parts: [{ text: prompt }] }],
        generationConfig: { responseModalities: ['TEXT', 'IMAGE'] },
      }

  const response = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'x-goog-api-key': model.apiKey,
    },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  })
  if (!response.ok) throw new ImageGenerationError('IMAGE_REQUEST_FAILED')

  const value = await response.json() as GeminiImagePayload
  if (value.error) throw new ImageGenerationError('IMAGE_REQUEST_FAILED')
  const prediction = value.predictions?.[0]
  if (typeof prediction?.bytesBase64Encoded === 'string' && prediction.bytesBase64Encoded.trim()) {
    return {
      dataBase64: normalizeBase64(prediction.bytesBase64Encoded),
      mimeType: typeof prediction.mimeType === 'string' ? prediction.mimeType : 'image/png',
    }
  }
  const inline = value.candidates?.[0]?.content?.parts?.find(part => part.inlineData)?.inlineData
  if (typeof inline?.data === 'string' && inline.data.trim()) {
    return {
      dataBase64: normalizeBase64(inline.data),
      mimeType: typeof inline.mimeType === 'string' ? inline.mimeType : 'image/png',
    }
  }
  throw new ImageGenerationError('IMAGE_RESPONSE_INVALID')
}

/**
 * 用模型配置生成一张竖版封面图。OpenAI 兼容网关先走 images/generations，
 * 失败后退到 chat/completions 的内联图片；gemini 协议按 imagen / generateContent 两类处理。
 */
export async function generateCoverImage(model: ModelProfile, prompt: string): Promise<CoverImageResult> {
  if (!model.modelName.trim()) throw new ImageGenerationError('IMAGE_MODEL_REQUIRED')

  const result = isGeminiProtocol(model)
    ? await generateWithGemini(model, prompt)
    : await generateWithImagesEndpoint(model, prompt, {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${model.apiKey}`,
      }, true)
      .catch(async error => {
        // images 端点不存在（404）或网关拒绝时，退回 chat 端点拿内联图片。
        if (error instanceof ImageGenerationError && error.code === 'IMAGE_REQUEST_FAILED') {
          return generateWithChatEndpoint(model, prompt, {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${model.apiKey}`,
          })
        }
        throw error
      })

  if (!result.dataBase64) throw new ImageGenerationError('IMAGE_RESPONSE_INVALID')
  const bytes = Buffer.byteLength(result.dataBase64, 'base64')
  if (bytes > MAX_IMAGE_BYTES) throw new ImageGenerationError('IMAGE_RESPONSE_INVALID')
  return result
}
