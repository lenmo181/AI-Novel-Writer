# album 相簿与回忆故事

> 领域说明：仅在 `SKILL.md` 已把动作路由到 `album` 后加载。执行前先取 `cmcloud help --topic album --format json` 与 `cmcloud schema --command-id <command-id> --format json`。**参数类型、必填、枚举全集、上限与成功判定一律以 CLI schema 为准**；本文件只给命令索引、参数名索引和编排规则，不内联 schema、下游字段名或成功码。

## 何时触发

> 以下为**触发线索**；线索需能绑定明确对象（相簿/故事，或用户指代、上一轮 CLI 回执的候选）。

- **搜索相簿/故事 `cmcloud album search`**：相册、相簿、影集、照片集、回忆故事、故事 + 找/搜/有哪些（宾语＝关键词 + 对象类型）。
- **列表 `cmcloud album list`**：相册里有什么、有哪些相簿、有哪些故事（宾语＝对象类型）。
- **图片明细 `cmcloud album files`**：相簿里的照片、故事里有哪些图（宾语＝某个相簿/故事）。
- **新建相簿 `cmcloud album create --kind album`**：新建相册、建个旅行相册、创建影集（宾语＝名称）。
- **建册并加图 `cmcloud album create --kind album --fileRefs <JSON数组>`**：把这些照片建成相册、建册并放入这些图（宾语＝名称 + 图片）。
- **向已有相簿加图 `cmcloud album add`**：加到旅行相册、把这些图片放进相簿（宾语＝相簿 + 图片）。
- **新建故事 `cmcloud album create --kind story`**：创建回忆故事、做个故事、生成一个故事（宾语＝标题/副标题/封面/图片）。
- **修改相簿/故事 `cmcloud album update`**：相册改名、换封面、故事改标题、改副标题（宾语＝对象 + 变更项）。
- **生成分享链接 `cmcloud album share --deliveryMode link-only`**：相册分享链接、把故事分享出去、生成影集链接（宾语＝对象）。

`相册/相簿/影集` 统一为 album；`回忆/故事` 结合"故事、标题、封面"等对象词判定，避免把"生成回忆"误判为已有故事查询。

## 何时不触发

- `整理成相册`、`按月份自动成册`、`自动从照片生成故事` → 本期不支持，不调用
- `外链里有什么` → `cmcloud share browse`
- `设置播放顺序`、故事播放顺序 → 本期不支持，不调用
- 删除/移动相簿或故事 → 本期不做；改名走 album update
- `把相册分享给张三`/`发给别人`（生成链接后发送给某位接收人）→ **本期不支持**，说明后不调用；相册/故事只保留链接形式
- 需要先定位对象但无唯一候选 → 先消歧，不猜相簿/故事 ID

## 用户话术到结构化参数

> 本节说明各场景下**每个参数怎么选**；**参数类型、必填、上限、默认值与枚举全集以 `cmcloud schema` 为准**。
> 本领域命令：`cmcloud album search`、`cmcloud album list`、`cmcloud album files`、`cmcloud album create`、`cmcloud album add`、`cmcloud album update`、`cmcloud album share`；路由枚举 `--kind`＝`album`/`story`、`--deliveryMode`＝`link-only`（单值）；参数与枚举由 `cmcloud help --topic album`、`cmcloud schema --command-id <command-id>` 下发。

**搜索相簿/故事 `cmcloud album search`**

- `keyword`：单个搜索关键词（**必填、非空**）；不传多关键词数组。
- `--kind`：`album`（相簿）/ `story`（故事）。
- `pageSize`/`cursor`：`pageSize` 默认 10（接口另有默认时以其为准）；`cursor` 在用户说"继续/下一页"时取上一轮回执的游标。
  搜索无上游结束标记：本页不满或无游标即取尽、满页且有游标才未取尽；判定由 CLI 完成并体现在 `page.complete`（口径见 [协议](protocol.md)）。
- **本命令无时间筛选与排序入参**：只按 `keyword` + `--kind` 检索，排序固定为平台默认 `updatedAt`。用户诉求是**按创建时间**（"最近创建的相册""上个月建的影集"）时，**不得**用 `cmcloud album search` 编造关键词绕过：改用 `cmcloud album list --kind album` 列出后按回执中的 `createdAt` 说明排序限制；`createdAt` 缺失或 `page.complete=false` 时如实说明，**不得声称已查全**。
- **搜索返回的名称字段唯一为 `data.items[].storyName`**：Agent 展示相簿/故事搜索结果时只能读取 `storyName`，不得读取或回退到 `name`、`albumName`；搜索回执不再同时返回对象名称字段 `name`。本条仅约束搜索结果，不改变其他命令的字段约定。故事搜索仅使用搜索接口返回的故事记录，不额外查询故事列表核验主键或替换名称；搜索索引是否同步由服务端负责。

