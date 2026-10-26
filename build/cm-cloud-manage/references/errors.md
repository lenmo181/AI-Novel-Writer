# errors 错误话术与恢复

> 本文件按需加载：出现非 `SUCCESS`/`EMPTY` 的返回、`error.code`、或需要恢复时读取。**状态判定与消费规则在 [状态处理](protocol.md#状态处理)**，本文件只给错误码到用户话术的映射与恢复口径；Skill 只做三件事——解释发生了什么、说明影响面、给**一个**可执行动作。

## 1. 何时触发

授权与确认分别直达auth.md和confirmation.md，不叠加错误模板。其他命令返回非 `SUCCESS`/`EMPTY`；出现 `RUNNING`/`PARTIAL`/`REJECTED`/`FAILED`/`UNKNOWN`；带 `error.code`；`page.complete=false`、`counts` 为 `null`；用户再次询问此前未完成的任务。

## 2. 错误码 → 话术与恢复

| `error.code` | 用户话术 | 恢复 | 默认 `retryable` |
| :-- | :-- | :-- | :-: |
| `INVALID_ARGUMENT` | 参数不完整或不合法（业务语言，不堆字段名） | 补正后再发起；**不提供"重试"建议** | `false` |
| `INVALID_ARGUMENT`（文件夹命名） | **点名**名称里不能使用的符号，例如：当前文件夹名称中包含 ; & $，无法创建，请您重新命名 | 请用户改名后重新发起；**不提供"重试"建议**、不改名代替用户决定 | `false` |
| `AUTH_REQUIRED`/`AUTH_EXPIRED` | 需要完成云盘授权 | 仅在回执要求auth.login时调用一次；最终回复只用[授权模板及Agent渲染表](auth.md)，不加错误卡或额外总结。退出解绑例外按auth.md处理 | `false` |
| `CONSENT_DENIED` | 你取消了本次授权 | 需要时重新说"登录云盘" | `false` |
| `BINDING_MISMATCH`（实例/应用/绑定） | 当前账号与云盘的绑定关系不一致 | 云盘 App 核对绑定 / 重新授权 / 联系管理员 | `false` |
| `BINDING_MISMATCH`（签名、IP 校验） | 请求校验未通过（环境或时间偏差） | 稍后重试或检查网络；**不拉起授权** | **`true`** |
| `SCOPE_DENIED`（平台 HTTP 403 或业务码 13000006） | 平台拒绝了当前资源操作 | 说明原目标未成功操作；可建议用户选择专属目录或在云盘侧处理权限，改变目标前须重新确认 | `false` |
| 任意操作返回 `16110013`（资源路径未在授权范围内） | 当前资源路径不在授权范围内 | 不论当前操作是什么，CLI 收到该码都强制调用 `/open-ai-space/richlifeApp/idaas/user/folder/query` 重查当前可操作专属目录并缓存 24 小时（后续优先用缓存，再报则重查更新），把当前可操作目录挂到回执：`data.dedicatedRoot` 给出 `rootRef`（不透明引用，后续请求可直接指定该目录）与 `namePath`，消息提示「当前可操作的专属目录为 <namePath>；请与用户确认后重新发起操作，确认后本次操作将在该目录内完成」。**本次不自动重试、不改写目标**：把当前可操作目录告知用户，**用户确认后重新发起**，重新发起的请求将使用该当前目录完成（真实目录 ID/ID 路径不进回执，由 CLI 在后续请求中内部使用）。**不要让用户重新授权或去 App 排查绑定** | `false` |
| `CAPABILITY_UNAVAILABLE` | 该能力在当前版本未开放 | 说明未开放、不调用；有替代给替代 | `false` |
| `NOT_FOUND` | 未找到符合条件的对象 | 确认名称或范围；**经同意后**才扩大条件 | `false` |
| `AMBIGUOUS_RESOURCE` | 找到多个同名对象 | 列出脱敏候选让用户选择 | `false` |
| `CONFIRMATION_REQUIRED` | 等待确认，不是失败 | 只用[确认模板](confirmation.md)。组合任务使用一个orchestrate冻结计划、一次确认、一次执行；不拆为逐项确认 | `false` |
| `PLAN_EXPIRED` | 计划已过期 | 重新生成计划后再 `operation.confirm`，不复用旧确认 | `false` |
| `CONFLICT` | 存在同名对象或资源被占用 | 只提示当前命令支持的处理方式；建目录可换名或选择已有目录，上传不可建议覆盖，下载覆盖须确认。不擅自换策略 | `false` |
| `RATE_LIMITED` | 请求过于频繁 | 说明稍后再试；**不说成失败，也不说成已完成** | **`true`** |
| `UPSTREAM_TIMEOUT`（只读） | 查询超时，或任务仍在处理（已达本轮 300 秒等待上限） | 报告稍后再试；不自行循环 | **`true`** |
| `UPSTREAM_TIMEOUT`（写，顶层 `RUNNING`） | 任务仍在处理，本轮等待已达上限，**不是失败** | 信封带 `nextAction=operation.status` 时续查一次；否则结束本轮，用户追问时经 `operation.status` 复核，**不重发** | **`true`** |
| `UNKNOWN`（状态枚举未冻结或下游无法判定） | 提交结果不确定，正在核对 | 按 `references/protocol.md` 第 4 节 复核（用户追问时经 `operation.status`）或幂等复核，**不重发** | `false` |
| `TASK_STATUS_QUERY_FAILED` | 转存任务已提交，但状态查询失败，目前无法确认是否完成 | `retryable=true` 仅允许稍后查询原任务（`operation.status`）；不重新提交转存，不声称失败、成功或目标目录为空 | `true` |
| `UPSTREAM_CONTRACT_MISMATCH` | 接口契约与当前版本不匹配，本次请求已拒绝 | 升级 CLI/Skill；**不主动输出 traceId**（用户索要时再给） | `false` |
| `SECURE_STORE_UNAVAILABLE` | 本机安全存储不可用，无法安全保存身份材料 | 按后端检查——`keychain` 后端查系统凭据库（按 `references/protocol.md` 第 2 节自检），`file` 后端查状态文件所在目录是否可写（磁盘满 / 只读 / 权限不足）；**始终不明文降级**：探测阶段不可用即不继续取票，探测通过后才失败时回退到 file 后端的加密文件继续 | `false` |
| `FAILED`（无更具体 code） | 操作失败 + 用 `counts` 说明影响面；本机问题（文件不可读、磁盘不足、目录不可写）按此表述 | 展示失败明细；批量任务逐项反馈 | `false` |
| `FAILED` 且 `message` 含「网络不可达：运行环境缺少可用 IPv6 出口 … (host: <域名>)」 | 目标域名返回了 IPv6 地址但运行环境无 v6 出口（Errno 101），地址族问题，**非用户本机问题** | 转平台侧处理（出口地址族/解析策略）；**不让用户检查手机网络** | `false` |
| `FAILED` 且 `message` 含「连接被中间设备重置 … (host: <域名>)」 | 出口链路对该传输通道的拦截或抖动（Errno 104），**非用户本机问题**；CLI 已自动重试仍失败 | 稍后由用户重新发起；持续失败转平台侧核对出口链路与对端拦截策略 | `true` |
| `FAILED` 且 `message` 含「网络请求失败 … (host: <域名>)」（不含上面 101/104 语义） | 目标主机无法连接或解析（业务接口域名或上传/下载存储域名），**非用户本机问题**；话术点名该 host：服务 <host> 暂不可达，本次操作没有完成 | 转平台侧处理（域名解析/出站放通）；**不让用户检查手机网络、不反复重试**；恢复后由用户重新发起 | `false` |
| `FAILED` 且 `message` 含「请求可能已送达（结果未知），请先核查原操作，不要直接重发」 | **写操作的网络结果不确定**：请求可能已到达服务端。CLI 按"不可证明未发送就绝不重发"的原则**主动停止重试**，以免重复提交（重复建单/重复转存等） | **先核查原操作是否已生效**（目标目录 `files list` 或 `operation.status`）；已生效则告知用户已完成，未生效再由用户重新发起。**不得**自行重发同一条写命令 | `false` |
| 表外或 `error.code` 缺失 | 按 `UNKNOWN` 兜底措辞 | 按 `references/protocol.md` 第 4 节 复核（用户追问时经 `operation.status`）；仍不可判定转人工（**不主动输出 traceId**） | `false` |

- `error.code` 缺失或表外 → 按 `UNKNOWN` 处理，不改写不猜测；`error.code` 与 `status` 矛盾 → **以 `status` 为准**。
- **预签名 host 例外的转述口径**：上传/下载传输失败的 `message` 会带 `(host: <域名>)`，该 host **可以转述**（与文件夹命名符号例外同级），用于把故障定位到平台存储域名；签名参数、URL 路径与 query 仍一律不输出。上传/下载走的是服务端下发的存储地址，与业务接口不同域——业务接口正常而传输失败时，优先按此条归类，**不要归因为"本机网络"或建议用户检查手机网络**。
- `SCOPE_DENIED` 表示平台明确拒绝当前资源操作；`message` 仅供模型理解原因、**不得原样转述**。平台明确拒绝时可建议改到专属目录，但不能静默更改原目标。
- **文件夹命名例外（可以、也应当转述具体信息）**：`INVALID_ARGUMENT` 可以告诉用户**具体哪些符号**不能用（如：当前文件夹名称中包含 ; & $，无法创建，请您重新命名）。例外只放开“哪些符号”这一点，`pattern`、schema 原文、上游报文、堆栈、错误码原文仍一律不输出。
- `PLAN_EXPIRED` 的顶层 `status` 是 `CONFIRMATION_REQUIRED`、`nextAction=operation.confirm`（见 [状态处理](protocol.md#状态处理)）；但计划已失效、`confirm-token` 同时失效，**不得重复调用 `operation.confirm`**，应停止并请用户重新发起（见 [状态处理](protocol.md#状态处理)）。本表只负责话术，不参与状态判定。
- **重试只按 `retryable` 给**：以信封下发的 `retryable` 为准，不从 `code` 反推；`false` 时不给"重试"建议，`true` 只表示具备安全重试条件、**不授权自动循环**。写请求是否受理不明时，**无论 `retryable` 取值都不给重试建议**，只能按 `references/protocol.md` 第 4 节 的恢复入口查询原操作或做幂等复核（`nextAction` 可能为 `null`，不据此重发）。授权类挂起等用户，超时即停。
- **只读超时与限流同为 `UNKNOWN`**：`UPSTREAM_TIMEOUT`（只读，`retryable=true`）与 `RATE_LIMITED`（`retryable=true`）的顶层 `status` 都是 `UNKNOWN`；**恢复动作是重新调用该只读命令**（不据此走 `operation.status` 复核）；写请求超时（已受理）为 `RUNNING` + `UPSTREAM_TIMEOUT` + `retryable=true`，仅表示可安全续查/复核，**不得重发**；状态枚举未冻结的写任务为 `UNKNOWN` + `retryable=false`。
- **`SECURE_STORE_UNAVAILABLE` 按实际后端区分**：当前及缺省为 `file`（检查状态文件目录可写性、磁盘与加密状态）；只有显式 `keychain` 才检查系统凭据库。`auth status --all` 为独立的系统凭据库诊断入口，与 file profile 无关。

## 3. 话术三要素与禁忌

**三要素（缺一不可）**：① 发生了什么——用户语言，不带错误码原文、HTTP 状态、DTO 字段堆砌；② 影响面——哪些已完成、哪些没有、有无副作用（`PARTIAL` 必须给成功/失败数）；③ 下一步——只给一个可执行动作，给不出时明说"需要人工处理"。
**禁忌**：不输出错误码原文与 traceId（用户主动索要 traceId 时除外）、上游报文、headers、堆栈、schema 原文；**不把"签名/IP 校验失败"表述为"未登录"**，也不因此拉起授权页；不把 `UNKNOWN`/`REJECTED`/`CONFLICT`/`RATE_LIMITED` 表述为"失败"；不承诺"一定会成功"；`status` 非 `SUCCESS` 时不说"已完成"。

**traceId 展示时机**：**不主动输出**——确认卡、结果与失败说明里都不给；仅在用户主动索要时给出（[确认与安全边界](confirmation.md)、`references/protocol.md` 第 3 节）。

## 4. 恢复

- **写请求**：一经确认发出，结果不明时**结束本轮**（例外：写执行超时信封带 `nextAction=operation.status` 时先续查一次），用户追问时按 `references/protocol.md` 第 4 节 经 `operation.status` 或幂等复核，**不重发、不因换票重发**；"已发送但无回执"记为 `UNKNOWN`，禁止从头重放。
- **只读请求**：由 CLI 按自身策略有限重试；仍失败则报告，不自行循环。
- **不做探针式排障**：非 `SUCCESS`/`EMPTY` 时只解释该回执并结束本轮，**不追加任何调用**——不换参数/换编码重跑、不试其它域命令探路、不复取 `capabilities`/`schema`、不用路径或猜测引用重试、不翻 `cmcloud.json`/`auth_profiles.json` 找引用。加载本文件是规定动作，不算探测。根因在发布侧（如缺专属目录根 ID）时如实说明由谁处理，不代用户验证（见 [状态处理](protocol.md#状态处理)）。
- **转人工**：复核后仍不可判定，说明需要人工处理；**不主动输出 traceId**（用户索要时再给）。
- 台账与持久化由 CLI 完成，Skill 不落盘、不写凭据。
