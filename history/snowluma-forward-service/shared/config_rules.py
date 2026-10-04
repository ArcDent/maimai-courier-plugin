"""受控配置取值规则：环境变量名文法与回环主机判定（design D9 的单一定义处）。

插件客户端（`remote_client.py` 的服务地址与凭证检查）与服务端（`service/config.py`
的监听地址、OneBot 连接与凭证变量名校验）必须对「什么算合法的凭证环境变量名」与
「什么算回环主机」给出同一判定：这两条规则曾经在两侧各写一份，回环判定还出现了
漂移——`127.0.0.1.`（带尾点的同一地址）服务端放行、客户端拒绝。本模块是这两条规则
的唯一定义处，两侧只复用；只做字面量判定、不做 DNS 解析，也不依赖第三方库。
"""

from __future__ import annotations

import ipaddress
import re
from typing import Final

LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})
"""视为回环的主机字面量（不做 DNS 解析）。"""

_ENV_VAR_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
"""环境变量名文法：字母/下划线开头，后跟字母、数字或下划线。"""


def valid_env_var_name(name: str) -> bool:
    """凭证环境变量名是否合法（配置里只出现变量名，凭证值只从环境读取）。"""

    return bool(_ENV_VAR_NAME_PATTERN.match(str(name or "")))


def is_loopback_host(host: str) -> bool:
    """主机字面量是否回环：`localhost` / 回环 IP，容忍尾点与 `[]` 包装。

    只做字面量判定、不做 DNS 解析：`127.0.0.1.`、`[::1]`、`LOCALHOST` 都按回环
    处理（服务端监听判定与客户端服务地址判定同一份口径）；解析不了的一律不是回环。
    """

    text = str(host or "").strip().lower().rstrip(".")
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if not text:
        return False
    if text in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False
