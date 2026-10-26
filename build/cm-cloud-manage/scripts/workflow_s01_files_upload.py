# S01 上传文件：把本地文件上传到用户指定的个人云目录。
# 流程：本地/目标核验 -> 创建上传 -> 分片传输 -> 完成 -> 详情核验。
# CLI: cmcloud files upload --format json

import os

from cmcloud_core import result as result_module
from cmcloud_adapters import (
    file_complete,
    file_create,
    file_form_upload,
    file_get,
    file_get_upload_url,
    file_multipart_upload,
)
from cmcloud_validation import file_name_rules
from cmcloud_workflows import common

MIN_PART_SIZE = 5 * 1024 * 1024
MAX_PARTS_PER_CREATE = 100
MAX_PART_NUMBER = 10000

RENAME_MODE_BY_POLICY = {
    "rename": "force_rename",
    "reject": "refuse",
    "dedupe": "auto_rename",
}


def prepare(context, input_data):
    """冻结本地绝对路径、文件元数据、目标 ID、策略、覆盖 fileId 与摘要；无副作用。"""
    # 摘要算法按发布配置选（pds 底座只认 sha1、云空间底座只认 sha256），
    # 算法在 prepare 冻结进计划，execute 必须用同一个算法复核与提交。
    hash_algorithm = context.upload_hash_algorithm()
    info = common.local_file_info(context, input_data["localFile"], algorithms=(hash_algorithm,))
    if not info["name"] or len(info["name"]) > 255:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "文件名长度不合法")
    # 上传后的文件名（本地文件名的最后一段）与 files.mkdir 共用同一份禁用字符集合：
    # 目录名不允许的字符，这里也拒绝上传，避免"建不了的目录名却能传进去"的口径分歧。
    name_message = file_name_rules.file_name_error_message(info["name"])
    if name_message is not None:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, name_message)
    destination_id = context.real_id(input_data["destinationRef"], "folder")
    common.ensure_directory(context, destination_id)
    policy = input_data.get("overwritePolicy") or "rename"
    if policy not in RENAME_MODE_BY_POLICY:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "overwritePolicy 取值非法")
    return {
        "summary": "上传本地文件 %s（%d 字节）到目标目录，冲突策略 %s" % (info["name"], info["size"], policy),
        "resolvedIds": {"parentFileId": destination_id},
        "localFile": info["path"],
        "fileName": info["name"],
        "size": info["size"],
        "hashAlgorithm": hash_algorithm,
        "contentHash": info[hash_algorithm],
        "overwritePolicy": policy,
        "renameMode": RENAME_MODE_BY_POLICY[policy],
        "destinationRef": input_data["destinationRef"],
    }


def execute(context, plan):
    # 确认窗口内本地文件可能被替换（同尺寸换内容只靠大小查不出来），
    # 因此执行前重新计算摘要并与冻结计划比对。
    _assert_local_file_unchanged(context, plan)
    parent_id = plan["resolvedIds"]["parentFileId"]
    parts = _plan_parts(plan["size"])
    create_kwargs = {
        "name": plan["fileName"],
        "size": plan["size"],
        "parent_file_id": parent_id,
        "part_infos": parts,
        "content_hash": plan["contentHash"],
        "content_hash_algorithm": plan["hashAlgorithm"],
    }
    if plan["renameMode"]:
        create_kwargs["file_rename_mode"] = plan["renameMode"]
    payload = common.guard_upstream(context, file_create.call, **create_kwargs)
    data = common.payload_data(context, payload)
    file_id = common.field_text(context, data, "fileId")
    if data.get("exist") is True:
        if plan["overwritePolicy"] == "reject":
            raise result_module.ExecutionError(result_module.CONFLICT, "目标目录已有同名文件")
        if data.get("rapidUpload") is not True:
            if plan["overwritePolicy"] != "dedupe":
                raise result_module.ExecutionError(
                    result_module.CONFLICT, "平台返回已存在文件，未建立新上传任务；请核查目标文件"
                )
            # 已存在不是上传任务：不能再读取 uploadId、传分片或调用 complete。
            # 只有详情证明内容一致时才复用，不能把同名同大小当作内容相同。
            verified = _verify(context, file_id, plan, rapid=False, existing=True)
            context.note_step("verify", "已核验内容并复用目标目录中的已有文件")
            return verified
    context.note_step("create", "已创建上传任务")
    if data.get("rapidUpload") is True:
        context.note_step("upload", "命中秒传，跳过分片传输")
        return _verify(context, file_id, plan, rapid=True)
    upload_id = common.field_text(context, data, "uploadId")
    form_info = data.get("formInfo")
    if isinstance(form_info, dict):
        _form_upload(context, form_info, plan)
    else:
        response_parts = common.list_field(context, data, "partInfos")
        _multipart_upload(context, file_id, upload_id, response_parts, plan, parts)
    common.guard_upstream(
        context,
        file_complete.call,
        file_id=file_id,
        upload_id=upload_id,
        content_hash=plan["contentHash"],
        content_hash_algorithm=plan["hashAlgorithm"],
    )
    context.note_step("complete", "已完成上传")
    return _verify(context, file_id, plan, rapid=False)


def _assert_local_file_unchanged(context, plan):
    """复核冻结的本地文件未被替换；路径范围与摘要都重新核对一次。

    摘要必须用计划里冻结的算法重算：摘要是散列值，换算法算出来的东西不可比。
    """
    current = common.local_file_info(
        context, plan["localFile"], algorithms=(plan["hashAlgorithm"],)
    )
    if current["size"] != plan["size"] or current[plan["hashAlgorithm"]] != plan["contentHash"]:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "本地文件在计划冻结后发生变化，已中止"
        )
    return current


