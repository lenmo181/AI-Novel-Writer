export interface CmCloudProjectSessionContext {
  projectId: string
  leaseId: string
  projectPath: string
}

export type CmCloudBackupState =
  | 'disabled'
  | 'ready'
  | 'running'
  | 'backup-complete'
  | 'auth-required'
  | 'failed'
  | 'unknown'

export interface CmCloudStatusView {
  available: boolean
  pythonCommand: string | null
  scriptRoot: string | null
  authenticated: boolean | null
  autoBackupEnabled: boolean
  disclosureConfirmed: boolean
  state: CmCloudBackupState
  lastBackupAt: string | null
  lastFileName: string | null
  lastErrorCode: string | null
  dedicatedRootName: string | null
}

export interface CmCloudConfigureRequest {
  projectSession: CmCloudProjectSessionContext
  enabled: boolean
  disclosureConfirmed: boolean
  profile?: string
}

export interface CmCloudBackupRequest {
  projectSession: CmCloudProjectSessionContext
  manual?: boolean
}

export interface CmCloudFailure {
  success: false
  state: 'failed' | 'cancelled' | 'auth-required' | 'unknown'
  errorCode: string
  authPageUrl?: string
}

export interface CmCloudChannels {
  'cm-cloud:status': {
    args: [projectSession: CmCloudProjectSessionContext]
    return: { success: true; status: CmCloudStatusView } | CmCloudFailure
  }
  'cm-cloud:configure': {
    args: [request: CmCloudConfigureRequest]
    return: { success: true; status: CmCloudStatusView } | CmCloudFailure
  }
  'cm-cloud:login': {
    args: []
    return: {
      success: true
      state: 'authorized' | 'pending'
      authPageUrl?: string
    } | CmCloudFailure
  }
  'cm-cloud:open-auth': {
    args: []
    return: { success: true } | CmCloudFailure
  }
  'cm-cloud:backup': {
    args: [request: CmCloudBackupRequest]
    return: {
      success: true
      state: 'backup-complete' | 'skipped'
      uploadedName?: string
      backupAt?: string
      status: CmCloudStatusView
    } | CmCloudFailure
  }
  'cm-cloud:cancel': {
    args: []
    return: { success: true; cancelled: boolean } | CmCloudFailure
  }
}
