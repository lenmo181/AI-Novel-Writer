"""受控 JSON Schema 子集校验器。

设计约束：
- 只服务 S01～S35 触发入参校验，不追求 JSON Schema 全规范。
- 必须支持 additionalProperties/oneOf/anyOf/allOf/not/if-then-else/dependentRequired，
  否则无法表达场景互斥与条件必填。
- 未知关键字直接报错，避免 Schema 写错后被静默忽略（x- 前缀用于本地注解，不参与校验）。
"""

import datetime
import re
import zoneinfo


class SchemaValidationError(Exception):
    """校验失败；path 为指向出错字段的 JSON Pointer 风格路径。"""

    def __init__(self, message, path=""):
        super().__init__(message if not path else "%s: %s" % (path, message))
        self.message = message
        self.path = path


_ANNOTATION_KEYWORDS = frozenset(
    (
        "title",
        "description",
        "default",
        "examples",
        "$schema",
        "$id",
        "$comment",
        "deprecated",
        "readOnly",
        "writeOnly",
    )
)

_KNOWN_KEYWORDS = frozenset(
    (
        "type",
        "enum",
        "const",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
        "if",
        "then",
        "else",
        "properties",
        "required",
        "additionalProperties",
        "minProperties",
        "maxProperties",
        "dependentRequired",
        "dependentSchemas",
        "items",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
    )
)

_TYPES = ("object", "array", "string", "integer", "number", "boolean", "null")

_RFC3339_DATETIME = re.compile(
    r"\A\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})\Z"
)
_RFC3339_DATE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
_RFC3339_TIME = re.compile(r"\A\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})?\Z")
_REF_PATTERN = re.compile(r"\Acmr_[A-Za-z0-9_]+_[A-Za-z0-9._-]+\Z")
_CURSOR_PATTERN = re.compile(r"\Acmc_[A-Za-z0-9_-]+\Z")


class SchemaError(RuntimeError):
    """Schema 定义本身不合法（内部错误）。"""


