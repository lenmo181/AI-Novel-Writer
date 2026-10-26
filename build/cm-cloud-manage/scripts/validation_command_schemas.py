"""S01～S36（S17 已移除）触发入参 Schema 的唯一代码副本。

事实来源与优先级：
1. 《CLI 场景清单与流程 V1.8》：场景范围 S01～S35，S17 已移除。
2. 《CLI 场景触发入参 Schema V1.8》：Agent 可提交字段、类型、必填、互斥与条件关系。
3. 共享主命令（share.save / share.list / album.create / album.update）通过判别字段
   （saveKind / listKind / kind / fileRefs 存在性）确保恰好命中一个场景；命令级 Schema 为
   各场景 Schema 的 oneOf，选择由注册表统一判定，不依赖注册顺序。
4. adapter 的 `call()` 签名与真实实现以 `scripts/adapter_*.py` 为准。

全部 Schema 保持 additionalProperties=false；未在 Schema 中出现的字段一律拒绝，绝不透传
给 adapter。Agent 不得提交 userId/userDomainId/appId/skillId/clawUid/openclawId/taskId/
requestHeaders/票据/专属目录根 ID/可信智能体名称/planHash 等字段。
"""

from cmcloud_core import file_types
from cmcloud_validation import file_name_rules

SCHEMA_ID_SUFFIX = "_TRIGGER_INPUT_V2_0"

REF_KINDS = (
    "file",
    "folder",
    "resource",
    "link",
    "content",
    "catalog",
    "album",
    "story",
    "object",
    "image",
    "dynamic",
    "operation",
)

REF_ITEM_ERROR_POLICY_KEY = "x-ref-item-error-policy"
REF_ALLOWED_KINDS_KEY = "x-ref-allowed-kinds"
REF_ITEM_ERROR_ISOLATE = "isolate"

FORBIDDEN_CLIENT_FIELDS = (
    "userId",
    "userDomainId",
    "belongUserId",
    "createUserId",
    "updateUserId",
    "appId",
    "appSecret",
    "skillId",
    "clawUid",
    "openclawId",
    "channelId",
    "sourceChannel",
    "sessionId",
    "messageId",
    "taskId",
    "taskRef",
    "requestHeaders",
    "headers",
    "ticket",
    "token",
    "dedicatedRootId",
    "planHash",
    "confirmed",
    "force",
    "waitMode",
    "nextPageCursor",
    "pageAfter",
    "startNum",
    "stopNum",
    "bNum",
    "eNum",
)

# 共享主命令的判别字段取值（V1.8）：Agent 必填，注册表据此选择唯一场景。
SAVE_KIND_SUBSCRIPTION_UPDATES = "subscription-updates"
SAVE_KIND_SELECTED_RESOURCES = "selected-resources"
LIST_KIND_SENT = "sent"
LIST_KIND_SUBSCRIPTIONS = "subscriptions"
EVENT_KINDS_BROWSE = "browse"
EVENT_KINDS_UPLOAD = "upload"
EVENT_KINDS_SHARE_SAVE = "share-save"
EVENT_KINDS_GROUP_SAVE = "group-save"
EVENT_KINDS_DISCOVER_SAVE = "discover-save"
EVENT_KINDS_ALL_SAVE = "all-save"
ACTIVITY_EVENT_KINDS = (
    EVENT_KINDS_BROWSE,
    EVENT_KINDS_UPLOAD,
    EVENT_KINDS_SHARE_SAVE,
    EVENT_KINDS_GROUP_SAVE,
    EVENT_KINDS_DISCOVER_SAVE,
    EVENT_KINDS_ALL_SAVE,
)
ALBUM_KIND = "album"
STORY_KIND = "story"
DELIVERY_MODE_LINK_ONLY = "link-only"

_TIME_FIELDS_FILE = ("createdAt", "updatedAt", "takenAt")
_TIME_FIELDS_SHARE = ("shareTime", "subscribeTime")
_SEARCH_FILE_TYPES = ("0", "1", "2", "3", "4", "5", "6", "100")
_VALIDITY_UNITS = ("day", "month", "year")