**列表 `cmcloud album list`**

- `--kind`：`album` / `story`，决定对象类型。
- `pageSize`/`cursor`：同搜索。
- 只列相簿/故事本身，不返回其中的图片。

**图片明细 `cmcloud album files`**

- `--kind`：`album` / `story`，决定对象类型。
- `albumRef`：宾语＝用户指代的相簿/故事（真实对象引用）；**必填**，不得编造 ID。
- `pageSize`/`cursor`：同搜索；按该相簿/故事分页。
- 相簿图片接口所需的 `x-yun-client-info` 由 CLI 在调用时从当前设备实时采集并注入，字段按服务端规范为**网络类型｜用户终端IP｜客户端类型｜客户端当前版本｜设备品牌｜设备型号｜设备标识｜MAC地址｜操作系统和版本｜分辨率｜客户端语言类型｜设备经度｜设备纬度｜**（字段缺失置空、分隔符必带；网络类型为枚举编码 1 WiFi／2 2G／3 3G／4 4G／5 有线网络／6 5G，按默认路由接口判定；序列号缺失时以设备指纹 UUID 兜底，经度/纬度留空）。现网抓包值在 13 段后另有 4 段空字段（合计 17 段），CLI 按同一形状输出；2026-09-18 实测 14 段旧形状被拒为 `08000101`，空相簿不校验该头。发布环境可用 `CMCLOUD_CLIENT_INFO` 显式覆盖，Agent 不得提交该请求头。

**新建相簿 `cmcloud album create --kind album`**

- `--kind`：固定 `album`。
- `name`：相簿名（**必填、非空**；长度与字符规则见 schema）。
- 本场景**不接收** `fileRefs`；建册同时加图见下一条。

**建册并加图 `cmcloud album create --kind album --fileRefs <JSON数组>`**

