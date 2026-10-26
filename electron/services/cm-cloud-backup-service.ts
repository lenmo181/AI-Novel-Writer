import fs from 'node:fs'
import path from 'node:path'
import { randomUUID } from 'node:crypto'

import { shell } from 'electron'

import type {
  CmCloudBackupRequest,
  CmCloudConfigureRequest,
  CmCloudProjectSessionContext,
  CmCloudStatusView,
} from '../../src/shared/cm-cloud'
import { getCurrentProjectPath } from '../database'
import { projectAccess } from './project-access'
import {
  exportPortableProject,
  type ExportPortableProjectInput,
  type ExportPortableProjectReceipt,
} from './project-archive-service'
import { createPortableProjectAssetProvider } from './portable-project-assets'
import { getGlobalDataRoot } from './app-data-locator'
import { CmCloudCli, CmCloudCliError, envelopeData, envelopeStatus, type CmCloudEnvelope } from './cm-cloud-cli'
import { CmCloudConfigStore, type CmCloudProjectConfig } from './cm-cloud-config-store'

const portableAssets = createPortableProjectAssetProvider()
const AUTO_BACKUP_DEBOUNCE_MS = 1_500

export class CmCloudServiceError extends Error {
  constructor(
    readonly code: string,
    readonly state: 'failed' | 'cancelled' | 'auth-required' | 'unknown' = 'failed',
    readonly authPageUrl?: string,
  ) {
    super(code)
    this.name = 'CmCloudServiceError'
  }
}

export interface CmCloudProjectSavedNotification {
  projectId: string
  projectPath: string
  context?: CmCloudProjectSessionContext
}

export interface CmCloudBackupSuccess {
  uploadedName?: string
  backupAt: string
  status: CmCloudStatusView
}

export interface CmCloudBackupServiceOptions {
  cli?: CmCloudCli
  configStore?: CmCloudConfigStore
  getCurrentProjectPath?: () => string | null
  getGlobalDataRoot?: () => string
  exportPortableProject?: (input: ExportPortableProjectInput) => Promise<ExportPortableProjectReceipt>
}

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function validString(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0
}

function authPageUrl(value: unknown): string | undefined {
  if (!validString(value)) return undefined
  try {
    const parsed = new URL(value)
    if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) return undefined
    return parsed.toString()
  } catch {
    return undefined
  }
}

function envelopeCode(envelope: CmCloudEnvelope): string {
  const code = record(envelope.error).code
  return typeof code === 'string' && code ? code : `CM_CLOUD_${envelopeStatus(envelope)}`
}

function stateForEnvelope(envelope: CmCloudEnvelope): CmCloudServiceError['state'] {
  const status = envelopeStatus(envelope)
  if (status === 'AUTH_REQUIRED') return 'auth-required'
  if (status === 'UNKNOWN' || status === 'RUNNING' || status === 'PARTIAL') return 'unknown'
  return 'failed'
}

function throwForEnvelope(envelope: CmCloudEnvelope): never {
  throw new CmCloudServiceError(envelopeCode(envelope), stateForEnvelope(envelope), authPageUrl(envelopeData(envelope).authPageUrl))
}

function profileOf(config: CmCloudProjectConfig): string {
  return config.profile.trim() || 'default'
}

function readDedicatedRoot(envelope: CmCloudEnvelope): { rootRef: string; namePath: string | null } {
  const root = record(envelopeData(envelope).dedicatedRoot)
  if (!validString(root.rootRef)) throw new CmCloudServiceError('CM_CLOUD_DEDICATED_ROOT_UNAVAILABLE')
  return {
    rootRef: root.rootRef,
    namePath: validString(root.namePath) ? root.namePath : null,
  }
}

function readConfirmToken(envelope: CmCloudEnvelope): string {
  const token = envelopeData(envelope).confirmToken
  if (!validString(token)) throw new CmCloudServiceError('CM_CLOUD_CONFIRMATION_TOKEN_MISSING', 'unknown')
  return token
}

function operationRefs(envelope: CmCloudEnvelope): { operationRef: string; planHash: string } {
  const data = envelopeData(envelope)
  if (!validString(data.operationRef) || !validString(data.planHash)) {
    throw new CmCloudServiceError('CM_CLOUD_OPERATION_REFERENCE_MISSING', 'unknown')
  }
  return { operationRef: data.operationRef, planHash: data.planHash }
}

