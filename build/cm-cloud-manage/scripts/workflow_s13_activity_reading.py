# S13 最近阅读/听书记录：查询阅读/听书记录（查询范围由平台固定）。
# 流程：入参透传 -> 查询 -> 成功码校验 -> RecordInfo 归一 -> 出参。
# CLI: cmcloud activity reading --format json

from cmcloud_core import result as result_module
from cmcloud_adapters import idaas_reading_record_list
from cmcloud_workflows import common

# 契约口径：一次返回全部、最多 30 条、按 updatedAt 降序；接口没有时间入参、
# 没有分页游标、没有 total，因此本场景不截断、不生成游标，page 固定为 null。
UPSTREAM_MAX_ITEMS = 30

# 契约枚举全集；契约称"两个类型字段都不传即筛选所有阅读记录"。
ALL_CONTENT_TYPES = (1, 5, 20, 100)

# RecordInfo 的标量字段直出（契约字段名，不做改名以外的加工）
_SCALAR_FIELDS = (
    "contentType",
    "progress",
    "shelfStatus",
    "chapterOffset",
    "currentChapterPosition",
    "totalChapterCount",
    "chapterName",
    "fileExtension",
    "fileOwnerType",
)

# 真实标识一律换发用途受限的受控引用，不把下游 ID 直接交给 Agent。
# contentId 在 V2/V3 是云盘文件 ID 或内容平台 ID，与个人云 fileId 的映射尚未验收，
# 因此不冒充 fileRef。
_REF_FIELDS = (
    ("id", "recordRef", "reading-record"),
    ("contentId", "contentRef", "reading-content"),
    ("thirdContentId", "thirdContentRef", "reading-third-content"),
    ("chapterId", "chapterRef", "reading-chapter"),
    ("bookId", "bookRef", "reading-book"),
)


def run(context, input_data):
    payload = common.guard_upstream(
        context, idaas_reading_record_list.call, **_type_kwargs(input_data)
    )
    _require_success(context, payload)
    data = common.payload_data(context, payload)
    raw_items = [item for item in common.list_field(context, data, "items") if isinstance(item, dict)]
    normalized = [_reading_item(context, item) for item in raw_items]
    if not normalized:
        return common.empty(data={"items": []})
    return common.success(data={"items": normalized}, counts={"returned": len(normalized)})


def _type_kwargs(input_data):
    """类型入参：单选原样透传；多选为空数组时按全量下发；两个都不传时也**显式下发全量**。

    契约写的是"两个类型字段都不传即筛选所有阅读记录"，但 2026-09-21 实测平台对空请求体
    按 `[1,5,100]` 处理（不含 `20`），导致类型 20 的记录查不到。为兑现契约语义，
    这里显式下发枚举全集，而不是依赖平台那个与契约不符的默认。
    `allowContentTypeList=[]` 与"不传"语义一致（都是"不限类型"），同样翻译成全量，
    避免把空数组当作"过滤掉所有类型"而查到空结果。
    """
    allow = input_data.get("allowContentTypeList")
    if allow is not None:
        types = [item for item in allow]
        return {"allow_content_type_list": types or list(ALL_CONTENT_TYPES)}
    if input_data.get("contentType") is not None:
        return {"content_type": input_data["contentType"]}
    return {"allow_content_type_list": list(ALL_CONTENT_TYPES)}


def _require_success(context, payload):
    """成功判定：契约 `code=0`；现网实测为 `success=true` + `code="0000"`，两种都接受。

    只按成功码判定，不把"没有 data"当成业务失败；缺 data 由 payload_data 按契约不符处理。
    """
    if not isinstance(payload, dict):
        common.contract_mismatch(context, "平台响应不是对象")
    code = payload.get("code")
    accepted = False
    if not isinstance(code, bool):
        if code == 0:
            accepted = True
        elif isinstance(code, str) and code.strip() in ("0", "0000"):
            accepted = True
    if accepted and payload.get("success") is not False:
        return
    message = payload.get("message")
    detail = "下游接口返回失败: code=%s" % (code if code is not None else "<missing>")
    if isinstance(message, str) and message:
        detail += " message=%s" % message
    raise result_module.ExecutionError(result_module.FAILED, detail)


def _reading_item(context, item):
    normalized = {}
    name = item.get("contentName")
    if isinstance(name, str) and name:
        normalized["name"] = name
    for source_key, output_key, kind in _REF_FIELDS:
        value = item.get(source_key)
        if isinstance(value, str) and value:
            normalized[output_key] = context.issue_ref(kind, value, common.SCOPE_ACTIVITY)
    for key in _SCALAR_FIELDS:
        value = item.get(key)
        if isinstance(value, common.SCALAR_TYPES) and not isinstance(value, bool):
            normalized[key] = value
    occurred = item.get("updatedAt")
    if isinstance(occurred, str) and occurred:
        normalized["occurredAt"] = occurred
    progress_time = item.get("progressTime")
    if isinstance(progress_time, str) and progress_time:
        normalized["progressAt"] = progress_time
    return normalized
