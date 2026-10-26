import json
import os
import re
import sys
import urllib.parse

_CONFIG_FILENAME = "cmcloud.json"
_HOST_PATTERN = re.compile(r"\A[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?\Z")
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
_BASE_KEYS = ("base_url", "personal_saas_base_url", "oauth_base_url", "qr_oauth_base_url")
_REQUEST_COMMON_HEADERS_KEY = "commonHeaders"

_cached_config = None


class PlatformConfigError(RuntimeError):
    def __init__(self, message, *, config_path=None):
        super().__init__(message)
        self.config_path = config_path


class PlatformConfig:
    """只读平台配置，进程内首次读取后缓存，修改 JSON 需重启 CLI 生效。"""

    __slots__ = (
        "_base_url", "_personal_saas_base_url", "_oauth_base_url", "_qr_oauth_base_url",
        "_request_common_headers", "_config_path",
    )

    def __init__(
        self, base_url, personal_saas_base_url, oauth_base_url, qr_oauth_base_url,
        request_common_headers, config_path,
    ):
        self._base_url = base_url
        self._personal_saas_base_url = personal_saas_base_url
        self._oauth_base_url = oauth_base_url
        self._qr_oauth_base_url = qr_oauth_base_url
        self._request_common_headers = dict(request_common_headers)
        self._config_path = config_path

    @property
    def base_url(self):
        """V3 网关入口（含部署前缀，例如 /open-ai-space）。"""
        return self._base_url

    @property
    def personal_saas_base_url(self):
        return self._personal_saas_base_url

    @property
    def oauth_base_url(self):
        return self._oauth_base_url

    @property
    def qr_oauth_base_url(self):
        return self._qr_oauth_base_url

    @property
    def request_common_headers(self):
        """所有业务 API 的公共请求头；调用方拿到副本，不能污染进程缓存。"""
        return dict(self._request_common_headers)

    @property
    def config_path(self):
        return self._config_path

    def require(self, name):
        if name not in _BASE_KEYS:
            raise PlatformConfigError("未知的配置项: %s" % name, config_path=self._config_path)
        value = getattr(self, name)
        if not value:
            raise PlatformConfigError("未配置 %s" % name, config_path=self._config_path)
        return value


def load_platform_config(path=None, use_cache=True):
    """加载平台配置。

    打包运行读 EXE 同目录的 cmcloud.json；源码运行读代码包内的同名文件；
    显式传入 path 时按给定文件读取（供受控测试或部署使用），不写入缓存。
    """
    global _cached_config
    if path is None:
        if use_cache and _cached_config is not None:
            return _cached_config
        resolved = _default_config_path()
    else:
        resolved = os.path.abspath(path)
    config = _read_config(resolved)
    if path is None:
        _cached_config = config
    return config


def reset_cache():
    global _cached_config
    _cached_config = None


def _default_config_path():
    if getattr(sys, "frozen", False):
        # 冻结 EXE：只认可执行文件所在目录，不依赖当前工作目录或单文件解压目录
        return os.path.join(os.path.dirname(os.path.abspath(sys.executable)), _CONFIG_FILENAME)
    # 源码运行：只认代码包内的开发配置，不与 EXE 旁的发布配置互相择优
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), _CONFIG_FILENAME)


def _read_config(config_path):
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as err:
        raise PlatformConfigError("平台配置文件不可读取: %s" % err, config_path=config_path)
    try:
        document = json.loads(raw)
    except ValueError:
        raise PlatformConfigError("平台配置文件不是合法 JSON", config_path=config_path)
    if not isinstance(document, dict):
        raise PlatformConfigError("平台配置文件顶层必须是对象", config_path=config_path)
    values = {}
    for key in _BASE_KEYS:
        value = document.get(key)
        if value is None:
            values[key] = None
            continue
        if not isinstance(value, str) or not value.strip():
            raise PlatformConfigError("%s 必须是非空字符串" % key, config_path=config_path)
        values[key] = _normalize_base_url(value, key, config_path)
    if values["base_url"] is None:
        raise PlatformConfigError("必须配置 base_url", config_path=config_path)
    request_common_headers = _read_request_common_headers(document, config_path)
    return PlatformConfig(
        values["base_url"],
        values["personal_saas_base_url"],
        values["oauth_base_url"],
        values["qr_oauth_base_url"],
        request_common_headers,
        config_path,
    )


def _read_request_common_headers(document, config_path):
    request = document.get("request")
    if request is None:
        return {}
    if not isinstance(request, dict):
        raise PlatformConfigError("request 必须是对象", config_path=config_path)
    headers = request.get(_REQUEST_COMMON_HEADERS_KEY)
    if headers is None:
        return {}
    if not isinstance(headers, dict):
        raise PlatformConfigError("request.commonHeaders 必须是对象", config_path=config_path)
    normalized = {}
    for key, value in headers.items():
        if not isinstance(key, str) or not key.strip():
            raise PlatformConfigError("request.commonHeaders 的键必须是非空字符串", config_path=config_path)
        if not isinstance(value, str) or not value.strip():
            raise PlatformConfigError(
                "request.commonHeaders 的值必须是非空字符串", config_path=config_path
            )
        normalized[key] = value
    return normalized


def _normalize_base_url(value, key, config_path):
    url = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(url)
    local = parsed.hostname in _LOCAL_HOSTS
    if parsed.scheme == "https":
        pass
    elif parsed.scheme == "http" and local:
        # 仅文档 1 明示的本地开发地址允许 HTTP
        pass
    else:
        raise PlatformConfigError("%s 仅允许 HTTPS（本地 127.0.0.1/localhost 可例外）" % key, config_path=config_path)
    if not parsed.hostname or _HOST_PATTERN.match(parsed.hostname) is None:
        raise PlatformConfigError("%s 主机名不合法" % key, config_path=config_path)
    if parsed.username is not None or parsed.password is not None:
        raise PlatformConfigError("%s 不得包含用户名或密码" % key, config_path=config_path)
    try:
        parsed.port
    except ValueError:
        raise PlatformConfigError("%s 端口不合法" % key, config_path=config_path)
    if parsed.query or parsed.fragment:
        raise PlatformConfigError("%s 不得包含查询串或片段" % key, config_path=config_path)
    if "//" in parsed.path or any(ch.isspace() for ch in url):
        raise PlatformConfigError("%s 路径不合法" % key, config_path=config_path)
    return url
