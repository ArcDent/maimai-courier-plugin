"""客户端接入测试设施（第 6 组）：真实插件 → 真实客户端 → 真实服务 → 模拟 OneBot。

本模块把第 5 组已经完成的服务端设施（`tests/service_support.py` 的
`launch_authorized_service`）接成客户端用例的夹具：

- `ServiceAppTransport`：插件侧 `remote_client.HttpTransport` 的可注入替身，把请求
  交给真实 `service.http_api.ServiceApp`（含真实路由与认证中间件），全程在内存里、
  不连接任何真实地址；支持注入「响应丢失」与「不可达」，供有界结果恢复用例使用；
- `ScriptedServiceTransport`：协议级替身，按脚本返回 v1 响应（用于 `sending` /
  `failed` / `unknown` / 落盘失败等真实服务难以自然构造的映射用例），只记录请求、
  不发送任何平台消息；
- `ClientServiceFixture`：装配「真实插件 + 真实服务会话 + 真实服务 + 模拟 OneBot」，
  对外只暴露公共入口（工具调用、组件声明、生命周期、服务只读查询）；
- `make_client_service(...)` / `make_scripted_plugin(...)`：一把构造上述两种夹具。

宿主侧源消息（`FakePluginContext`）与网关侧源消息（`FakeOneBot`）分开装配：客户端
预检读宿主副本，服务端核对读网关副本；`onebot_segments(...)` 负责把宿主形态的段
序列转换成网关存储副本形态（图片 → 已核实的 https 媒体引用、卡片 → 资源引用），
与第 1 组核实的两套形态一致。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from delivery_ledger import DeliveryKey
from remote_client import TransportReply, TransportUnavailable
from service.http_api import HttpRequest, ServiceApp
from shared.protocol import delivery_id_for

from tests.service_support import (
    AuthorizedService,
    DEFAULT_TOKEN,
    FakeOneBot,
    launch_authorized_service,
    result_of,
    store_source_message,
)
from plugin import create_plugin

from tests.support import (
    SOURCE_ACCOUNT_ID,
    SOURCE_GROUP_ID,
    TARGET_GROUP_ID,
    FakeClock,
    enabled_config,
    make_plugin,
    prepare_source,
)

DEFAULT_SERVICE_TOKEN_ENV = "COURIER_SERVICE_TOKEN"
"""客户端凭证环境变量名（与配置模型默认一致，合成值）。"""

DEFAULT_SERVICE_TOKEN = DEFAULT_TOKEN
"""合成服务凭证：与服务端夹具的认证替身同一取值，只在内存环境映射里使用。"""

DEFAULT_MIGRATION_CONFIRMATION = "migration-2026-10-02"
"""合成迁移确认标识（显式确认本次数据集；留空时插件应关闭发送）。"""

DEFAULT_RESOURCE_ID = "res-card-synthetic"
"""合成卡片资源引用（服务端只接受可信源消息里的顶层 `forward.data.id`）。"""

DEFAULT_MEDIA_URL = "https://cdn.example.invalid/media.png"
"""合成图片媒体地址（https、非本机/内网；服务端只接受可安全重放的引用）。"""

DEFAULT_SOURCE_USER_ID = "34567"
"""合成源消息作者（显式工具调用不限制作者，仅用于构造可信源消息）。"""


def client_service_config(
    ledger_identity: str,
    *,
    endpoint: str = "http://127.0.0.1:8787",
    token_env_var: str = DEFAULT_SERVICE_TOKEN_ENV,
    migration_confirmation: str = DEFAULT_MIGRATION_CONFIRMATION,
    source_groups: Sequence[str] = (SOURCE_GROUP_ID,),
    target_groups: Sequence[str] = (TARGET_GROUP_ID,),
    message_types: Sequence[str] = ("forward",),
) -> dict[str, Any]:
    """构造「总开关开启 + 服务绑定」的插件配置（供自建传输/真实回环用例复用）。"""

    config = enabled_config(
        source_groups=tuple(source_groups),
        target_groups=tuple(target_groups),
        message_types=tuple(message_types),
    )
    config["service"] = {
        "endpoint": endpoint,
        "client_identity": "maibot-plugin",
        "token_env_var": token_env_var,
        "expected_ledger_id": ledger_identity,
        "migration_confirmation": migration_confirmation,
    }
    return config


class MirrorClock(FakeClock):
    """插件时钟：`advance` 同时推进服务端时钟，两侧共享同一虚拟时间轴。

    插件与服务端各持有独立 `FakeClock`（避免服务端内部网关超时 `sleep(10)` 污染
    插件的等待记录），但测试推进插件时钟时服务端时钟必须同步前进——否则源成功后的
    0.5 秒最短间隔校验永远不满足。`sleeps` 因此只记录**插件侧**登记的等待。
    """

    def __init__(self, mirror: FakeClock, *, start: float = 0.0) -> None:
        super().__init__(start=start)
        self._mirror = mirror

    def advance(self, seconds: float) -> None:
        super().advance(seconds)
        self._mirror.advance(seconds)


def media_url_for(ordinal: int) -> str:
    """第 n 张图片的合成媒体地址（与服务端夹具的图片转换规则一致）。"""

    if ordinal <= 1:
        return DEFAULT_MEDIA_URL
    return DEFAULT_MEDIA_URL.replace(".png", f"-{ordinal}.png")


def config_with(base_plugin: Any, **overrides: Any) -> dict[str, Any]:
    """基于插件当前配置深拷贝并按节点覆盖（保留 `[service]` 绑定，供热更新用例）。

    宿主热更新注入的是归一后的完整配置；用例只改需要的节点，其余（尤其服务绑定）
    必须与当前会话一致，避免「改白名单顺带清空账本绑定」这类夹具误差。
    """

    import copy

    base = copy.deepcopy(base_plugin.config.model_dump())
    for section, values in overrides.items():
        node = base.setdefault(section, {})
        if not isinstance(node, dict) or not isinstance(values, dict):
            raise TypeError(f"配置节点 {section} 必须按映射覆盖")
        node.update(values)
    return base


def onebot_segments(
    raw_message: Sequence[Mapping[str, Any]],
    *,
    resource_id: str = DEFAULT_RESOURCE_ID,
    media_url: str | None = None,
) -> list[dict[str, Any]]:
    """把宿主形态段序列转换成网关存储副本的 OneBot 形态。

    映射依据第 1 组核实：文本 `{"type":"text","data":{"text":…}}`、提及
    `{"type":"at","data":{"qq":…}}`（显示名可选 `card`/`nickname`）、图片
    `{"type":"image","data":{"url":…}}`、卡片 `{"type":"forward","data":{"id":…}}`。
    未展开的卡片包装（宿主 `dict` 段）在网关侧仍是原 `forward` 段，因此同样取
    资源引用；其余段类型原样保留，交由服务端按不支持段整条拒绝。
    """

    converted: list[dict[str, Any]] = []
    image_index = 0
    for segment in raw_message:
        segment_type = str(segment.get("type") or "")
        if segment_type == "text":
            payload = segment.get("data")
            text = payload if isinstance(payload, str) else ""
            converted.append({"type": "text", "data": {"text": text}})
            continue
        if segment_type == "at":
            converted.append(_onebot_at(segment))
            continue
        if segment_type == "image":
            image_index += 1
            converted.append(
                {"type": "image", "data": {"url": media_url or media_url_for(image_index)}}
            )
            continue
        if segment_type == "forward":
            converted.append({"type": "forward", "data": {"id": resource_id}})
            continue
        if segment_type == "dict":
            inner = segment.get("data")
            if isinstance(inner, Mapping) and str(inner.get("type") or "") == "forward":
                converted.append({"type": "forward", "data": {"id": resource_id}})
                continue
        converted.append(dict(segment))
    return converted


def _onebot_at(segment: Mapping[str, Any]) -> dict[str, Any]:
    """宿主提及段 → OneBot 提及段：标识换 `qq`，显示名按群名片优先映射。"""

    data = segment.get("data")
    fields = data if isinstance(data, Mapping) else {}
    payload: dict[str, Any] = {"qq": fields.get("target_user_id")}
    cardname = fields.get("target_user_cardname")
    nickname = fields.get("target_user_nickname")
    if isinstance(cardname, str) and cardname:
        payload["card"] = cardname
    elif isinstance(nickname, str) and nickname:
        payload["nickname"] = nickname
    return {"type": "at", "data": payload}


def _host_raw_message(raw_message: Sequence[Any]) -> list[Any]:
    """宿主 raw_message 深拷贝：结构异常的元素原样保留（供「结构异常」用例）。"""

    return [dict(segment) if isinstance(segment, Mapping) else segment for segment in raw_message]


class ServiceAppTransport:
    """把插件侧 HTTP 请求交给真实 `ServiceApp` 的内存传输（不连接任何真实地址）。

    控制面：
    - `fail_paths`：命中的请求**不进入服务端**、直接按不可达抛出（模拟服务不可达）；
    - `drop_response_paths`：命中的请求**先真实进入服务端**（副作用照常发生），再把
      响应丢失（模拟「执行已发生但客户端没收到响应」）；
    - `requests` 记录全部请求（含被丢弃响应的请求），供「只查询同一投递、不重复提交
      发送探测」一类断言。

    路径匹配按请求路径的**结尾**判定（如 `"/source"` 命中
    `/v1/forwards/<id>/source`）。
    """

    def __init__(self, app: ServiceApp) -> None:
        self.app = app
        self.requests: list[HttpRequest] = []
        self.fail_paths: list[str] = []
        self.drop_response_paths: list[str] = []
        self.closed = False

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
    ) -> TransportReply:
        parts = urlsplit(url)
        path = parts.path if not parts.query else f"{parts.path}?{parts.query}"
        request = HttpRequest(method=method, path=path, headers=dict(headers), body=body)
        self.requests.append(request)
        if any(path.endswith(suffix) for suffix in self.fail_paths):
            raise TransportUnavailable("模拟转发服务不可达（不进入服务端）")
        response = await self.app.handle(request)
        if any(path.endswith(suffix) for suffix in self.drop_response_paths):
            raise TransportUnavailable("模拟执行响应丢失（服务端已处理，客户端未收到响应）")
        return TransportReply(status=response.status, payload=dict(response.payload))

    async def close(self) -> None:
        """内存传输无可释放资源（幂等）。"""

        self.closed = True

    def paths(self) -> list[str]:
        """已请求的路径列表（端点调用序列断言用）。"""

        return [request.path for request in self.requests]

    def count(self, suffix: str) -> int:
        """命中某路径结尾的请求次数（默认只统计未被断言清理过的全部请求）。"""

        return sum(1 for path in self.paths() if path.endswith(suffix))

    def body_of(self, path: str) -> dict[str, Any]:
        """取某次请求（按完整路径）的 JSON 请求体；不存在时断言失败。"""

        import json

        for request in self.requests:
            if request.path == path:
                decoded = json.loads(request.body.decode("utf-8"))
                assert isinstance(decoded, dict), decoded
                return decoded
        raise AssertionError(f"没有请求路径 {path}；实际请求={self.paths()}")


@dataclass
class ClientServiceFixture:
    """客户端用例夹具：插件 + 上下文 + 注入时钟 + 真实服务 + 内存传输。"""

    plugin: Any
    ctx: Any
    clock: FakeClock
    service: AuthorizedService
    transport: ServiceAppTransport
    data_dir: Path
    ledger_identity: str
    token_env_var: str = DEFAULT_SERVICE_TOKEN_ENV
    environ: Mapping[str, str] = field(default_factory=dict)
    target_group_id: str = TARGET_GROUP_ID

    @property
    def onebot(self) -> FakeOneBot:
        """模拟 OneBot（服务端唯一平台出口）。"""

        return self.service.onebot

    def sent_segments(self, *, target_group_id: str | None = None) -> list[list[dict[str, Any]]]:
        """服务端实际发送出去的段序列列表（按发生顺序）。"""

        wanted = target_group_id or self.target_group_id
        return [
            list(call.detail.get("message") or [])
            for call in self.onebot.send_calls
            if call.target == wanted
        ]

    def sent_texts(self) -> list[str]:
        """服务端实际发送的纯文本消息列表（只取单文本段的发送）。"""

        texts: list[str] = []
        for segments in self.sent_segments():
            if len(segments) == 1 and segments[0].get("type") == "text":
                texts.append(str((segments[0].get("data") or {}).get("text") or ""))
        return texts

    def old_send_calls(self) -> list[Any]:
        """宿主旧发送通道的调用记录（design D9：新投递路径必须为零）。"""

        return list(self.ctx.send.calls)

    def delivery_id(
        self,
        *,
        message_id: str = "m-source-1",
        target_group_id: str | None = None,
    ) -> str:
        """四字段投递身份摘要（与服务端账本同一算法，公开函数）。"""

        return delivery_id_for(
            DeliveryKey(
                account_id=SOURCE_ACCOUNT_ID,
                source_group_id=SOURCE_GROUP_ID,
                source_message_id=message_id,
                target_group_id=target_group_id or self.target_group_id,
            )
        )

    def record(
        self,
        *,
        message_id: str = "m-source-1",
        target_group_id: str | None = None,
    ) -> dict[str, Any] | None:
        """经服务端只读查询取权威投递状态；不存在返回 `None`（同步用例用）。"""

        response = self.service.query(self.delivery_id(message_id=message_id, target_group_id=target_group_id))
        if response.status == 404:
            return None
        return result_of(response)

    async def record_async(
        self,
        *,
        message_id: str = "m-source-1",
        target_group_id: str | None = None,
    ) -> dict[str, Any] | None:
        """`record` 的异步版本（可在事件循环内使用）。"""

        response = await self.service.call_async(
            "GET", f"/v1/forwards/{self.delivery_id(message_id=message_id, target_group_id=target_group_id)}"
        )
        if response.status == 404:
            return None
        return result_of(response)

    def fresh_plugin(self, transport_factory: Any = None) -> "ClientServiceFixture":
        """在同一服务、同一账本、同一宿主上下文上重建插件实例（模拟重新加载后调用）。

        宿主上下文（会话表、消息存储、日志）在重新加载时保持不变，因此新实例能继续
        定位同一条源消息；旧实例的卸载日志也保留在同一上下文里供断言。
        `transport_factory` 缺省沿用夹具的共享内存传输；传入时用于逐会话记录/包装
        传输（如验证退役会话关闭）。
        """

        plugin = create_plugin(
            clock=self.clock,
            transport_factory=transport_factory or (lambda section: self.transport),
            environ=dict(self.environ),
        )
        plugin.set_plugin_config(self.plugin.config.model_dump())
        plugin._set_context(self.ctx)
        return ClientServiceFixture(
            plugin=plugin,
            ctx=self.ctx,
            clock=self.clock,
            service=self.service,
            transport=self.transport,
            data_dir=self.data_dir,
            ledger_identity=self.ledger_identity,
            token_env_var=self.token_env_var,
            environ=dict(self.environ),
            target_group_id=self.target_group_id,
        )

    def restart_service(self) -> None:
        """重启真实服务并把内存传输重新指向新的服务应用（账本事实保留）。"""

        self.service.restart()
        self.transport.app = self.service.app

    def close(self) -> None:
        """关闭服务账本（释放状态目录锁；不删除任何投递事实）。"""

        self.service.close()


def make_client_service(
    tmp_path: Path,
    *,
    raw_message: Sequence[Mapping[str, Any]] | None = None,
    service_raw_message: Sequence[Mapping[str, Any]] | None = None,
    resource_id: str = DEFAULT_RESOURCE_ID,
    source_groups: Sequence[str] = (SOURCE_GROUP_ID,),
    target_groups: Sequence[str] = (TARGET_GROUP_ID,),
    server_source_groups: Sequence[str] | None = None,
    server_target_groups: Sequence[str] | None = None,
    message_types: Sequence[str] = ("forward",),
    config_overrides: Mapping[str, Any] | None = None,
    register_source: bool = True,
    message_id: str = "m-source-1",
    source_user_id: str = DEFAULT_SOURCE_USER_ID,
    token_env_var: str = DEFAULT_SERVICE_TOKEN_ENV,
    environ: Mapping[str, str] | None = None,
    expected_ledger_id: str | None = None,
    migration_confirmation: str = DEFAULT_MIGRATION_CONFIRMATION,
    request_timeout_seconds: float | None = None,
    data_dir: Path | None = None,
) -> ClientServiceFixture:
    """装配「插件 → 真实客户端 → 真实服务 → 模拟 OneBot」的端到端夹具。

    - 服务账本在 `tmp_path/service-state` 新装初始化，插件的预期账本 ID 取自它的
      真实身份（迁移绑定一致）；
    - 插件配置默认开启总开关、白名单恰为给定源/目标群、候选类型由参数指定；
      `config_overrides` 可覆盖任一节点（深一层合并的语义由用例自行构造完整节点）；
    - `register_source=False` 时不向网关写入源消息（供「服务端缓存缺失」用例）；
    - `server_source_groups` / `server_target_groups` 缺省与客户端白名单一致；
    - 返回的 `clock` 是插件时钟（`advance` 同步推进服务端时间轴），用例的 0.5 秒
      等待断言与时钟推进都作用于它。
    """

    service_clock = FakeClock()
    plugin_clock = MirrorClock(service_clock)
    state_dir = tmp_path / "service-state"
    service = launch_authorized_service(
        state_dir,
        clock=service_clock,
        server_source_groups=tuple(server_source_groups if server_source_groups is not None else source_groups),
        server_target_groups=tuple(server_target_groups if server_target_groups is not None else target_groups),
    )
    ledger_identity = asyncio.run(service.ledger.identity())
    onebot = service.onebot
    known_groups = list(onebot.groups)
    for group in (*target_groups, *(server_target_groups or ()), SOURCE_GROUP_ID):
        if group not in known_groups:
            known_groups.append(group)
    onebot.groups = tuple(known_groups)

    if register_source:
        store_source_message(
            onebot,
            message_id=message_id,
            group_id=source_groups[0],
            user_id=source_user_id,
            message=onebot_segments(
                list(raw_message if raw_message is not None else [{"type": "text", "data": "源消息正文"}]),
                resource_id=resource_id,
            )
            if service_raw_message is None
            else list(service_raw_message),
        )

    config = client_service_config(
        ledger_identity if expected_ledger_id is None else expected_ledger_id,
        token_env_var=token_env_var,
        migration_confirmation=migration_confirmation,
        source_groups=source_groups,
        target_groups=target_groups,
        message_types=message_types,
    )
    if config_overrides:
        for section, values in config_overrides.items():
            config.setdefault(section, {}).update(dict(values))

    transport = ServiceAppTransport(service.app)
    environ_map = dict(environ) if environ is not None else {token_env_var: DEFAULT_SERVICE_TOKEN}
    plugin, ctx = make_plugin(
        data_dir if data_dir is not None else tmp_path,
        config=config,
        clock=plugin_clock,
        transport_factory=lambda section: transport,
        environ=environ_map,
        request_timeout_seconds=request_timeout_seconds,
    )
    prepare_source(
        ctx,
        raw_message=None if raw_message is None else _host_raw_message(raw_message),
        message_id=message_id,
    )
    return ClientServiceFixture(
        plugin=plugin,
        ctx=ctx,
        clock=plugin_clock,
        service=service,
        transport=transport,
        data_dir=data_dir if data_dir is not None else tmp_path,
        ledger_identity=ledger_identity,
        token_env_var=token_env_var,
        environ=environ_map,
        target_group_id=target_groups[0] if target_groups else TARGET_GROUP_ID,
    )


# --------------------------------------------------------------------------- 协议级替身


def _scripted_reply(script: Any, *args: Any) -> dict[str, Any] | None:
    """解析脚本项：字典原样复制，可调用对象按参数求值，`None` 表示未脚本化。"""

    if script is None:
        return None
    outcome = script(*args) if callable(script) else script
    if not isinstance(outcome, Mapping):
        return None
    return {str(key): value for key, value in outcome.items()}


@dataclass
class ScriptedServiceTransport:
    """协议级服务替身：按脚本返回 v1 响应，只记录请求、不发送任何平台消息。

    用途：覆盖真实服务难以自然构造的客户端映射用例（`sending` / `failed` /
    `unknown` / 回执未落盘原因 / 响应丢失后的查询恢复 / 有界请求超时）。

    脚本项（缺省为「成功」）：
    - `prepare_reply`：准备响应体；缺省按请求四字段返回 `prepared` 与源阶段挑战；
    - `source_reply` / `comment_reply`：阶段响应体；缺省源成功（附评论挑战）、评论成功；
    - `query_reply`：查询响应体或 `None`（按脚本判定）；缺省回放本替身记录的阶段终态；
    - `cancel_reply`：撤销响应体；缺省 `{"status": "cancelled"}`；
    - `session_reply` / `policy_reply` / `heartbeat_reply`：控制面响应覆盖；
    - `fail_paths`：命中的请求**不进入替身**、直接不可达（模拟服务不可达）；
    - `lose_response_paths`：命中的请求**先进入替身并按脚本推进状态**，再把响应丢弃
      （模拟「执行已发生但客户端没收到响应」）；
    - `hang_paths`：命中的请求等待到调用方超时（模拟无响应），由客户端有界上界收口。

    `sent_messages` 只记录脚本声明的「已发送」（不涉及任何真实平台）。
    """

    ledger_identity: str = "scripted-ledger-1"
    account_id: str = SOURCE_ACCOUNT_ID
    session_id: str = "s-scripted"
    requests: list[HttpRequest] = field(default_factory=list)
    prepare_reply: Any = None
    source_reply: Any = None
    comment_reply: Any = None
    query_reply: Any = None
    cancel_reply: Any = None
    session_reply: Any = None
    policy_reply: Any = None
    heartbeat_reply: Any = None
    prepare_error: str = ""
    prepare_error_reason: str = ""
    fail_paths: list[str] = field(default_factory=list)
    lose_response_paths: list[str] = field(default_factory=list)
    hang_paths: list[str] = field(default_factory=list)
    states: dict[str, dict[str, Any]] = field(default_factory=dict)
    on_source: Any = None
    on_comment: Any = None
    last_data_dir: Path | None = None

    # ------------------------------------------------------------------ 传输

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
    ) -> TransportReply:
        parts = urlsplit(url)
        path = parts.path if not parts.query else f"{parts.path}?{parts.query}"
        request = HttpRequest(method=method, path=path, headers=dict(headers), body=body)
        self.requests.append(request)
        if any(path.endswith(suffix) for suffix in self.fail_paths):
            raise TransportUnavailable("模拟转发服务不可达（不进入服务端）")
        if any(path.endswith(suffix) for suffix in self.hang_paths):
            await asyncio.sleep(3600.0)
        status, payload = self._dispatch(method, path, request)
        if any(path.endswith(suffix) for suffix in self.lose_response_paths):
            raise TransportUnavailable("模拟执行响应丢失（服务端已处理，客户端未收到响应）")
        return TransportReply(status=status, payload=payload)

    async def close(self) -> None:
        """内存传输无可释放资源（幂等）。"""

        return None

    def paths(self) -> list[str]:
        return [request.path for request in self.requests]

    def count(self, suffix: str) -> int:
        return sum(1 for path in self.paths() if path.endswith(suffix))

    def body_of(self, path: str) -> dict[str, Any]:
        import json

        for request in self.requests:
            if request.path == path:
                decoded = json.loads(request.body.decode("utf-8"))
                assert isinstance(decoded, dict), decoded
                return decoded
        raise AssertionError(f"没有请求路径 {path}；实际请求={self.paths()}")

    # ------------------------------------------------------------------ 路由

    def _dispatch(self, method: str, path: str, request: HttpRequest) -> tuple[int, dict[str, Any]]:
        if method == "POST" and path == "/v1/sessions":
            return self._ok(
                _scripted_reply(self.session_reply)
                or {
                    "protocol_version": 1,
                    "session_id": self.session_id,
                    "account_id": self.account_id,
                    "ledger_identity": self.ledger_identity,
                    "server_generation": "scripted-gen-1",
                }
            )
        if method == "POST" and path.startswith("/v1/sessions/") and path.endswith("/heartbeat"):
            return self._ok(
                _scripted_reply(self.heartbeat_reply)
                or {"session_id": self.session_id, "expires_in_seconds": 15.0}
            )
        if method == "PUT" and path.startswith("/v1/sessions/") and path.endswith("/policy"):
            body = self._body(request)
            return self._ok(
                _scripted_reply(self.policy_reply, body)
                or {
                    "session_id": self.session_id,
                    "policy_generation": int(body.get("policy_generation") or 1),
                    "enabled": bool(body.get("enabled")),
                }
            )
        if method == "DELETE" and path.startswith("/v1/sessions/"):
            return self._ok({"session_id": self.session_id})
        if method == "POST" and path == "/v1/forwards/prepare":
            return self._prepare(self._body(request))
        if method == "POST" and path.startswith("/v1/forwards/") and path.endswith("/source"):
            return self._stage(self._body(request), self._stage_delivery(path), stage="source")
        if method == "POST" and path.startswith("/v1/forwards/") and path.endswith("/comment"):
            return self._stage(self._body(request), self._stage_delivery(path), stage="comment")
        if method == "POST" and path.startswith("/v1/forwards/") and path.endswith("/cancel"):
            return self._ok(
                _scripted_reply(self.cancel_reply)
                or {"delivery_id": self._stage_delivery(path), "status": "cancelled", "reason": ""}
            )
        if method == "GET" and path.startswith("/v1/forwards/"):
            return self._query(path.removeprefix("/v1/forwards/"))
        return 404, {"ok": False, "error": "not_found", "reason": "替身没有登记该路径"}

    def _prepare(self, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        delivery_id = delivery_id_for(
            DeliveryKey(
                account_id=self.account_id,
                source_group_id=str(body.get("source_group_id") or ""),
                source_message_id=str(body.get("source_message_id") or ""),
                target_group_id=str(body.get("target_group_id") or ""),
            )
        )
        if self.prepare_error:
            return 403, {
                "ok": False,
                "error": self.prepare_error,
                "reason": self.prepare_error_reason or "替身按脚本拒绝本次准备",
            }
        scripted = _scripted_reply(self.prepare_reply, body)
        if scripted is not None:
            scripted.setdefault("delivery_id", delivery_id)
            return self._ok(scripted)
        comment = str(body.get("comment") or "")
        return self._ok(
            {
                "delivery_id": delivery_id,
                "attempt_id": "a-scripted-1",
                "status": "prepared",
                "challenge": "challenge-source",
                "challenge_expires_in_seconds": 30.0,
                "target_message_id": "",
                "comment_status": "pending" if comment.strip() else "not_requested",
            }
        )

    def _stage(
        self, body: Mapping[str, Any], delivery_id: str, *, stage: str
    ) -> tuple[int, dict[str, Any]]:
        if stage == "source" and self.on_source is not None:
            self.on_source(delivery_id, body)
        if stage == "comment" and self.on_comment is not None:
            self.on_comment(delivery_id, body)
        script = self.source_reply if stage == "source" else self.comment_reply
        if stage == "source":
            default: dict[str, Any] = {
                "status": "sent",
                "target_message_id": "m-target-src",
                "comment_status": "pending",
                "comment_challenge": "challenge-comment",
                "comment_challenge_expires_in_seconds": 30.0,
                "reason": "",
            }
        else:
            default = {
                "status": "sent",
                "comment_status": "sent",
                "comment_message_id": "m-target-comment",
                "reason": "",
            }
        reply = _scripted_reply(script, body) or default
        reply.setdefault("delivery_id", delivery_id)
        reply.setdefault("attempt_id", str(body.get("attempt_id") or "a-scripted-1"))
        reply.setdefault("stage", stage)
        self._advance_state(delivery_id, stage, reply)
        return self._ok(reply)

    def _advance_state(
        self, delivery_id: str, stage: str, reply: Mapping[str, Any]
    ) -> None:
        """按脚本声明的结果更新本替身的只读查询面（不发送、不落任何真实真相）。"""

        state = dict(self.states.get(delivery_id) or {})
        state["delivery_id"] = delivery_id
        if stage == "source":
            state["status"] = str(reply.get("status") or "")
            state["target_message_id"] = str(reply.get("target_message_id") or "")
            state["comment_status"] = str(reply.get("comment_status") or "not_requested")
            state["comment_message_id"] = ""
        else:
            state["comment_status"] = str(reply.get("comment_status") or "not_requested")
            state["comment_message_id"] = str(reply.get("comment_message_id") or "")
        state["reason"] = str(reply.get("reason") or "")
        self.states[delivery_id] = state

    def _query(self, delivery_id: str) -> tuple[int, dict[str, Any]]:
        scripted = _scripted_reply(self.query_reply, delivery_id)
        if scripted is not None:
            return self._ok(scripted)
        state = self.states.get(delivery_id)
        if state is None:
            return 404, {"ok": False, "error": "not_found", "reason": "没有该投递的持久记录"}
        payload = {
            "delivery_id": delivery_id,
            "status": state.get("status") or "unknown",
            "target_message_id": state.get("target_message_id") or "",
            "comment_status": state.get("comment_status") or "not_requested",
            "comment_message_id": state.get("comment_message_id") or "",
            "comment_challenge": "",
            "comment_challenge_expires_in_seconds": 0.0,
            "reason": state.get("reason") or "",
        }
        return self._ok(payload)

    @staticmethod
    def _ok(result: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        return 200, {"ok": True, "result": dict(result)}

    @staticmethod
    def _body(request: HttpRequest) -> dict[str, Any]:
        import json

        if not request.body:
            return {}
        decoded = json.loads(request.body.decode("utf-8"))
        assert isinstance(decoded, dict), decoded
        return decoded

    @staticmethod
    def _stage_delivery(path: str) -> str:
        return path.split("/")[3]


def make_scripted_plugin(
    tmp_path: Path,
    *,
    raw_message: Sequence[Mapping[str, Any]] | None = None,
    transport: ScriptedServiceTransport | None = None,
    source_groups: Sequence[str] = (SOURCE_GROUP_ID,),
    target_groups: Sequence[str] = (TARGET_GROUP_ID,),
    message_types: Sequence[str] = ("forward",),
    expected_ledger_id: str | None = None,
    token_env_var: str = DEFAULT_SERVICE_TOKEN_ENV,
    environ: Mapping[str, str] | None = None,
    request_timeout_seconds: float | None = None,
) -> tuple[Any, Any, ScriptedServiceTransport, MirrorClock]:
    """装配「插件 + 协议级服务替身」：返回（插件, 上下文, 传输, 插件时钟）。

    与 `make_client_service` 同一配置面（预期账本 ID 缺省等于替身上报的身份），
    但不装配任何真实服务、账本或平台出口；适用于客户端映射与编排边界用例。
    """

    scripted = transport or ScriptedServiceTransport()
    plugin_clock = MirrorClock(FakeClock())
    config = enabled_config(
        source_groups=tuple(source_groups),
        target_groups=tuple(target_groups),
        message_types=tuple(message_types),
    )
    config["service"] = {
        "endpoint": "http://127.0.0.1:8787",
        "client_identity": "maibot-plugin",
        "token_env_var": token_env_var,
        "expected_ledger_id": scripted.ledger_identity if expected_ledger_id is None else expected_ledger_id,
        "migration_confirmation": DEFAULT_MIGRATION_CONFIRMATION,
    }
    plugin, ctx = make_plugin(
        tmp_path,
        config=config,
        clock=plugin_clock,
        transport_factory=lambda section: scripted,
        environ=dict(environ) if environ is not None else {token_env_var: DEFAULT_SERVICE_TOKEN},
        request_timeout_seconds=request_timeout_seconds,
    )
    prepare_source(
        ctx,
        raw_message=None if raw_message is None else _host_raw_message(raw_message),
    )
    return plugin, ctx, scripted, plugin_clock