class SchemaValidator:
    """无状态校验器；同一实例可复用。"""

    def __init__(self, formats=None):
        self._formats = dict(DEFAULT_FORMATS)
        if formats:
            self._formats.update(formats)

    def validate(self, schema, instance):
        self._check_schema(schema, "")
        self._validate(schema, instance, "")

    def is_valid(self, schema, instance):
        try:
            self.validate(schema, instance)
        except SchemaValidationError:
            return False
        return True

    # ---- schema 自检 ----

    def _check_schema(self, schema, path):
        if isinstance(schema, bool):
            return
        if not isinstance(schema, dict):
            raise SchemaError("%s: Schema 节点必须是对象或布尔值" % path)
        for key, value in schema.items():
            if key.startswith("x-") or key in _ANNOTATION_KEYWORDS:
                continue
            if key not in _KNOWN_KEYWORDS:
                raise SchemaError("%s: 不支持的 Schema 关键字: %s" % (path, key))
            if key in ("allOf", "anyOf", "oneOf"):
                if not isinstance(value, list) or not value:
                    raise SchemaError("%s.%s 必须是非空数组" % (path, key))
                for index, item in enumerate(value):
                    self._check_schema(item, "%s.%s[%d]" % (path, key, index))
            elif key in ("not", "if", "then", "else", "items", "additionalProperties"):
                if key == "additionalProperties" and isinstance(value, bool):
                    continue
                self._check_schema(value, "%s.%s" % (path, key))
            elif key == "properties":
                if not isinstance(value, dict):
                    raise SchemaError("%s.properties 必须是对象" % path)
                for name, item in value.items():
                    self._check_schema(item, "%s.properties.%s" % (path, name))
            elif key == "dependentSchemas":
                if not isinstance(value, dict):
                    raise SchemaError("%s.dependentSchemas 必须是对象" % path)
                for name, item in value.items():
                    self._check_schema(item, "%s.dependentSchemas.%s" % (path, name))
            elif key == "type":
                types = value if isinstance(value, list) else [value]
                if not types:
                    raise SchemaError("%s.type 不得为空" % path)
                for item in types:
                    if item not in _TYPES:
                        raise SchemaError("%s.type 取值非法: %s" % (path, item))
            elif key == "format":
                if not isinstance(value, str) or value not in self._formats:
                    raise SchemaError("%s.format 未登记: %s" % (path, value))
            elif key == "enum":
                if not isinstance(value, list) or not value:
                    raise SchemaError("%s.enum 必须是非空数组" % path)
            elif key == "pattern":
                if not isinstance(value, str):
                    raise SchemaError("%s.pattern 必须是字符串" % path)
                try:
                    re.compile(value)
                except re.error as err:
                    raise SchemaError("%s.pattern 不是合法正则: %s" % (path, err))
            elif key in ("required", "dependentRequired"):
                if key == "required":
                    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                        raise SchemaError("%s.required 必须是字符串数组" % path)
                else:
                    if not isinstance(value, dict):
                        raise SchemaError("%s.dependentRequired 必须是对象" % path)
                    for name, deps in value.items():
                        if not isinstance(deps, list) or not all(isinstance(x, str) for x in deps):
                            raise SchemaError("%s.dependentRequired.%s 必须是字符串数组" % (path, name))
            elif key in ("minProperties", "maxProperties", "minItems", "maxItems", "minLength", "maxLength"):
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise SchemaError("%s.%s 必须是非负整数" % (path, key))
            elif key == "uniqueItems":
                if not isinstance(value, bool):
                    raise SchemaError("%s.uniqueItems 必须是布尔值" % path)
            elif key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"):
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise SchemaError("%s.%s 必须是数字" % (path, key))
            elif key == "const":
                continue

    # ---- 实例校验 ----

    def _validate(self, schema, value, path):
        if schema is True:
            return
        if schema is False:
            raise SchemaValidationError("不允许出现", path)

        if "const" in schema and not _equal(value, schema["const"]):
            raise SchemaValidationError("取值必须是常量 %r" % (schema["const"],), path)
        if "enum" in schema and not any(_equal(value, item) for item in schema["enum"]):
            raise SchemaValidationError("取值不在允许集合内: %s" % _enum_hint(schema["enum"]), path)
        if "type" in schema and not _type_matches(schema["type"], value):
            raise SchemaValidationError("类型必须是 %s" % _type_hint(schema["type"]), path)

        for key in ("allOf",):
            if key in schema:
                for item in schema[key]:
                    self._validate(item, value, path)
        if "anyOf" in schema:
            if not any(self.is_valid(item, value) for item in schema["anyOf"]):
                raise SchemaValidationError("不满足任一 anyOf 分支", path)
        if "oneOf" in schema:
            matched = sum(1 for item in schema["oneOf"] if self.is_valid(item, value))
            if matched != 1:
                raise SchemaValidationError(
                    "必须且只能满足一个 oneOf 分支（实际匹配 %d 个）" % matched, path
                )
        if "not" in schema and self.is_valid(schema["not"], value):
            raise SchemaValidationError("命中 not 禁止条件", path)
        if "if" in schema:
            branch = schema.get("then") if self.is_valid(schema["if"], value) else schema.get("else")
            if branch is not None:
                self._validate(branch, value, path)

        if isinstance(value, str):
            self._validate_string(schema, value, path)
        elif isinstance(value, bool):
            pass
        elif isinstance(value, (int, float)):
            self._validate_number(schema, value, path)
        elif isinstance(value, list):
            self._validate_array(schema, value, path)
        elif isinstance(value, dict):
            self._validate_object(schema, value, path)

    def _validate_string(self, schema, value, path):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise SchemaValidationError("长度不得少于 %d" % schema["minLength"], path)
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise SchemaValidationError("长度不得超过 %d" % schema["maxLength"], path)
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise SchemaValidationError("格式不符合 pattern %s" % schema["pattern"], path)
        if "format" in schema:
            checker = self._formats[schema["format"]]
            message = checker(value)
            if message:
                raise SchemaValidationError(message, path)

    def _validate_number(self, schema, value, path):
        if "multipleOf" in schema:
            multiple = schema["multipleOf"]
            if multiple <= 0:
                raise SchemaError("multipleOf 必须为正数")
            quotient = value / multiple
            if abs(quotient - round(quotient)) > 1e-9:
                raise SchemaValidationError("必须是 %s 的整数倍" % multiple, path)
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaValidationError("不得小于 %s" % schema["minimum"], path)
        if "maximum" in schema and value > schema["maximum"]:
            raise SchemaValidationError("不得大于 %s" % schema["maximum"], path)
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise SchemaValidationError("必须大于 %s" % schema["exclusiveMinimum"], path)
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            raise SchemaValidationError("必须小于 %s" % schema["exclusiveMaximum"], path)

    def _validate_array(self, schema, value, path):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise SchemaValidationError("元素个数不得少于 %d" % schema["minItems"], path)
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise SchemaValidationError("元素个数不得超过 %d" % schema["maxItems"], path)
        if schema.get("uniqueItems"):
            seen = []
            for item in value:
                if any(_equal(item, other) for other in seen):
                    raise SchemaValidationError("元素不得重复", path)
                seen.append(item)
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(value):
                self._validate(item_schema, item, "%s[%d]" % (path, index))

    def _validate_object(self, schema, value, path):
        properties = schema.get("properties", {})
        required = schema.get("required", ())
        for name in required:
            if name not in value:
                raise SchemaValidationError("缺少必填字段: %s" % name, path)
        for name, item in value.items():
            child = "%s.%s" % (path, name)
            if name in properties:
                self._validate(properties[name], item, child)
                continue
            additional = schema.get("additionalProperties", True)
            if additional is False:
                raise SchemaValidationError("不允许出现未声明字段: %s" % name, path)
            if isinstance(additional, dict):
                self._validate(additional, item, child)
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            raise SchemaValidationError("字段个数不得少于 %d" % schema["minProperties"], path)
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            raise SchemaValidationError("字段个数不得超过 %d" % schema["maxProperties"], path)
        for name, deps in schema.get("dependentRequired", {}).items():
            if name in value:
                for dep in deps:
                    if dep not in value:
                        raise SchemaValidationError("字段 %s 存在时必须同时提供 %s" % (name, dep), path)
        for name, sub in schema.get("dependentSchemas", {}).items():
            if name in value:
                self._validate(sub, value, path)

    # ---- format 校验入口在模块级 DEFAULT_FORMATS 中登记 ----


