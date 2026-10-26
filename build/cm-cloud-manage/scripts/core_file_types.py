"""搜索类命令的资产类型（``conditions.typeList``）取值：码表唯一真源。

数值码由 IDaaS 侧给定，**以本模块为唯一真源**，schema、adapter 与 workflow 一律从这里取，
避免同一枚举在多处各写一份后漂移：

    0＝其他、1＝图片、2＝音频、3＝视频、4＝文档、5＝APP、6＝压缩类型、100＝目录

不给 ``typeList`` 表示不限制类型（在其它条件范围内搜全部类型）。

本模块不 import 任何其它 cmcloud 模块，可被 validation / adapters / workflows 任意引用。
"""

TYPE_CODES = ("0", "1", "2", "3", "4", "5", "6", "100")

TYPE_CODE_LABELS = {
    "0": "其他",
    "1": "图片",
    "2": "音频",
    "3": "视频",
    "4": "文档",
    "5": "APP",
    "6": "压缩类型",
    "100": "目录",
}

# 模型可提交的取值：数值码与中文规范词都收，完整枚举随 schema 下发。
CONTENT_TYPE_CHOICES = TYPE_CODES + tuple(TYPE_CODE_LABELS[code] for code in TYPE_CODES)

_LABEL_TO_CODE = {label: code for code, label in TYPE_CODE_LABELS.items()}


def describe_choices():
    """可读的取值说明（用于本地拒绝时的业务话术，不含内部标识）。"""
    return "、".join("%s（%s）" % (TYPE_CODE_LABELS[code], code) for code in TYPE_CODES)


def normalize_content_type(value):
    """把类型筛选归一化为数值码字符串；不可识别时抛 ValueError。

    接受数值码（字符串或整数）与中文规范词；同义词（照片/相片/录像…）由调用方
    的话术层负责归到规范词，本函数不做同义词扩展。
    """
    if isinstance(value, bool):
        raise ValueError("类型筛选必须是字符串或整数")
    if isinstance(value, int):
        candidate = str(value)
    elif isinstance(value, str):
        candidate = value.strip()
    else:
        raise ValueError("类型筛选必须是字符串或整数")
    if candidate in TYPE_CODES:
        return candidate
    code = _LABEL_TO_CODE.get(candidate)
    if code is None:
        # 不回显调用方传入的原始取值：该文本可能进入诊断日志。
        raise ValueError("类型筛选取值不在码表内")
    return code


__all__ = (
    "CONTENT_TYPE_CHOICES",
    "TYPE_CODES",
    "TYPE_CODE_LABELS",
    "describe_choices",
    "normalize_content_type",
)
