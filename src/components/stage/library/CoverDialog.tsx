import { useEffect, useId, useState } from 'react'
import { ImageIcon, LoaderCircle, Sparkles } from 'lucide-react'

import { ipc } from '../../../services/ipc-client'
import { useLLMStore } from '../../../stores/llm-store'
import { useLocaleStore } from '../../../stores/locale-store'
import type { CoverImagePayload } from '../../../shared/ipc-channels'
import { Button } from '../../ui/Button'
import { Check } from '../../ui/Check'
import { InkDialog } from '../InkDialog'
import { setCachedCover, toCoverDataUrl, useCover } from './use-covers'

const IMAGE_MODEL_STORAGE_KEY = 'pagekin:cover-image-model'

export interface CoverDialogTarget {
  projectId: string
  name: string
  /** 当前作品才有的类型提示，供提示词参考。 */
  genre?: string
}

type Text = (zh: string, en: string) => string

interface CoverGenerateResponse {
  success: boolean
  image?: CoverImagePayload
  error?: string
}

const COVER_ERROR_COPY: Record<string, { zh: string; en: string }> = {
  IMAGE_MODEL_REQUIRED: { zh: '请先填写图像模型名。', en: 'Enter an image model name first.' },
  MODEL_NOT_FOUND: { zh: '没有可用的模型配置，请先在「设置 · AI 模型」里添加一个模型。', en: 'No model profile found. Add one in Settings · AI models first.' },
  IMAGE_REQUEST_FAILED: { zh: '图像服务拒绝了这次请求：请确认该网关支持所选的图像模型。', en: 'The image service rejected this request: make sure the gateway supports the chosen image model.' },
  IMAGE_RESPONSE_INVALID: { zh: '图像服务返回了无法识别的结果，请换一个图像模型再试。', en: 'The image service returned an unrecognized result. Try another image model.' },
  COVER_GENERATE_FAILED: { zh: '封面生成失败，请稍后再试。', en: 'Cover generation failed. Try again later.' },
}

function coverErrorText(code: string | undefined, text: Text): string {
  const copy = code ? COVER_ERROR_COPY[code] : undefined
  return copy ? text(copy.zh, copy.en) : text('封面生成失败，请稍后再试。', 'Cover generation failed. Try again later.')
}

/**
 * 书架上的 AI 封面工作台：文本模型把书名与风格写成生图提示词，图像模型出图，
 * 预览满意后存进应用数据目录。生成不写作品文件，随时可以重新生成。
 */