function uploadName(envelope: CmCloudEnvelope): string | undefined {
  const data = envelopeData(envelope)
  for (const key of ['name', 'fileName', 'filename', 'renamedTo']) {
    if (validString(data[key])) return data[key]
  }
  return undefined
}

function statusFromConfig(config: CmCloudProjectConfig, runtime: { available: boolean; pythonCommand: string | null }, authenticated: boolean | null): CmCloudStatusView {
  return {
    available: runtime.available,
    pythonCommand: runtime.pythonCommand,
    scriptRoot: null,
    authenticated,
    autoBackupEnabled: config.enabled,
    disclosureConfirmed: config.disclosureConfirmed,
    state: config.enabled ? config.state : 'disabled',
    lastBackupAt: config.lastBackupAt,
    lastFileName: config.lastFileName,
    lastErrorCode: config.lastErrorCode,
    dedicatedRootName: config.dedicatedRootName,
  }
}

export class CmCloudBackupService {
  private readonly cli: CmCloudCli | null
  private readonly configStore: CmCloudConfigStore
  private readonly resolveCurrentProjectPath: () => string | null
  private readonly resolveGlobalDataRoot: () => string
  private readonly exportProject: (input: ExportPortableProjectInput) => Promise<ExportPortableProjectReceipt>
  private readonly pendingTimers = new Map<string, NodeJS.Timeout>()
  private readonly latestSaved = new Map<string, CmCloudProjectSavedNotification>()
  private readonly activeRuns = new Map<string, AbortController>()
  private readonly dirtyAfterRun = new Set<string>()
  private pendingAuthUrl: string | null = null

  constructor(options: CmCloudBackupServiceOptions = {}) {
    if (options.cli) {
      this.cli = options.cli
    } else {
      try {
        this.cli = new CmCloudCli()
      } catch (error) {
        if (error instanceof CmCloudCliError && error.code === 'CM_CLOUD_BUNDLE_MISSING') this.cli = null
        else throw error
      }
    }
    this.configStore = options.configStore ?? new CmCloudConfigStore()
    this.resolveCurrentProjectPath = options.getCurrentProjectPath ?? getCurrentProjectPath
    this.resolveGlobalDataRoot = options.getGlobalDataRoot ?? getGlobalDataRoot
    this.exportProject = options.exportPortableProject ?? exportPortableProject
  }

  async status(projectSession: CmCloudProjectSessionContext): Promise<CmCloudStatusView> {
    const active = this.assertSession(projectSession)
    const config = this.configStore.get(active.projectId)
    const runtime = this.cli
      ? await this.cli.runtime()
      : { available: false, pythonCommand: null }
    let authenticated: boolean | null = null
    if (runtime.available && config.enabled && this.cli) {
      try {
        const result = await this.cli.authStatus(profileOf(config))
        const status = envelopeStatus(result.envelope)
        if (status === 'SUCCESS') {
          const ticket = record(record(envelopeData(result.envelope).localCredentials).ticket)
          authenticated = typeof ticket.present === 'boolean' ? ticket.present : null
        } else if (status === 'AUTH_REQUIRED') {
          authenticated = false
        }
      } catch {
        authenticated = null
      }
    }
    return statusFromConfig(config, runtime, authenticated)
  }

  async configure(request: CmCloudConfigureRequest): Promise<CmCloudStatusView> {
    const active = this.assertSession(request.projectSession)
    if (request.enabled && request.disclosureConfirmed !== true) {
      throw new CmCloudServiceError('CM_CLOUD_DISCLOSURE_REQUIRED')
    }
    const profile = typeof request.profile === 'string' && request.profile.trim() ? request.profile.trim() : 'default'
    this.configStore.save(active.projectId, {
      enabled: request.enabled,
      disclosureConfirmed: request.enabled && request.disclosureConfirmed === true,
      profile,
      state: request.enabled ? 'ready' : 'disabled',
      lastErrorCode: null,
    })
    return this.status(request.projectSession)
  }

