# S35 生成相簿/故事链接：只为完整相簿或故事内容生成分享链接。
# 流程：读取对象及图片元数据 -> 构造完整 DTO -> 可信确认 -> 创建链接 -> 回执核验。
# CLI: cmcloud album share --format json

import re
from datetime import datetime, timedelta, timezone

from cmcloud_core import result as result_module
from cmcloud_adapters import (
    idaas_get_album_share_info,
    idaas_photo_customization_file_list,
    idaas_photo_customization_list,
    idaas_story_memory_file_list,
    idaas_story_memory_list,
)
from cmcloud_workflows import common

# 契约口径：1-回忆相册（故事），2-普通相册（相簿）
ALBUM_KIND_TO_TYPE = {"story": 1, "album": 2}

# 分享有效期：1 一天 / 2 七天 / 3 长期；用户未要求时用本地默认（七天）
DEFAULT_SHARE_TIME_TYPE = 2

# 判别字段由命令 Schema 固定：V1.8 只支持只生成链接，不含送达语义。
DELIVERY_MODE = "link-only"

# 相簿/故事列表与图片列表都是游标分页（一次最多 100 条）：只取首页会漏掉对象
# （误报"找不到该相簿"）或把 photos 截断。prepare 阶段内部自带书签翻页，
# 页数上限与 S33/S34 的内部扫描保护一致，避免上游一直给书签时无限循环；
# 上限用尽时一律明确失败，绝不把"没翻到"说成"对象不存在"、也不下发残缺 photos。
MAX_PAGES = 20

# 契约示例的展示时间形如"2021年07月"（中文年月、月份两位）；平台列表给的是
# "2026-09-18 14:08:47" 这类时间戳串，原样下发会被下游判为非法（实测 01000101 服务器响应异常）。
SHOWTIME_FIELD_ORDER = ("showtime", "createTime", "updateTime", "beginTime", "endTime")
_MONTH_TEXT = re.compile(r"^\d{4}年\d{1,2}月")
_SEPARATED_TIME = re.compile(r"^(\d{4})[-/.](\d{1,2})(?:[-/.](\d{1,2}))?")
_COMPACT_TIME = re.compile(r"^(\d{4})(\d{2})(\d{2})(?:\d{6})?$")
_EPOCH_DIVISOR = {10: 1, 13: 1000}
CHINA_TIME_ZONE = timezone(timedelta(hours=8))

# 契约《getAlbumShareInfo》响应码：只有 0 是成功。下列码有明确用户语义；
# 其余（含网关 01000101 服务器响应异常）一律按系统失败上报，不猜测原因。
FAILURE_BY_CODE = {
    "100": (result_module.AUTH_REQUIRED, "云盘鉴权失败，请重新授权后再试"),
    "101": (result_module.AUTH_REQUIRED, "账号与鉴权不匹配，请重新授权后再试"),
    "115": (result_module.AUTH_REQUIRED, "身份校验不通过，请重新授权后再试"),
    "116": (result_module.NOT_FOUND, "该相簿或故事已不存在，请重新选择要分享的对象"),
    "102": (result_module.INVALID_ARGUMENT, "相簿内的资源全部无效，无法生成分享链接"),
    "4000": (result_module.INVALID_ARGUMENT, "分享请求参数为空，无法生成分享链接"),
    "53000": (result_module.FAILED, "云盘系统维护中，请稍后再试"),
    "01001": (result_module.RATE_LIMITED, "今天已分享多次，请明天再试"),
    "01002": (result_module.RATE_LIMITED, "分享过于频繁，请稍后再试"),
    "-1": (result_module.FAILED, "获取相册分享信息失败，请稍后再试"),
}

_SUCCESS_CODES = ("0", "0000")


