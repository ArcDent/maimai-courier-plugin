"""麦麦搬运喵 — 配置模型。

字段面只有三块：**总开关**（`plugin.enabled`）、**自动入口开关**（`auto.enabled`）
与**源／目标白名单**（`scope.source_groups` / `scope.target_groups`）。评论延迟
（0.5 秒）、自动去重与等待上限都是代码常量，不提供随机概率、冷却或延迟配置项
（design D1）。默认值须与 config.toml 逐项一致（项目铁律）。
"""

from __future__ import annotations

from typing import ClassVar

from maibot_sdk import Field, PluginConfigBase


class PluginSection(PluginConfigBase):
    """`[plugin]` 节：插件总开关与配置版本。"""

    __ui_label__: ClassVar[str] = "插件开关"
    __ui_order__: ClassVar[int] = 0

    enabled: bool = Field(
        default=False,
        description="插件总开关，关闭后不做自动判断也不执行工具发送。",
        json_schema_extra={
            "hint": "默认关闭：安装后先填好白名单再打开，避免安装即跨群发送。",
            "label": "启用插件",
            "order": 0,
        },
    )
    config_version: str = Field(
        default="1.0.0",
        description="配置版本号。",
        json_schema_extra={"disabled": True, "hidden": True, "label": "配置版本", "order": 99},
    )


class AutoSection(PluginConfigBase):
    """`[auto]` 节：合并转发的自动判断入口开关。"""

    __ui_label__: ClassVar[str] = "自动入口"
    __ui_order__: ClassVar[int] = 1

    enabled: bool = Field(
        default=True,
        description="源群合并转发是否给源群 Maisaka 一次自动判断机会。",
        json_schema_extra={
            "hint": "只控制合并转发的自动判断机会，不影响总开关开启时的显式工具调用。",
            "label": "启用自动入口",
            "order": 0,
        },
    )


class ScopeSection(PluginConfigBase):
    """`[scope]` 节：源／目标白名单。"""

    __ui_label__: ClassVar[str] = "白名单"
    __ui_order__: ClassVar[int] = 2

    source_groups: list[str] = Field(
        default_factory=list,
        description="允许麦麦从中转发消息的源群号白名单。",
        json_schema_extra={
            "hint": "留空 = 没有源群，不触发自动判断，也不接受工具调用。",
            "label": "源群白名单",
            "order": 0,
        },
    )
    target_groups: list[str] = Field(
        default_factory=list,
        description="允许接收转发消息的目标群号白名单。",
        json_schema_extra={
            "hint": "留空 = 没有目标群，工具调用只能得到拒绝原因。",
            "label": "目标群白名单",
            "order": 1,
        },
    )


class CourierConfig(PluginConfigBase):
    """插件配置模型：三个节，无其他字段面。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    auto: AutoSection = Field(default_factory=AutoSection)
    scope: ScopeSection = Field(default_factory=ScopeSection)