  async login(): Promise<{ state: 'authorized' | 'pending'; authPageUrl?: string }> {
    const cli = this.requireCli()
    const profile = 'default'
    let result
    try {
      result = await cli.login(profile)
    } catch (error) {
      throw this.asServiceError(error)
    }
    const envelope = result.envelope
    const status = envelopeStatus(envelope)
    if (status === 'SUCCESS') {
      this.pendingAuthUrl = null
      return { state: 'authorized' }
    }
    if (status === 'AUTH_REQUIRED') {
      const url = authPageUrl(envelopeData(envelope).authPageUrl)
      if (url) this.pendingAuthUrl = url
      if (url) return { state: 'pending' as const, authPageUrl: url }
      throw new CmCloudServiceError(envelopeCode(envelope), 'auth-required')
    }
    throwForEnvelope(envelope)
  }

  async openAuth(): Promise<void> {
    if (!this.pendingAuthUrl) throw new CmCloudServiceError('CM_CLOUD_AUTH_URL_UNAVAILABLE')
    await shell.openExternal(this.pendingAuthUrl)
  }

  async backup(request: CmCloudBackupRequest): Promise<CmCloudBackupSuccess | { skipped: true; status: CmCloudStatusView }> {
    const active = this.assertSession(request.projectSession)
    const config = this.configStore.get(active.projectId)
    if (!config.enabled) throw new CmCloudServiceError('CM_CLOUD_NOT_ENABLED')
    if (!config.disclosureConfirmed) throw new CmCloudServiceError('CM_CLOUD_DISCLOSURE_REQUIRED')
    if (this.activeRuns.has(active.projectId)) {
      this.dirtyAfterRun.add(active.projectId)
      return { skipped: true, status: await this.status(request.projectSession) }
    }

    const controller = new AbortController()
    this.activeRuns.set(active.projectId, controller)
    let stagingRoot: string | null = null
    try {
      this.configStore.save(active.projectId, { state: 'running', lastErrorCode: null })
      const cli = this.requireCli()
      const runtime = await cli.runtime()
      if (!runtime.available) throw new CmCloudServiceError('CM_CLOUD_PYTHON_UNAVAILABLE')
      stagingRoot = this.createStagingRoot()
      const archivePath = path.join(stagingRoot, `novel-backup-${Date.now()}.ainovel`)
      await this.exportProject({
        sourceProjectRoot: active.rootPath,
        projectSession: request.projectSession,
        targetArchivePath: archivePath,
        attemptParentPath: stagingRoot,
        assertCurrentContext: (context, sourceRoot) => {
          const current = projectAccess.assertCurrentProjectContext(context, this.resolveCurrentProjectPath())
          return projectAccess.sameCanonicalProjectRoot(current.rootPath, sourceRoot)
        },
        assets: portableAssets,
      })

      const agent = await cli.agentInfo(profileOf(config), controller.signal)
      if (envelopeStatus(agent.envelope) !== 'SUCCESS') throwForEnvelope(agent.envelope)
      const dedicatedRoot = readDedicatedRoot(agent.envelope)
      const upload = await cli.upload({
        localFile: archivePath,
        destinationRef: dedicatedRoot.rootRef,
        profile: profileOf(config),
        signal: controller.signal,
      })
      const uploadStatus = envelopeStatus(upload.envelope)
      if (uploadStatus !== 'CONFIRMATION_REQUIRED') {
        if (uploadStatus === 'SUCCESS') return await this.finishSuccess(active.projectId, request.projectSession, upload.envelope, dedicatedRoot.namePath)
        throwForEnvelope(upload.envelope)
      }
      const refs = operationRefs(upload.envelope)
      const confirmed = await cli.confirm({ ...refs, profile: profileOf(config), signal: controller.signal })
      if (envelopeStatus(confirmed.envelope) !== 'SUCCESS') throwForEnvelope(confirmed.envelope)
      const confirmToken = readConfirmToken(confirmed.envelope)
      const executed = await cli.execute({ operationRef: refs.operationRef, confirmToken, profile: profileOf(config), signal: controller.signal })
      let finalEnvelope = executed.envelope
      if (envelopeStatus(finalEnvelope) === 'RUNNING' && finalEnvelope.nextAction === 'operation.status') {
        const checked = await cli.status({ operationRef: refs.operationRef, profile: profileOf(config), signal: controller.signal })
        finalEnvelope = checked.envelope
      }
      if (envelopeStatus(finalEnvelope) !== 'SUCCESS') throwForEnvelope(finalEnvelope)
      return await this.finishSuccess(active.projectId, request.projectSession, finalEnvelope, dedicatedRoot.namePath)
    } catch (error) {
      const failure = this.asServiceError(error)
      try {
        this.configStore.save(active.projectId, {
          state: failure.state === 'cancelled' ? 'unknown' : failure.state,
          lastErrorCode: failure.code,
        })
      } catch {
        // A damaged or read-only state file must not keep the in-memory run locked.
      }
      throw failure
    } finally {
      this.activeRuns.delete(active.projectId)
      if (stagingRoot) await this.removeStagingRoot(stagingRoot)
      if (this.dirtyAfterRun.delete(active.projectId)) {
        const latest = this.latestSaved.get(active.projectId)
        if (latest) this.schedule(latest)
      }
    }
  }

