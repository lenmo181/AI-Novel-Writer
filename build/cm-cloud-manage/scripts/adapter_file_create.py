import datetime
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import core_control_http
from collections.abc import Mapping

from cmcloud_core import platform_config, upstream_trace

_PATH = "/richlifeApp/idaasV3/personalSaas/file/create"
_TIMEOUT_SECONDS = 15

_HASH_PATTERN_SHA1 = re.compile(r"\A[0-9a-fA-F]{40}\Z")
_HASH_PATTERN_SHA256 = re.compile(r"\A[0-9a-fA-F]{64}\Z")
_MD5_PATTERN = re.compile(r"\A[0-9a-fA-F]{32}\Z")
_RFC3339_ZONE_PATTERN = re.compile(r"([Zz]|[+-]\d{2}:\d{2})\Z")
_FORBIDDEN_NAME_CHARS = '\\/:*?"<>|'
_RENAME_MODES = ("force_rename", "refuse", "auto_rename")
_MIN_PART_SIZE = 5 * 1024 * 1024
_FORM_UPLOAD_MAX_SIZE = 5 * 1024 * 1024 * 1024
_PARENT_PATH_MAX_LEVELS = 29
_PART_KEYS = {
    "part_number": "partNumber",
    "partNumber": "partNumber",
    "part_size": "partSize",
    "partSize": "partSize",
    "part_offset": "partOffset",
    "partOffset": "partOffset",
    "parallel_hash_ctx": "parallelHashCtx",
    "parallelHashCtx": "parallelHashCtx",
}
_PARALLEL_CTX_KEYS = {
    "hash_ctx": "hashCtx",
    "hashCtx": "hashCtx",
    "part_offset": "partOffset",
    "partOffset": "partOffset",
}
_HASH_CTX_LENGTH = 8
_TAG_KEYS = {
    "story_file_upload": "storyFileUpload",
    "storyFileUpload": "storyFileUpload",
    "file_source": "fileSource",
    "fileSource": "fileSource",
    "file_channel": "fileChannel",
    "fileChannel": "fileChannel",
}
_TAG_ENUMS = {
    "storyFileUpload": ("story_video_file", "album_video_file"),
    "fileSource": ("short_movie_resources",),
    "fileChannel": ("migu_short_video",),
}


