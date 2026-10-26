import ctypes
import locale
import platform
import socket
import sys
import uuid

# 网络类型编码（服务端枚举）
NETWORK_TYPE_WIFI = 1
NETWORK_TYPE_2G = 2
NETWORK_TYPE_3G = 3
NETWORK_TYPE_4G = 4
NETWORK_TYPE_WIRED = 5
NETWORK_TYPE_5G = 6

# Windows 接口类型（IP_ADAPTER_ADDRESSES.IfType）
_IF_TYPE_ETHERNET_CSMACD = 6
_IF_TYPE_IEEE80211 = 71

_IF_TYPE_TO_NETWORK_TYPE = {
    _IF_TYPE_IEEE80211: NETWORK_TYPE_WIFI,
    _IF_TYPE_ETHERNET_CSMACD: NETWORK_TYPE_WIRED,
}

_AF_UNSPEC = 0
_GAA_SKIP_FLAGS = 0x1 | 0x2 | 0x4 | 0x8  # 跳过单播/任播/组播/DNS，仅取接口类型
_ERROR_BUFFER_OVERFLOW = 111
_IF_OPER_STATUS_UP = 1
_BEST_INTERFACE_PROBE = "8.8.8.8"


def collect_client_info():
    """返回 13 个规范字段 + 4 个尾部保留空段，不读取 Agent 输入。"""
    manufacturer, model, serial = _device_identity()
    fields = (
        _network_type_code(),    # 网络类型（平台枚举编码；无法判定时置空）
        _local_ip(),             # 用户终端IP
        "",                      # 客户端类型（本运行时未提供，置空）
        "",                      # 客户端当前版本（本运行时未提供，置空）
        manufacturer,            # 设备品牌
        model,                   # 设备型号
        serial,                  # 设备标识
        _mac_address(),          # MAC地址
        _os_text(),              # 操作系统和版本
        _resolution_text(),      # 分辨率
        _language_tag(),         # 客户端语言类型
        "",                      # 设备经度（无法可信取值，置空）
        "",                      # 设备纬度（无法可信取值，置空）
        "",                      # 以下 4 段为抓包值尾部保留的分隔符
        "",
        "",
        "",
    )
    return "|".join(_clean(item) for item in fields)


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


def _network_type_code():
    """返回网络类型编码（1 WiFi／2 2G／3 3G／4 4G／5 有线网络／6 5G）。

    以默认路由所在接口为准；无法判定（含移动宽带 2G/3G/4G/5G 不可区分）时置空。
    """
    if sys.platform != "win32":
        return ""
    entries = _win_adapter_entries()
    if not entries:
        return ""
    index = _best_interface_index()
    if index is not None:
        for if_index, if_type, up in entries:
            if if_index == index:
                if not up:
                    return ""
                return str(_IF_TYPE_TO_NETWORK_TYPE.get(if_type, ""))
    return _fallback_network_type_code(entries)


def _fallback_network_type_code(entries):
    up_types = {if_type for _, if_type, up in entries if up}
    if _IF_TYPE_IEEE80211 in up_types:
        return str(NETWORK_TYPE_WIFI)
    if _IF_TYPE_ETHERNET_CSMACD in up_types:
        return str(NETWORK_TYPE_WIRED)
    return ""


def _win_adapter_entries():
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


def _best_interface_index():
    """默认路由（按外网地址探测）所在接口的 IfIndex；无法取得时返回 None。"""
    try:
        destination = int.from_bytes(socket.inet_aton(_BEST_INTERFACE_PROBE), "little")
        index = ctypes.c_ulong(0)
        if ctypes.windll.iphlpapi.GetBestInterface(destination, ctypes.byref(index)) != 0:
            return None
        return int(index.value)
    except (AttributeError, OSError, ValueError):
        return None


def _local_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # UDP connect 不发送数据，仅让系统选择当前默认路由对应的本机地址。
        sock.connect(("8.8.8.8", 80))
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
        # 现网样本接受设备指纹 UUID 作为设备标识；同机保持稳定。
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
    text = str(value or "")
    return text.replace("|", " ").replace(";", " ").replace("\r", " ").replace("\n", " ").strip()