def _ref(kind, max_length=2048):
    return {
        "type": "string",
        "format": "cmcloud-ref",
        "minLength": 1,
        "maxLength": max_length,
        "x-ref-kind": kind,
    }


def _ref_array(
    kind,
    min_items=1,
    max_items=200,
    *,
    item_error_policy=None,
    allowed_kinds=None,
):
    schema = {"type": "array", "items": _ref(kind), "minItems": min_items}
    if max_items is not None:
        schema["maxItems"] = max_items
    if item_error_policy is not None:
        schema[REF_ITEM_ERROR_POLICY_KEY] = item_error_policy
    if allowed_kinds is not None:
        schema[REF_ALLOWED_KINDS_KEY] = list(allowed_kinds)
    return schema


def _cursor():
    return {"type": "string", "format": "cmcloud-cursor", "minLength": 1, "maxLength": 4096}


def _page_size(minimum=1, maximum=100):
    return {"type": "integer", "minimum": minimum, "maximum": maximum}


def _date_time():
    return {"type": "string", "format": "date-time", "minLength": 20, "maxLength": 40}


def _validity(units=None):
    allowed_units = tuple(units or _VALIDITY_UNITS)
    return {
        "type": "object",
        "properties": {
            "period": {"type": "integer", "minimum": 1, "maximum": 3650},
            "periodUnit": {"type": "string", "enum": list(allowed_units)},
        },
        "required": ["period", "periodUnit"],
        "additionalProperties": False,
    }


def _schema(properties, required=None, **extra):
    schema = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    schema.update(extra)
    return schema


def _activity_query_schema(event_kinds, extra_properties=None, with_page=True):
    """动态查询公共入参；`eventKinds` 是共享主命令 `activity.list` 的必填判别字段。"""
    properties = {
        "eventKinds": {"const": event_kinds},
        "from": _date_time(),
        "to": _date_time(),
        "keyword": {"type": "string", "minLength": 1, "maxLength": 100},
        "contentType": {"type": "integer", "enum": [1, 2, 3, 4]},
    }
    if with_page:
        properties["pageSize"] = _page_size()
        properties["cursor"] = _cursor()
    if extra_properties:
        properties.update(extra_properties)
    return _schema(properties, required=["eventKinds"])


# ---------------------------------------------------------------- S01～S15

_FILES_UPLOAD = _schema(
    {
        "localFile": {"type": "string", "minLength": 1, "maxLength": 4096},
        "destinationRef": _ref("folder"),
        "overwritePolicy": {"type": "string", "enum": ["rename", "reject", "dedupe"]},
    },
    required=["localFile", "destinationRef"],
)

_SEARCH_TIME_RANGE = {
    "type": "object",
    "properties": {
        "filed": {"type": "string", "enum": list(_TIME_FIELDS_FILE)},
        "startTime": {"type": "string", "pattern": r"^[0-9]{14}$"},
        "endTime": {"type": "string", "pattern": r"^[0-9]{14}$"},
    },
    "required": ["filed", "startTime", "endTime"],
    "additionalProperties": False,
}

