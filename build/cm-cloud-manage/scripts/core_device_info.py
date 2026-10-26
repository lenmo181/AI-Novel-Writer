"""x-DeviceInfo 生成规则（相册分享专用）。

规则来自接口文档《客户端设备信息》：

    网络类型|用户终端IP|客户端类型|客户端当前版本|设备品牌|设备型号|设备标识|MAC地址|
    操作系统和版本|分辨率|客户端语言类型|设备经度|设备纬度|

- 字段缺失时置空，但分隔符必带；
- 字段内不允许出现 `|`（出现即清洗掉，绝不多产出分隔符）；
- 网络类型是枚举编码：1 WiFi／2 2G／3 3G／4 4G／5 有线网络／6 5G，无法判定时置空；
- 客户端类型、客户端当前版本、设备经度、设备纬度 在本运行时没有可信来源，固定置空。

尾部按抓包口径保留 4 段空段，与 `x-yun-client-info` 的 17 段输出一致。

本模块与 `cmcloud/client_info.py` 是两套独立实现：两个请求头字段口径相同但下游消费者不同，
刻意不做相互复用，避免任一头的口径调整连带影响另一个头。
"""

import ctypes
import locale
import platform
import socket
import sys
import uuid

# 网络类型（服务端枚举编码）
NETWORK_TYPE_WIFI = 1
NETWORK_TYPE_2G = 2
NETWORK_TYPE_3G = 3
NETWORK_TYPE_4G = 4
NETWORK_TYPE_WIRED = 5
NETWORK_TYPE_5G = 6

# Windows 接口类型（IP_ADAPTER_ADDRESSES.IfType）到网络类型编码
_IF_TYPE_ETHERNET_CSMACD = 6
_IF_TYPE_IEEE80211 = 71

_IF_TYPE_TO_NETWORK_TYPE = {
    _IF_TYPE_IEEE80211: NETWORK_TYPE_WIFI,
    _IF_TYPE_ETHERNET_CSMACD: NETWORK_TYPE_WIRED,
}

# 尾部保留空段数：13 个规范字段 + 4 段 = 17 段
_TRAILING_RESERVED_SEGMENTS = 4

_AF_UNSPEC = 0
_GAA_SKIP_FLAGS = 0x1 | 0x2 | 0x4 | 0x8  # 跳过单播/任播/组播/DNS，只取接口类型
_ERROR_BUFFER_OVERFLOW = 111
_IF_OPER_STATUS_UP = 1
_DEFAULT_ROUTE_PROBE = "8.8.8.8"

# 字段内不允许出现的字符：`|` 是分隔符；`;` 与换行同样按平台拒绝的口径清洗
_ILLEGAL_CHARACTERS = ("|", ";", "\r", "\n")

# 无可信来源、按规则固定置空的字段下标（客户端类型、客户端当前版本、设备经度、设备纬度）
_BLANK_FIELDS = (2, 3, 11, 12)


def build_device_info():
    """按规则生成本次请求的 x-DeviceInfo，不接受任何调用方输入。"""
    manufacturer, model, serial = _device_identity()
    fields = [
        _network_type_code(),   # 网络类型（枚举编码；无法判定置空）
        _local_ip(),            # 用户终端IP
        "",                     # 客户端类型（无可信来源，置空）
        "",                     # 客户端当前版本（无可信来源，置空）
        manufacturer,           # 设备品牌
        model,                  # 设备型号
        serial,                 # 设备标识
        _mac_address(),         # MAC地址
        _os_text(),             # 操作系统和版本
        _resolution_text(),     # 分辨率
        _language_tag(),        # 客户端语言类型
        "",                     # 设备经度（无可信来源，置空）
        "",                     # 设备纬度（无可信来源，置空）
    ]
    for index in _BLANK_FIELDS:
        fields[index] = ""
    fields.extend([""] * _TRAILING_RESERVED_SEGMENTS)
    return "|".join(_clean(item) for item in fields)


def _network_type_code():
    """网络类型编码：以默认路由所在接口为准；移动宽带 2G/3G/4G/5G 不可区分时置空。"""
    if sys.platform != "win32":
        return ""
    entries = _adapter_entries()
    if not entries:
        return ""
    index = _default_route_interface_index()
    if index is not None:
        for if_index, if_type, is_up in entries:
            if if_index == index:
                return str(_IF_TYPE_TO_NETWORK_TYPE.get(if_type, "")) if is_up else ""
    return _fallback_network_type_code(entries)


def _fallback_network_type_code(entries):
    up_types = {if_type for _, if_type, is_up in entries if is_up}
    if _IF_TYPE_IEEE80211 in up_types:
        return str(NETWORK_TYPE_WIFI)
    if _IF_TYPE_ETHERNET_CSMACD in up_types:
        return str(NETWORK_TYPE_WIRED)
    return ""


class _IpAdapterAddresses(ctypes.Structure):
    """IP_ADAPTER_ADDRESSES 前段布局（只用到 IfIndex/IfType/OperStatus）。"""


