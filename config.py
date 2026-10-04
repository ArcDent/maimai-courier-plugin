"""麦麦搬运喵配置：开关、自动判断、白名单及固定 SnowLuma 连接。

`[snowluma]` 保存地址、可选令牌与凭证环境变量名，默认空；模型不能覆盖连接配置。
`plugin.config_version` 是宿主要求的数字点分版本，新增字段时提升。
评论延迟、概率与超时均不进入配置；默认值与 config.toml 保持一致。
旧服务节只保留退役诊断，不提供活跃配置模型或备用通道。
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from maibot_sdk import Field, PluginConfigBase

from .shared.config_rules import onebot_endpoint_rejection, valid_env_var_name

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
        default="1.4.0",
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


class SnowLumaSection(PluginConfigBase):
    """`[snowluma]` 节：固定正向连接地址、令牌与可选凭证引用。"""

    __ui_label__: ClassVar[str] = "SnowLuma 直连"
    __ui_order__: ClassVar[int] = 3

    websocket_url: str = Field(
        default="",
        description="SnowLuma 已有正向 WebSocket 地址；回环、私有或链路本地目标可用 ws，公网目标必须 wss。",
        json_schema_extra={
            "hint": "默认未配置；明文 ws 只允许回环、私有或链路本地目标，主机名在建连前解析确认，解析出公网或失败即拒绝；禁止在地址中填写凭证、查询串或片段，不自动发现端点。",
            "label": "WebSocket 地址",
            "order": 0,
        },
    )
    token: str = Field(
        default="",
        repr=False,
        description="SnowLuma 令牌；填写后优先使用，留空或全空白时使用凭证环境变量。",
        json_schema_extra={
            "hint": "令牌会明文保存于插件配置；请限制文件权限，不要将真实令牌提交到仓库。",
            "label": "连接令牌",
            "order": 1,
        },
    )
    token_env_var: str = Field(
        default="",
        description="保存 SnowLuma 凭证的环境变量名；只填变量名，不填凭证值。",
        json_schema_extra={
            "hint": "未填写令牌时，建连才按名取凭证；变量缺失或为空时拒绝连接。",
            "label": "凭证环境变量名",
            "order": 2,
        },
    )


    def __init__(self, **data: Any) -> None:
        """在 SDK 字段校验前拒绝危险地址，避免错误携带输入。"""

        endpoint = data.get("websocket_url", "")
        if endpoint != "":
            reason = onebot_endpoint_rejection(endpoint)
            if reason:
                raise ValueError(reason)
        token = data.get("token", "")
        if not isinstance(token, str):
            raise ValueError("连接令牌必须为字符串")
        token_env_var = data.get("token_env_var", "")
        if token_env_var != "" and (not isinstance(token_env_var, str) or not valid_env_var_name(token_env_var)):
            raise ValueError("凭证环境变量名非法（只允许字母、数字与下划线，且不以数字开头）")
        super().__init__(**data)


class CourierConfig(PluginConfigBase):
    """插件配置模型：四个节，无其他字段面。"""

    # SDK 基类使用 pydantic；其嵌套校验错误默认会带原输入，必须在根模型隐藏。
    model_config: ClassVar[dict[str, Any]] = {"hide_input_in_errors": True}

    plugin: PluginSection = Field(default_factory=PluginSection)
    auto: AutoSection = Field(default_factory=AutoSection)
    scope: ScopeSection = Field(default_factory=ScopeSection)
    snowluma: SnowLumaSection = Field(default_factory=SnowLumaSection)