def _format_datetime(value):
    if _RFC3339_DATETIME.match(value) is None:
        return "必须是 RFC 3339 日期时间（需带时区偏移）"
    try:
        datetime.datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return "不是可解析的日期时间"
    return None


def _format_date(value):
    if _RFC3339_DATE.match(value) is None:
        return "必须是 RFC 3339 日期"
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return "不是可解析的日期"
    return None


def _format_time(value):
    if _RFC3339_TIME.match(value) is None:
        return "必须是 RFC 3339 时间"
    return None


def _format_uri(value):
    import urllib.parse

    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "必须是 http/https URL"
    return None


_TZ_FALLBACK = frozenset(
    """
    UTC Africa/Abidjan Africa/Accra Africa/Addis_Ababa Africa/Algiers Africa/Cairo Africa/Casablanca
    Africa/Dar_es_Salaam Africa/Johannesburg Africa/Khartoum Africa/Lagos Africa/Nairobi Africa/Tripoli
    Africa/Tunis Africa/Windhoek America/Anchorage America/Argentina/Buenos_Aires America/Bogota
    America/Caracas America/Chicago America/Denver America/Edmonton America/Guatemala America/Halifax
    America/Havana America/Lima America/Los_Angeles America/Mexico_City America/Montevideo America/New_York
    America/Panama America/Phoenix America/Santiago America/Sao_Paulo America/St_Johns America/Toronto
    America/Vancouver America/Winnipeg Asia/Almaty Asia/Amman Asia/Baghdad Asia/Bahrain Asia/Baku
    Asia/Bangkok Asia/Beirut Asia/Colombo Asia/Dhaka Asia/Dubai Asia/Hanoi Asia/Ho_Chi_Minh Asia/Hong_Kong
    Asia/Hovd Asia/Irkutsk Asia/Jakarta Asia/Jerusalem Asia/Kabul Asia/Karachi Asia/Kathmandu Asia/Kolkata
    Asia/Krasnoyarsk Asia/Kuala_Lumpur Asia/Kuwait Asia/Macau Asia/Magadan Asia/Manila Asia/Muscat
    Asia/Novosibirsk Asia/Omsk Asia/Phnom_Penh Asia/Pyongyang Asia/Qatar Asia/Riyadh Asia/Seoul
    Asia/Shanghai Asia/Singapore Asia/Taipei Asia/Tashkent Asia/Tbilisi Asia/Tehran Asia/Thimphu
    Asia/Tokyo Asia/Ulaanbaatar Asia/Vladivostok Asia/Yakutsk Asia/Yangon Asia/Yekaterinburg Asia/Yerevan
    Atlantic/Azores Atlantic/Canary Atlantic/Reykjavik Atlantic/South_Georgia Australia/Adelaide
    Australia/Brisbane Australia/Darwin Australia/Hobart Australia/Melbourne Australia/Perth Australia/Sydney
    Europe/Amsterdam Europe/Athens Europe/Belgrade Europe/Berlin Europe/Bratislava Europe/Brussels
    Europe/Bucharest Europe/Budapest Europe/Copenhagen Europe/Dublin Europe/Helsinki Europe/Istanbul
    Europe/Kaliningrad Europe/Kiev Europe/Lisbon Europe/Ljubljana Europe/London Europe/Luxembourg
    Europe/Madrid Europe/Malta Europe/Minsk Europe/Monaco Europe/Moscow Europe/Oslo Europe/Paris
    Europe/Prague Europe/Riga Europe/Rome Europe/Samara Europe/Sofia Europe/Stockholm Europe/Tallinn
    Europe/Vienna Europe/Vilnius Europe/Volgograd Europe/Warsaw Europe/Zagreb Europe/Zurich
    Indian/Antananarivo Indian/Chagos Indian/Christmas Indian/Cocos Indian/Comoro Indian/Maldives
    Indian/Mauritius Indian/Mayotte Indian/Reunion Pacific/Auckland Pacific/Chatham Pacific/Easter
    Pacific/Fiji Pacific/Guam Pacific/Honolulu Pacific/Kiritimati Pacific/Majuro Pacific/Noumea
    Pacific/Pago_Pago Pacific/Port_Moresby Pacific/Tahiti Pacific/Tongatapu Pacific/Wake
    """.split()
)

