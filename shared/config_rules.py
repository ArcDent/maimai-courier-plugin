"""受控配置取值规则：环境变量名文法与主机地址判定的单一定义处。

消费者是 `config.py`（配置期地址/凭证变量名校验）与 `snowluma_transport.py`
（建连前解析判定）：两者必须对「什么算合法的凭证环境变量名」与「什么算可信
主机」给出同一判定。本模块只做字面量判定、不做 DNS 解析、不依赖第三方库。

固定正向端点的口径：`https` 只做协议与形态校验；`http` 明文在配置期拒绝公网 IP
字面量，回环/私有/链路本地字面量与主机名放行；主机名是否可信在建立连接前另行
解析判定，不在配置期做 DNS。
"""

from __future__ import annotations

import ipaddress
import re
from typing import Final
from urllib.parse import urlsplit

LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})
"""视为回环的主机字面量（不做 DNS 解析）。"""

ENDPOINT_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})
"""固定正向端点只允许的两种协议：HTTP API 直连，无 ws 入口。"""

_ENV_VAR_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
"""环境变量名文法：字母/下划线开头，后跟字母、数字或下划线。"""


def valid_env_var_name(name: str) -> bool:
    """仅校验可选凭证环境变量名；不读取令牌或环境值。"""

    return bool(_ENV_VAR_NAME_PATTERN.match(str(name or "")))


def _host_text(host: str) -> str:
    """主机字面量归一化：去空白/大小写/尾点、去 `[]` 包装与名字区域后缀。"""

    text = str(host or "").strip().lower().rstrip(".")
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return text.split("%", 1)[0]


def is_loopback_host(host: str) -> bool:
    """主机字面量是否回环：`localhost` / 回环 IP，容忍尾点与 `[]` 包装。

    只做字面量判定、不做 DNS 解析：`127.0.0.1.`、`[::1]`、`LOCALHOST` 都按回环
    处理（配置期校验与建连前判定同一份口径）；解析不了的一律不是回环。
    """

    text = _host_text(host)
    if not text:
        return False
    if text in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def is_private_host(host: str) -> bool:
    """主机字面量是否回环、私有、链路本地或 ULA；只做字面量判定、不做 DNS 解析。

    用于「明文连接是否只面向可信网络」的判定：回环、RFC 1918 私有地址、链路本地、
    ULA 与 IPv4 映射地址按可信处理；主机名（容器名等）与公网地址都不是，必须先经
    建连前解析判定。解析器给出的区域后缀（如 `fe80::1%en0`）不改变地址族判定。
    """

    if is_loopback_host(host):
        return True
    text = _host_text(host)
    if not text:
        return False
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    # 与 `onebot_endpoint_rejection` 同一口径：映射地址显式按底层 IPv4 判定。
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    # ULA 已包含在标准库 is_private 中，无需不存在的 is_unique_local 属性。
    return bool(address.is_private or address.is_link_local)


def onebot_endpoint_rejection(endpoint: object) -> str | None:
    """校验固定正向端点地址（http/https），只返回安全原因；不解析 DNS、不回显输入。

    加密协议（`https`）只做协议与形态校验；`http` 明文在配置期拒绝公网 IP 字面量，
    回环/私有/链路本地字面量与主机名放行，主机名是否解析到可信网络由建连前检查
    负责。四条形态规则——用户信息、查询串、片段、未指定监听地址——对两种协议
    一视同仁。
    """

    if not isinstance(endpoint, str) or not endpoint:
        return "未配置 SnowLuma 地址"
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in endpoint):
        return "SnowLuma 地址不得包含空白或控制字符"
    if any(char in endpoint for char in ("\\", "?", "#")):
        return "SnowLuma 地址不得包含反斜杠、查询串或片段"
    try:
        parts = urlsplit(endpoint)
        host = parts.hostname or ""
        port = parts.port
        if parts.scheme not in ENDPOINT_SCHEMES:
            return "SnowLuma 地址必须使用 http 或 https 协议"
        if parts.username is not None or parts.password is not None:
            return "SnowLuma 地址不得包含用户信息或凭证"
        if not host or port == 0 or parts.netloc.endswith(":"):
            return "SnowLuma 地址缺少合法主机或端口"
        literal = host.rstrip(".")
        address: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
        try:
            address = ipaddress.ip_address(literal)
        except ValueError:
            if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", literal):
                return "SnowLuma 地址主机形态非法"
            if re.fullmatch(r"(?:0[xX][0-9A-Fa-f]+|[0-9]+)(?:\.(?:0[xX][0-9A-Fa-f]+|[0-9]+))*", literal):
                return "SnowLuma 地址必须使用完整 IP 字面量"
        else:
            # Python 3.11 的 IPv6 映射地址属性不透传 IPv4，显式按底层地址检查。
            if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
                address = address.ipv4_mapped
            if address.is_unspecified:
                return "SnowLuma 连接目标不得使用未指定监听地址"
        if parts.scheme == "http" and address is not None and not is_private_host(literal):
            return "非回环或私有的 SnowLuma 连接必须使用 https"
    except ValueError:
        return "SnowLuma 地址形态非法"
    return None
