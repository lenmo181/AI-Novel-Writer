"""文件/文件夹命名规则与禁用字符提示。

命名规则只有一处事实来源：**禁用字符集合**。Schema 的 `pattern` 由集合派生，
校验失败时还要能点名"用户输入里到底哪个符号不可用"，因此这里同时提供提示文案。
"""

import re

# 文件系统非法字符（Windows 口径，也是云盘侧拒绝的字符）
ILLEGAL_FILESYSTEM_CHARACTERS = '\\/:*?"<>|'

# 追加禁用：shell 元字符（本项目没有 shell 出口，按安全用例口径收紧命名入参）
SHELL_METACHARACTERS = ';&$`{}'

FORBIDDEN_CHARACTERS = ILLEGAL_FILESYSTEM_CHARACTERS + SHELL_METACHARACTERS

# 命中即给出点名提示的字段注解：`schema_validator` 会忽略 `x-` 前缀，不影响契约校验
FORBIDDEN_MARKER = "x-forbiddenCharacters"

DOT_NAMES = (".", "..")

_CONTROL_CHARACTER_LABEL = "控制字符"

_CLASS_ESCAPES = {"\\": "\\\\", "]": "\\]", "^": "\\^", "-": "\\-"}


def _class_body(characters):
    return "".join(_CLASS_ESCAPES.get(character, character) for character in characters)


# 由禁用集合派生：不允许 . / .. 单名，长度 1~255，控制字符一律拒绝
NAME_PATTERN = r"^(?!\.{1,2}$)[^" + _class_body(FORBIDDEN_CHARACTERS) + r"\x00-\x1f]{1,255}$"

_NAME_PATTERN_REGEX = re.compile(NAME_PATTERN)


def is_valid_name(value):
    return isinstance(value, str) and _NAME_PATTERN_REGEX.match(value) is not None


def forbidden_characters(value):
    """返回输入中实际出现的禁用字符（按首次出现顺序去重）。"""
    if not isinstance(value, str):
        return ()
    found = []
    for character in value:
        if character in FORBIDDEN_CHARACTERS and character not in found:
            found.append(character)
    return tuple(found)


def has_control_characters(value):
    return isinstance(value, str) and any(ord(character) < 0x20 for character in value)


def name_error_message(value):
    """命名不合规时的点名提示；名称合规返回 None。

    只描述"哪些符号不可用"，不输出 pattern 原文。
    """
    if not isinstance(value, str):
        return None
    if value in DOT_NAMES:
        return "文件夹名称不能使用 %s，请您重新命名" % value
    parts = _offending_parts(value)
    if not parts:
        return None
    return "当前文件夹名称中包含 %s，无法创建，请您重新命名" % " ".join(parts)


def file_name_error_message(value):
    """文件命名不合规时的点名提示；合规返回 None。

    与文件夹共用同一个禁用集合，只有称谓和动作不同（文件是"上传"不是"创建"）。
    """
    if not isinstance(value, str):
        return None
    if value in DOT_NAMES:
        return "文件名不能使用 %s，请您重新命名" % value
    parts = _offending_parts(value)
    if not parts:
        return None
    return "当前文件名称中包含 %s，无法上传，请您重新命名" % " ".join(parts)


def _offending_parts(value):
    """输入中实际命中的禁用项：先列符号，控制字符按标签兜底。"""
    parts = list(forbidden_characters(value))
    if has_control_characters(value):
        parts.append(_CONTROL_CHARACTER_LABEL)
    return parts