_FILES_SEARCH = _schema(
    {
        "keyword": {"type": "string", "minLength": 1, "maxLength": 100},
        "folderRef": _ref("folder"),
        "folderRefs": dict(_ref_array("folder", 1, 20), uniqueItems=True),
        # contentType is retained for existing callers; typeList exposes the upstream multi-select form.
        "typeList": {"type": "array", "items": {"type": "string", "enum": list(_SEARCH_FILE_TYPES)}, "minItems": 1, "maxItems": len(_SEARCH_FILE_TYPES), "uniqueItems": True},
        "contentType": {"type": "string", "enum": list(file_types.CONTENT_TYPE_CHOICES)},
        "extension": {"type": "string", "minLength": 1, "maxLength": 10, "pattern": r"^[A-Za-z0-9]{1,10}$"},
        "extensionList": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 10, "pattern": r"^[A-Za-z0-9]{1,10}$"}, "minItems": 1, "uniqueItems": True},
        "addressList": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 256}, "maxItems": 10, "uniqueItems": True},
        "tagList": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 256}, "maxItems": 10, "uniqueItems": True},
        "timeList": {"type": "array", "items": _SEARCH_TIME_RANGE, "minItems": 1, "maxItems": 10},
        "sizeRange": {"type": "object", "properties": {"startSize": {"type": "integer", "minimum": 0}, "endSize": {"type": "integer", "minimum": 0}}, "required": ["startSize", "endSize"], "additionalProperties": False},
        "isMatchFolder": {"type": "boolean"},
        "from": _date_time(),
        "to": _date_time(),
        "timeField": {"type": "string", "enum": list(_TIME_FIELDS_FILE)},
        "pageSize": _page_size(1, 1000),
        "cursor": _cursor(),
    },
    anyOf=[{"required": [key]} for key in ("keyword", "folderRef", "folderRefs", "contentType", "typeList", "extension", "extensionList", "addressList", "tagList", "timeList", "sizeRange", "from", "to")],
    **{
        "not": {"required": ["contentType", "typeList"]},
        "allOf": [
            {"not": {"required": ["folderRef", "folderRefs"]}},
            {"not": {"required": ["extension", "extensionList"]}},
            {"not": {"required": ["timeList", "from"]}},
            {"not": {"required": ["timeList", "to"]}},
            {"not": {"required": ["timeList", "timeField"]}},
        ],
    },
)

_FILES_LIST = _schema(
    {
        "folderRef": _ref("folder"),
        "rootScope": {"type": "string", "enum": ["dedicated", "personal"]},
        "pageSize": _page_size(10, 100),
        "cursor": _cursor(),
        "sort": {
            "type": "object",
            "properties": {
                "orderBy": {"type": "string", "enum": ["created_at", "updated_at", "size", "name"]},
                "orderDirection": {"type": "string", "enum": ["ASC", "DESC"]},
            },
            "required": ["orderBy"],
            "additionalProperties": False,
        },
    },
    # 省略 folderRef 默认列个人云根；rootScope=dedicated 时列专属根。
)

_FILES_MKDIR = _schema(
    {
        "parentRef": _ref("folder"),
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 255,
            # 在文件系统非法字符之外，额外排除 shell 元字符 ; & $ ` { }（含它们的组合）：
            # 本项目没有 shell 出口，这里按安全用例口径收紧命名入参；空格与 () 保留，
            # 避免拒掉「报销单(2026)」这类常见文件名。
            # pattern 由 file_name_rules 的禁用集合派生，命中时另给点名提示。
            "pattern": file_name_rules.NAME_PATTERN,
            file_name_rules.FORBIDDEN_MARKER: True,
        },
    },
    required=["parentRef", "name"],
)

_FILES_DOWNLOAD = _schema(
    {
        "resources": _ref_array(
            "file",
            1,
            100,
            item_error_policy=REF_ITEM_ERROR_ISOLATE,
        ),
        "localDirectory": {"type": "string", "minLength": 1, "maxLength": 1024},
        "conflictPolicy": {"type": "string", "enum": ["fail", "overwrite", "rename"]},
        "integrityPolicy": {"type": "string", "enum": ["size", "sha256"]},
    },
    required=["resources", "localDirectory"],
)

_FILES_COPY = _schema(
    {
        # 下游批量复制不设单批数量上限；CLI 不额外臆造业务上限。
        "resources": _ref_array(
            "*",
            1,
            None,
            item_error_policy=REF_ITEM_ERROR_ISOLATE,
            allowed_kinds=("file", "folder", "resource"),
        ),
        "destinationRef": _ref("folder"),
    },
    required=["resources", "destinationRef"],
)

