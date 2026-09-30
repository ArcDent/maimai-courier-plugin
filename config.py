"""麦麦搬运喵 — 配置模型。

字段面只有三块：**总开关**（`plugin.enabled`）、**自动入口**（`auto.enabled`
与两个判断输入：候选类型 `auto.message_types`、分享标准 `auto.share_criteria`）
以及**源／目标白名单**（`scope.source_groups` / `scope.target_groups`）；此外
`plugin.config_version` 是宿主强制项（SDK `extract_plugin_config_version` 缺它
即抛 `PluginConfigVersionError`），不是本插件的业务开关。评论延迟
（0.5 秒）、自动去重与等待上限都是代码常量，不提供随机概率、冷却或延迟配置项
（design D1）。默认值须与 config.toml 逐项一致（项目铁律）。
"""

from __future__ import annotations

from typing import ClassVar, Literal

from maibot_sdk import Field, PluginConfigBase

MessageType = Literal["text", "image", "forward"]
"""自动候选消息类型的唯一词汇来源（design D3）。

配置枚举 `auto.message_types` 与自动入口的候选分类共用本别名；宿主 Schema 生成器
识别 `list[Literal[...]]` 形态，因此别名必须在这两个消费方之一的被依赖方（本模块）
定义一次，禁止各写一份。
"""


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
        default="1.1.0",
        description="配置版本号；新增字段时提升（数字点分），宿主据此把新字段写回配置文件。",
        json_schema_extra={"disabled": True, "hidden": True, "label": "配置版本", "order": 99},
    )


class AutoSection(PluginConfigBase):
    """`[auto]` 节：自动判断入口开关、候选类型与分享标准。"""

    __ui_label__: ClassVar[str] = "自动入口"
    __ui_order__: ClassVar[int] = 1

    enabled: bool = Field(
        default=True,
        description="源群消息是否给源群 Maisaka 一次自动判断机会。",
        json_schema_extra={
            "hint": "只控制所选候选类型的自动判断机会，不影响总开关开启时的显式工具调用。",
            "label": "启用自动入口",
            "order": 0,
        },
    )
    message_types: list[MessageType] = Field(
        default_factory=lambda: ["forward"],
        description="允许自动进入判断的候选消息类型：普通文字、图片、合并转发。",
        json_schema_extra={
            "hint": (
                "默认仅合并转发；留空 = 停止所有自动判断机会（显式工具调用不受影响）；"
                "开启普通文字或图片会增加模型调用。"
            ),
            "label": "自动候选类型",
            "order": 1,
        },
    )
    share_criteria: str = Field(
        default="结合消息内容与目标群，自行判断是否值得分享；不值得或无法判断时保持沉默，不必回复，不要编造消息内容。",
        max_length=4000,
        description="传给源群 Maisaka 的自动分享标准：只作自然语言判断依据，不改变路由与权限。",
        json_schema_extra={
            "hint": "留空或全空白时回退默认标准；最多 4000 字符，超出会被配置校验拒绝。",
            "label": "自动分享标准",
            "max_length": 4000,
            "order": 2,
            "rows": 4,
            "x-widget": "textarea",
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
