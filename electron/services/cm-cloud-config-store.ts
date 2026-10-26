import path from 'node:path'

import { tryReadJsonFile, writeJsonFile } from '../utils/config-utils'
import { getGlobalDataRoot } from './app-data-locator'

const STORE_FILE_NAME = 'cm-cloud-backup.json'
const STORE_VERSION = 1

export interface CmCloudProjectConfig {
  enabled: boolean
  disclosureConfirmed: boolean
  profile: string
  lastBackupAt: string | null
  lastFileName: string | null
  lastErrorCode: string | null
  state: 'disabled' | 'ready' | 'running' | 'backup-complete' | 'auth-required' | 'failed' | 'unknown'
  dedicatedRootName: string | null
}

interface CmCloudConfigDocument {
  version: typeof STORE_VERSION
  projects: Record<string, CmCloudProjectConfig>
}

const DEFAULT_CONFIG: CmCloudProjectConfig = {
  enabled: false,
  disclosureConfirmed: false,
  profile: 'default',
  lastBackupAt: null,
  lastFileName: null,
  lastErrorCode: null,
  state: 'disabled',
  dedicatedRootName: null,
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function validProjectId(value: string): boolean {
  return value.length > 0 && value.length <= 256 && !/[\u0000-\u001f\u007f-\u009f]/u.test(value)
}

function validConfig(value: unknown): value is CmCloudProjectConfig {
  if (!isRecord(value)) return false
  return typeof value.enabled === 'boolean'
    && typeof value.disclosureConfirmed === 'boolean'
    && typeof value.profile === 'string'
    && value.profile.length > 0
    && value.profile.length <= 256
    && (value.lastBackupAt === null || typeof value.lastBackupAt === 'string')
    && (value.lastFileName === null || typeof value.lastFileName === 'string')
    && (value.lastErrorCode === null || typeof value.lastErrorCode === 'string')
    && ['disabled', 'ready', 'running', 'backup-complete', 'auth-required', 'failed', 'unknown'].includes(String(value.state))
    && (value.dedicatedRootName === null || typeof value.dedicatedRootName === 'string')
}

function parseDocument(value: unknown): CmCloudConfigDocument {
  if (!isRecord(value) || value.version !== STORE_VERSION || !isRecord(value.projects)) {
    throw new Error('CM_CLOUD_CONFIG_INVALID')
  }
  const projects: Record<string, CmCloudProjectConfig> = {}
  for (const [projectId, config] of Object.entries(value.projects)) {
    if (!validProjectId(projectId) || !validConfig(config)) throw new Error('CM_CLOUD_CONFIG_INVALID')
    projects[projectId] = {
      ...DEFAULT_CONFIG,
      ...config,
      state: config.state as CmCloudProjectConfig['state'],
    }
  }
  return { version: STORE_VERSION, projects }
}

export interface CmCloudConfigStoreOptions {
  rootPath?: string
}

export class CmCloudConfigStore {
  private readonly filePath: string

  constructor(options: CmCloudConfigStoreOptions = {}) {
    this.filePath = path.join(options.rootPath ?? getGlobalDataRoot(), STORE_FILE_NAME)
  }

  get(projectId: string): CmCloudProjectConfig {
    if (!validProjectId(projectId)) throw new Error('CM_CLOUD_INPUT_INVALID')
    const document = this.load()
    return { ...DEFAULT_CONFIG, ...(document.projects[projectId] ?? {}) }
  }

  save(projectId: string, patch: Partial<CmCloudProjectConfig>): CmCloudProjectConfig {
    if (!validProjectId(projectId)) throw new Error('CM_CLOUD_INPUT_INVALID')
    const document = this.load()
    const next = { ...DEFAULT_CONFIG, ...(document.projects[projectId] ?? {}), ...patch }
    if (!validConfig(next)) throw new Error('CM_CLOUD_CONFIG_INVALID')
    document.projects[projectId] = next
    writeJsonFile(this.filePath, document)
    return { ...next }
  }

  private load(): CmCloudConfigDocument {
    const result = tryReadJsonFile<unknown>(this.filePath)
    if (result.status === 'missing') return { version: STORE_VERSION, projects: {} }
    if (result.status === 'error') throw new Error('CM_CLOUD_CONFIG_INVALID')
    return parseDocument(result.value)
  }
}
