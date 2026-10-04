"""服务端认证：Bearer 凭证只从指定环境变量读取（design D5）。

- 配置里只有凭证的**环境变量名**；值只在进程环境里，不落盘、不进日志、不回显；
- 比较用 `hmac.compare_digest`（定长比较，不泄漏长度差异）；请求头名不区分大小写；
- 环境变量缺失或为空时**不认证任何请求**（返回 `None`），由 HTTP 层统一 401。
"""

from __future__ import annotations

import hmac
import os
from typing import Final, Mapping

AUTHORIZATION_HEADER: Final[str] = "authorization"
"""认证请求头名（比较时统一小写）。"""

BEARER_PREFIX: Final[str] = "bearer "
"""Bearer 凭证前缀（大小写不敏感）。"""


def _bearer_token(headers: Mapping[str, str]) -> str:
    """从请求头取 Bearer 凭证；非 Bearer 形态一律视为没有凭证。"""

    for key, value in headers.items():
        if str(key).lower() != AUTHORIZATION_HEADER:
            continue
        text = str(value).strip()
        if text.lower().startswith(BEARER_PREFIX):
            return text[len(BEARER_PREFIX) :].strip()
        return ""
    return ""


def credential_available(environ: Mapping[str, str], token_env_var: str) -> bool:
    """凭证环境变量是否已提供非空值（只判断存在性，不返回也不记录值）。"""

    return bool(str(environ.get(token_env_var, "") or ""))


class EnvTokenAuthGate:
    """环境变量凭证门：认证成功返回固定主体标识，否则返回 `None`。

    主体与服务端受控配置里的账号绑定由部署保证：一个服务实例只服务一个客户端主体。
    """

    def __init__(
        self,
        *,
        token_env_var: str,
        subject: str,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        environment = os.environ if environ is None else environ
        self._token = str(environment.get(token_env_var, "") or "")
        self._subject = subject

    @property
    def credential_available(self) -> bool:
        """环境变量里是否有可用凭证（没有时本门不会认证任何请求）。"""

        return bool(self._token)

    def authenticate(self, request: object) -> str | None:
        """校验请求头里的 Bearer 凭证；不返回凭证本身，只返回主体标识。"""

        headers = getattr(request, "headers", None)
        if not isinstance(headers, Mapping):
            return None
        provided = _bearer_token(headers)
        if not provided or not self._token:
            return None
        if not hmac.compare_digest(provided, self._token):
            return None
        return self._subject