def _plan_parts(size):
    if size <= 0:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "空文件不支持分片上传流程")
    count = max(1, min(MAX_PARTS_PER_CREATE, MAX_PART_NUMBER, (size + MIN_PART_SIZE - 1) // MIN_PART_SIZE))
    base = max(MIN_PART_SIZE, -(-size // count))
    parts = []
    offset = 0
    number = 1
    while offset < size and len(parts) < MAX_PART_NUMBER:
        part_size = min(base, size - offset)
        parts.append({"partNumber": number, "partSize": part_size})
        offset += part_size
        number += 1
    return parts


def _multipart_upload(context, file_id, upload_id, response_parts, plan, planned_parts):
    # 首轮不查已上传分片：本次会话刚由 file/create 建立，平台鉴权层还不认这个未完成上传的 fileId
    # （file/get、file/getUploadUrl、file/listUploadedParts 均返回 16110039 鉴权资源路径不可用），
    # 且 create 响应已给出全部分片地址（分片数恒 <= 100）。将来做同 uploadId 断点续传时再引入查询。
    uploaded = set()
    url_by_number = {}
    for item in response_parts:
        number = item.get("partNumber")
        url = item.get("uploadUrl")
        if isinstance(number, int) and isinstance(url, str) and url:
            url_by_number[number] = url
    with open(plan["localFile"], "rb") as handle:
        offset = 0
        for part in planned_parts:
            number = part["partNumber"]
            part_size = part["partSize"]
            if number not in uploaded:
                url = url_by_number.get(number)
                if not url:
                    url = _fetch_part_url(context, file_id, upload_id, part)
                handle.seek(offset)
                blob = handle.read(part_size)
                if len(blob) != part_size:
                    raise result_module.ExecutionError(
                        result_module.INVALID_ARGUMENT, "本地文件在计划冻结后发生变化，已中止"
                    )
                common.guard_upstream(
                    context,
                    file_multipart_upload.call,
                    upload_url=url,
                    data=blob,
                    content_length=part_size,
                )
            offset += part_size
    context.note_step("upload", "分片传输完成，共 %d 片" % len(planned_parts))


def _fetch_part_url(context, file_id, upload_id, part):
    url_payload = common.guard_upstream(
        context,
        file_get_upload_url.call,
        file_id=file_id,
        upload_id=upload_id,
        part_infos=[{"partNumber": part["partNumber"], "partSize": part["partSize"]}],
    )
    data = common.payload_data(context, url_payload)
    for item in common.list_field(context, data, "partInfos"):
        if item.get("partNumber") == part["partNumber"] and isinstance(item.get("uploadUrl"), str):
            return item["uploadUrl"]
    common.contract_mismatch(context, "未能取得分片上传地址")


def _form_upload(context, form_info, plan):
    common.guard_upstream(
        context,
        file_form_upload.call,
        upload_url=common.field_text(context, form_info, "uploadUrl"),
        data_field=common.field_text(context, form_info, "dataField"),
        data=open(plan["localFile"], "rb").read(),
        form_data=form_info.get("formData"),
        content_length=plan["size"],
    )


def _verify(context, file_id, plan, *, rapid, existing=False):
    payload = common.guard_upstream(context, file_get.call, file_id=file_id)
    data = common.payload_data(context, payload)
    if existing:
        known_match = (
            data.get("fileId") == file_id
            and data.get("parentFileId") == plan["resolvedIds"]["parentFileId"]
            and type(data.get("size")) is int and data["size"] == plan["size"]
            and isinstance(data.get("contentHash"), str)
            and data["contentHash"].lower() == plan["contentHash"].lower()
            and isinstance(data.get("contentHashAlgorithm"), str)
            and data["contentHashAlgorithm"].lower() == plan["hashAlgorithm"].lower()
        )
        if not known_match:
            raise result_module.ExecutionError(
                result_module.CONFLICT,
                "目标已有文件，但无法核实与本地内容一致；请使用 rename 策略另存，不会覆盖原文件",
                retryable=False,
            )
    detail_id = data.get("fileId")
    if isinstance(detail_id, str) and detail_id and detail_id != file_id:
        common.contract_mismatch(context, "上传结果的文件标识与本次创建不一致")
    name = common.field_text(context, data, "name")
    if data.get("type") != "file":
        common.contract_mismatch(context, "上传结果不是文件类型")
    parent_id = data.get("parentFileId")
    if isinstance(parent_id, str) and parent_id and parent_id != plan["resolvedIds"]["parentFileId"]:
        raise result_module.ExecutionError(result_module.SCOPE_DENIED, "上传结果不在冻结的目标目录内")
    size = data.get("size")
    if isinstance(size, int) and not isinstance(size, bool) and size != plan["size"]:
        common.contract_mismatch(context, "上传后的文件大小与本地文件不一致")
    # 名称不参与契约判定：rename(force_rename)/dedupe(auto_rename) 策略下服务端会按规则改名
    # （如 哈哈哈(1).txt），这是策略的预期结果，不是契约不一致。契约以本次创建的 fileId +
    # 冻结目标目录 + 本地大小为准；实际名只作为结果输出，并标记是否发生过改名。
    renamed = name != plan["fileName"]
    scope = common.SCOPE_AUTHORIZED_PERSONAL
    return common.success(
        data={
            "fileRef": context.issue_ref("file", file_id, scope),
            "name": name,
            "size": plan["size"],
            "rapidUpload": bool(rapid),
            "localFile": os.path.basename(plan["localFile"]),
            "renamed": renamed,
            "renamedFrom": plan["fileName"] if renamed else None,
            **({"reused": True} if existing else {}),
        },
        counts={"uploaded": 0, "reused": 1} if existing else {"uploaded": 1},
    )