  cancel(): boolean {
    let cancelled = false
    for (const controller of this.activeRuns.values()) {
      controller.abort()
      cancelled = true
    }
    return cancelled
  }

  onProjectSaved(input: CmCloudProjectSavedNotification): void {
    if (!input.context) return
    this.latestSaved.set(input.projectId, input)
    if (this.activeRuns.has(input.projectId)) {
      this.dirtyAfterRun.add(input.projectId)
      return
    }
    this.schedule(input)
  }

  private schedule(input: CmCloudProjectSavedNotification): void {
    const config = this.configStore.get(input.projectId)
    if (!config.enabled || !config.disclosureConfirmed || !input.context) return
    const previous = this.pendingTimers.get(input.projectId)
    if (previous) clearTimeout(previous)
    const timer = setTimeout(() => {
      this.pendingTimers.delete(input.projectId)
      void this.backup({ projectSession: input.context!, manual: false }).catch(() => undefined)
    }, AUTO_BACKUP_DEBOUNCE_MS)
    this.pendingTimers.set(input.projectId, timer)
  }

  private async finishSuccess(projectId: string, session: CmCloudProjectSessionContext, envelope: CmCloudEnvelope, dedicatedRootName: string | null): Promise<CmCloudBackupSuccess> {
    const backupAt = new Date().toISOString()
    const uploadedName = uploadName(envelope)
    this.configStore.save(projectId, {
      state: 'backup-complete',
      lastBackupAt: backupAt,
      lastFileName: uploadedName ?? null,
      lastErrorCode: null,
      dedicatedRootName,
    })
    return { uploadedName, backupAt, status: await this.status(session) }
  }

  private assertSession(session: CmCloudProjectSessionContext) {
    try {
      return projectAccess.assertCurrentProjectContext(session, this.resolveCurrentProjectPath())
    } catch {
      throw new CmCloudServiceError('CM_CLOUD_PROJECT_SESSION_REQUIRED')
    }
  }

  private requireCli(): CmCloudCli {
    if (!this.cli) throw new CmCloudServiceError('CM_CLOUD_BUNDLE_MISSING')
    return this.cli
  }

  private createStagingRoot(): string {
    const parent = path.join(this.resolveGlobalDataRoot(), 'cm-cloud-staging')
    fs.mkdirSync(parent, { recursive: true, mode: 0o700 })
    const root = fs.mkdtempSync(path.join(parent, `${randomUUID()}-`))
    try { fs.chmodSync(root, 0o700) } catch { /* Windows may not expose POSIX modes. */ }
    return root
  }

  private async removeStagingRoot(root: string): Promise<void> {
    try { await fs.promises.rm(root, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 }) } catch { /* best effort */ }
  }

  private asServiceError(error: unknown): CmCloudServiceError {
    if (error instanceof CmCloudServiceError) return error
    if (error instanceof CmCloudCliError) {
      const state = error.code === 'CM_CLOUD_CANCELLED' ? 'cancelled' : error.code === 'CM_CLOUD_TIMEOUT' ? 'unknown' : 'failed'
      return new CmCloudServiceError(error.code, state)
    }
    const message = error instanceof Error ? error.message : ''
    const safeCode = /^[A-Z][A-Z0-9_:-]{0,127}$/u.test(message) ? message : 'CM_CLOUD_FAILED'
    return new CmCloudServiceError(safeCode)
  }
}