def prepare(context, input_data):
    if input_data.get("deliveryMode") != DELIVERY_MODE:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "deliveryMode 必须为 %s" % DELIVERY_MODE
        )
    kind = input_data["kind"]
    # 正式来源是 S27 的 album/story 引用；保留旧台账 object 引用的兼容读取。
    object_id = common.real_id_for_kinds(context, input_data["objectRef"], (kind, "object"), "objectRef")
    need_watermark = input_data.get("needWatermark")
    seq_no = input_data.get("seqNo")
    if need_watermark == 1 and not seq_no:
        # 契约要求 needWatermark=1 时建议必传 seqNo（水印唯一序列号），缺省下游拿不到序列号
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "needWatermark=1 时必须提供 seqNo"
        )
    detail, scan_complete = _read_object(context, kind, object_id)
    _require_share_metadata(kind, detail, scan_complete)
    # 契约：needWatermark=1 时下游不处理 photos，无需回查图片列表（省一次或多次请求）
    photos = [] if need_watermark == 1 else _read_photos(context, kind, object_id)
    share_time_type = input_data.get("shareTimeType")
    if share_time_type is None:
        share_time_type = DEFAULT_SHARE_TIME_TYPE
    return {
        "summary": "为%s %s 生成分享链接" % ("相簿" if kind == "album" else "故事", detail["name"]),
        "resolvedIds": {
            "objectId": object_id,
            "albumName": detail.get("name"),
            "albumShowtime": detail.get("showtime"),
            "albumType": ALBUM_KIND_TO_TYPE[kind],
            "storyId": object_id,
            "photos": photos,
        },
        "kind": kind,
        "deliveryMode": DELIVERY_MODE,
        "mood": input_data.get("mood"),
        "needWatermark": need_watermark,
        "seqNo": seq_no,
        "shareTimeType": share_time_type,
        "visitUnlimited": input_data.get("visitUnlimited"),
    }


def _require_share_metadata(kind, detail, scan_complete=True):
    """分享 DTO 的 albumName/albumShowtime 都是契约必填，必须在 prepare 阶段拿到。

    取不到时不得把 None 冻进计划：否则用户确认之后才会卡在下游入参校验上，
    把"对象不存在或元数据缺失"伪装成执行期故障。

    另外区分两种"取不到"：列表翻完了确实没有该对象（用户侧问题），
    与页数上限用尽没翻到（本次扫描能力问题，不能反过来说对象不存在）。
    """
    label = "相簿" if kind == "album" else "故事"
    if not detail:
        if not scan_complete:
            raise result_module.ExecutionError(
                result_module.FAILED,
                "已到扫描上限%d页（每页%d条，共%d条%s记录）仍未找到该%s，无法生成分享链接；"
                "它可能存在但不在本次扫描范围内，不再继续扫描以免请求过多"
                % (MAX_PAGES, common.MAX_PAGE_SIZE, MAX_PAGES * common.MAX_PAGE_SIZE, label, label),
            )
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT,
            "未能在%s列表中找到该%s，无法生成分享链接；请先确认它仍然存在后再试" % (label, label),
        )
    name = detail.get("name")
    if not isinstance(name, str) or not name.strip():
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "该%s缺少名称，无法生成分享链接" % label
        )
    showtime = detail.get("showtime")
    if not isinstance(showtime, str) or not showtime.strip():
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT,
            "该%s缺少可用于分享的展示时间（契约要求形如 2026年09月），无法生成分享链接" % label,
        )


