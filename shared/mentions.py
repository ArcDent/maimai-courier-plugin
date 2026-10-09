"""提及呈现：两条输入形态共用的唯一转换实现。

个人与全体成员提及是「保持原消息形态」的唯一例外：转成不触发提醒的普通文本。
宿主与 OneBot 两侧输入字段名不同，但呈现格式与压平口径必须逐字一致，因此收敛到
本模块；本模块不读取来源字段，对象可确认性判定归两侧各自负责。
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

    文本来自源消息、属不可信内容：只压平，不求值、不查询成员信息。压平后可能是
    空串，处置由调用方决定：显示名回退对象标识，对象标识整条拒绝。
    """

    if not isinstance(raw, str) or not raw:
        return ""
    printable = "".join(" " if not character.isprintable() else character for character in raw)
    return " ".join(printable.split())


def mention_display_text(identifier: str, name: str = "") -> str:
    """按对象标识（与可选显示名称）给出不提醒的普通文本呈现。

    调用方必须先确认对象可确认；`identifier` 恰为 `all` 时按全体成员呈现（名称
    不参与），否则带名称给出 `［提及：名称（标识）］`、缺名称给出 `［提及：标识］`。
    """

    if identifier == ALL_MENTION_IDENTIFIER:
        return ALL_MENTION_TEXT
    if name:
        return MENTION_WITH_NAME_FORMAT.format(name=name, identifier=identifier)
    return MENTION_FORMAT.format(identifier=identifier)