- `--kind`：固定 `album`。
- `name`：新相簿名（**必填、非空**）。
- `fileRefs`：创建后立即加入的真实图片引用（**必填、非空**）；可直接使用文件搜索/目录列表返回的图片 `fileRef`，或相簿图片明细返回的图片引用。（数组写法见 [调用入口与参数](protocol.md#调用入口与参数)）
- 两步**非原子**：建册成功而加图失败记 `PARTIAL`，保留相簿 ID 供恢复。

**向已有相簿加图 `cmcloud album add`**

- `albumRef`：宾语＝用户指定的相簿（**必填**，真实引用）。
- `fileRefs`：待加入的图片（**必填、非空**，真实引用）；可直接承接文件搜索/目录列表返回的图片 `fileRef`。
- **不接收 `duplicatePolicy`**：接口无重复处理字段，不承诺跳过/报错/覆盖语义；不得按输入数组长度虚报成功数。

**新建故事 `cmcloud album create --kind story`** 

- `--kind`：固定 `story`。
- `name`：故事标题（**必填、非空**）；`secondTitle`：副标题（可选；**未提供时 CLI 默认与标题一致**，用户主动给出才单独传）。
- `coverRef`：封面图片（**必填**，真实引用）。
- `fileRefs`：故事包含的图片集合（**必填、非空**，真实引用）。

**修改相簿 `cmcloud album update --kind album`**

- `--kind`：固定 `album`。
- `albumRef`：宾语＝待修改相簿（**必填**，真实引用）。
- `name` / `coverRef`：**至少一项**；提供的值必须为非空字符串；未传的字段**保持原值**（该命令不支持显式清空，用户要求清空时如实告知）。

**修改故事 `cmcloud album update --kind story`** 

- `--kind`：固定 `story`。
- `storyRef`：宾语＝待修改故事（**必填**，真实引用）。
- `name` / `secondTitle` / `coverRef`：**至少一项**；提供的值必须为非空字符串；未传的字段**保持原值**（该命令不支持显式清空，用户要求清空时如实告知）。
- 生成计划前以及确认后的写入前都核对故事仍在当前列表中；已不存在返回 `NOT_FOUND`，不继续生成或执行更新计划。搜索索引更新仍由服务端负责。

**生成分享链接 `cmcloud album share --deliveryMode link-only`** 

- `--deliveryMode`：固定 `link-only`。
- `objectRef` / `--kind`：宾语＝相簿或故事（真实引用），`objectRef` **必填**；`--kind` 取 `album`/`story`。
- 可选字段与 `getAlbumShareInfo` 契约同名同义：`mood`（分享心情）、`needWatermark`（`0`/`1`，传 `1` 时必须同时提供 `seqNo` 水印序列号）、`shareTimeType`（`1` 一天 / `2` 七天 / `3` 永久）、`visitUnlimited`（`0`/`1`，申请访问次数不受限）；未指定时用服务端默认。

## 读流程

- **搜索（`cmcloud album search`）**：单个非空关键词 + `--kind`；排序固定为平台默认 `updatedAt`，**不假定排序方向**；取尽判定由 CLI 完成并体现在 `page.complete`：本页不满或无游标即取尽（`complete=true`），只有满页且仍有游标时才 `complete=false`。
- **列表（`cmcloud album list`）**：用 `--kind` 选对象类型；返回相簿/故事本身的对象 ID 与分页信息。
- **图片明细（`cmcloud album files`）**：用 `--kind` 选对象类型，**必须有真实 `albumRef`**；返回该相簿/故事内的图片 ID 与分页信息。
- 缺少 `total` 不表示未查全（搜索接口本就不返回总数）；仅 `page.complete=false`（满页且有游标）时标注"未查全"，此时不给确定总量。

## 写流程与确认点

统一走计划 → 确认 → 执行（确认卡见 [确认与安全边界](confirmation.md)）。
> 以下为业务语义步骤；**Skill 按场景调用对应命令**，其余环节（解析 ID、构造 DTO、两步编排）都在 CLI 内部完成。

1. **新建相簿 / 新建故事**：计划只含创建副作用 → 确认 → 创建 → 以真实回执取相簿/故事 ID。
2. **建册并加图**：两步独立回执（创建 → 加图）；建册成功、加图失败记 `PARTIAL` 并保留相簿 ID，只重试加图。
3. **向已有相簿加图**：校验真实相簿与图片引用，按实际回执或明细复核成功数。
4. **修改**：至少一项变更；**"未提供"与"显式清空"要区分**——该命令不支持显式清空，未提供的字段保持原值。
5. **分享**：只生成分享链接，**不发送**；链接由用户自行转发。

确认卡追加项（结构与必展项见 [确认与安全边界](confirmation.md)）：相簿名/故事标题、副标题（未提供时默认同标题）、图片数、封面；多步流程按顺序列出。

**可编排链**（多个写动作合成一次确认，见 [确认与组合任务](confirmation.md)）：
- 新建相簿并加图：既可用单命令一次带图建册（`fileRefs` 非空，一次计划），也可编排成"步骤 1：新建相簿 C""步骤 2：把图片 D 加入步骤 1 新建的相簿"；两条路径都是一次确认，按用户语序与既有候选选择。
- 建册后分享：编排成"步骤 1：新建相簿 C""步骤 2：为步骤 1 新建的相簿生成分享链接"，卡面写明目标取自步骤 1。
- 上传本地图片后建册：编排成"步骤 1：把图片上传到云盘""步骤 2：新建相簿并加入步骤 1 上传的图片"。

## 领域异常和恢复

| 异常 | 处理 |
| :-- | :-- |
| 建册成功、加图失败 | 保留真实相簿 ID，记 `PARTIAL`，可只重试加图步骤 |
| 未提供字段被误当清空 | 只有显式清空动作才发送清空语义 |
| 目标相簿/故事不唯一 | 先消歧，再生成计划 |
| 满页且仍有游标（`complete=false`） | 说明可能未查全、不假定倒序；本页未满或无游标即为取尽，不得标"未查全" |
| 查不到 | 说明未找到；**经用户同意后**才扩大关键词/类型 |

## 输出追加项

> 通用骨架见 [结果展示](output.md)；本节只列 album 域必须额外出现的信息。

- **搜索 / 列表**：汇总句给数量；明细逐行 `- <名称>（<图片数> 张，更新于 <时间>）`；`createdAt` 缺失或未查全时如实说明，**不得声称"最近创建的"已查全**。
- **图片明细**：明细逐行 `- <名称>（<类型>，<大小>）`。
- **建册并加图**：结果句给相簿名与加图成功数；失败项给原因。
- **生成链接**：只给链接；**不得说"已分享给张三"**。