def execute(context, plan):
    kwargs = {
        "album_name": plan["resolvedIds"]["albumName"],
        "album_showtime": plan["resolvedIds"]["albumShowtime"],
        "album_type": plan["resolvedIds"]["albumType"],
        "story_id": plan["resolvedIds"]["storyId"],
    }
    # 契约 photos 可选：无照片时不下发该字段
    if plan["resolvedIds"]["photos"]:
        kwargs["photos"] = plan["resolvedIds"]["photos"]
    if plan["mood"] is not None:
        kwargs["mood"] = plan["mood"]
    if plan["needWatermark"] is not None:
        kwargs["need_watermark"] = plan["needWatermark"]
    if plan["seqNo"] is not None:
        kwargs["seq_no"] = plan["seqNo"]
    if plan["shareTimeType"] is not None:
        kwargs["share_time_type"] = plan["shareTimeType"]
    if plan["visitUnlimited"] is not None:
        kwargs["visit_unlimited"] = plan["visitUnlimited"]
    # x-DeviceInfo 由可信发布配置生成，不接受 Agent 输入
    context.device_info()
    payload = common.guard_upstream(context, idaas_get_album_share_info.call, **kwargs)
    context.note_step("create-link", "已请求创建相册分享链接")
    result = _share_result(context, payload)
    link_id = common.field_text(context, result, "albumId")
    link_url = common.field_text(context, result, "url")
    return common.success(
        data={
            "linkCreated": True,
            "linkRef": context.issue_ref("link", link_id, common.SCOPE_OUTER_LINK),
            "linkUrl": link_url,
            "albumId": link_id,
            "url": link_url,
            "albumName": result.get("albumName"),
            "mood": result.get("mood"),
            "deliveryMode": DELIVERY_MODE,
            "deliveryNote": "本场景只生成链接，不发送、也不报告送达",
        },
        counts={"created": 1},
    )


def _share_result(context, payload):
    """取分享结果对象。

    实测同一接口有两种成功信封：平台包络（success=true/code="0000"/data）与
    契约包络（code="0"/ret）。成功码只认 0/0000，结果对象在 ret 或 data 里，
    位置不固定；不能只看 success 字段，也不能只认 "0000"。
    """
    code = payload.get("code")
    if _is_success(code, payload.get("success")):
        return _result_object(context, payload)
    _raise_share_failure(payload, code)


def _is_success(code, success):
    if success is False:
        return False
    text = "" if code is None else str(code).strip()
    if text in _SUCCESS_CODES:
        return True
    # 兜底：既不带 code 又显式 success=true 的响应（如 00010002 类网关简化包络）
    return code is None and success is True


def _result_object(context, payload):
    for key in ("ret", "data"):
        value = payload.get(key)
        if value is None:
            continue
        if not isinstance(value, dict):
            common.contract_mismatch(context, "相簿分享成功响应 %s 不是对象" % key)
        return value
    return {}


def _raise_share_failure(payload, code):
    """按契约响应码上报失败；未登记的码保持原始 code/message，便于按 traceId 上报。"""
    key = str(code).strip() if code is not None else ""
    mapped = FAILURE_BY_CODE.get(key)
    if mapped is not None:
        raise result_module.ExecutionError(
            mapped[0], mapped[1], data={"upstream": {"upstreamCode": key}}
        )
    detail = "相簿分享失败: code=%s" % (key or "<missing>")
    message = _error_text(payload)
    if message:
        detail += " message=%s" % message
    raise result_module.ExecutionError(result_module.FAILED, detail)


