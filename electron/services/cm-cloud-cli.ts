import fs from 'node:fs'
import path from 'node:path'
import { spawn } from 'node:child_process'

export interface CmCloudEnvelope {
  schemaVersion?: string
  command?: string
  status?: string
  data?: unknown
  counts?: unknown
  page?: unknown
  error?: { code?: unknown; retryable?: unknown } | null
  nextAction?: string | null
  traceId?: string
}

export class CmCloudCliError extends Error {
  constructor(readonly code: string, readonly envelope?: CmCloudEnvelope) {
    super(code)
    this.name = 'CmCloudCliError'
  }
}

export interface CmCloudCliCommandResult {
  envelope: CmCloudEnvelope
  pythonCommand: string
}

export interface CmCloudCliOptions {
  scriptRoot?: string
  platform?: NodeJS.Platform
  env?: NodeJS.ProcessEnv
  commandTimeoutMs?: number
}

interface PythonInvocation {
  command: string
  prefixArgs: string[]
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function safeErrorCode(envelope: CmCloudEnvelope): string | null {
  const code = record(envelope.error)?.code
  return typeof code === 'string' && code.length <= 128 ? code : null
}

function parseEnvelope(stdout: string): CmCloudEnvelope {
  const text = stdout.trim()
  const candidates = [text]
  const first = text.indexOf('{')
  const last = text.lastIndexOf('}')
  if (first >= 0 && last > first) candidates.push(text.slice(first, last + 1))
  for (const candidate of candidates) {
    try {
      const parsed = JSON.parse(candidate) as unknown
      const envelope = record(parsed)
      if (envelope && typeof envelope.status === 'string') return envelope as CmCloudEnvelope
    } catch {
      // The CLI contract is JSON; keep trying the bounded substring in case a
      // Python bootstrap message preceded the envelope.
    }
  }
  throw new CmCloudCliError('CM_CLOUD_INVALID_RESPONSE')
}

function scriptRootCandidates(): string[] {
  const candidates: string[] = []
  if (typeof process.resourcesPath === 'string' && process.resourcesPath) {
    candidates.push(path.join(process.resourcesPath, 'cm-cloud-manage'))
  }
  if (process.env.APP_ROOT) candidates.push(path.join(process.env.APP_ROOT, 'build', 'cm-cloud-manage'))
  candidates.push(path.resolve('build', 'cm-cloud-manage'))
  return [...new Set(candidates)]
}

function resolveScriptRoot(explicit?: string): string {
  const candidates = explicit ? [explicit] : scriptRootCandidates()
  const root = candidates.find(candidate => fs.existsSync(path.join(candidate, 'scripts', 'cm_cloud_cli_api.py')))
  if (!root) throw new CmCloudCliError('CM_CLOUD_BUNDLE_MISSING')
  return path.resolve(root)
}

function pythonCandidates(platform: NodeJS.Platform): PythonInvocation[] {
  return platform === 'win32'
    ? [{ command: 'py', prefixArgs: ['-3'] }, { command: 'python', prefixArgs: [] }]
    : [{ command: 'python3', prefixArgs: [] }, { command: 'python', prefixArgs: [] }]
}

export class CmCloudCli {
  readonly scriptRoot: string
  private readonly platform: NodeJS.Platform
  private readonly env: NodeJS.ProcessEnv
  private readonly commandTimeoutMs: number
  private python: PythonInvocation | null = null

  constructor(options: CmCloudCliOptions = {}) {
    this.scriptRoot = resolveScriptRoot(options.scriptRoot)
    this.platform = options.platform ?? process.platform
    this.env = {
      ...process.env,
      ...options.env,
      PYTHONIOENCODING: 'utf-8',
      PYTHONUTF8: '1',
    }
    this.commandTimeoutMs = options.commandTimeoutMs ?? 6 * 60 * 1_000
  }

  async runtime(): Promise<{ available: boolean; pythonCommand: string | null }> {
    try {
      const python = await this.resolvePython()
      return { available: true, pythonCommand: this.displayPython(python) }
    } catch (error) {
      if (error instanceof CmCloudCliError && error.code === 'CM_CLOUD_PYTHON_UNAVAILABLE') {
        return { available: false, pythonCommand: null }
      }
      throw error
    }
  }

  async run(args: string[], options: { signal?: AbortSignal; timeoutMs?: number } = {}): Promise<CmCloudCliCommandResult> {
    const python = await this.resolvePython()
    const script = this.scriptForArgs(args)
    const result = await this.spawnJson(python, [script, ...args, '--format', 'json'], options)
    const envelope = parseEnvelope(result.stdout)
    return { envelope, pythonCommand: this.displayPython(python) }
  }

  async login(profile: string, signal?: AbortSignal): Promise<CmCloudCliCommandResult> {
    return this.run(['auth', 'login', ...this.profileArgs(profile)], { signal })
  }

  async authStatus(profile: string, signal?: AbortSignal): Promise<CmCloudCliCommandResult> {
    return this.run(['auth', 'status', ...this.profileArgs(profile)], { signal })
  }

