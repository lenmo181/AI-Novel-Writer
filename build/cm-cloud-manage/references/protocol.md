# cmcloud CLI 协议参考

> 按需加载：需要核对信封字段语义、通用命令行为、操作状态机或排障口径时读取。
> 状态消费规则（每条命令返回后必做）见下文 [状态处理](#状态处理)。

## 目录

1. 通用命令
2. 不可用与排障
3. 结果信封
4. 操作状态与异步任务

## 1. 通用命令

通用命令**不触发业务授权、不申请票据、不调用下游业务接口**；除 `--profile <ref>`（隔离身份与环境）与 `--format json` 外不接受业务参数。

| 命令 | 关键行为 |
| :-- | :-- |
| `cmcloud capabilities --format json` | 看当前发布版**开放的命令与已开放的枚举值**及禁用原因；按 [执行流程](../SKILL.md)取一次。 |
| `cmcloud help --topic <domain> --format json` | 取该领域语义摘要、命令索引、适用场景与禁用能力；`--topic` 取值含 `operation`。**宿主不支持读文件时用它替代加载 reference**。 |
| `cmcloud schema --command-id <command-id> --format json` | 取输入/输出 Schema、路由枚举、必填、上限与确认策略；**禁用命令也返回 Schema 并给出禁用原因**，据此判断能力开关。 |
| `cmcloud operation confirm --operation-ref <ref> [--plan-hash <hash> --confirmation CONFIRM]` | 把计划 `PENDING_CONFIRMATION`→`CONFIRMED`。支持带宿主签名的确认事件或人工 CLI 交互；对话确认默认开启，用户在展示计划后的后续消息中明确同意时，Agent 可使用原计划的 `operationRef`/`planHash` 与字面量 `CONFIRM` 进行非交互确认；发布环境可设置 `CMCLOUD_ALLOW_CONVERSATION_CONFIRMATION=false` 显式关闭。对话确认是**模型侧流程约束，不是用户身份证明**、也不构成不可越过的安全边界。`--plan-hash` 与 `--confirmation` 必须同时出现；校验 profile、计划内容与有效期后签发一次性 `data.confirmToken`，**只确认，不调业务接口**。**例外**：`auth revoke`／`auth logout` 的计划用 `op_…` 引用、不写入台账，其“确认＋执行”是**重调原命令**并带 `--operationRef`+`--planHash`，**不能走本命令**（见 `auth.md`）。 |
| `cmcloud operation execute --operation-ref <ref> --confirm-token <ref>` | 执行已确认计划。`--confirm-token` **由 CLI 确认接口返回并与计划绑定，模型不得生成**；`--profile` 须与计划一致；按已冻结步骤执行，**不重新解释用户意图**。 |
| `cmcloud operation status --operation-ref <ref>` | 按台账 `operationRef` 返回该操作的当前状态：台账已处可信终态时直接折算返回、**不重复调用下游**；仍有下游任务时在 CLI 内按固定 5 秒/最多 60 次/单轮 300 秒轮询，到终态或预算耗尽返回单个信封（非终态＝`RUNNING` + `UPSTREAM_TIMEOUT`）；状态枚举未冻结的任务单次只查原任务、按 `UNKNOWN` 输出。**不续查、不重交写请求**。**写链不自动调用**，唯一例外是写执行超时信封下发 `nextAction=operation.status`（调用一次）；其余由用户追问触发一次、不循环；消费规则与状态口径见本文件第 4 节。 |

## 2. 不可用与排障

- **CLI 不可用**：裸命令不存在（PATH 没有、入口脚本路径写错）、Python 解释器缺失（退出码 9009／127，或 stderr 报 `python3`/`python`/`py` 不是内部或外部命令）、退出码 7 或 `SECURE_STORE_UNAVAILABLE` → **先按 [调用入口与参数](protocol.md#调用入口与参数) 用技能根目录下的完整路径重试一次**，不要重复下发裸命令、也不要声称"需要安装 CLI"；仍不可用则向用户说明"环境或依赖不可用"并给一个可执行建议（例如改用完整路径调用、或确认技能包完整）。**不编造任何云盘数据、不降级为凭记忆作答**；排障只用真实命令的 `error.code`、退出码与 traceId，并直接说明环境异常、不猜测原因。

- **三平台环境前提**（缺任一项都表现为上一段的"环境不可用"，不是代码缺陷；换平台**不需要换技能版本**，同一份源码在 Windows／macOS／Linux 都可运行）：

| 前提 | Windows | macOS | Linux |
| :-- | :-- | :-- | :-- |
| 解释器 | Python ≥ 3.10（`py -3` 或 `python`） | Python ≥ 3.10（`python3`） | Python ≥ 3.10（`python3`） |
| 依赖 | `requests`/`pycryptodome`/`keyring` | 同左 | 同左 |
| 凭据后端（`storage_backend=keychain`） | 凭据管理器（Generic Credentials，service `skill_auth_code`） | 登录钥匙串（首次访问可能弹出"允许访问钥匙串"授权框；无人值守时会失败） | Secret Service（需运行 gnome-keyring 或 KWallet，且会话内存在 `DBUS_SESSION_BUS_ADDRESS`；纯 SSH／容器／无桌面会话通常不可用） |

  凭据后端由 profile 的 `storage_backend` 决定：登记为 `file` 时凭据落在本机按 `agent / profile / skill` 隔离的目录内，**不需要**系统凭据库；上表与下面的钥匙串自检只适用于 `keychain` 后端。

  自检（`keychain` 后端；不改任何凭据，只读）：`python3 -c "import keyring;print(keyring.get_keyring())"`——macOS 期望 `macOS.Keyring`、Linux 期望 `SecretService.Keyring`、Windows 期望 `Windows.WinVaultKeyring`；打印出 `fail.Keyring` 即系统凭据库不可用。业务侧另可用 `cmcloud auth status --format json` 观察 `localCredentials.ticket.present`（两种后端都支持；回执里的 `backend` 字段即当前后端）。

- **Windows 超长凭据透明分片**（仅 `keychain` 后端；file 后端不涉及分片）：Windows 凭据管理器单条 `CredentialBlob` 容量有限。包装层在 Windows 上按 UTF-16 长度判断，超过安全阈值时把逻辑值编码后拆成多个 Generic Credential，原槽位仅保存带 SHA-256、长度和代次的清单；读取、更新、删除均由包装层透明还原或清理，CLI 与 SDK 无需感知。分片仍全部位于系统凭据管理器，不生成明文文件。macOS／Linux 继续单条存储，但能读取该分片格式；任一分片缺失、长度或摘要不符时拒绝返回凭据，不使用残缺值继续取票。

- **缺省与显式后端**：`storage_backend` 缺省（未登记 / `null` / 空串 / 纯空白）按 `file` 处理；只有显式 `keychain` 才走系统凭据库。非字符串（如 `false`/`0`）或枚举外取值在 profile 加载阶段被**明确拒绝**，不会当成"未配置"。当前发行配置登记为 `file`。
- **SDK 与文件指纹**：随包使用供应方原始 `IDaaS_AUT_KMS_v3.1.1`，SDK 目录不做本地修改或运行时函数替换。外层加密状态、SDK 授权文件预检、待授权 uuid 和 clawUid 读取都复用 SDK 的同一指纹实现：machine-id + `sys.platform`，machine-id 不可用时回退用户名 + `sys.platform`。本地信封仍为 v=2 / PBKDF2-SHA256（600000 次）/ AES-256-GCM；3.1.0 四参数指纹生成的密文不能用新指纹解密，升级后需重新授权一次，不自动删除旧凭据、不降级明文。
- **沙箱持久化条件**：hostname/MAC 变化不再参与文件密钥派生；machine-id、凭据目录、实例身份 clawUid 和智能体/profile 路径仍须跨重建保持一致。machine-id 变化或文件丢失时不能承诺恢复授权。Linux 读取 `/etc/machine-id` 或 `/var/lib/dbus/machine-id`，Windows 读取注册表 `MachineGuid`。供应方 3.1.1 的 macOS `IOPlatformUUID` 解析存在返回字段名而非实际 UUID 的已知问题；当前保持原包行为，外层不另行修正，以免双方密钥不一致，待供应方版本修复后统一升级。
- **系统凭据库访问范围**：`keychain` 后端与 `auth status --all true` 会在本机系统凭据库读写；`--all` 是全局诊断入口，不依赖单个 file profile（详见 [授权](auth.md)）。不能笼统承诺"file 配置下任何命令都不访问系统凭据库"。
- **调试日志**：默认关闭；仅显式 `debug.enabled=true` 且**请求目标**命中固定测试域常量（不可通过任何配置扩权）时启用，只记录脱敏后的 URL、请求头与正文长度/摘要，不落明文；目标域不匹配的请求照常执行但不记录。权限收紧失败时停止落盘、业务继续。日志结束后关闭句柄、恢复钩子并清理故障及配置缓存，下一次调用重新判断配置。
- **Windows ACL**：为保护本技能台账与日志目录，CLI 调用 `whoami` 读取当前账号 SID，并用 `icacls` 断开继承、只授权当前用户；不申请管理员提权，不修改业务目录之外的无关条目。

- **存储不可用时不降级**：`SECURE_STORE_UNAVAILABLE` 表示本机无法安全保存身份材料，CLI **拒绝任何明文落盘降级**（探测到不可用即不继续取票）；**含义与恢复动作按后端区分**：`keychain` 后端是"系统凭据库不可用"（macOS 上是"钥匙串被锁/未授权本解释器访问"，Linux 上是"没有运行中的 Secret Service 或缺少会话 D-Bus"），按上表补齐前提后重试；`file` 后端是"状态文件所在目录不可写"（磁盘满 / 只读 / 权限不足），让用户检查磁盘与目录权限。两者都属本机环境问题，**不表述为"未授权"或"需要重新登录云盘"**，也不要改用其它方式保存票据。**注意**：`keychain` 后端在探测通过之后、取票过程中才失败时，SDK 会改用 file 后端的加密信封文件继续，**不会**报 `SECURE_STORE_UNAVAILABLE`；该回退文件同样是加密的，不产生明文。

## 3. 结果信封

所有命令返回同一顶层信封，**每次返回后按 [状态处理](protocol.md#状态处理) 消费**。下表字段语义属 **CLI 侧契约**——本 Skill **只依赖、不校验**，读到与契约不符时按 [状态处理](protocol.md#状态处理) 以 `status` 为准兜底。逐字段含义：

| 字段 | 含义 |
| :-- | :-- |
| `schemaVersion` | 信封结构版本 |
| `command` | 命令 id，如 `share.save` |
| `status` | 顶层状态；判定与措辞的唯一依据 |
| `data` | 本次命令自身的输出；结构由该命令 schema 决定，不得按名称猜字段 |
| `counts` | `{requested, succeeded, failed}`；未知为 `null`。读到 `null` 时按"数量未知"措辞，**不得当作 0** |
| `page` | `null` 或分页对象，六字段见本节末；`complete=false` 表示未查全 |
| `error` | 为 `null` 或 `{"code", "message", "retryable"}`。消费：`code` 决定话术，`message` 仅供模型理解原因、**不得原样转述**；`retryable` **只用来决定是否给"重试"建议**——`false` 时不给，`true` 也**不授权自动循环**；只用于措辞、不用于选路。异步任务达到 300 秒等待上限的 `RUNNING` 信封带 `error.code=UPSTREAM_TIMEOUT`，按"本轮等待结束"处理，**不说成失败** |
| `nextAction` | **当前命令自动续办的下一步命令**，取值见 [状态处理](protocol.md#状态处理) 白名单（`auth.login`／`operation.confirm`／`operation.execute`／`operation.status`）；`auth.login` 只在 `AUTH_REQUIRED` 时下发——未授权时本轮唯一动作是**取一次授权入口**，入口由该命令回执携带，**只调一次**（`auth.login` 自身的回执、以及 `auth revoke` 的 `AUTH_REQUIRED` 均为 `null`）；`operation.status` 只在写执行超时（`RUNNING` + `error.code=UPSTREAM_TIMEOUT`）时下发，用于续查**一次**；`null` 表示当前命令无需自动续办；原任务的其他明确步骤按主文档处理（恢复入口见本文件第 4 节） |
| `traceId` | 排障标识。**不主动输出**：确认卡、结果与失败说明里都不给；仅当用户主动索要时才给出（见 [确认与安全边界](confirmation.md)） |

```json
{
  "schemaVersion": "1.0",
  "command": "share.save",
  "status": "CONFIRMATION_REQUIRED",
  "data": {
    "planRef": "plan_xxx",
    "operationRef": "op_xxx",
    "expiresAt": "2026-09-16T14:32:00+08:00"
  },
  "counts": { "requested": 12, "succeeded": null, "failed": null },
  "page": null,
  "error": null,
  "nextAction": "operation.confirm",
  "traceId": "trace_xxx"
}
```

顶层 `status` 取值见 [状态处理](protocol.md#状态处理)
成功或空结果时 `error` 为 `null`；失败时形如 `"error": {"code": "SCOPE_DENIED"}`，模型只取 `code` 换话术，**不得转述 `error` 原文**。`status` 为 `SUCCESS`/`EMPTY` 而 `error` 非空时，忽略 `error`、**以 `status` 为准**；`RUNNING` 时按来源区分——已确认待执行（`nextAction=operation.execute`）或等待中的普通信封，而**达到 300 秒等待上限的那封带 `{"code":"UPSTREAM_TIMEOUT","retryable":true}`**，按"本轮等待结束"处理，**不说成任务失败**；`REJECTED`、`FAILED` 靠 `error.code` 决定措辞。

**`page` 字段**：`null`；或六字段对象，如下：

| 字段 | 类型 | 含义 |
| :-- | :-- | :-- |
| `pageSize` | integer | 实际采用的单页数量（契约上 > 0） |
| `returned` | integer | 分页查询＝本次返回条数；内部多页汇总＝已纳入计算的源记录数。批量写的成功/失败数在 `counts` |
| `total` | integer | **仅下游提供且口径已验收**时给非负整数；否则 `null` |
| `hasMore` | boolean | `true`/`false` 表示**已确认**有无更多，`null` 表示无法判断 |
| `nextCursor` | string | CLI 生成的**不透明受控引用**，只原样回传作为同一命令的 `--cursor` |
| `complete` | boolean | 是否已完整覆盖用户要求的范围 |

> **分页终止判定（搜索类下游）**：搜索类下游**不返回"是否还有下一页"的结束标记**，终止只能由调用方推断：命中 **本页不满（上游原始条数 < `pageSize`）** 或 **上游不再下发 `nextCursor`** 之一即视为**已取尽**（`complete=true`）；只有**满页且有 `nextCursor`** 才是未取尽（`complete=false、hasMore=true`）。对应地 `hasMore` 不再取 `null`：满页且有游标 → `true`；命中任一终止信号 → `false`。判定由 CLI 完成并体现在 `page.complete`，**Skill 以 `page.complete` 为准，不用回执里的 `returned` 与 `pageSize` 自行复算**（本地过滤——专属根、类型、故事类型筛选——会让 `returned` 小于 `pageSize` 而仍有后续页）。搜索类上游不提供 `total`，**`total=null` 不构成"未查全"**，不得据此标未查全。续翻只原样回传同一命令的 `nextCursor`，不自行拼 offset。

## 4. 操作状态与异步任务

- CLI 的计划、确认凭据和状态仍以单个 `operationRef` 为边界。同一用户请求里的多个写动作由 **CLI 编排**冻结成**同一个计划**：一次调用返回一个 `operationRef`、一张卡、一次用户同意，随后一次 `operation.confirm` + 一次 `operation.execute` 按语序把全部步骤执行到底。不得由 Skill 侧把多个独立计划拼成"用户可见计划组"，也不得对同一张卡逐项索要确认。
- 编排计划按步骤顺序执行；任一步骤失败、拒绝、未知、需要重新授权或计划过期时立即停止。此前已完成的步骤不重做，尚未开始的步骤不执行；后续步骤依赖前一步产出的引用时，该绑定在计划里已声明（卡面写明"目标取自步骤 N"），执行期注入真实引用，不需要第二次确认。
- **无 `operationRef` 的命令**（只读命令，以及停留在计划前的调用）没有状态查询入口：返回终态即结束本轮；要更新结果只能**重新调用该命令**，不得当作"任务恢复"。
- **持有 `operationRef` 的操作**（写计划与异步任务）统一经 `operation.status` 查询，不设领域级 status；**写链不自动调用，由用户追问触发该入口复核**（返回含义见下表），**不重新提交**。
- `operation.status` 的 `data` 携带的是**操作状态**，须先按下表折算为顶层 `status` 再措辞：

| 操作状态 | 顶层 status | nextAction | 措辞要点 |
| :-- | :-- | :-- | :-- |
| `PENDING_CONFIRMATION` | `CONFIRMATION_REQUIRED` | `operation.confirm` | 计划已生成，等待确认 |
| `CONFIRMED` | `RUNNING` | `operation.execute` | 已确认，待执行（尚未开始） |
| `EXECUTING` | `RUNNING` | `null`（轮询预算耗尽时 `operation.status`） | 已受理，处理中 |
| `EXECUTED` | `SUCCESS` | `null` | 已完成 |
| `PARTIAL` | `PARTIAL` | `null` | 部分成功 |
| `FAILED` | `FAILED` | `null` | 失败 + 原因 |
| `REJECTED` | `REJECTED` | `null` | 被拒绝（范围／权限／能力），按 [状态处理](protocol.md#状态处理) 的错误码指引纠正条件后再发起 |

> 上表即台账枚举（`operation_ledger.OPERATION_STATUSES`）：`PENDING_CONFIRMATION`／`CONFIRMED`／`EXECUTING`／`EXECUTED`／`PARTIAL`／`FAILED`／`REJECTED`。`UNKNOWN` 只在"下游任务状态枚举未冻结、无法判定"时作为顶层 `status` 出现（`nextAction=null`，禁止自动重放）。

- 顶层 `RUNNING` 有三种来源：`CONFIRMED`（已确认待执行，`nextAction=operation.execute`）；`EXECUTING`（已受理，`nextAction=null`）；轮询预算耗尽（`error.code=UPSTREAM_TIMEOUT`，写链带 `nextAction=operation.status`、状态查询为 `null`）；**由 `nextAction` 与 `error.code` 区分**，措辞也随之不同。
- 状态机：`PENDING_CONFIRMATION → CONFIRMED → EXECUTING → EXECUTED | PARTIAL | FAILED | REJECTED`（下游状态枚举未冻结时顶层为 `UNKNOWN`，**禁止自动重放**）。`CONFIRMED` = 确认有效未开始，`EXECUTING` = 已受理且已有 `taskRef`。计划在确认前失效时不经本状态机，按 `CONFIRMATION_REQUIRED` + `error.code=PLAN_EXPIRED` 处理。
- **编排计划同样只有一条台账记录**：`operation.status` 的 `data.steps` 是该记录已登记的步骤流水（哪一步开始、哪一步失败），`data.stepResults` 是上一次执行的逐步结果（每步的 `data`/`counts`/`error`）；通常只读台账；暂停在转存步骤且结果未确认时，可按该步骤保存的任务 ID 查询原任务，**绝不重放已执行的步骤，也不执行后续写入**。原任务确认成功后，若仍有未执行步骤，明确列出并为剩余步骤重新计划。整体 `status` 按步骤推进折算：全部成功为 `SUCCESS`，中途失败且已有步骤完成为 `PARTIAL`（`counts` 给出 `completed`/`failed`）；查询异常或任务未确认时仍为 `UNKNOWN`/`RUNNING`，`failed=null`，不得计为失败，第一步就失败时原样保留该步的状态（如需要重新授权时仍为 `AUTH_REQUIRED` + `nextAction=auth.login`）。
- 非终态（`RUNNING`/`UNKNOWN`）统一走 `operation.status`；用户再问时从台账恢复，不重新创建任务；无法取得完整步骤状态时按可确认结果输出，不合并成"全部完成"。
- **轮询与超时都在 CLI 内完成**：`operation.status` **不在写链中自动调用**（唯一例外：写执行超时下发 `nextAction=operation.status` 时先续查一次），其余由用户追问时调用一次；由 CLI 内部轮询到终态、或自身超时预算耗尽后返回单个信封。Skill **不循环、不传等待时长、不自行判断超时**。返回 `RUNNING`：`nextAction=operation.execute`＝已确认待执行（继续执行链）；`nextAction=operation.status`＝本轮等待已达上限（续查一次）；为 `null`＝仍在处理（本轮结束，用户再说"好了吗"就再调一次）。返回 `UNKNOWN` = CLI 也无法判定（如状态枚举未冻结）。

错误码 → 话术、恢复与默认 `retryable` 见 [状态处理](protocol.md#状态处理)。

## 调用入口与参数


- **入口**：本文档与所有 reference 里写的 `cmcloud <domain> <action> …`，实际一律指**技能根目录下** `scripts/cm_cloud_<domain>_api.py` 的 `<action>` 动词；调用口径：
  - 形态：`python3 "<技能根目录>/scripts/cm_cloud_<domain>_api.py" <action> [--旗标 值 …]`；**Windows** 通常没有 `python3`，改用 `py -3`（或 `python`）；**macOS/Linux** 用 `python3`，不要用 `py`；
  - 控制面入口 `cm_cloud_cli_api.py` **仅接受** `capabilities`/`schema`/`help`/`orchestrate`：例如 `python3 "<技能根目录>/scripts/cm_cloud_cli_api.py" schema --command-id files.list --format json`；
  - `operation.confirm`/`operation.execute`/`operation.status` 均走独立入口 `cm_cloud_operation_api.py`，动词直接写 `confirm`/`execute`/`status`，不再加 `operation`，不得交给 `cm_cloud_cli_api.py`。`help --topic operation` 只是读取帮助，不是执行操作；
  - 位置参数只用于「主字段」：`search "<关键词>"` 等价 `search --keyword "<关键词>"`，`browse "<链接>"` 等价 `--linkUrl`，`operation status|confirm|execute <operationRef>` 等价 `--operation-ref`；**其余参数一律 `--旗标 值`**，不写位置参数；
  - 每个域脚本支持 `-h`／`--help`，等价于 `help --topic <domain>`；缺 `--format json` 时脚本自动补上；
  - 技能根目录必须由**当前实际加载的 `SKILL.md` 所在目录**推出，再拼接 `scripts/cm_cloud_<domain>_api.py`；不得假设或写死 Codex、WorkBuddy、OpenCode 等宿主的安装目录，也不得复用另一台机器的注册路径；
  - 只有在确认裸命令可用时才可继续用裸命令，否则一律用完整路径。

普通操作的完整调用示例（`<Python>`：Windows 用 `py -3`，macOS/Linux 用 `python3`；引用、摘要和令牌必须来自实际回执）：

```text
<Python> "<技能根目录>/scripts/cm_cloud_operation_api.py" confirm --operation-ref <ref> --plan-hash <hash> --confirmation CONFIRM --profile <profile> --format json
<Python> "<技能根目录>/scripts/cm_cloud_operation_api.py" execute --operation-ref <ref> --confirm-token <token> --profile <profile> --format json
<Python> "<技能根目录>/scripts/cm_cloud_operation_api.py" status --operation-ref <ref> --profile <profile> --format json
```

确认和续查时机仍按[状态处理](#状态处理)及[确认协议](confirmation.md)，上面三条不是要求连续执行的固定流程。auth revoke/logout 的计划继续走原 auth 命令。auth reauthorize 的强制授权计划虽使用台账防止重复执行，也必须用原命令携带 operationRef/planHash 确认执行或只读复查，不走 operation confirm/execute/status；其待确认及授权入口回执 nextAction 均为 null，按 auth.md 展示确认或授权入口。

- **形态**：`cmcloud <domain> <action> --参数名 值 [--profile <ref>] --format json`；下发参数时保留 `--` 前缀。

- **参数**：参数名、类型、必填、枚举与上限以 `cmcloud schema --command-id <command-id>` 为唯一真源；业务参数名必须逐字使用 Schema `properties` 中的原始大小写，只在前面添加 `--`，**不得把 camelCase 转成 kebab-case**。例如新建目录使用 `--parentRef <ref> --name <名称>`，上传使用 `--localFile <路径> --destinationRef <ref> --overwritePolicy rename`；`--parent-ref`、`--local-file`、`--destination-ref` 都会被拒绝。**该传哪些参数、值怎么取**按对应 reference 的「用户话术到结构化参数」节。不自行拼 HTTP、不另立参数名。

- **数组参数**：`array` 字段只收 **JSON 字面量**、整体作单个 argv（`--fileRefs '["<a>","<b>"]'`）；裸值、逗号拼接、重复 flag 一律被拒。

- **Windows 引号**：经 PowerShell 调入口脚本时内层双引号会被吃掉，须写 `'[\"<a>\",\"<b>\"]'`；报 `不符合 Schema 的 array` 先查引号，别当成"不支持清单"。

- **命令 id**：信封 `command`、`nextAction` 与 manifest 用点号（`share.save`）；**实际下发改为空格分隔**（`cmcloud share save`）；`schema --command-id` 例外仍传点号。

**通用命令**（`capabilities` / `help` / `schema` / `operation`）的行为与不可用排障口径见 `references/protocol.md` 第 1–2 节。


## 状态处理

每次读取JSON回执，不能只根据进程退出码判断业务成功。status决定业务状态，nextAction仅用于当前命令续办；它为null不取消用户已经明确的独立后续任务。

| 状态 | 后续动作 |
| --- | --- |
| SUCCESS / EMPTY | 呈现真实结果，继续原任务已明确的步骤；空结果不自行扩大范围 |
| AUTH_REQUIRED | 仅nextAction=auth.login时本轮取一次授权入口；auth.login不递归调用自身；退出解绑例外见auth |
| CONFIRMATION_REQUIRED | 读取confirmation.md；PLAN_EXPIRED不得继续confirm |
| RUNNING | nextAction=operation.execute且确认有效时执行原计划；nextAction=operation.status时续查一次；其他情况保留引用，不重交写请求 |
| PARTIAL | 展示逐项结果；用户要求重试时只为失败项重新计划 |
| FAILED / REJECTED | 读取errors.md解释，不自动改参数或扩大范围绕过拒绝 |
| UNKNOWN | 不断言成功/失败，不重交写请求；用户追问且该命令支持状态查询时查询原任务，否则说明无法自动核验 |

只接受auth.login、operation.confirm、operation.execute、operation.status作为命令续办值；未知值停止。用户取消和确认前置条件优先于nextAction。轮询预算由CLI管理，Agent不循环查询。错误结果不通过尝试其他业务域探路；本地读取必要参考文档不受此限。

退出码辅助分类：0成功或空；2参数问题；3需要授权/确认；4权限/能力拒绝；5失败/部分失败；6未知/等待超时；7存储或运行设施不可用。仍须以回执具体状态判断。