export default function CoverDialog({ target, onClose }: { target: CoverDialogTarget | null; onClose: () => void }) {
  const text = useLocaleStore(s => s.text)
  const models = useLLMStore(s => s.models)
  const defaultModelId = useLLMStore(s => s.defaultModelId)
  const initModels = useLLMStore(s => s.init)
  // 目标书变化由调用方的 key 重挂载处理：这里的初始值只在打开时取一次。
  const [hint, setHint] = useState('')
  const [showTitle, setShowTitle] = useState(true)
  const [imageModel, setImageModel] = useState(() => localStorage.getItem(IMAGE_MODEL_STORAGE_KEY) ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [preview, setPreview] = useState<{ payload: CoverImagePayload; url: string } | null>(null)
  const ids = useId()
  const existingCover = useCover(target?.projectId)

  useEffect(() => {
    void initModels()
  }, [initModels])

  const modelGatewayReady = models.length > 0
  const currentCoverUrl = preview?.url ?? existingCover

  const generate = async () => {
    if (!target || busy) return
    setBusy(true)
    setError(null)
    try {
      localStorage.setItem(IMAGE_MODEL_STORAGE_KEY, imageModel.trim())
      const result = await ipc.invoke('cover:generate', {
        projectId: target.projectId,
        name: target.name,
        hint,
        showTitle,
        imageModel: imageModel.trim(),
        modelId: defaultModelId ?? undefined,
      }) as CoverGenerateResponse
      if (!result.success || !result.image) {
        setError(coverErrorText(result.error, text))
        return
      }
      setPreview({ payload: result.image, url: toCoverDataUrl(result.image) })
    } catch {
      setError(coverErrorText('COVER_GENERATE_FAILED', text))
    } finally {
      setBusy(false)
    }
  }

  const keep = async () => {
    if (!target || !preview || busy) return
    setBusy(true)
    setError(null)
    try {
      const result = await ipc.invoke('cover:save', target.projectId, preview.payload)
      if (!result?.success) {
        setError(text('封面保存失败，请稍后再试。', 'Saving the cover failed. Try again later.'))
        return
      }
      setCachedCover(target.projectId, preview.payload)
      onClose()
    } catch {
      setError(text('封面保存失败，请稍后再试。', 'Saving the cover failed. Try again later.'))
    } finally {
      setBusy(false)
    }
  }

  const removeCover = async () => {
    if (!target || busy) return
    setBusy(true)
    setError(null)
    try {
      await ipc.invoke('cover:remove', target.projectId)
      onClose()
    } catch {
      setError(text('封面移除失败，请稍后再试。', 'Removing the cover failed. Try again later.'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <InkDialog
      open={target !== null}
      onOpenChange={open => { if (!open) onClose() }}
      keepOnOutsideClick={busy || preview !== null}
      title={text(`《${target?.name ?? ''}》封面工作台`, `Cover studio for “${target?.name ?? ''}”`)}
      description={text(
        '用 AI 生成一张竖版封面。生成需要支持出图的模型；不满意可以随时重新生成。',
        'Generate a portrait cover with AI. The image model must support image output; regenerate any time.',
      )}
      footer={
        <>
          {existingCover && !preview && (
            <Button variant="text" size="sm" disabled={busy} onClick={() => void removeCover()}>
              {text('移除封面', 'Remove cover')}
            </Button>
          )}
          <Button variant="text" onClick={onClose} disabled={busy}>{text('关闭', 'Close')}</Button>
          <Button variant="secondary" disabled={busy || !modelGatewayReady || !imageModel.trim()} onClick={() => void generate()}>
            {busy
              ? <><LoaderCircle size={13} className="stage-update-spin" aria-hidden="true" />{text('生成中…', 'Generating…')}</>
              : <><Sparkles size={13} aria-hidden="true" />{preview ? text('重新生成', 'Regenerate') : text('生成封面', 'Generate cover')}</>}
          </Button>
          {preview && (
            <Button variant="primary" disabled={busy} onClick={() => void keep()}>{text('使用此封面', 'Use this cover')}</Button>
          )}
        </>
      }
    >
      <div className="cover-studio">
        <div className="cover-studio-preview" aria-busy={busy}>
          {currentCoverUrl
            ? <img src={currentCoverUrl} alt={text('封面预览', 'Cover preview')} draggable={false} />
            : (
              <span className="cover-studio-placeholder">
                <ImageIcon size={22} aria-hidden="true" />
                {text('还没有封面', 'No cover yet')}
              </span>
            )}
          {busy && <span className="cover-studio-veil" aria-hidden="true"><LoaderCircle className="stage-update-spin" size={20} aria-hidden="true" /></span>}
        </div>
        <div className="cover-studio-form">
          <div className="pi-field">
            <label htmlFor={`${ids}-model`}>{text('图像模型', 'Image model')}</label>
            <input
              id={`${ids}-model`}
              className="pi-input"
              value={imageModel}
              placeholder={text('例如 gemini-2.5-flash-image、stable-diffusion-3.5-large', 'e.g. gemini-2.5-flash-image, stable-diffusion-3.5-large')}
              onChange={event => setImageModel(event.target.value)}
            />
            <em>
              {modelGatewayReady
                ? text('与当前默认模型走同一网关，只替换模型名。', 'Uses the same gateway as the default model, with this model name.')
                : text('还没有配置模型，请先到「设置 · AI 模型」添加。', 'No model configured yet. Add one in Settings · AI models.')}
            </em>
          </div>
          <div className="pi-field">
            <label htmlFor={`${ids}-hint`}>{text('风格描述（可选）', 'Style notes (optional)')}</label>
            <textarea
              id={`${ids}-hint`}
              className="pi-input"
              rows={3}
              value={hint}
              placeholder={text('例如：国风悬疑，冷青色调，雾中吊脚楼，工笔质感', 'e.g. Chinese mystery, cold teal palette, stilt houses in fog, gongbi texture')}
              onChange={event => setHint(event.target.value)}
            />
          </div>
          <Check checked={showTitle} onChange={setShowTitle} label={text('封面上渲染书名', 'Render the book title on the cover')} />
          {error && <p className="cover-studio-error" role="alert">{error}</p>}
        </div>
      </div>
    </InkDialog>
  )
}