  async agentInfo(profile: string, signal?: AbortSignal): Promise<CmCloudCliCommandResult> {
    return this.run(['agent', 'info', ...this.profileArgs(profile)], { signal })
  }

  async upload(input: { localFile: string; destinationRef: string; profile: string; signal?: AbortSignal }): Promise<CmCloudCliCommandResult> {
    return this.run([
      'files', 'upload',
      '--localFile', input.localFile,
      '--destinationRef', input.destinationRef,
      '--overwritePolicy', 'rename',
      ...this.profileArgs(input.profile),
    ], { signal: input.signal })
  }

  async confirm(input: { operationRef: string; planHash: string; profile: string; signal?: AbortSignal }): Promise<CmCloudCliCommandResult> {
    return this.run([
      'operation', 'confirm',
      '--operation-ref', input.operationRef,
      '--plan-hash', input.planHash,
      '--confirmation', 'CONFIRM',
      ...this.profileArgs(input.profile),
    ], { signal: input.signal })
  }

  async execute(input: { operationRef: string; confirmToken: string; profile: string; signal?: AbortSignal }): Promise<CmCloudCliCommandResult> {
    return this.run([
      'operation', 'execute',
      '--operation-ref', input.operationRef,
      '--confirm-token', input.confirmToken,
      ...this.profileArgs(input.profile),
    ], { signal: input.signal })
  }

  async status(input: { operationRef: string; profile: string; signal?: AbortSignal }): Promise<CmCloudCliCommandResult> {
    return this.run([
      'operation', 'status',
      '--operation-ref', input.operationRef,
      ...this.profileArgs(input.profile),
    ], { signal: input.signal })
  }

  static errorCode(envelope: CmCloudEnvelope): string | null { return safeErrorCode(envelope) }

  private profileArgs(profile: string): string[] {
    return profile ? ['--profile', profile] : []
  }

  private scriptForArgs(args: string[]): string {
    const domain = args[0]
    const scriptName = domain === 'auth' || domain === 'agent' || domain === 'files' || domain === 'operation'
      ? `cm_cloud_${domain}_api.py`
      : 'cm_cloud_cli_api.py'
    return path.join(this.scriptRoot, 'scripts', scriptName)
  }

  private displayPython(python: PythonInvocation): string {
    return [python.command, ...python.prefixArgs].join(' ')
  }

  private async resolvePython(): Promise<PythonInvocation> {
    if (this.python) return this.python
    for (const candidate of pythonCandidates(this.platform)) {
      try {
        await this.spawnProcess(candidate, ['--version'], { timeoutMs: 10_000 })
        this.python = candidate
        return candidate
      } catch {
        // Try the next installed interpreter.
      }
    }
    throw new CmCloudCliError('CM_CLOUD_PYTHON_UNAVAILABLE')
  }

  private async spawnJson(python: PythonInvocation, args: string[], options: { signal?: AbortSignal; timeoutMs?: number }): Promise<{ stdout: string; stderr: string }> {
    return this.spawnProcess(python, args, options)
  }

  private async spawnProcess(python: PythonInvocation, args: string[], options: { signal?: AbortSignal; timeoutMs?: number }): Promise<{ stdout: string; stderr: string }> {
    const timeoutMs = options.timeoutMs ?? this.commandTimeoutMs
    const child = spawn(python.command, [...python.prefixArgs, ...args], {
      cwd: this.scriptRoot,
      env: this.env,
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    let stdout = ''
    let stderr = ''
    const maxStdout = 8 * 1024 * 1024
    const maxStderr = 128 * 1024
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', chunk => {
      stdout += String(chunk)
      if (stdout.length > maxStdout) child.kill()
    })
    child.stderr.on('data', chunk => {
      stderr += String(chunk)
      if (stderr.length > maxStderr) stderr = stderr.slice(-maxStderr)
    })

    let timedOut = false
    const timer = setTimeout(() => {
      timedOut = true
      child.kill()
    }, timeoutMs)
    const abort = () => child.kill()
    if (options.signal?.aborted) abort()
    options.signal?.addEventListener('abort', abort, { once: true })
    try {
      const exitCode = await new Promise<number | null>((resolve, reject) => {
        child.once('error', () => reject(new CmCloudCliError('CM_CLOUD_COMMAND_UNAVAILABLE')))
        child.once('close', code => resolve(code))
      })
      if (options.signal?.aborted) throw new CmCloudCliError('CM_CLOUD_CANCELLED')
      if (timedOut) throw new CmCloudCliError('CM_CLOUD_TIMEOUT')
      if (exitCode !== 0 && !stdout.trim()) throw new CmCloudCliError('CM_CLOUD_COMMAND_FAILED')
      return { stdout, stderr }
    } finally {
      clearTimeout(timer)
      options.signal?.removeEventListener('abort', abort)
    }
  }
}

export function envelopeData(value: CmCloudEnvelope): Record<string, unknown> {
  return record(value.data) ?? {}
}

export function envelopeStatus(value: CmCloudEnvelope): string {
  return typeof value.status === 'string' ? value.status : 'UNKNOWN'
}