_FILES_INFO = _schema(
    {
        "dynamicRefs": _ref_array("dynamic", 1, 100),
        "fileIds": {"type": "array", "items": _ref("file"), "minItems": 1, "maxItems": 100},
        "fields": {
            "type": "array",
            "items": {"type": "string", "enum": ["name", "size", "time", "location"]},
            "minItems": 1,
            "maxItems": 4,
        },
    },
    oneOf=[{"required": ["dynamicRefs"]}, {"required": ["fileIds"]}],
)

_ACTIVITY_BROWSE = _activity_query_schema(EVENT_KINDS_BROWSE)
_ACTIVITY_UPLOADS = _activity_query_schema(
    EVENT_KINDS_UPLOAD,
    {
        "uploadKind": {
            "type": "string",
            "enum": ["normal", "beauty"],
            # 合并口径以本处为准：normal 只查普通上传，beauty 只查美图上传；
            # 通用“上传记录”必须省略本字段，才会合并查询两类（均不含咪咕上传等版权资源）。
            "description": "上传来源：normal=普通上传，beauty=美图上传；"
            "省略本字段时合并查询 normal 与 beauty 两类",
        }
    },
)
_ACTIVITY_SHARE_SAVES = _activity_query_schema(EVENT_KINDS_SHARE_SAVE)
_ACTIVITY_GROUP_SAVES = _activity_query_schema(
    EVENT_KINDS_GROUP_SAVE, {"sourceKind": {"const": "circle"}}
)
_ACTIVITY_DISCOVER_SAVES = _activity_query_schema(EVENT_KINDS_DISCOVER_SAVE)
_ACTIVITY_ALL_SAVES = _activity_query_schema(EVENT_KINDS_ALL_SAVE, with_page=False)

# S13 contentType 取值以下游 readingRecord/list 契约为准：1=咪咕云阅读、5=咪咕云听书、
# 20=内容平台入库的版权书籍、100=云盘书籍。平台对空请求体按 [1,5,100] 处理（不含 20），
# 与契约"不传即筛选所有"不符，因此 CLI 在未限定类型（含传空数组）时显式下发全量 [1,5,20,100]。
# 不收枚举外整数：下游对非法值返回 50010002，在本地拦下才能给出可解释的参数错误。
# 下游同时提供单选 contentType 与多选 allowContentTypeList；两者互斥，同时提交由 Schema 拒绝，
# 让 Agent 一次只能选一种表达方式，避免"单选 + 多选"叠加出上游未定义的组合。
_ACTIVITY_READING_CONTENT_TYPES = (1, 5, 20, 100)

_ACTIVITY_READING = _schema(
    {
        "contentType": {
            "type": "integer",
            "enum": list(_ACTIVITY_READING_CONTENT_TYPES),
            "description": "内容类型（单选）：1=咪咕云阅读，5=咪咕云听书，"
            "20=内容平台入库的版权书籍，100=云盘书籍；省略时不传该字段",
        },
        "allowContentTypeList": {
            "type": "array",
            "items": {"type": "integer", "enum": list(_ACTIVITY_READING_CONTENT_TYPES)},
            "maxItems": len(_ACTIVITY_READING_CONTENT_TYPES),
            "uniqueItems": True,
            "description": "内容类型（多选，与 contentType 互斥）：一次查询多类，"
            "如 [1,5,20] 表示看书、听书加版权书籍；传空数组 [] 或省略该字段表示不限类型，"
            "CLI 统一按全量 [1,5,20,100] 下发（不依赖平台默认，平台默认会漏掉 20）",
        },
    },
    **{"not": {"required": ["contentType", "allowContentTypeList"]}},
)

_ACTIVITY_MONTHLY_UPLOADS = _schema(
    {
        "from": _date_time(),
        "to": _date_time(),
        "timezone": {"type": "string", "format": "iana-timezone", "minLength": 1, "maxLength": 64},
        "uploadKinds": {
            "type": "array",
            "items": {"type": "string", "enum": ["normal", "beauty"]},
            "minItems": 1,
            "maxItems": 2,
            "description": "纳入统计的上传来源：normal=普通上传，beauty=美图上传；"
            "省略本字段时默认两者",
        },
    },
    required=["from", "to"],
)

