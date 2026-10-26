# share 分享、订阅与转存

> 领域说明：仅在 `SKILL.md` 已把动作路由到 `share` 后加载。执行前先取 `cmcloud help --topic share --format json` 与 `cmcloud schema --command-id <command-id> --format json`。**参数类型、必填、枚举全集、上限与成功判定一律以 CLI schema 为准**；本文件只给命令索引、参数名索引和编排规则，不内联 schema、下游字段名或成功码。

## 何时触发

> 以下为**触发线索**；线索需能绑定明确对象（真实文件/目录、外链引用或链接、上一轮 CLI 回执的候选；不唯一先消歧）。

- **创建公开分享 `cmcloud share create`**：生成分享链接、公开分享、发个链接、分享这个文件/报告/照片（宾语＝文件/目录）。
- **查询订阅外链分享记录 `cmcloud share list --listKind sent`**：用户要查该类记录且提供关键词时使用；不用于回答“我发出的全部公开分享”。
- **查询订阅外链 `cmcloud share list --listKind subscriptions`**：我订阅的、关注的分享、订阅链接（宾语＝关键词/类型/时间）。
- **浏览外链 `cmcloud share browse`**：这个链接里有什么、外链目录、分享目录、提取码（宾语＝外链 + 目录）。
- **订阅外链 `cmcloud share subscribe`**：订阅这个链接、关注这个分享、订阅外链（宾语＝链接 + 提取码）。
- **查询待转存更新 `cmcloud share updates`**：有更新吗、待转存多少、外链更新（宾语＝已订阅外链）。
- **转存订阅更新 `cmcloud share save --saveKind subscription-updates`**：转存更新、把订阅的更新都存下来、"有更新的都存"（宾语＝**已订阅外链的更新资产**）。转存对象是订阅侧的整批更新项（来源见 `cmcloud share updates`）；**转存落点只能是智能体专属目录及其子目录，用户未说明落点时默认专属目录**。
- **转存指定文件/目录 `cmcloud share save --saveKind selected-resources`**：转存到云盘+（位置）、保存到 AI 空间、把外链里的某几个文件/目录存下来（宾语＝**用户挑中的项** + 目标目录）。转存对象是浏览外链时逐个选中的文件/目录（引用由 `cmcloud share browse` 签发，必须同属一条外链）；**转存落点只能是智能体专属目录及其子目录，用户未说明落点时默认专属目录**。
- **查询转存任务 `cmcloud operation status`**：转存完了吗、成功几个、转存进度、保存到哪（宾语＝上一轮操作引用）。

## 何时不触发

- `转存了什么` → `cmcloud activity list` 转存动态，不走写命令
- `我发过哪些分享`、`我发出的链接`、`分享历史` → 当前查询不能覆盖本人发出的全部公开分享；先说明范围限制。用户接受仅查订阅外链分享记录后，再请其提供关键词
- `微信/短信/QQ` 发送 → 不支持，不调用
- 本地文件夹查询 → `cmcloud files list`
- 相册/故事 → `album`；整理/成册 → 本期不支持，不调用
- 转存落点在专属目录外 → 不生成计划、不执行，先提示「只能操作 <专属目录实际路径> 内的文件」；未经同意不自动改目标；
- 后台自动化转存（"外链更新后自动转存/无人值守/以后自动保存"）→ 本期不支持，不调用
- 只想按时间问本人发出的分享（"昨天发出的分享""最近分享过什么"）→ 当前分支不支持时间筛选，也不覆盖全部已发公开分享；不得改成关键词查询后声称满足原请求
- 单独说"分享一下/订阅一下"但未指明对象 → 先追问对象，不调用写命令

## 用户话术到结构化参数

> 本节说明各场景下**每个参数怎么选**；**参数类型、必填、上限、默认值与枚举全集以 `cmcloud schema` 为准**。
> 本领域命令：`cmcloud share create`、`cmcloud share subscribe`、`cmcloud share updates`、`cmcloud share list`、`cmcloud share browse`、`cmcloud share save`；`cmcloud share list` 由 `--listKind` 路由（`sent`/`subscriptions`）、`cmcloud share save` 由 `--saveKind` 路由（`subscription-updates`/`selected-resources`）；任务查询用 `cmcloud operation status`；参数与枚举由 `cmcloud help --topic share`、`cmcloud schema --command-id <command-id>` 下发。

**创建公开分享 `cmcloud share create`**

