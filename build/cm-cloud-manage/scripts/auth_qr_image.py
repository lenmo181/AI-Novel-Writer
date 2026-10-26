"""授权入口二维码：把 CLI 已校验的授权入口短链编码成可直接显示的二维码。

用途与边界：
- 只编码 `http`/`https` 短链，且必须由调用方先用 `validate_auth_page_url` 校验来源；
  本模块不校验主机白名单，也不接受任何凭据、uuid 或额外字段。
- 同时给出两种形态，供宿主按自身能力选择：`qrImageDataUri`（PNG data URI，可直接内联
  渲染）与 `qrImagePath`（本机 PNG 文件，可加载为图片显示）。
- **任何失败一律返回空结果**，由调用方回退为纯文本短链，绝不让"二维码生成不了"变成
  "登录不可用"。
- 不写日志；图片**固定落在一个文件上、每次覆盖**（只保留最新一张），不按内容生成新文件，
  避免本地产物随登录次数堆积。
- 编码器是随包分发的纯 Python 实现（`vendor_segno.py`）：无第三方安装依赖、无 C
  扩展、三平台行为一致；首次调用才导入，避免给不涉及登录的命令加启动开销。
"""

import base64
import io
import os
import tempfile

_PNG_DATA_URI_PREFIX = "data:image/png;base64,"
_MAX_TEXT_LENGTH = 1024
_ALLOWED_PREFIXES = ("http://", "https://")
_OUTPUT_DIR_NAME = "cmcloud-qr"
_OUTPUT_FILE_NAME = "auth-entry-qr.png"
_OUTPUT_SUFFIX = ".png"
DEFAULT_SCALE = 4
DEFAULT_BORDER = 4


def render_entry_qr(text, *, directory=None, scale=DEFAULT_SCALE, border=DEFAULT_BORDER):
    """把授权入口短链渲染成回执字段；不可用时返回空 dict。

    返回 `{"qrImageDataUri": <data URI>, "qrImagePath": <本机 PNG 文件>}` 的子集：
    两者都可缺省（编码或落盘失败），调用方按存在与否降级，不把缺失当失败。
    """
    png = _png_bytes(text, scale=scale, border=border)
    if png is None:
        return {}
    result = {"qrImageDataUri": _PNG_DATA_URI_PREFIX + base64.b64encode(png).decode("ascii")}
    path = _write_png_file(png, directory=directory)
    if path is not None:
        result["qrImagePath"] = path
    return result


def render_data_uri(text, *, scale=DEFAULT_SCALE, border=DEFAULT_BORDER):
    """只取 data URI；不可用时返回 None。"""
    png = _png_bytes(text, scale=scale, border=border)
    if png is None:
        return None
    return _PNG_DATA_URI_PREFIX + base64.b64encode(png).decode("ascii")


def render_png_file(text, *, directory=None, scale=DEFAULT_SCALE, border=DEFAULT_BORDER):
    """只取本机 PNG 文件路径；不可用时返回 None。"""
    png = _png_bytes(text, scale=scale, border=border)
    if png is None:
        return None
    return _write_png_file(png, directory=directory)


def _png_bytes(text, *, scale, border):
    """编码为 PNG 字节；文本不合法或编码器异常时返回 None。"""
    if not _is_renderable(text):
        return None
    if not _is_positive_int(scale) or not _is_positive_int(border):
        return None
    try:
        import vendor_segno as segno
    except Exception:
        return None
    try:
        buffer = io.BytesIO()
        # 纠错级别 M：短链长度下仍取到小版本（实测 v6、约 500 B），屏幕扫码余量足够。
        segno.make(text, error="m").save(buffer, kind="png", scale=scale, border=border)
    except Exception:
        return None
    data = buffer.getvalue()
    return data or None


def _is_renderable(text):
    if not isinstance(text, str):
        return False
    if not text or len(text) > _MAX_TEXT_LENGTH:
        return False
    if not text.startswith(_ALLOWED_PREFIXES):
        return False
    return not any(ord(char) < 0x20 or ord(char) == 0x7F for char in text)


def _is_positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _write_png_file(png, *, directory):
    """把二维码写到固定文件上并返回路径；失败返回 None。

    固定文件名 + 原子替换：每次登录覆盖上一张，本机只保留最新一张授权二维码。
    内容与上一张相同时直接复用，不做无谓写盘。
    """
    custom_dir = directory is not None
    try:
        target_dir = directory or _output_dir()
        path = os.path.join(target_dir, _OUTPUT_FILE_NAME)
        if not _has_same_content(path, png):
            temp_path = path + ".tmp"
            with open(temp_path, "wb") as handle:
                handle.write(png)
            # POSIX 收紧为仅属主可读写；Windows 上 chmod 不改变 ACL，仅作最小化尝试。
            try:
                os.chmod(temp_path, 0o600)
            except OSError:
                pass
            os.replace(temp_path, path)
    except OSError:
        return None
    if not custom_dir:
        _prune_stale(target_dir, path)
    return path


def _output_dir():
    target_dir = os.path.join(tempfile.gettempdir(), _OUTPUT_DIR_NAME)
    os.makedirs(target_dir, mode=0o700, exist_ok=True)
    return target_dir


def _prune_stale(target_dir, keep_path):
    """清掉本技能产物目录里的历史/残留图片，只保留本次写出的那张（失败不阻断）。"""
    try:
        entries = os.listdir(target_dir)
    except OSError:
        return
    for name in entries:
        if not name.endswith(_OUTPUT_SUFFIX):
            continue
        stale = os.path.join(target_dir, name)
        if stale == keep_path:
            continue
        try:
            os.remove(stale)
        except OSError:
            continue


def _has_same_content(path, png):
    try:
        with open(path, "rb") as handle:
            return handle.read() == png
    except OSError:
        return False
