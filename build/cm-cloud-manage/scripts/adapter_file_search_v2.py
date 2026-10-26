# 本期场景范围外：整体注释停用，保留原实现供后续明确纳入范围时恢复。
# 不注册路由、不定义调用入口、不发起网络请求。
#
# import json
# import ssl
# import urllib.error
# import urllib.parse
# import urllib.request
# from collections.abc import Mapping
#
# from cmcloud_core import platform_config, upstream_trace
#
# # 正式调用路径为用户已确认的单斜杠；接口文档原文写作双斜杠，联调核实，不做双斜杠回退
# _PATH = "/richlifeApp/idaasV3/personalSaas/file/searchV2"
# _TIMEOUT_SECONDS = 15
#
# _CATEGORIES = ("app", "zip", "image", "doc", "video", "audio", "folder", "others")
# _ORDER_BY = ("created_at", "updated_at", "size", "name", "taken_at", "category")
# _ORDER_DIRECTIONS = ("ASC", "DESC")
# _THUMBNAIL_STYLES = ("Small", "Middle", "Big", "Large")
# _TAG_VALUES = {
#     "storyFileUpload": ("story_video_file", "album_video_file"),
#     "fileSource": ("short_movie_resources",),
#     "fileChannel": ("migu_short_video",),
# }
# _FILE_LIST_KEYS = ("fileIdList", "dirIdList", "dirPathList")
# _FILE_LIST_LIMIT = 29
#
#
# class FileSearchV2Error(RuntimeError):
#     def __init__(self, message, *, code=None, http_status=None):
#         super().__init__(message)
#         self.code = code
#         self.http_status = http_status
#
#
# class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
#     def redirect_request(self, req, fp, code, msg, headers, newurl):
#         return None
#
#
# _OPENER = urllib.request.build_opener(
#     _NoRedirectHandler,
#     urllib.request.HTTPSHandler(context=ssl.create_default_context()),
# )
#
#
# def call(
#     *,
#     parent_file_id,
#     request_headers,
#     recursive=None,
#     category_list=None,
#     tag_info=None,
#     create_time_range=None,
#     duration_range=None,
#     size_range=None,
#     extension_list=None,
#     include_file_list=None,
#     exclude_file_list=None,
#     order_by=None,
#     order_direction=None,
#     image_thumbnail_style_list=None,
#     page_info=None,
#     is_need_special_dir=None,
# ):
#     _require_id(parent_file_id, "parent_file_id")
#     if recursive is not None and not isinstance(recursive, bool):
#         raise FileSearchV2Error("recursive 必须是布尔值")
#     if is_need_special_dir is not None and not isinstance(is_need_special_dir, bool):
#         raise FileSearchV2Error("is_need_special_dir 必须是布尔值")
#     if include_file_list is not None and exclude_file_list is not None:
#         raise FileSearchV2Error("include_file_list 与 exclude_file_list 互斥，不能同时指定")
#     if (include_file_list is not None or exclude_file_list is not None) and recursive is not True:
#         raise FileSearchV2Error("include_file_list/exclude_file_list 仅 recursive=true 时可用")
#     body = {"parentFileId": parent_file_id}
#     if recursive is not None:
#         body["recursive"] = recursive
#     if category_list is not None:
#         body["categoryList"] = _enum_list(category_list, _CATEGORIES, "category_list")
#     if tag_info is not None:
#         body["tagInfo"] = _normalize_tag_info(tag_info)
#     if create_time_range is not None:
#         body["createTimeRange"] = _normalize_range(create_time_range, "create_time_range")
#     if duration_range is not None:
#         body["durationRange"] = _normalize_range(duration_range, "duration_range")
#     if size_range is not None:
#         body["sizeRange"] = _normalize_range(size_range, "size_range")
#     if extension_list is not None:
#         body["extensionList"] = _string_list(extension_list, "extension_list")
#     if include_file_list is not None:
#         body["includeFileList"] = _normalize_file_list(include_file_list, "include_file_list")
#     if exclude_file_list is not None:
#         body["excludeFileList"] = _normalize_file_list(exclude_file_list, "exclude_file_list")
#     if order_by is not None:
#         if order_by not in _ORDER_BY:
#             raise FileSearchV2Error("order_by 取值不在平台枚举内")
#         body["orderBy"] = order_by
#     if order_direction is not None:
#         if not isinstance(order_direction, str) or order_direction.upper() not in _ORDER_DIRECTIONS:
#             raise FileSearchV2Error("order_direction 仅支持 ASC/DESC")
#         body["orderDirection"] = order_direction.upper()
#     if image_thumbnail_style_list is not None:
#         styles = _enum_list(image_thumbnail_style_list, _THUMBNAIL_STYLES, "image_thumbnail_style_list")
#         if styles:
#             body["imageThumbnailStyleList"] = styles
#     if page_info is not None:
#         page = _normalize_page_info(page_info)
#         if page:
#             body["pageInfo"] = page
#     if is_need_special_dir is not None:
#         body["isNeedSpecialDir"] = is_need_special_dir
#     payload = _post(body, request_headers)
#     data = payload.get("data")
#     if not isinstance(data, dict):
#         raise FileSearchV2Error("响应缺少业务 data 对象")
#     items = data.get("items")
#     if not isinstance(items, list):
#         raise FileSearchV2Error("响应缺少 items 列表")
#     for item in items:
#         if not isinstance(item, dict):
#             raise FileSearchV2Error("items 每项必须是对象")
#     cursor = data.get("nextPageCursor")
#     if cursor is not None and not isinstance(cursor, str):
#         raise FileSearchV2Error("响应 nextPageCursor 类型非法")
#     # items 为空但游标非空仍只是一页结果，不代表搜索完成；翻页、关键词过滤与总数推断由调用方负责
#     return payload
#
#
# def _enum_list(value, allowed, name):
#     if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
#         raise FileSearchV2Error("%s 必须是列表" % name)
#     result = []
#     for item in value:
#         if item not in allowed:
#             raise FileSearchV2Error("%s 含不支持的取值: %s" % (name, item))
#         result.append(item)
#     return result
#
#
# def _string_list(value, name):
#     if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
#         raise FileSearchV2Error("%s 必须是列表" % name)
#     result = []
#     for item in value:
#         if not isinstance(item, str) or not item:
#             raise FileSearchV2Error("%s 每项必须是非空字符串" % name)
#         result.append(item)
#     return result
#
#
# def _normalize_range(value, name):
#     if not isinstance(value, dict):
#         raise FileSearchV2Error("%s 必须是对象" % name)
#     result = {}
#     for key, item in value.items():
#         if key not in ("start", "end"):
#             raise FileSearchV2Error("%s 含未支持字段: %s" % (name, key))
#         if item is None:
#             continue
#         if isinstance(item, bool) or not isinstance(item, int) or item < 0:
#             raise FileSearchV2Error("%s.%s 必须是非负整数" % (name, key))
#         result[key] = item
#     if not result:
#         raise FileSearchV2Error("%s 至少需要 start 或 end 一端" % name)
#     if "start" in result and "end" in result and result["start"] >= result["end"]:
#         raise FileSearchV2Error("%s 需满足 start < end" % name)
#     return result
#
#
# def _normalize_file_list(value, name):
#     if not isinstance(value, dict):
#         raise FileSearchV2Error("%s 必须是对象" % name)
#     result = {}
#     for key, item in value.items():
#         if key not in _FILE_LIST_KEYS:
#             raise FileSearchV2Error("%s 含未支持字段: %s" % (name, key))
#         if item is None:
#             continue
#         if isinstance(item, (str, bytes)) or not isinstance(item, (list, tuple)):
#             raise FileSearchV2Error("%s.%s 必须是列表" % (name, key))
#         if not 1 <= len(item) <= _FILE_LIST_LIMIT:
#             raise FileSearchV2Error("%s.%s 为 1~%d 项" % (name, key, _FILE_LIST_LIMIT))
#         entries = []
#         for entry in item:
#             if not isinstance(entry, str) or not entry:
#                 raise FileSearchV2Error("%s.%s 每项必须是非空字符串" % (name, key))
#             entries.append(entry)
#         result[key] = entries
#     if not result:
#         raise FileSearchV2Error("%s 至少需要指定一个列表" % name)
#     # 目录列表合计上限：dirIdList 与 dirPathList 之和不得超过 29 项
#     if len(result.get("dirIdList", ())) + len(result.get("dirPathList", ())) > _FILE_LIST_LIMIT:
#         raise FileSearchV2Error("%s 的 dirIdList 与 dirPathList 合计不得超过 %d 项" % (name, _FILE_LIST_LIMIT))
#     return result
#
#
# def _normalize_tag_info(value):
#     if not isinstance(value, dict):
#         raise FileSearchV2Error("tag_info 必须是对象")
#     result = {}
#     for key, item in value.items():
#         allowed = _TAG_VALUES.get(key)
#         if allowed is None:
#             raise FileSearchV2Error("tag_info 含未支持字段: %s" % key)
#         if item is None:
#             continue
#         if isinstance(item, (str, bytes)) or not isinstance(item, (list, tuple)) or len(item) != 1:
#             raise FileSearchV2Error("tag_info.%s 当前仅支持一个标签值" % key)
#         if item[0] not in allowed:
#             raise FileSearchV2Error("tag_info.%s 含不支持的取值" % key)
#         result[key] = [item[0]]
#     if not result:
#         raise FileSearchV2Error("tag_info 至少需要指定一个标签")
#     return result
#
#
# def _normalize_page_info(page_info):
#     if not isinstance(page_info, dict):
#         raise FileSearchV2Error("page_info 必须是对象")
#     normalized = {}
#     for key, value in page_info.items():
#         if key in ("page_size", "pageSize"):
#             target = "pageSize"
#         elif key in ("page_cursor", "pageCursor"):
#             target = "pageCursor"
#         else:
#             raise FileSearchV2Error("page_info 含未支持字段: %s" % key)
#         if value is None:
#             continue
#         if target == "pageSize":
#             if isinstance(value, bool) or not isinstance(value, int) or not 10 <= value <= 100:
#                 raise FileSearchV2Error("page_info.page_size 必须是 10~100 的整数")
#         elif not isinstance(value, str) or not value:
#             raise FileSearchV2Error("page_info.page_cursor 必须是非空字符串")
#         normalized[target] = value
#     return normalized or None
#
#
# def _require_id(value, name):
#     if not isinstance(value, str) or not value or len(value) > 64:
#         raise FileSearchV2Error("%s 必须是长度不超过 64 的非空字符串" % name)
#
#
# def _require_headers(value):
#     if not isinstance(value, Mapping):
#         raise FileSearchV2Error("request_headers 必须是映射")
#     headers = {}
#     for key, item in value.items():
#         if not isinstance(key, str) or not key:
#             raise FileSearchV2Error("request_headers 的键必须是非空字符串")
#         if item is None:
#             continue
#         if not isinstance(item, str):
#             raise FileSearchV2Error("request_headers 的值必须是字符串")
#         headers[key] = item
#     return headers
#
#
# def _build_url():
#     config = platform_config.load_platform_config()
#     base = config.require("personal_saas_base_url").rstrip("/")
#     url = base + _PATH
#     parsed = urllib.parse.urlsplit(url)
#     base_parsed = urllib.parse.urlsplit(base)
#     # 地址来自 EXE 旁 cmcloud.json 的受批准配置，不接受任何调用参数覆盖
#     if parsed.scheme != "https" or parsed.netloc != base_parsed.netloc:
#         raise FileSearchV2Error("请求地址校验失败")
#     return url
#
#
# def _failure_message(code, message):
#     return "平台返回失败(code=%s): %s" % (code, str(message) if message is not None else "无平台消息")
#
#
# def _validate_response(payload):
#     success = payload.get("success")
#     code = payload.get("code")
#     message = payload.get("message")
#     if success is None and code is None:
#         raise FileSearchV2Error("平台响应缺少 success 与 code，不能判定为成功")
#     if success is not None and str(success).strip().lower() not in ("true", "1"):
#         raise FileSearchV2Error(_failure_message(code, message), code=code)
#     if code is not None and str(code).strip().lower() not in ("0000", "0"):
#         raise FileSearchV2Error(_failure_message(code, message), code=code)
#     return payload
#
#
# def _post(body, request_headers):
#     url = _build_url()
#     payload_bytes = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
#     headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}
#     headers.update(_require_headers(request_headers))
#     request = urllib.request.Request(url, data=payload_bytes, headers=headers, method="POST")
#     try:
#         response = _OPENER.open(request, timeout=_TIMEOUT_SECONDS)
#         try:
#             status = response.getcode()
#             trace_id = upstream_trace.read(response.headers)
#             raw = response.read()
#         finally:
#             response.close()
#     except urllib.error.HTTPError as err:
#         code = message = None
#         try:
#             failed = json.loads(err.read().decode("utf-8"))
#             if isinstance(failed, dict):
#                 code = failed.get("code")
#                 message = failed.get("message")
#         except (UnicodeDecodeError, ValueError):
#             pass
#         raise FileSearchV2Error(
#             "HTTP 状态码 %s: %s" % (err.code, str(message) if message is not None else "无平台消息"),
#             code=code,
#             http_status=err.code,
#         )
#     except OSError as err:
#         raise FileSearchV2Error("网络请求失败: %s" % (getattr(err, "reason", None) or err))
#     if not isinstance(status, int) or not 200 <= status < 300:
#         raise FileSearchV2Error("HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None)
#     try:
#         payload = json.loads(raw.decode("utf-8"))
#     except (UnicodeDecodeError, ValueError):
#         raise FileSearchV2Error("平台响应不是合法 UTF-8 JSON")
#     if not isinstance(payload, dict):
#         raise FileSearchV2Error("平台响应不是 JSON 对象")
#     return _validate_response(upstream_trace.attach(payload, trace_id))