- `resources`：宾语＝用户选定的真实文件/目录（**必填**）；只能来自可信回执或当前附件，不得编造。（数组写法见 [调用入口与参数](protocol.md#调用入口与参数)）
- `encrypt`：用户是否要提取码 →"要提取码/加个密码"＝需要，否则按 schema 默认。
- `validity`：用户说出的有效期（"一天/七天/长期"等）；未指定时用服务端默认（取值见 schema）。
- 分享范围固定为公开；"发给某个人/发送给他人"**本期不支持**，直接说明，不转其他写命令、也不得改用公开分享冒充。

**查询订阅外链分享记录 `cmcloud share list --listKind sent`**

- `--listKind`：固定 `sent`。
- `keyword`：**必填**；只使用用户给出的检索词。用户要查全部已发公开分享时，先说明此命令的覆盖范围；不能仅追问关键词后把局部结果当成完整结果。
- `searchScope`：只能从枚举中选择 —— `records`（查分享记录）或 `files`（查分享里的文件）。
- `pageSize`/`cursor`：建议显式传 `pageSize=10`；省略时 CLI 默认 50。下一页使用上一轮回执的游标。
- 匹配口径：`records` 按分享标题匹配、`files` 按分享内文件/目录名匹配；`linkId`／`shareCode`／链接片段不参与匹配，拿链接当关键词必然查不到。
- 覆盖范围固定为平台「订阅外链分享记录」分区；即使返回空结果，也只能说该分区未找到匹配记录，不能说“你没有分享过”。
- 回执条目中的 `linkRef` 由平台记录的 `shareId`／分享内文件的 `shareCode` 签发，可交给 `cmcloud share browse` 使用。

**查询订阅外链 `cmcloud share list --listKind subscriptions`**

- `--listKind`：固定 `subscriptions`。
- `keyword`：用户给出的检索词（可选）。
- `typeList`：类型词 → 图片/视频/音频/文档/压缩包（取值见 schema）。
- `from`/`to`：时间词规范化为半开区间；成对使用。
- `timeField`：时间字段（枚举 `shareTime`／`subscribeTime`）；用户明确"分享时间" → `shareTime`、"订阅时间" → `subscribeTime`。
- `pageSize`/`cursor`：同 `sent`。
- 回执条目中的 `linkRef` 由平台记录的 `shareId` 签发，可直接交给 `cmcloud share updates`／`cmcloud share browse`／`cmcloud share save`，不需要用户另找链接。

**浏览外链 `cmcloud share browse`**

- `linkRef` 或 `linkUrl`：**二选一必填**；宾语＝用户提供的链接或已定位的外链引用。用户直接给出链接时用 `linkUrl`（CLI 校验域名并提取链接标识），已定位的受控引用用 `linkRef`；由 CLI 解析为真实外链，Skill 不自行拼 ID。**无链接且无可信候选时先追问，不得省略或以占位引用顶替**；若上下文存在已订阅外链，可先经 `cmcloud share list --listKind subscriptions` 定位，不猜。
- `passwd`：用户给出的提取码，非空且最多32字符；未给出则不传。
- `parentCatalogRef`："进入某个子目录" → 用上一轮回执中该目录的真实引用；根目录不传。CLI从引用中取接口返回的完整路径传入pCaID，目录ID仍用于转存；旧引用缺少路径时重新浏览获取新引用。
- `begin`/`end`/`sort`：用户明确翻页范围或排序时才传（取值见 schema）。
- 链接路由支持 `/w/i/`、群组 `/g/i/` 和 `/s/`；放行群组路由仅表示可调用详情接口，实际能否列出文件以接口回执为准，不承诺群组一定返回明细。
- `sort.by` 支持 `time`（修改时间）、`name`、`size`、`type`；`sort.order` 支持 `asc`/`desc`，省略时按升序。名称排序映射 `caSrt=1,coSrt=1`；修改时间映射 `0,0`；文件大小映射 `0,3`；文件类型映射 `0,2`；方向映射 `srtDr=0/1`。大小/类型排序时目录按修改时间排序，目录与文件仍分组展示，不承诺混合排序。

**订阅外链 `cmcloud share subscribe`**

- `linkUrl` 或 `linkRef`：**二选一必填**；用户给出链接用 `linkUrl`，已定位引用用 `linkRef`。
- `passwd`：提取码**必填且非空**（当前契约要求 ≤10 字符）；用户没给就先追问，不能猜，也不能按"可选"直接调用。

**查询待转存更新 `cmcloud share updates`**

- `linkRef`：宾语＝已订阅外链（**必填**）；未提供时先经 `cmcloud share list --listKind subscriptions` 定位，不猜。
- 本命令**不接受 `includeDetails`**：接口仅接收 `linkId`；实际回执含资产摘要时按已有字段返回，不因参数虚构。

**转存订阅更新 `cmcloud share save --saveKind subscription-updates`**

- `--saveKind`：固定 `subscription-updates`。
- `linkRef`：宾语＝订阅外链（**必填**）。
- `selectedResources`：待转存的更新资产（**必填、非空**）；须逐项属于该订阅。（数组写法见 [调用入口与参数](protocol.md#调用入口与参数)）
- `destinationRef`：转存落点，**可选**；省略时 CLI 自动使用智能体专属目录。显式目标只能位于智能体专属目录及其子目录内，超出范围时不生成计划，并返回允许写入的专属目录实际路径。
- **不提供 `copyPolicy`**：请求 DTO 无同名冲突字段；计划须展示平台实际采用的冲突行为，未确认前不承诺覆盖/跳过/改名。

**转存指定文件/目录 `cmcloud share save --saveKind selected-resources`**

- `--saveKind`：固定 `selected-resources`。
- `linkRef`：宾语＝目标外链（**必填**）。
- `contentRefs`/`catalogRefs`：用户选定的文件或目录（**至少一项非空**）；须全部属于同一外链。（数组写法见 [调用入口与参数](protocol.md#调用入口与参数)）
- `destinationRef`：转存落点，**可选**；省略时 CLI 自动使用智能体专属目录。显式目标只能位于智能体专属目录及其子目录内，超出范围时不生成计划；**同样不提供 `copyPolicy`**。

**查询转存任务 `cmcloud operation status`**

- `operation-ref`：上一轮 `cmcloud share save` 回执中的操作引用（**必填**），与当前身份绑定。
- **不接受 `waitMode`**：任务仍在执行时固定每 5 秒查询一次、最多等待 300 秒，不接受调用方覆盖轮询方式、间隔或等待上限。
- 无 `operation-ref` 时先查本地台账或追问，**不得猜测任务引用、不得重新提交相同转存**。

**域内同词读写**：`我订阅的外链有哪些` = 读（`cmcloud share list --listKind subscriptions`）；`订阅这个链接` = 写（`cmcloud share subscribe`）。前者不得调用写命令，后者不得只查询。

## 读流程

`cmcloud share list`/`cmcloud share browse`/`cmcloud share updates` 直接执行。

- **分享记录与订阅外链（`cmcloud share list`）**：按 `--listKind` 选定唯一分支后执行；两类覆盖范围不同，需分别保存各自游标并按真实标识去重；取尽判定同搜索——本页不满或无 `nextCursor` 即取尽（判定由 CLI 完成并体现在 `page.complete`，口径见 [协议](protocol.md)），`page.complete=false` 时 `hasMore=true`，**不把只覆盖部分来源的结果扩称"全部外链"**。`sent` 结果须按回执的 `coverage` 说明范围，空结果仅代表该分区未命中；未取尽时标明 `page.complete=false`。
- **外链浏览（`cmcloud share browse`）**：只使用本次真实返回的目录引用进入子目录，不拼 ID；分页范围与总数不明确时标"未查全"。
- **待转存更新（`cmcloud share updates`）**：区分"更新数"与"待转存数"，**不自动执行转存**；口径无法区分时相关数量记为"未知"，**不填 0**。

## 写流程与确认点

统一走计划 → 确认 → 执行。
> 以下为业务语义步骤；**Skill 只调用一次对应的 `cmcloud share <action>` 命令**，其余环节（解析外链引用、校验目标范围、批量任务分片、任务轮询）都在 CLI 内部完成，不由 Skill 分步调用。

1. **创建公开分享**：确认资源与公开范围 → 计划（提取码、有效期）→ 确认 → 创建 → 报告真实链接。
2. **订阅外链**：解析链接为真实外链 → 确认卡**必须写明"订阅不会自动转存"** → 确认 → 订阅。
3. **转存（两类 `saveKind`）**：解析外链与选中项 → 校验落点位于智能体专属目录及其子目录内（未说明时默认专属目录）→ 确认（来源外链、选中项数、落点、冲突策略）→ 提交 → 返回终态即报告；非终态（`RUNNING`/`UNKNOWN`）**结束本轮**，恢复入口见 `references/protocol.md` 第 4 节。
4. **任务查询**：仅在用户追问时按真实 `operationRef` 经 `operation.status` 复核（见 `references/protocol.md` 第 4 节）；**禁止重新创建相同转存**。

确认卡结构与必展项见 [确认与安全边界](confirmation.md)。本领域追加：`cmcloud share create` 公开范围；`cmcloud share subscribe` **必须写明"订阅不会自动转存"**；`cmcloud share save`（两类）来源外链、选中的项数与**转存落点（默认专属目录，并写出其实际路径）**。

**可编排链**（多个写动作合成一次确认，见 [确认与组合任务](confirmation.md)）：
- 订阅 + 转存该订阅的现有更新：一次编排、一张卡；卡面按用户语序列出"步骤 1：订阅外链 X""步骤 2：转存该外链当前可转存的更新"，**不得**再说成"订阅会自动转存"——`subscription-updates` 只转当时已存在的更新，没有更新时该步骤如实报"无更新可转存"。
- 订阅/转存 + 建册加图：先转存、再把转存出的图片放进新相册时，目标引用取自前一步产出，卡面写明"目标取自步骤 N"。
  多文件使用 `--fileRefs '["?step1.fileRefs"]'` 显式数组绑定；不要用要求唯一产物的 `?step1.resultRef`。仅有明确成功的文件转存可继续，转存目录、未知结果及非图片不继续建册。

## 领域异常和恢复

| 异常 | 处理 |
| :-- | :-- |
| 订阅成功但没有自动转存 | 分两步反馈；需用户另行确认转存 |
| 转存任务超时或结果未知 | 记 `RUNNING`/`UNKNOWN`，按 `references/protocol.md` 第 4 节 复核（用户追问时经 `operation.status`）；仍不可判定记 `UNKNOWN`，**禁止重建相同任务** |
| 已提交后状态查询异常 | `UNKNOWN` + `TASK_STATUS_QUERY_FAILED`：任务已提交，结果尚未确认；保留原任务供后续查询，成功/失败数量未知，不得说“一个文件也没存”或重新提交转存。`retryable=true` 仅针对查询原任务 |
| 缺失外链引用 | 先定位已订阅外链或让用户提供链接，不猜测 |
| 提取码缺失或为空 | 追问提取码；**不得按"可选"直接调用订阅** |
| 更新数与待转存数无法区分 | 数量记为"未知"，**不填 0**，并说明口径限制 |
| 目标越出允许范围 | 执行前拒绝，不靠路径前缀绕过 |
| 平台返回资源路径未在授权范围内（码 16110013） | CLI 已自动重查当前可操作专属目录，回执 `data.dedicatedRoot` 给出 `rootRef`/`namePath`；把当前可操作目录告知用户，**用户确认后重新发起**，重新发起的请求将使用该当前目录完成（转存落点仍只能是智能体专属目录及其子目录）；**不自动更改目标、不重试** |
| 转存部分成功 | `PARTIAL`；逐项给出成功/失败，不把受理数当成功数 |
| 逐项结果字段语义 | 契约中 `reason` 是失败原因、成功时为空；现网成功转存仍返回 `reason=0000`，因此保留原有 `0/0000` 成功兼容。其他非空 `reason` 均算**失败**，即使有 `rstId`；目标引用保留，但不证明操作完整成功。`reason` 为空且 `rstId` 非空算**成功**；两者都为空算**未知**。成功码无目标引用时只报成功，不编造落点，也不能继续数组绑定。计数是接口返回的请求项数：目录项失败不能扩写成内部所有文件失败，也不能说没有内容落地；不自动重交转存 |
| 查不到 | 说明未找到；**经用户同意后**才扩大时间/类型/关键词 |

## 输出追加项

> 通用骨架见 [结果展示](output.md)；本节只列 share 域必须额外出现的信息。

- **链接生成**：给出链接与有效期；**不得说"已发送"**。
- **订阅**：**必须写明"订阅不会自动转存"**，并给出后续入口（"需要时说'转存更新'"）。
- **待转存更新**：给数量与是否查全；口径不可区分时写"待转存数量未知"，**不填 0**。
- **转存**：受理中写"完成前不代表已生成"；完成后逐项给成功/失败与原因，并写明落点。
- **浏览外链**：汇总句给条目数；明细逐行 `- <名称>（<类型>，<大小>）`。
- **分享记录**：说明只查到订阅外链分享记录；不称“我发出的全部分享”，空结果不推断没有公开分享。