_tz_database_available = None


def _tz_database_loadable():
    """判断当前环境是否装载了 IANA 时区数据库（Windows 需 tzdata 包）。"""
    global _tz_database_available
    if _tz_database_available is None:
        try:
            zoneinfo.ZoneInfo("UTC")
            _tz_database_available = True
        except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
            _tz_database_available = False
    return _tz_database_available


def _format_iana_timezone(value):
    try:
        zoneinfo.ZoneInfo(value)
        return None
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        pass
    if _tz_database_loadable():
        # 环境有时区库但加载失败：按真实 IANA 名称拒绝
        return "时区必须是可加载的 IANA 名称"
    # 环境无时区库（例如未安装 tzdata 的 Windows）：退化为内置 IANA 名称白名单
    if value in _TZ_FALLBACK:
        return None
    return "时区必须是可加载的 IANA 名称"


def _format_ref(value):
    if _REF_PATTERN.match(value) is None:
        return "必须是 CLI 签发的受控引用（cmr_...）"
    return None


def _format_cursor(value):
    if _CURSOR_PATTERN.match(value) is None:
        return "必须是 CLI 签发的受控游标（cmc_...）"
    return None


DEFAULT_FORMATS = {
    "date-time": _format_datetime,
    "date": _format_date,
    "time": _format_time,
    "uri": _format_uri,
    "iana-timezone": _format_iana_timezone,
    "cmcloud-ref": _format_ref,
    "cmcloud-cursor": _format_cursor,
}


def _type_matches(expected, value):
    types = expected if isinstance(expected, list) else [expected]
    actual = _actual_type(value)
    if actual in types:
        return True
    return actual == "integer" and "number" in types


def _actual_type(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _type_hint(expected):
    return "、".join(expected) if isinstance(expected, list) else expected


def _enum_hint(values):
    shown = ", ".join(repr(item) for item in values[:8])
    return shown if len(values) <= 8 else shown + ", ..."


def _equal(left, right):
    """JSON 语义相等：bool 不等于 1，int 等于同值 float。"""
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_equal(left[k], right[k]) for k in left)
    return left == right


_DEFAULT_VALIDATOR = SchemaValidator()


def validate_or_raise(schema, instance):
    _DEFAULT_VALIDATOR.validate(schema, instance)