# ---------------------------------------------------------------- S16

_SHARE_CREATE = _schema(
    {
        "resources": _ref_array("*", 1, 100),
        "encrypt": {"type": "boolean"},
        # 当前 getOutLinkNew 契约只明确 1=天；月/年的整数编码尚未冻结。
        "validity": _validity(("day",)),
    },
    required=["resources"],
)

_SHARE_SUBSCRIBE = _schema(
    {
        "linkRef": _ref("link"),
        # 用户自然语言中通常提供完整分享链接；CLI 负责校验域名与路由并提取 linkId。
        # linkRef 仅用于承接前序 CLI 返回的受控引用，两者严格二选一。
        "linkUrl": {"type": "string", "format": "uri", "minLength": 8, "maxLength": 2048},
        "passwd": {"type": "string", "minLength": 1, "maxLength": 10},
    },
    required=["passwd"],
    oneOf=[{"required": ["linkRef"]}, {"required": ["linkUrl"]}],
)

_SHARE_UPDATES = _schema({"linkRef": _ref("link")}, required=["linkRef"])

_SHARE_SAVE_UPDATES = _schema(
    {
        "saveKind": {"const": SAVE_KIND_SUBSCRIPTION_UPDATES},
        "linkRef": _ref("link"),
        "selectedResources": _ref_array("*", 1, 200),
        "destinationRef": _ref("folder"),
    },
    required=["saveKind", "linkRef", "selectedResources"],
)

_SHARE_SAVE_SELECTED = _schema(
    {
        "saveKind": {"const": SAVE_KIND_SELECTED_RESOURCES},
        "linkRef": _ref("link"),
        "destinationRef": _ref("folder"),
        "contentRefs": _ref_array("content", 1, 200),
        "catalogRefs": _ref_array("catalog", 1, 200),
    },
    required=["saveKind", "linkRef"],
    anyOf=[{"required": ["contentRefs"]}, {"required": ["catalogRefs"]}],
)

_OPERATION_STATUS = _schema(
    {
        # 字段名与分派层一致，用 kebab 的 operation-ref：main.py 的 CONTROL_COMMAND_FIELDS
        # 只接受 --operation-ref，若此处沿用驼峰 operationRef，会下发一个 CLI 实际拒绝的名字。
        # 值仍是写场景计划阶段直接返回的本地台账键（cmop_*），不是资源引用
        # cmr_operation_*，因此不走 x-ref-kind 预解析。
        "operation-ref": {
            "type": "string",
            "pattern": r"^cmop_[A-Za-z0-9_-]+$",
            "minLength": 6,
            "maxLength": 256,
        }
    },
    required=["operation-ref"],
)

# S23 keyword 必填：records/files 两个分支的下游都强制要求关键词——未传、空串或纯空格
# 均返回 code=401（"keywords is null" / "keyword is required"，2026-09-19 实测），
# 不存在“省略关键词返回全部发出分享”的下游口径；S24 的订阅接口不受此约束，仍可省略。
_SHARE_LIST_SENT = _schema(
    {
        "listKind": {"const": LIST_KIND_SENT},
        "searchScope": {"type": "string", "enum": ["records", "files"]},
        "keyword": {"type": "string", "minLength": 1, "maxLength": 100},
        "pageSize": _page_size(),
        "cursor": _cursor(),
    },
    required=["listKind", "searchScope", "keyword"],
)