def _error_text(payload):
    for key in ("message", "msg"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _showtime_text(value):
    """把平台时间字段转成契约要求的展示串（如 2026年09月）；无法解析返回 None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if _MONTH_TEXT.match(text):
        # 平台已经给的是展示串（如回忆相册的"2021年07月"），原样透传
        return text
    match = _SEPARATED_TIME.match(text) or _COMPACT_TIME.match(text)
    if match is not None:
        month_text = _month_text(match.group(1), match.group(2))
        if month_text is not None:
            return month_text
    return _epoch_month_text(text)


def _epoch_month_text(text):
    divisor = _EPOCH_DIVISOR.get(len(text)) if text.isdigit() else None
    if divisor is None:
        return None
    try:
        moment = datetime.fromtimestamp(int(text) / divisor, tz=CHINA_TIME_ZONE)
    except (OverflowError, OSError, ValueError):
        return None
    return _month_text(moment.year, moment.month)


def _month_text(year, month):
    try:
        year = int(year)
        month = int(month)
    except (TypeError, ValueError):
        return None
    if not 1970 <= year <= 2100 or not 1 <= month <= 12:
        return None
    return "%04d年%02d月" % (year, month)


def _object_showtime(item):
    """按字段优先级取第一个可解析的展示时间；全部不可解析时返回 None（不猜格式）。"""
    for field in SHOWTIME_FIELD_ORDER:
        text = _showtime_text(item.get(field))
        if text is not None:
            return text
    return None


def _read_object(context, kind, object_id):
    """翻页查找对象；返回 (detail, scan_complete)。

    scan_complete=False 表示页数上限用尽仍未翻完，此时"没找到"不能表述成"对象不存在"。
    """
    action = idaas_photo_customization_list.call if kind == "album" else idaas_story_memory_list.call
    page_info = {"pageSize": common.MAX_PAGE_SIZE}
    for _ in range(MAX_PAGES):
        payload = common.guard_upstream(context, action, **_list_kwargs(context, page_info))
        data = common.payload_data(context, payload, required=False) or {}
        items = data.get("list")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                # 平台列表项主键为 id（无 albumId 字段），时间字段为 createTime/updateTime/beginTime，无 showtime
                if (item.get("albumId") or item.get("id")) == object_id:
                    return {"name": item.get("name"), "showtime": _object_showtime(item)}, True
        cursor = _next_cursor(data)
        if cursor is None:
            return {}, True
        page_info["pageCursor"] = cursor
    return {}, False


def _list_kwargs(context, page_info):
    kwargs = {"page_info": page_info}
    if context.publication.album_owner_type is not None:
        kwargs["owner_type"] = context.publication.album_owner_type
    return kwargs


def _next_cursor(holder):
    """只有响应真实给出非空 nextPageCursor 才继续翻页；缺失即视为已取完。"""
    if not isinstance(holder, dict):
        return None
    cursor = holder.get("nextPageCursor")
    if isinstance(cursor, str) and cursor:
        return cursor
    return None


def _read_photos(context, kind, object_id):
    """翻页取全图片；页数上限用尽仍有下一页时明确失败，不静默下发残缺 photos。"""
    label = "相簿" if kind == "album" else "故事"
    action = idaas_photo_customization_file_list.call if kind == "album" else idaas_story_memory_file_list.call
    page_info = {"pageSize": common.MAX_PAGE_SIZE}
    items = []
    for _ in range(MAX_PAGES):
        kwargs = _list_kwargs(context, page_info)
        kwargs["album_id"] = object_id
        if kind == "album":
            # 相簿图片列表接口要求排序字段必填；故事图片列表接口不接受这两个字段
            kwargs["order_by"] = common.ALBUM_PHOTO_ORDER_BY
            kwargs["order_direction"] = common.ALBUM_PHOTO_ORDER_DIRECTION
        payload = common.guard_upstream(context, action, **kwargs)
        data = common.payload_data(context, payload, required=False) or {}
        if kind == "story":
            file_page = data.get("file")
            holder = file_page if isinstance(file_page, dict) else {}
        else:
            holder = data
        page_items = holder.get("list")
        if isinstance(page_items, list):
            items.extend(page_items)
        cursor = _next_cursor(holder)
        if cursor is None:
            return _photo_entries(kind, items)
        page_info["pageCursor"] = cursor
    # photos 是分享的照片集合：取不全就会生成内容不全的链接，因此宁可明确失败
    raise result_module.ExecutionError(
        result_module.FAILED,
        "该%s图片超过本次可读取上限（%d页×%d条），photos 无法取全，未生成分享链接"
        % (label, MAX_PAGES, common.MAX_PAGE_SIZE),
    )


def _photo_entries(kind, items):
    """按跨页的全局顺序编号：sort 是分享 DTO 内图片的次序，不能每页从 1 重新开始。"""
    photos = []
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            continue
        file_info = item.get("fileInfo") if kind == "story" else item
        if not isinstance(file_info, dict):
            continue
        file_id = file_info.get("fileId")
        if isinstance(file_id, str) and file_id:
            photos.append({"sort": index, "contentID": file_id})
    return photos
