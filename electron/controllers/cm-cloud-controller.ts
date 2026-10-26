import { ipcMain } from 'electron'

import type {
  CmCloudBackupRequest,
  CmCloudConfigureRequest,
  CmCloudProjectSessionContext,
} from '../../src/shared/cm-cloud'
import { CmCloudServiceError, CmCloudBackupService } from '../services/cm-cloud-backup-service'

interface CmCloudIpcRegistrar {
  handle(channel: string, handler: (_event: unknown, ...args: unknown[]) => unknown): void
}

export interface CmCloudControllerDependencies {
  ipc: CmCloudIpcRegistrar
  service: Pick<CmCloudBackupService, 'status' | 'configure' | 'login' | 'openAuth' | 'backup' | 'cancel'>
}

function stringField(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null
}

function session(value: unknown): CmCloudProjectSessionContext {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new CmCloudServiceError('CM_CLOUD_INPUT_INVALID')
  const input = value as Record<string, unknown>
  const projectId = stringField(input.projectId)
  const leaseId = stringField(input.leaseId)
  const projectPath = stringField(input.projectPath)
  if (!projectId || !leaseId || !projectPath) throw new CmCloudServiceError('CM_CLOUD_INPUT_INVALID')
  return { projectId, leaseId, projectPath }
}

function configureRequest(value: unknown): CmCloudConfigureRequest {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new CmCloudServiceError('CM_CLOUD_INPUT_INVALID')
  const input = value as Record<string, unknown>
  return {
    projectSession: session(input.projectSession),
    enabled: input.enabled === true,
    disclosureConfirmed: input.disclosureConfirmed === true,
    ...(typeof input.profile === 'string' ? { profile: input.profile } : {}),
  }
}

function backupRequest(value: unknown): CmCloudBackupRequest {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new CmCloudServiceError('CM_CLOUD_INPUT_INVALID')
  const input = value as Record<string, unknown>
  return {
    projectSession: session(input.projectSession),
    ...(input.manual === true ? { manual: true } : {}),
  }
}

function failure(error: unknown) {
  if (error instanceof CmCloudServiceError) {
    return {
      success: false as const,
      state: error.state,
      errorCode: error.code,
      ...(error.authPageUrl ? { authPageUrl: error.authPageUrl } : {}),
    }
  }
  return { success: false as const, state: 'failed' as const, errorCode: 'CM_CLOUD_FAILED' }
}

export function registerCmCloudController(
  injected?: CmCloudControllerDependencies,
): void {
  const deps = injected ?? {
    ipc: {
      handle(channel: string, handler: (_event: unknown, ...args: unknown[]) => unknown) {
        ipcMain.handle(channel, (event, ...args) => handler(event, ...args))
      },
    },
    service: new CmCloudBackupService(),
  }

  deps.ipc.handle('cm-cloud:status', async (_event, rawSession) => {
    try {
      return { success: true as const, status: await deps.service.status(session(rawSession)) }
    } catch (error) {
      return failure(error)
    }
  })

  deps.ipc.handle('cm-cloud:configure', async (_event, rawRequest) => {
    try {
      return { success: true as const, status: await deps.service.configure(configureRequest(rawRequest)) }
    } catch (error) {
      return failure(error)
    }
  })

  deps.ipc.handle('cm-cloud:login', async () => {
    try {
      return { success: true as const, ...(await deps.service.login()) }
    } catch (error) {
      return failure(error)
    }
  })

  deps.ipc.handle('cm-cloud:open-auth', async () => {
    try {
      await deps.service.openAuth()
      return { success: true as const }
    } catch (error) {
      return failure(error)
    }
  })

  deps.ipc.handle('cm-cloud:backup', async (_event, rawRequest) => {
    try {
      const result = await deps.service.backup(backupRequest(rawRequest))
      if ('skipped' in result) return { success: true as const, state: 'skipped' as const, status: result.status }
      return {
        success: true as const,
        state: 'backup-complete' as const,
        ...(result.uploadedName ? { uploadedName: result.uploadedName } : {}),
        backupAt: result.backupAt,
        status: result.status,
      }
    } catch (error) {
      return failure(error)
    }
  })

  deps.ipc.handle('cm-cloud:cancel', async () => ({ success: true as const, cancelled: deps.service.cancel() }))
}