# S24 keyword 可选：SearchSubscribeShareCode 契约把 keyword 标为 O，未提供时不发 conditions.keyword，
# 由下游返回全部订阅外链；typeList/timeList 同为可选筛选维度。
_SHARE_LIST_SUBSCRIPTIONS = _schema(
    {
        "listKind": {"const": LIST_KIND_SUBSCRIPTIONS},
        "keyword": {"type": "string", "minLength": 1, "maxLength": 100},
        "typeList": {
            "type": "array",
            "items": {"type": "string", "enum": ["0", "1", "2", "3", "4", "100"]},
            "minItems": 1,
            "maxItems": 6,
        },
        "from": _date_time(),
        "to": _date_time(),
        "timeField": {"type": "string", "enum": list(_TIME_FIELDS_SHARE)},
        "pageSize": _page_size(),
        "cursor": _cursor(),
    },
    required=["listKind"],
    # 契约 SubscribeShareCodeTimeRangeDTO 的 startTime/endTime 均为必填：CLI 的 from/to 映射到
    # 同一个 timeList 条目，必须成对出现，不允许只发半开区间的一端。
    dependentRequired={"from": ["to"], "to": ["from"]},
)

_SHARE_BROWSE = _schema(
    {
        "linkRef": _ref("link"),
        "linkUrl": {"type": "string", "format": "uri", "minLength": 8, "maxLength": 2048},
        "passwd": {"type": "string", "minLength": 1, "maxLength": 32},
        "parentCatalogRef": _ref("catalog"),
        "begin": {"type": "integer", "minimum": 1, "maximum": 100000000},
        "end": {"type": "integer", "minimum": 1, "maximum": 100000000},
        "sort": {
            "type": "object",
            "properties": {
                "by": {"type": "string", "enum": ["name", "time", "size", "type"]},
                "order": {"type": "string", "enum": ["asc", "desc"]},
            },
            "required": ["by"],
            "additionalProperties": False,
        },
    },
    oneOf=[{"required": ["linkRef"]}, {"required": ["linkUrl"]}],
)

# ---------------------------------------------------------------- S26～S35 相簿与故事

_ALBUM_SEARCH = _schema(
    {
        "keyword": {"type": "string", "minLength": 1, "maxLength": 64},
        "kind": {"type": "string", "enum": [ALBUM_KIND, STORY_KIND]},
        "pageSize": _page_size(),
        "cursor": _cursor(),
    },
    required=["keyword", "kind"],
)

# S27 相簿/故事列表：只列对象，不接受对象引用。
# pageSize 下限 10：服务端对小于 10 的页大小返回 01000001 参数错误（2026-09-18 实测）。
_ALBUM_LIST = _schema(
    {
        "kind": {"type": "string", "enum": [ALBUM_KIND, STORY_KIND]},
        "pageSize": _page_size(10, 100),
        "cursor": _cursor(),
    },
    required=["kind"],
)

# S28 相簿/故事图片明细：必须提供受控对象引用。
# pageSize 下限 10：同相簿/故事列表族接口约束（2026-09-18 实测）。
_ALBUM_FILES = _schema(
    {
        "kind": {"type": "string", "enum": [ALBUM_KIND, STORY_KIND]},
        # kind 决定引用必须是 album 还是 story；先统一预解析，再由工作流做精确类型校验。
        "albumRef": _ref("*"),
        "pageSize": _page_size(10, 100),
        "cursor": _cursor(),
    },
    required=["kind", "albumRef"],
)

# S29 新建相簿：kind=album 且不带 fileRefs（带 fileRefs 命中 S30）。
_ALBUM_CREATE = _schema(
    {
        "kind": {"const": ALBUM_KIND},
        "name": {"type": "string", "minLength": 1, "maxLength": 64},
    },
    required=["kind", "name"],
)

# S30 建册并加图：kind=album 且 fileRefs 非空。
_ALBUM_CREATE_WITH_FILES = _schema(
    {
        "kind": {"const": ALBUM_KIND},
        "name": {"type": "string", "minLength": 1, "maxLength": 64},
        "fileRefs": _ref_array("*", 1, 500),
    },
    required=["kind", "name", "fileRefs"],
)

_ALBUM_ADD = _schema(
    {
        "albumRef": _ref("album"),
        "fileRefs": _ref_array("*", 1, 500),
    },
    required=["albumRef", "fileRefs"],
)

