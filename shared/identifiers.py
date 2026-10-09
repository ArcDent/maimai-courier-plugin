"""标识符规则：载荷派生 ID 与请求字段共用的唯一字符/长度实现。

规则：非空、限长 `MAX_IDENTIFIER_LENGTH`、无空白与控制字符、不含
`IDENTIFIER_FORBIDDEN_CHARACTERS`；不合格一律返回空串，由调用方按「拒绝 / 放弃
候选」处理——标识符必须原样可用，不能净化成另一个字符串。两种入口只差首尾空白：
`safe_identifier` 先裁剪再校验（宿主载荷口径），`verbatim_identifier` 逐字校验
（源记录与请求字段口径，任何空白都直接不合格）。
"""

from __future__ import annotations

from typing import Any, Final

MAX_IDENTIFIER_LENGTH: Final[int] = 128
"""标识符（消息 ID / 会话 ID / 群号 / 投递 ID）的长度上限：超出即不合格。"""

IDENTIFIER_FORBIDDEN_CHARACTERS: Final[frozenset[str]] = frozenset('<>"`')
"""标识符禁止字符：尖括号、双引号与反引号（引用/转义语义里不安全）。"""


def _checked(value: Any) -> str:
    """字符与长度规则本体：通过返回原文，否则返回空串。"""

    if not isinstance(value, str) or not value or len(value) > MAX_IDENTIFIER_LENGTH:
        return ""
    if any(character.isspace() or ord(character) < 32 for character in value):
        return ""
    if IDENTIFIER_FORBIDDEN_CHARACTERS.intersection(value):
        return ""
    return value


def safe_identifier(raw: Any) -> str:
    """载荷派生标识符（消息 ID / 会话 ID）：允许首尾空白，裁剪后再走同一套规则。"""

    return _checked(raw.strip() if isinstance(raw, str) else raw)


def verbatim_identifier(raw: Any) -> str:
    """请求字段标识符：逐字校验，任何空白（含首尾）都直接判不合格。

    请求来自网络对端，不能把 `" 10001"` 悄悄当成 `"10001"`：那会变成对不上真实
    消息的另一个字符串，必须按非法 ID 拒绝。
    """

    return _checked(raw)
