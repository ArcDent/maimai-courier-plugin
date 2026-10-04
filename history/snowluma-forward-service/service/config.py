"""服务端受控配置：字段模型、校验与加载（design D5/D10）。

- 配置只描述**受控部署**事实：绑定账号、预期账本身份、状态目录、监听地址、源/目标
  许可上限，以及凭证的**环境变量名**（不是凭证值）；模型参数与请求字段都不能覆盖；
- 默认只监听回环地址；跨主机必须配置 TLS 证书与私钥（校验证书）或改用受控加密隧道；
- 加载失败一律返回 `ConfigUnavailable`（带可定位中文原因），绝不静默回落默认值，
  也绝不把缺失当成空配置继续启动。
"""

from __future__ import annotations

import tomllib
from dataclasses import MISSING, dataclass, fields
from pathlib import Path
from typing import Any, Final, Mapping
from urllib.parse import urlsplit

from shared.config_rules import LOOPBACK_HOSTS, is_loopback_host, valid_env_var_name
from shared.identifiers import verbatim_identifier

DEFAULT_LISTEN_HOST: Final[str] = "127.0.0.1"
"""默认监听地址：只在本机回环可用（design D5）。"""

DEFAULT_LISTEN_PORT: Final[int] = 8790
"""默认监听端口：部署时按实际情况填写，不影响协议与状态机。"""

DEFAULT_TOKEN_ENV_VAR: Final[str] = "MAIMAI_COURIER_SERVICE_TOKEN"
"""默认凭证环境变量名：凭证值只从该变量读取，配置文件里只出现变量名。"""

ONEBOT_ENDPOINT_SCHEMES: Final[frozenset[str]] = frozenset({"ws", "wss"})
"""允许的 OneBot 连接协议（受控地址；明文 http 不在其中）。

非回环主机只允许 `wss`（远程明文 `ws://` 会让 Bearer 凭证明文出网）。
"""


def onebot_endpoint_rejection(endpoint: str) -> ConfigUnavailable | None:
    """OneBot 连接地址的受控校验；`config` 校验与 `onebot` 解析共用同一实现。

    规则：只接受 `ws://` / `wss://`；必须带主机名；不得携带用户信息（凭证只从
    环境变量读取）；不得携带查询串或片段（不把凭证写进配置文件的地址里）；非回环
    主机必须 `wss`。通过返回 `None`，否则返回确定的不可用原因。
    """

    try:
        parts = urlsplit(endpoint)
        host = parts.hostname or ""
    except ValueError:
        return ConfigUnavailable("OneBot 连接地址结构非法（不接受任意地址）")
    if parts.scheme not in ONEBOT_ENDPOINT_SCHEMES:
        return ConfigUnavailable(
            "OneBot 连接地址必须以 ws:// 或 wss:// 开头（受控地址，远程必须 wss）"
        )
    if not host:
        return ConfigUnavailable("OneBot 连接地址缺少主机名（不接受任意地址）")
    if parts.username is not None or parts.password is not None:
        return ConfigUnavailable(
            "OneBot 连接地址不得包含用户信息（凭证只从环境变量读取，不写进配置文件的地址）"
        )
    if parts.query or parts.fragment:
        return ConfigUnavailable(
            "OneBot 连接地址不得包含查询串或片段（不把凭证写进配置文件的地址里）"
        )
    if parts.scheme == "ws" and not is_loopback_host(host):
        return ConfigUnavailable(
            "非回环主机的 OneBot 连接必须使用 wss://（远程明文 WebSocket 会让 Bearer 凭证明文出网）"
        )
    return None


@dataclass(frozen=True, slots=True)
class ConfigUnavailable:
    """配置不可用：带可定位中文原因；调用方必须停止启动，不做任何网络动作。"""

    reason: str


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    """服务端受控配置（字段名即配置文件 `[service]` 的键名）。

    凭证值**不属于**本模型：只保存 `token_env_var` / `onebot_token_env_var` 这两个
    环境变量名。
    """

    account_id: str
    state_dir: str
    client_identity: str
    ledger_id: str = ""
    listen_host: str = DEFAULT_LISTEN_HOST
    listen_port: int = DEFAULT_LISTEN_PORT  # 0 = 由系统分配临时端口（就绪行报告真实端口）
    token_env_var: str = DEFAULT_TOKEN_ENV_VAR
    allowed_source_groups: tuple[str, ...] = ()
    allowed_target_groups: tuple[str, ...] = ()
    tls_cert_file: str = ""
    tls_key_file: str = ""
    onebot_endpoint: str = ""
    onebot_token_env_var: str = ""

    @property
    def loopback_only(self) -> bool:
        """监听地址是否只在本机回环可用。"""

        return str(self.listen_host).strip().lower() in LOOPBACK_HOSTS