# S32 新建回忆故事：kind=story。
# 云盘 story/memory/add 要求 secondTitle 字段存在且非空（缺失返回 01000001 参数错误，2026-09-19 实测）；
# CLI 入参 secondTitle 保持可选，未提供时由工作流默认用故事标题填充，保证发往下游的字段非空。
_STORY_CREATE = _schema(
    {
        "kind": {"const": STORY_KIND},
        "name": {"type": "string", "minLength": 1, "maxLength": 64},
        "secondTitle": {"type": "string", "minLength": 1, "maxLength": 64},
        "coverRef": _ref("*"),
        "fileRefs": _ref_array("*", 1, 500),
    },
    required=["kind", "name", "coverRef", "fileRefs"],
)

# S33 修改相簿：kind=album。
_ALBUM_UPDATE = _schema(
    {
        "kind": {"const": ALBUM_KIND},
        "albumRef": _ref("album"),
        "name": {"type": "string", "minLength": 1, "maxLength": 64},
        "coverRef": _ref("*"),
    },
    required=["kind", "albumRef"],
    anyOf=[{"required": ["name"]}, {"required": ["coverRef"]}],
)

# S34 修改故事：kind=story。
_STORY_UPDATE = _schema(
    {
        "kind": {"const": STORY_KIND},
        "storyRef": _ref("story"),
        "name": {"type": "string", "minLength": 1, "maxLength": 64},
        "secondTitle": {"type": "string", "minLength": 1, "maxLength": 64},
        "coverRef": _ref("*"),
    },
    required=["kind", "storyRef"],
    anyOf=[{"required": ["name"]}, {"required": ["secondTitle"]}, {"required": ["coverRef"]}],
)

# S35 只生成相簿/故事分享链接：不含送达语义。
# 可选字段与 getAlbumShareInfo 契约同名同义：mood、needWatermark/seqNo、shareTimeType、visitUnlimited。
_ALBUM_SHARE = _schema(
    {
        "objectRef": _ref("*"),
        "kind": {"type": "string", "enum": [ALBUM_KIND, STORY_KIND]},
        "deliveryMode": {"const": DELIVERY_MODE_LINK_ONLY},
        "mood": {"type": "string", "minLength": 1, "maxLength": 500},
        "needWatermark": {"type": "integer", "enum": [0, 1]},
        "seqNo": {"type": "string", "minLength": 1, "maxLength": 200},
                # 1 一天 / 2 七天 / 3 长期；用户未指定时用本地默认 2（七天）
                "shareTimeType": {"type": "integer", "enum": [1, 2, 3], "default": 2},
        "visitUnlimited": {"type": "integer", "enum": [0, 1]},
    },
    required=["objectRef", "kind", "deliveryMode"],
)

SCENARIO_SCHEMAS = {
    "S01": _FILES_UPLOAD,
    "S02": _FILES_SEARCH,
    "S03": _FILES_LIST,
    "S04": _FILES_MKDIR,
    "S05": _FILES_DOWNLOAD,
    "S06": _FILES_COPY,
    "S07": _ACTIVITY_BROWSE,
    "S08": _ACTIVITY_UPLOADS,
    "S09": _ACTIVITY_SHARE_SAVES,
    "S10": _ACTIVITY_GROUP_SAVES,
    "S11": _ACTIVITY_DISCOVER_SAVES,
    "S12": _ACTIVITY_ALL_SAVES,
    "S13": _ACTIVITY_READING,
    "S14": _FILES_INFO,
    "S15": _ACTIVITY_MONTHLY_UPLOADS,
    "S16": _SHARE_CREATE,
    "S18": _SHARE_SUBSCRIBE,
    "S19": _SHARE_UPDATES,
    "S20": _SHARE_SAVE_UPDATES,
    "S21": _SHARE_SAVE_SELECTED,
    "S22": _OPERATION_STATUS,
    "S23": _SHARE_LIST_SENT,
    "S24": _SHARE_LIST_SUBSCRIPTIONS,
    "S25": _SHARE_BROWSE,
    "S26": _ALBUM_SEARCH,
    "S27": _ALBUM_LIST,
    "S28": _ALBUM_FILES,
    "S29": _ALBUM_CREATE,
    "S30": _ALBUM_CREATE_WITH_FILES,
    "S31": _ALBUM_ADD,
    "S32": _STORY_CREATE,
    "S33": _ALBUM_UPDATE,
    "S34": _STORY_UPDATE,
    "S35": _ALBUM_SHARE,
    # 实例名称仅来自服务端，不接受调用方指定名称、平台或身份。
    "S36": _schema({}),
}