class FileCreateError(RuntimeError):
    def __init__(self, message, *, code=None, http_status=None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = core_control_http.build_opener(
    _NoRedirectHandler,
    urllib.request.HTTPSHandler(context=ssl.create_default_context()),
)


def call(
    *,
    name,
    size,
    request_headers,
    parent_file_id=None,
    parent_path=None,
    part_infos=None,
    file_rename_mode=None,
    file_id=None,
    content_hash=None,
    content_hash_algorithm=None,
    description=None,
    pre_hash=None,
    proof_code=None,
    proof_version=None,
    local_created_at=None,
    local_updated_at=None,
    parallel_upload=None,
    form_upload=None,
    tag_info=None,
    content_md5=None,
):
    # §1.1 字段表：parentFileId 与 parentPath 互斥，二者必须且只能提供一个；
    # parentPath 目标不存在时平台会自动创建目录，是否允许由上层按获准目录范围决定
    if (parent_file_id is None) == (parent_path is None):
        raise FileCreateError("parent_file_id 与 parent_path 必须二选一")
    _require_name(name)
    _require_size(size)
    if parent_file_id is not None:
        _require_id(parent_file_id, "parent_file_id")
    else:
        _require_parent_path(parent_path)
    for key, value in (("parallel_upload", parallel_upload), ("form_upload", form_upload)):
        if value is not None and not isinstance(value, bool):
            raise FileCreateError("%s 必须是布尔值" % key)
    if parallel_upload is True and form_upload is True:
        # 文档：并行上传与表单上传互斥
        raise FileCreateError("parallel_upload 与 form_upload 互斥")
    if form_upload is True:
        # 文档：表单上传最大支持单文件 5GB；partInfos 为非表单场景分片信息
        if size > _FORM_UPLOAD_MAX_SIZE:
            raise FileCreateError("表单上传单文件不得超过 5GB")
        if part_infos is not None:
            raise FileCreateError("表单上传不使用 part_infos")
    elif part_infos is None:
        # 文档：分片信息为“非表单场景必填（单文件上传采用单分片上传机制，也必填）”
        raise FileCreateError("非表单场景必须提供 part_infos")
    if file_rename_mode is not None and file_rename_mode not in _RENAME_MODES:
        raise FileCreateError("file_rename_mode 仅支持 force_rename/refuse/auto_rename")
    if file_id is not None:
        _require_id(file_id, "file_id")
    if (content_hash is None) != (content_hash_algorithm is None):
        raise FileCreateError("content_hash 与 content_hash_algorithm 必须同时提供")
    if content_hash is not None:
        content_hash_algorithm = _require_content_hash(content_hash, content_hash_algorithm)
    elif file_rename_mode == "auto_rename":
        # 文档：auto_rename 同名处理模式下 contentHash/contentHashAlgorithm 必填
        raise FileCreateError("auto_rename 必须同时提供 content_hash 与 content_hash_algorithm")
    if description is not None:
        _require_description(description)
    if pre_hash is not None:
        _require_text(pre_hash, "pre_hash")
    if proof_code is not None:
        _require_text_len(proof_code, "proof_code", 64)
    if proof_version is not None and proof_version != "v1":
        # 文档：秒传存储证明算法版本当前只支持 v1
        raise FileCreateError("proof_version 当前仅支持 v1")
    if local_created_at is not None:
        _require_local_time(local_created_at, "local_created_at")
    if local_updated_at is not None:
        _require_local_time(local_updated_at, "local_updated_at")
    if content_md5 is not None and (
        not isinstance(content_md5, str) or _MD5_PATTERN.fullmatch(content_md5) is None
    ):
        # 文档：32 位 16 进制，不区分大小写；仅已迁移用户等场景使用，adapter 只校验与透传
        raise FileCreateError("content_md5 必须是 32 位十六进制 MD5 摘要")
    tags = _normalize_tag_info(tag_info) if tag_info is not None else None
    # 文档 createV2 字段表：type 必选（枚举 file/folder）；本 adapter 只负责文件创建，
    # 目录创建走 createFolder，因此这里固定 file。
    body = {"name": name, "type": "file", "size": size}
    if parent_file_id is not None:
        body["parentFileId"] = parent_file_id
    else:
        body["parentPath"] = parent_path
    if description is not None:
        body["description"] = description
    if part_infos is not None:
        body["partInfos"] = _normalize_parts(size, part_infos, parallel=parallel_upload is True)
    if file_rename_mode is not None:
        body["fileRenameMode"] = file_rename_mode
    if file_id is not None:
        body["fileId"] = file_id
    if content_hash is not None:
        body["contentHash"] = content_hash
        body["contentHashAlgorithm"] = content_hash_algorithm
    if pre_hash is not None:
        body["preHash"] = pre_hash
    if proof_code is not None:
        body["proofCode"] = proof_code
    if proof_version is not None:
        body["proofVersion"] = proof_version
    if local_created_at is not None:
        body["localCreatedAt"] = local_created_at
    if local_updated_at is not None:
        body["localUpdatedAt"] = local_updated_at
    if parallel_upload is not None:
        body["parallelUpload"] = parallel_upload
    if form_upload is not None:
        body["formUpload"] = form_upload
    if tags is not None:
        body["tagInfo"] = tags
    if content_md5 is not None:
        body["contentMd5"] = content_md5
    payload = _post(body, request_headers)
    data = payload.get("data")
    _validate_data(data)
    return payload


def _normalize_parts(file_size, part_infos, *, parallel):
    if isinstance(part_infos, (str, bytes)) or not isinstance(part_infos, (list, tuple)):
        raise FileCreateError("part_infos 必须是列表")
    if not 1 <= len(part_infos) <= 100:
        raise FileCreateError("part_infos 单次最多 100 项，超出部分的上传地址由 getUploadUrl 接口补取")
    parts = []
    for index, item in enumerate(part_infos):
        if not isinstance(item, dict):
            raise FileCreateError("part_infos 每项必须是对象")
        part = {}
        for key, value in item.items():
            target = _PART_KEYS.get(key)
            if target is None:
                raise FileCreateError("part_infos 含未支持字段: %s" % key)
            if value is not None:
                part[target] = value
        number = part.get("partNumber")
        if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= 10000:
            raise FileCreateError("partNumber 必须是 1~10000 的整数")
        part_size = part.get("partSize")
        if isinstance(part_size, bool) or not isinstance(part_size, int) or part_size <= 0:
            raise FileCreateError("partSize 必须是正整数")
        has_offset = "partOffset" in part
        has_context = "parallelHashCtx" in part
        if parallel:
            # 文档：并行上传时第一个分片仅需 partNumber/partSize，其余分片还需 partOffset、parallelHashCtx
            if index == 0:
                if has_offset or has_context:
                    raise FileCreateError("并行上传首片不携带 partOffset/parallelHashCtx")
            elif not (has_offset and has_context):
                raise FileCreateError("并行上传非首片必须提供 partOffset 与 parallelHashCtx")
        elif has_offset or has_context:
            # 文档：串行上传时 PartInfo 仅需填写 partNumber、partSize
            raise FileCreateError("串行上传的 partInfos 仅支持 partNumber/partSize")
        if has_offset:
            offset = part["partOffset"]
            if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
                raise FileCreateError("partOffset 必须是非负整数")
        if has_context:
            part["parallelHashCtx"] = _normalize_parallel_ctx(part["parallelHashCtx"])
        parts.append(part)
    total = 0
    expected_number = 1
    for part in parts:
        if part["partNumber"] != expected_number:
            # 编号不连续时无法可靠判断哪一片是文件最后一片，拒绝而不是猜测
            raise FileCreateError("partNumber 必须从 1 开始连续递增")
        expected_number += 1
        total += part["partSize"]
    if total > file_size:
        raise FileCreateError("part_infos 分片大小之和超过文件 size")
    if total < file_size:
        # 本次提交只是文件的一部分，每片都属于非最终片
        for part in parts:
            if part["partSize"] < _MIN_PART_SIZE:
                raise FileCreateError("非最后分片的大小不得小于 5MiB")
    else:
        # 仅当分片之和恰好覆盖整个文件时，最后一片才允许小于 5MiB
        for part in parts[:-1]:
            if part["partSize"] < _MIN_PART_SIZE:
                raise FileCreateError("非最后分片的大小不得小于 5MiB")
    return parts


def _normalize_parallel_ctx(value):
    if not isinstance(value, dict) or not value:
        raise FileCreateError("parallelHashCtx 必须是非空对象")
    context = {}
    for key, item in value.items():
        target = _PARALLEL_CTX_KEYS.get(key)
        if target is None:
            raise FileCreateError("parallelHashCtx 含未支持字段: %s" % key)
        if item is None:
            continue
        if target == "hashCtx":
            if isinstance(item, (str, bytes)) or not isinstance(item, (list, tuple)) or len(item) != _HASH_CTX_LENGTH:
                raise FileCreateError("parallelHashCtx.hashCtx 必须是 %d 个整数的数组" % _HASH_CTX_LENGTH)
            numbers = []
            for entry in item:
                if isinstance(entry, bool) or not isinstance(entry, int):
                    raise FileCreateError("parallelHashCtx.hashCtx 的元素必须是整数")
                numbers.append(entry)
            context["hashCtx"] = numbers
        else:
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise FileCreateError("parallelHashCtx.partOffset 必须是非负整数")
            if item % 64 != 0:
                raise FileCreateError("parallelHashCtx.partOffset 必须是 64 的倍数")
            context["partOffset"] = item
    return context


def _normalize_tag_info(value):
    if not isinstance(value, dict):
        raise FileCreateError("tag_info 必须是对象")
    tags = {}
    for key, item in value.items():
        target = _TAG_KEYS.get(key)
        if target is None:
            raise FileCreateError("tag_info 含未支持字段: %s" % key)
        if isinstance(item, (str, bytes)) or not isinstance(item, (list, tuple)):
            raise FileCreateError("tag_info.%s 必须是列表" % target)
        if len(item) != 1:
            # 文档：当前仅支持一个标签值
            raise FileCreateError("tag_info.%s 当前仅支持一个标签值" % target)
        tag_value = item[0]
        if not isinstance(tag_value, str) or tag_value not in _TAG_ENUMS[target]:
            raise FileCreateError("tag_info.%s 取值不在平台枚举内" % target)
        tags[target] = [tag_value]
    return tags


def _validate_data(data):
    if not isinstance(data, dict):
        raise FileCreateError("响应缺少业务 data 对象")
    for key in ("parentFileId", "fileId", "fileName"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise FileCreateError("响应缺少字符串字段: %s" % key)
    if data.get("type") != "file":
        raise FileCreateError("响应 type 必须为 file")
    if "exist" in data and _as_bool(data["exist"]) is None:
        raise FileCreateError("响应 exist 类型非法")
    rapid_upload = _as_bool(data.get("rapidUpload"))
    if rapid_upload is None:
        raise FileCreateError("响应缺少布尔字段 rapidUpload")
    # 工作流消费统一布尔值，避免合法的 "true" 被误当成普通上传分支。
    data["rapidUpload"] = rapid_upload
    if "exist" in data:
        data["exist"] = _as_bool(data["exist"])
    if rapid_upload:
        # 秒传不需要继续分片上传，uploadId/partInfos 允许为空
        return
    if _as_bool(data.get("exist")) is True:
        # renameMode=refuse/auto_rename 命中同名文件时不会创建上传任务，不要求 uploadId/partInfos
        return
    form_info = data.get("formInfo")
    if form_info is not None:
        # 文档：formInfo 仅表单上传方式返回
        _validate_form_info(form_info)
        return
    if not isinstance(data.get("uploadId"), str) or not data["uploadId"]:
        raise FileCreateError("分片上传响应缺少 uploadId")
    parts = data.get("partInfos")
    if not isinstance(parts, list) or not parts:
        raise FileCreateError("分片上传响应缺少 partInfos")
    for item in parts:
        if not isinstance(item, dict) or isinstance(item.get("partNumber"), bool) or not isinstance(item.get("partNumber"), int):
            raise FileCreateError("响应 partInfos 项缺少整数 partNumber")
        if not isinstance(item.get("uploadUrl"), str) or not item["uploadUrl"]:
            raise FileCreateError("响应 partInfos 项缺少 uploadUrl")
    # partInfos[].uploadUrl 为敏感预签名地址，仅限调用方内部上传使用，禁止输出到日志或 Agent


def _validate_form_info(value):
    if not isinstance(value, dict):
        raise FileCreateError("响应 formInfo 必须是对象")
    for key in ("uploadUrl", "dataField"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise FileCreateError("响应 formInfo 缺少字符串字段: %s" % key)
    form_data = value.get("formData")
    if form_data is not None:
        if not isinstance(form_data, dict):
            raise FileCreateError("响应 formInfo.formData 必须是对象")
        for key, item in form_data.items():
            if not isinstance(key, str) or not isinstance(item, str):
                raise FileCreateError("响应 formInfo.formData 必须是字符串映射")


def _require_id(value, name):
    if not isinstance(value, str) or not value or len(value) > 64:
        raise FileCreateError("%s 必须是长度不超过 64 的非空字符串" % name)


def _require_name(value):
    if not isinstance(value, str) or not value:
        raise FileCreateError("name 必须是非空字符串")
    if len(value.encode("utf-8")) > 255:
        raise FileCreateError("name 的 UTF-8 长度不得超过 255 字节")
    if any(ch in value for ch in _FORBIDDEN_NAME_CHARS):
        raise FileCreateError("name 含平台禁止的非法字符")


def _require_size(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FileCreateError("size 必须是非负整数字节数")


def _require_parent_path(value):
    if not isinstance(value, str) or not value:
        raise FileCreateError("parent_path 必须是非空字符串")
    if not value.startswith("/"):
        raise FileCreateError("parent_path 必须以 / 开头")
    if value != "/" and value.endswith("/"):
        raise FileCreateError("parent_path 不得以 / 结尾")
    if "//" in value:
        raise FileCreateError("parent_path 不得包含空层级")
    if len([segment for segment in value.split("/") if segment]) > _PARENT_PATH_MAX_LEVELS:
        raise FileCreateError("parent_path 最大 %d 层级" % _PARENT_PATH_MAX_LEVELS)


def _require_content_hash(value, algorithm):
    if not isinstance(algorithm, str) or algorithm.strip().lower() not in ("sha1", "sha256"):
        # 文档：当前仅支持 sha1 或者 sha256，不区分大小写；pds 底座仅支持 sha1、云空间底座仅支持 sha256
        raise FileCreateError("content_hash_algorithm 当前仅支持 sha1/sha256")
    normalized = algorithm.strip().lower()
    if normalized == "sha1":
        if not isinstance(value, str) or _HASH_PATTERN_SHA1.fullmatch(value) is None:
            raise FileCreateError("content_hash 必须是 40 位十六进制 sha1 摘要")
    elif not isinstance(value, str) or _HASH_PATTERN_SHA256.fullmatch(value) is None:
        raise FileCreateError("content_hash 必须是 64 位十六进制 sha256 摘要")
    return normalized


def _require_description(value):
    if not isinstance(value, str):
        raise FileCreateError("description 必须是字符串")
    if len(value) > 1024:
        raise FileCreateError("description 不得超过 1024 字符")


def _require_text(value, name):
    if not isinstance(value, str) or not value:
        raise FileCreateError("%s 必须是非空字符串" % name)


def _require_text_len(value, name, limit):
    _require_text(value, name)
    if len(value) > limit:
        raise FileCreateError("%s 不得超过 %d 字符" % (name, limit))


def _require_local_time(value, name):
    if not isinstance(value, str):
        raise FileCreateError("%s 必须是 RFC 3339 时间字符串" % name)
    match = _RFC3339_ZONE_PATTERN.search(value)
    if match is None or match.group(1) not in ("Z", "z", "+00:00", "-00:00", "+08:00"):
        # 文档：使用 RFC 3339（零时区或东八时区）
        raise FileCreateError("%s 必须是零时区或东八时区的 RFC 3339 时间" % name)
    normalized = value.replace("t", "T").replace("z", "+00:00").replace("Z", "+00:00")
    try:
        datetime.datetime.fromisoformat(normalized)
    except ValueError:
        raise FileCreateError("%s 不是合法的 RFC 3339 时间" % name)


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1"):
            return True
        if text in ("false", "0"):
            return False
    return None


def _require_headers(value):
    if not isinstance(value, Mapping):
        raise FileCreateError("request_headers 必须是映射")
    headers = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise FileCreateError("request_headers 的键必须是非空字符串")
        if item is None:
            continue
        if not isinstance(item, str):
            raise FileCreateError("request_headers 的值必须是字符串")
        headers[key] = item
    return headers


def _build_url():
    config = platform_config.load_platform_config()
    base = config.require("personal_saas_base_url").rstrip("/")
    url = base + _PATH
    parsed = urllib.parse.urlsplit(url)
    base_parsed = urllib.parse.urlsplit(base)
    # 地址来自 EXE 旁 cmcloud.json 的受批准配置，不接受任何调用参数覆盖
    if parsed.scheme != "https" or parsed.netloc != base_parsed.netloc:
        raise FileCreateError("请求地址校验失败")
    return url


def _failure_message(code, message):
    return "平台返回失败(code=%s): %s" % (code, str(message) if message is not None else "无平台消息")


def _validate_response(payload):
    success = payload.get("success")
    code = payload.get("code")
    message = payload.get("message")
    if success is None and code is None:
        raise FileCreateError("平台响应缺少 success 与 code，不能判定为成功")
    if success is not None and str(success).strip().lower() not in ("true", "1"):
        raise FileCreateError(_failure_message(code, message), code=code)
    if code is not None and str(code).strip().lower() not in ("0000", "0"):
        raise FileCreateError(_failure_message(code, message), code=code)
    return payload


def _post(body, request_headers):
    url = _build_url()
    payload_bytes = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}
    headers.update(_require_headers(request_headers))
    request = urllib.request.Request(url, data=payload_bytes, headers=headers, method="POST")
    try:
        response = _OPENER.open(request, timeout=_TIMEOUT_SECONDS)
        try:
            status = response.getcode()
            trace_id = upstream_trace.read(response.headers)
            raw = response.read()
        finally:
            response.close()
    except urllib.error.HTTPError as err:
        code = message = None
        try:
            failed = json.loads(err.read().decode("utf-8"))
            if isinstance(failed, dict):
                code = failed.get("code")
                message = failed.get("message")
        except (UnicodeDecodeError, ValueError):
            pass
        raise FileCreateError(
            "HTTP 状态码 %s: %s" % (err.code, str(message) if message is not None else "无平台消息"),
            code=code,
            http_status=err.code,
        )
    except OSError as err:
        raise FileCreateError("网络请求失败: %s" % (getattr(err, "reason", None) or err))
    if not isinstance(status, int) or not 200 <= status < 300:
        raise FileCreateError("HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise FileCreateError("平台响应不是合法 UTF-8 JSON")
    if not isinstance(payload, dict):
        raise FileCreateError("平台响应不是 JSON 对象")
    return _validate_response(upstream_trace.attach(payload, trace_id))
