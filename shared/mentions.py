"""提及呈现：两条输入形态共用的唯一转换实现。

个人与全体成员提及是「保持原消息形态」的唯一例外：转成不触发提醒的普通文本。
两端的输入字段名不同（宿主形态 `target_user_id` / `target_user_cardname` /
`target_user_nickname`，OneBot 形态 `qq` / `card` / `nickname` / `name`），但呈现
格式与压平口径必须逐字一致，因此都收敛到本模块：

- `flatten_mention_text`：不可打印字符转空格、折叠空白（只压平，不求值、不查询
  成员信息、不做模板展开）；
- `mention_display_text`：三种格式字面量——`［提及：显示名称（对象标识）］`、
  `［提及：对象标识］`、`［提及：全体成员］`（对象标识恰为 `all` 时）。

宿主与 OneBot 两侧只保留自己的字段映射与对象可确认性判定（例如只接受 `all`
或正整数 uin）；本模块不读取任何来源字段，也不判断对象是否可确认。
"""

from __future__ import annotations

from typing import Final

ALL_MENTION_IDENTIFIER: Final[str] = "all"
"""全体成员提及的对象标识（与平台侧同一字面量）。"""

ALL_MENTION_TEXT: Final[str] = "［提及：全体成员］"
"""全体成员提及的普通文本呈现：只表达含义，不形成提醒全群的段。"""

MENTION_WITH_NAME_FORMAT: Final[str] = "［提及：{name}（{identifier}）］"
"""个人提及带显示名称时的文本格式（名称与标识只来自源材料）。"""

MENTION_FORMAT: Final[str] = "［提及：{identifier}］"
"""个人提及只有对象标识时的文本格式（缺显示名不虚构）。"""


def flatten_mention_text(raw: object) -> str:
    """压平提及显示文本（显示名或对象标识）：不可打印字符转空格、折叠空白。

    文本来自源消息、属不可信内容：只做压平，不做模板或命令求值，也不查询成员
    信息。压平后可能是空串（原本只由控制字符／空白组成），空串的处置由调用方
    决定：显示名回退到对象标识，对象标识整条拒绝。
    """

    if not isinstance(raw, str) or not raw:
        return ""
    printable = "".join(" " if not character.isprintable() else character for character in raw)
    return " ".join(printable.split())


def mention_display_text(identifier: str, name: str = "") -> str:
    """按对象标识（与可选显示名称）给出不提醒的普通文本呈现。

    调用方必须先确认对象可确认（非空标识）；`identifier` 恰为 `all` 时按全体成员
    呈现（名称字段不参与），否则带名称给出 `［提及：名称（标识）］`、缺名称时给出
    `［提及：标识］`。
    """

    if identifier == ALL_MENTION_IDENTIFIER:
        return ALL_MENTION_TEXT
    if name:
        return MENTION_WITH_NAME_FORMAT.format(name=name, identifier=identifier)
    return MENTION_FORMAT.format(identifier=identifier)