# 公开命令路由 → 该路由下的场景（一个主命令可承载多个以判别字段区分的场景）。
# 共享主命令的判别字段由注册表以同一事实源校验；此处不重复声明。
COMMAND_SCENARIOS = {
    "files.upload": ("S01",),
    "files.search": ("S02",),
    "files.list": ("S03",),
    "files.mkdir": ("S04",),
    "files.download": ("S05",),
    "files.copy": ("S06",),
    "activity.list": ("S07", "S08", "S09", "S10", "S11", "S12"),
    "activity.reading": ("S13",),
    "files.info": ("S14",),
    "activity.stats": ("S15",),
    "share.create": ("S16",),
    "share.subscribe": ("S18",),
    "share.updates": ("S19",),
    "share.save": ("S20", "S21"),
    "operation.status": ("S22",),
    "share.list": ("S23", "S24"),
    "share.browse": ("S25",),
    "album.search": ("S26",),
    "album.list": ("S27",),
    "album.files": ("S28",),
    "album.create": ("S29", "S30", "S32"),
    "album.add": ("S31",),
    "album.update": ("S33", "S34"),
    "album.share": ("S35",),
    "agent.info": ("S36",),
}

SCENARIO_COMMAND_IDS = {
    scenario_id: command_id
    for command_id, scenario_ids in COMMAND_SCENARIOS.items()
    for scenario_id in scenario_ids
}

SCHEMA_IDS = {scenario_id: scenario_id + SCHEMA_ID_SUFFIX for scenario_id in SCENARIO_SCHEMAS}

_SCHEMA_ID_INDEX = {schema_id: scenario_id for scenario_id, schema_id in SCHEMA_IDS.items()}


def _command_schema(scenario_ids):
    branches = [SCENARIO_SCHEMAS[scenario_id] for scenario_id in scenario_ids]
    if len(branches) == 1:
        return branches[0]
    # 共享主命令：分支两两互斥且完备，oneOf 保证恰好命中一个场景。
    return {"type": "object", "oneOf": branches, "x-scenarios": list(scenario_ids)}


COMMAND_SCHEMAS = {
    command_id: _command_schema(scenario_ids) for command_id, scenario_ids in COMMAND_SCENARIOS.items()
}


def command_schema(command_id):
    try:
        return COMMAND_SCHEMAS[command_id]
    except KeyError:
        raise KeyError("未注册的 command-id: %s" % command_id)


def scenarios_for_command(command_id):
    try:
        return COMMAND_SCENARIOS[command_id]
    except KeyError:
        raise KeyError("未注册的 command-id: %s" % command_id)


def schema_for_command(command_id):
    """按 command_id 返回命令级触发入参 Schema（共享主命令为各场景 oneOf）。"""
    return command_schema(command_id)


def schema_for_scenario(scenario_id):
    try:
        return SCENARIO_SCHEMAS[scenario_id]
    except KeyError:
        raise KeyError("未登记的场景: %s" % scenario_id)


def schema_id_for_scenario(scenario_id):
    try:
        return SCHEMA_IDS[scenario_id]
    except KeyError:
        raise KeyError("未登记的场景: %s" % scenario_id)


def scenario_for_schema_id(schema_id):
    try:
        return _SCHEMA_ID_INDEX[schema_id]
    except KeyError:
        raise KeyError("未登记的 Schema id: %s" % schema_id)