def validate_service_config(config: ServiceConfig) -> ConfigUnavailable | None:
    """校验配置：通过返回 `None`，否则给出确定的不可用原因。"""

    if not verbatim_identifier(config.account_id):
        return ConfigUnavailable("绑定账号必须是安全可引用的标识符（非空、无空白/控制字符/尖括号/引号/反引号）")
    if not verbatim_identifier(config.client_identity):
        return ConfigUnavailable("客户端身份必须是安全可引用的标识符")
    if not isinstance(config.state_dir, str) or not config.state_dir.strip():
        return ConfigUnavailable("状态目录必须显式配置（服务端唯一持久状态目录）")
    if config.ledger_id and not verbatim_identifier(config.ledger_id):
        return ConfigUnavailable("预期账本身份必须是安全可引用的标识符")
    if not isinstance(config.token_env_var, str) or not valid_env_var_name(config.token_env_var):
        return ConfigUnavailable("凭证环境变量名不合法（只允许字母、数字与下划线，且不以数字开头）")
    if not isinstance(config.listen_host, str) or not config.listen_host.strip():
        return ConfigUnavailable("监听地址必须显式配置")
    if (
        not isinstance(config.listen_port, int)
        or isinstance(config.listen_port, bool)
        or not 0 <= config.listen_port <= 65535
    ):
        return ConfigUnavailable(
            "监听端口必须是 0–65535 之间的整数（0 表示由系统分配临时端口并如实报告）"
        )
    for label, groups in (
        ("源群", config.allowed_source_groups),
        ("目标群", config.allowed_target_groups),
    ):
        for group in groups:
            if not verbatim_identifier(group):
                return ConfigUnavailable(f"{label}许可上限含非法群号（不净化、不忽略）")
    if bool(config.tls_cert_file) != bool(config.tls_key_file):
        return ConfigUnavailable("TLS 必须同时配置证书与私钥")
    if not config.loopback_only and not (config.tls_cert_file and config.tls_key_file):
        return ConfigUnavailable(
            "跨主机监听必须配置 TLS 证书与私钥（校验证书），或改用受控加密隧道；禁止明文暴露"
        )

    endpoint = str(config.onebot_endpoint or "").strip()
    if endpoint:
        rejection = onebot_endpoint_rejection(endpoint)
        if rejection is not None:
            return rejection
        if not str(config.onebot_token_env_var or "").strip():
            return ConfigUnavailable(
                "配置了 OneBot 连接地址就必须同时配置凭证环境变量名（连接只使用服务端受控凭证）"
            )
    if config.onebot_token_env_var and not valid_env_var_name(config.onebot_token_env_var):
        return ConfigUnavailable(
            "OneBot 凭证环境变量名不合法（只允许字母、数字与下划线，且不以数字开头）"
        )
    return None


def load_service_config(path: Path) -> ServiceConfig | ConfigUnavailable:
    """读取并校验 TOML 配置；任何失败（不可读、坏 TOML、缺必填键、校验不过）都转成
    `ConfigUnavailable`，不抛给调用方。"""

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return ConfigUnavailable("服务配置不存在或不可读，已停止启动（未建立任何网络连接）")
    try:
        data = tomllib.loads(raw)
    except (tomllib.TOMLDecodeError, ValueError):
        return ConfigUnavailable("服务配置不是合法 TOML，已停止启动")
    table = data.get("service")
    if not isinstance(table, Mapping):
        return ConfigUnavailable("服务配置缺少 [service] 段，已停止启动")

    model_fields = fields(ServiceConfig)
    known = {field.name for field in model_fields}
    unknown = sorted(str(key) for key in table if key not in known)
    if unknown:
        return ConfigUnavailable(f"服务配置包含未知字段：{', '.join(unknown)}（不忽略拼写错误）")
    missing = sorted(
        field.name
        for field in model_fields
        if field.default is MISSING and field.default_factory is MISSING and field.name not in table
    )
    if missing:
        return ConfigUnavailable(
            f"服务配置缺少必填字段：{', '.join(missing)}（不把缺失当成默认值继续启动）"
        )

    values: dict[str, Any] = dict(table)
    for key in ("allowed_source_groups", "allowed_target_groups"):
        listed = values.get(key)
        if listed is None:
            continue
        if not isinstance(listed, list):
            return ConfigUnavailable(f"配置字段 {key} 必须是数组")
        values[key] = tuple(listed)

    config = ServiceConfig(**values)
    invalid = validate_service_config(config)
    if invalid is not None:
        return invalid
    return config
