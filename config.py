"""麦麦搬运喵配置：开关、自动判断、转发分组及固定 SnowLuma 连接。

`[scope]` 保存具名转发分组集合（名称 + 源群号 + 目标群号 + 私聊开关），目标群须与
来源群同组；默认空分组集合，等价于没有源群也没有目标群。`[snowluma]` 保存地址、
可选令牌与凭证环境变量名，默认空；模型不能覆盖连接配置。`plugin.config_version`
是宿主要求的数字点分版本，新增字段时提升。评论延迟、概率与超时均不进入配置；
默认值与 config.toml 保持一致。
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from maibot_sdk import Field, PluginConfigBase

from .shared.config_rules import onebot_endpoint_rejection, valid_env_var_name

MessageType = Literal["text", "image", "forward"]
"""自动候选消息类型的唯一词汇来源。

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
            "hint": "默认关闭：安装后先填好转发分组再打开，避免安装即跨群发送。",
            "label": "启用插件",
            "order": 0,
        },
    )
    config_version: str = Field(
        default="1.7.0",
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


class ForwardGroupSection(PluginConfigBase):
    """一个具名转发分组：分组名、源群号、目标群号与私聊开关。

    `name` 只作用户标识与日志前缀，不参与路由、权限或判断，不限长与字符集。
    `private` 默认 `false`：为真时该分组的源群才允许私聊目标，且不与 `target_groups`
    互相替代或推导。`name` 给空串默认是为了让宿主 Schema 生成器能无参构造默认视图
    （宿主要求嵌套模型字段全有默认）；实际生效以 `ScopeSection.__init__` 的配置期
    校验为准：去空白后为空、跨组重复、源群重复与 `private` 非布尔一律拒绝。
    """

    __ui_label__: ClassVar[str] = "转发分组"

    name: str = Field(
        default="",
        description="分组名称：只作用户标识与日志前缀，不参与路由或判断，不限长与字符集。",
        json_schema_extra={
            "hint": "必填；去空白后不能为空，且不能与其他分组同名。",
            "label": "分组名称",
            "order": 0,
        },
    )
    source_groups: list[str] = Field(
        default_factory=list,
        description="本分组的源群号：允许麦麦从中转发消息的群；同一源群只能属于一个分组。",
        json_schema_extra={
            "hint": "留空 = 本分组没有源群；同一源群不得出现在多个分组或在本分组内重复。",
            "label": "源群号",
            "order": 1,
        },
    )
    target_groups: list[str] = Field(
        default_factory=list,
        description="本分组的目标群号：允许接收转发消息的群；可跨组重复，源群也可兼作别组目标。",
        json_schema_extra={
            "hint": "留空 = 本分组源群的调用与自动判断都因没有同组目标而拒绝；目标群可跨组重复。",
            "label": "目标群号",
            "order": 2,
        },
    )
    private: bool = Field(
        default=False,
        description="私聊开关：为真时本分组源群才允许把消息转发到私聊目标（对端须为账号好友）。",
        json_schema_extra={
            "hint": (
                "默认关闭；开启后本分组源群的显式调用才接受 target_user_id 私聊目标，"
                "且可用只读名册工具取好友列表；不影响群目标的同组判定。"
            ),
            "label": "私聊开关",
            "order": 3,
        },
    )


class ScopeSection(PluginConfigBase):
    """`[scope]` 节：具名转发分组。

    默认空分组集合：没有源群也没有目标群。平铺键 `source_groups` / `target_groups`
    已移除（无兼容期）：旧配置里的平铺键按未知键忽略，表现为「源群不在任何转发分组」
    的可定位拒绝，安全失效方向为不发送。空目标分组与组内「源 = 目标」允许在本层
    保存：运行期由「目标 ≠ 来源群自身」与同组没有合法目标拒绝。
    """

    __ui_label__: ClassVar[str] = "转发分组"
    __ui_order__: ClassVar[int] = 2

    groups: list[ForwardGroupSection] = Field(
        default_factory=list,
        description="具名转发分组：每个分组由名称、源群号与目标群号构成，目标群须与来源群同组。",
        json_schema_extra={
            "hint": (
                "留空 = 没有分组，不触发自动判断，也不接受工具调用；"
                "同一源群只能属于一个分组，分组名去空白后不能为空且跨组唯一。"
            ),
            "label": "转发分组",
            "order": 0,
        },
    )

    def __init__(self, **data: Any) -> None:
        """分组结构校验在 SDK 字段校验前完成（沿用 `SnowLumaSection` 先例）。

        四项结构检查：源群跨组重复、同一分组内源群重复、`name` 去空白后为空或跨组
        重复、`private` 非布尔；命中即抛中文 `ValueError`，不静默归并、不放行部分
        配置。dict 与已构造的 `ForwardGroupSection` 实例两条输入路径都校验——宿主
        Schema 链路会拿默认视图落回配置，实例路径漏检等于放开空白 name。`private`
        必须在字段校验前拦：宿主 Schema 的宽松归一会把 `"true"` / `1` 收成布尔，
        那时再拒绝已经晚了一帧。
        """

        groups = data.get("groups") or []
        if isinstance(groups, (list, tuple)):
            names: dict[str, int] = {}
            source_owner: dict[str, str] = {}
            for position, raw_group in enumerate(groups, start=1):
                if isinstance(raw_group, ForwardGroupSection):
                    group_name = raw_group.name
                    raw_sources: Any = raw_group.source_groups
                    raw_private: Any = raw_group.private
                elif isinstance(raw_group, dict):
                    group_name = raw_group.get("name")
                    raw_sources = raw_group.get("source_groups")
                    raw_private = raw_group.get("private", False)
                else:
                    continue
                name = str(group_name or "").strip()
                if not name:
                    raise ValueError(f"第 {position} 个转发分组的名称去空白后不能为空")
                if name in names:
                    raise ValueError(f"转发分组名称「{name}」跨分组重复，分组名称必须唯一")
                names[name] = position
                if not isinstance(raw_private, bool):
                    raise ValueError(
                        f"第 {position} 个转发分组的私聊开关 private 必须是布尔值（true 或 false）"
                    )
                if not isinstance(raw_sources, (list, tuple)):
                    continue
                seen_sources: set[str] = set()
                for raw_source in raw_sources:
                    # 群号按去空白字符串比对，与运行期 `group_id_entries` 口径一致；
                    # 空值不参与，非字符串由字段校验拒绝。
                    source = str(raw_source).strip()
                    if not source:
                        continue
                    if source in seen_sources:
                        raise ValueError(f"转发分组「{name}」的源群 {source} 重复，同一分组内源群不能重复")
                    seen_sources.add(source)
                    if source in source_owner:
                        raise ValueError(
                            f"源群 {source} 同时出现在转发分组「{source_owner[source]}」与「{name}」中，"
                            "同一源群只能属于一个分组"
                        )
                    source_owner[source] = name
        super().__init__(**data)


class SnowLumaSection(PluginConfigBase):
    """`[snowluma]` 节：固定正向 HTTP API 地址、令牌与可选凭证引用。"""

    __ui_label__: ClassVar[str] = "SnowLuma 直连"
    __ui_order__: ClassVar[int] = 3

    http_url: str = Field(
        default="",
        description="SnowLuma 已有正向 HTTP API 地址；回环、私有或链路本地目标可用 http，公网目标必须 https。",
        json_schema_extra={
            "hint": (
                "默认未配置；明文 http 只允许回环、私有或链路本地目标，主机名在建连前解析确认，"
                "解析出公网或失败即拒绝；禁止在地址中填写用户信息、查询串或片段，不自动发现端点；"
                "只按固定动作 POST /{action} 访问，认证令牌只经请求头传递。"
            ),
            "label": "HTTP 地址",
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
            "hint": "未填写令牌时，仅在发出请求前按名读取凭证；变量缺失或为空时拒绝请求。",
            "label": "凭证环境变量名",
            "order": 2,
        },
    )

    def __init__(self, **data: Any) -> None:
        """在 SDK 字段校验前拒绝危险地址，避免错误携带输入。

        地址只接受 http/https，协议内安全规则（明文公网、用户信息、查询串、片段、
        未指定监听地址）复用 `onebot_endpoint_rejection` 的单一定义处。
        """

        endpoint = data.get("http_url", "")
        if endpoint != "":
            if not isinstance(endpoint, str):
                raise ValueError("SnowLuma 直连地址必须为字符串")
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