_IpAdapterAddresses._fields_ = [
    ("Length", ctypes.c_ulong),
    ("IfIndex", ctypes.c_ulong),
    ("Next", ctypes.POINTER(_IpAdapterAddresses)),
    ("AdapterName", ctypes.c_char_p),
    ("FirstUnicastAddress", ctypes.c_void_p),
    ("FirstAnycastAddress", ctypes.c_void_p),
    ("FirstMulticastAddress", ctypes.c_void_p),
    ("FirstDnsServerAddress", ctypes.c_void_p),
    ("DnsSuffix", ctypes.c_wchar_p),
    ("Description", ctypes.c_wchar_p),
    ("FriendlyName", ctypes.c_wchar_p),
    ("PhysicalAddress", ctypes.c_ubyte * 8),
    ("PhysicalAddressLength", ctypes.c_ulong),
    ("Flags", ctypes.c_ulong),
    ("Mtu", ctypes.c_ulong),
    ("IfType", ctypes.c_ulong),
    ("OperStatus", ctypes.c_int),
]


def _adapter_entries():
    """枚举本机接口：[(IfIndex, IfType, 是否 Up)]；失败返回空列表。"""
    if sys.platform != "win32":
        return []
    try:
        iphlpapi = ctypes.windll.iphlpapi
        size = ctypes.c_ulong(0)
        status = iphlpapi.GetAdaptersAddresses(
            _AF_UNSPEC, _GAA_SKIP_FLAGS, None, None, ctypes.byref(size)
        )
        if status not in (0, _ERROR_BUFFER_OVERFLOW):
            return []
        buffer = ctypes.create_string_buffer(max(size.value, 1))
        status = iphlpapi.GetAdaptersAddresses(
            _AF_UNSPEC, _GAA_SKIP_FLAGS, None, ctypes.byref(buffer), ctypes.byref(size)
        )
        if status != 0:
            return []
        entries = []
        pointer = ctypes.cast(buffer, ctypes.POINTER(_IpAdapterAddresses))
        while pointer:
            entry = pointer.contents
            entries.append(
                (int(entry.IfIndex), int(entry.IfType), int(entry.OperStatus) == _IF_OPER_STATUS_UP)
            )
            pointer = entry.Next
        return entries
    except (AttributeError, OSError, ValueError):
        return []


def _default_route_interface_index():
    """默认路由（按外网地址探测）所在接口的 IfIndex；无法取得时返回 None。"""
    try:
        destination = int.from_bytes(socket.inet_aton(_DEFAULT_ROUTE_PROBE), "little")
        index = ctypes.c_ulong(0)
        if ctypes.windll.iphlpapi.GetBestInterface(destination, ctypes.byref(index)) != 0:
            return None
        return int(index.value)
    except (AttributeError, OSError, ValueError):
        return None


def _local_ip():
    """用户终端IP：本机出口地址。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # UDP connect 不发送数据，只让系统选择当前默认路由对应的本机地址。
        sock.connect((_DEFAULT_ROUTE_PROBE, 80))
        value = sock.getsockname()[0]
        return value if isinstance(value, str) and value else "unknown"
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname()) or "unknown"
        except OSError:
            return "unknown"
    finally:
        sock.close()


def _device_identity():
    """返回 (设备品牌, 设备型号, 设备标识)；单字段读取失败互不影响。"""
    manufacturer = model = serial = None
    if sys.platform == "win32":
        try:
            import winreg

            path = r"HARDWARE\DESCRIPTION\System\BIOS"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
                manufacturer = _registry_text(winreg, key, "SystemManufacturer")
                model = _registry_text(winreg, key, "SystemProductName")
                serial = _registry_text(winreg, key, "SystemSerialNumber")
        except (OSError, AttributeError, ValueError):
            pass
    if not manufacturer:
        manufacturer = platform.system() or "unknown"
    if not model:
        model = platform.machine() or "unknown"
    if not serial:
        # 设备标识缺省用本机稳定指纹（同机同值），现网样本接受该形态
        seed = "%s|%s|%s" % (socket.gethostname() or "", _mac_address(), model)
        serial = str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))
    return manufacturer, model, serial


def _registry_text(winreg, key, name):
    try:
        value = winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None
    text = str(value).strip() if value is not None else ""
    return text or None


def _mac_address():
    return "%012X" % uuid.getnode()


def _os_text():
    text = "%s%s %s" % (platform.system() or "unknown", platform.release() or "", platform.version() or "")
    return text.strip() or "unknown"


def _screen_size():
    if sys.platform != "win32":
        return None, None
    try:
        user32 = ctypes.windll.user32
        return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))
    except (AttributeError, OSError, ValueError):
        return None, None


def _resolution_text():
    width, height = _screen_size()
    if width and height:
        return "%d%d" % (width, height)
    return "unknown"


def _language_tag():
    if sys.platform == "win32":
        try:
            buffer = ctypes.create_unicode_buffer(85)
            if ctypes.windll.kernel32.GetUserDefaultLocaleName(buffer, 85):
                value = buffer.value
                if value:
                    return value
        except (AttributeError, OSError, ValueError):
            pass
    language = locale.getlocale()[0]
    if not isinstance(language, str) or not language:
        return "unknown"
    if language.lower().startswith("chinese"):
        return "zh-CN"
    return language


def _clean(value):
    """清洗单字段：非法字符逐个替换为空格，缺失即空串。

    与 `x-yun-client-info` 的清洗口径完全一致（逐字符替换、不收敛连续空白），
    保证两个头的字段值在所有输入下逐字节相同。
    """
    text = str(value or "")
    for character in _ILLEGAL_CHARACTERS:
        text = text.replace(character, " ")
    return text.strip()
