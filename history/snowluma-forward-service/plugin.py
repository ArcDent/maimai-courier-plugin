"""麦麦搬运喵：麦麦在群聊间分享消息的独立插件。

本模块是组合根：承载 SDK 生命周期、一个群聊 Tool（`forward_message`）与两个 Hook
的声明——`maisaka.planner.after_response`（为工具调用签发一次性可信来源凭据）与
`chat.receive.after_process`（观察已处理入站消息，交给源群自动判断入口），并把
它们与专用转发服务客户端、注入时钟与自动入口装配在一起：

- `forward_precheck.py`：本地参数、来源、目标与内容预检（不发送、不联网）；
- `send_material.py` / `shared`：普通消息材料与提及文本化的复用实现；
- `forward_delivery.py`：客户端投递编排（远端准备 → 最新复验 → 源阶段 → 固定 0.5 秒
  评论延迟 → 评论阶段 → 统一结果面 `render_forward_result`）；
- `remote_client.py`：专用服务客户端（有界请求、账本绑定、会话与权限代次）；
- `auto_entry.py`：源群自动判断入口（候选筛选、可读性有限等待、有界去重、意图构造
  与一次 `maisaka.proactive.trigger` 唤起）；它只唤起判断，不发送任何消息。

生命周期（design D4/D6/D7）：

- `on_load` 只做**本地**绑定检查（地址/身份/凭证变量名/预期账本/迁移确认/收据）并
  如实告警，不联网；
- `on_unload` 先置卸载标记（立即停止签发新阶段授权），再尽力请求远端撤销未消费授权
  并注销会话（不可达时如实报告「撤销未确认」），随后取消并**有界等待**本插件自己的
  后台任务退出，最后关闭传输；不触碰其他插件的任务；
- `on_config_update` 立即取消失效的待执行自动判断；**发送许可**变化时推进远端权限
  代次并撤销已不再允许的待执行尝试（仅分享标准/候选类型变化不推进代次）；投递不
  整体取消——在途发送可能已产生副作用，源阶段与评论阶段授权前会按最新配置复验。

术语见 CONTEXT.md，设计取舍见同级 change 的 design.md。
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Any, Callable, Final, Mapping

from maibot_sdk import HookHandler, MaiBotPlugin, Tool

from .auto_entry import (
    AutoEntryDedup,
    ForwardCandidate,
    NotACandidate,
    auto_entry_invalid_reason,
    run_auto_entry,
    screen_forward_candidate,
)
from .clock import Clock, SystemClock
from .config import CourierConfig
from .forward_delivery import (
    PendingAttempt,
    deliver_forward,
    render_forward_result,
)
from .forward_precheck import (
    PreparedForward,
    precheck_forward_request,
    source_group_is_allowed,
    target_is_allowed,
    whitelist_entries,
)
from .remote_client import (
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    AiohttpTransport,
    HttpTransport,
    RemoteServiceSession,
    RemoteUnavailable,
    SERVICE_BINDING_RECEIPT_FILE_NAME,
    ServicePolicy,
    ServiceSettings,
    check_service_binding,
)
from .shared.result import DeliveryResult, STAGE_REJECTED, TOOL_STATUS_REJECTED
from .source_binding import (
    FORWARD_TOOL_NAME,
    FORWARD_TOOL_PARAMETERS,
    NOTHING_SENT_NOTE,
    Rejected,
    SourceBindingTable,
)

RECEIVE_AFTER_PROCESS_HOOK = "chat.receive.after_process"
"""观察已处理入站消息的命名 Hook（第 1 组核实的宿主 Hook 名）。"""

PLANNER_AFTER_RESPONSE_HOOK = "maisaka.planner.after_response"
"""签发一次性来源凭据的宿主 Hook（第 1 组核实：`chat_loop_service.py:231-288`）。

该 Hook 在模型输出工具调用后触发且允许改写 `output_items`（`allow_kwargs_mutation`
为 True），是工具侧唯一不可被模型伪造的来源：载荷由宿主构造，模型写不到
`session_id`。
"""

BIND_FORWARD_SOURCE_HANDLER = "forward_message_source_binding"
"""来源绑定处理器组件名（模型不可见，仅供宿主分发与测试定位）。"""

OBSERVE_HOOK_MODE = "observe"
"""观察模式字面量：`HookMode` 不在 SDK 根导出面，插件按字符串声明（SDK 会归一）。"""

BLOCKING_HOOK_MODE = "blocking"
"""阻塞模式字面量：需要改写 Hook 载荷（签发来源凭据）时使用。"""

EARLY_HOOK_ORDER = "early"
"""顺序槽位字面量：来源绑定必须早于其它处理器改写本轮输出。"""

TASK_EXIT_WAIT_SECONDS: Final[float] = 5.0
"""卸载时等待本插件后台任务退出的上限（秒，代码常量）。

宿主 Runner 的单次能力 RPC 自带 30 秒默认超时（`runner_main.py:749-750` 的
`request_timeout_ms = 30000` + `rpc_client.py:211` 的 `asyncio.wait_for`），取消会在
最近的 await 点立刻生效，因此正常卸载远小于本上限。这个上限只兜底「宿主调用异常
悬挂且吞掉取消」的边界：超出即停止等待并告警，不静默留下孤立任务，也不让卸载
无限阻塞。
"""

REVOKE_TOTAL_WAIT_SECONDS: Final[float] = 5.0
"""一次卸载/热更新中整段远端撤销的总时长上限（秒，代码常量）。

每个待撤销尝试各发一次 `cancel` RPC，单次 RPC 自带 10 秒上界；N 个尝试串行最坏
N × 10 秒，会突破「有界退出」承诺。整段撤销共享这一个总预算：预算用尽后不再发起
新的撤销请求，剩余尝试一律按「撤销未确认」如实告警，不宣称远端已停止。
"""

TransportFactory = Callable[[Any], HttpTransport]
"""传输工厂：`[service]` 配置节 → `HttpTransport`（测试注入内存/脚本化替身）。"""


class CourierPlugin(MaiBotPlugin):
    """插件主类：三生命周期 + 一个群聊 Tool + 来源绑定与观察两个 Hook。"""

    config_model = CourierConfig

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        transport_factory: TransportFactory | None = None,
        environ: Mapping[str, str] | None = None,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        """构造插件实例；`clock` / `transport_factory` / `environ` 供测试注入。

        生产缺省：真实时钟、按配置构造的真实 aiohttp 传输与环境变量。传输只在本地
        绑定检查通过后才构造（缺凭证/未绑定时不加载网络库、也不建立任何连接）。
        """

        super().__init__()
        self._clock: Clock = clock or SystemClock()
        self._bindings = SourceBindingTable()
        self._transport_factory = transport_factory or self._aiohttp_transport
        self._environ = environ
        self._request_timeout_seconds = float(request_timeout_seconds)
        self._auto_dedup = AutoEntryDedup(self._clock)
        self._closed = False
        self._tasks: dict[asyncio.Task[Any], str] = {}
        self._auto_tasks: dict[asyncio.Task[None], ForwardCandidate] = {}
        self._service: RemoteServiceSession | None = None
        self._service_signature: tuple[str, ...] = ()
        self._retired_services: list[RemoteServiceSession] = []
        self._active_attempts: dict[str, PendingAttempt] = {}

    def _is_enabled(self) -> bool:
        """插件总开关是否开启；关闭时不得产生任何发送。"""

        return bool(self.config.plugin.enabled)

    def _is_live(self) -> bool:
        """插件是否仍在工作：未卸载且总开关开启。

        工具入口、来源绑定、阶段授权前的复验与自动入口共用本谓词，避免「卸载标记 +
        总开关」这一组合在各处各写一遍（各处的后续判定与措辞仍各自保留）。
        """

        return not self._closed and self._is_enabled()

    # ------------------------------------------------------------------ 服务连接

    def _service_settings(self) -> ServiceSettings:
        """读最新的 `[service]` 配置（不缓存旧快照：热更新后必须按新值判定）。"""

        return ServiceSettings.from_config(self.config.service)

    def _binding_check(self) -> RemoteUnavailable | None:
        """本地（不联网）服务绑定检查：读**最新** `[service]` 配置与本地收据。

        工具入口与 `on_load` 共用同一份六参数调用（地址、身份、凭证变量名、账本绑定、
        收据路径、环境映射），避免两处各写一遍导致口径漂移；不通过时既不建立连接，
        也不加载网络库。
        """

        settings = self._service_settings()
        return check_service_binding(
            endpoint=settings.endpoint,
            client_identity=settings.client_identity,
            token_env_var=settings.token_env_var,
            binding=settings.binding(),
            receipt_path=self.ctx.paths.data_dir / SERVICE_BINDING_RECEIPT_FILE_NAME,
            environ=self._environ,
        )

    def _service_ready(self) -> RemoteServiceSession | RemoteUnavailable:
        """取（或按最新配置重建）服务会话；本地绑定检查不通过直接返回不可用。

        静态检查先于传输构造：缺少凭证、预期账本或迁移确认时既不建立连接，也不
        加载网络库，更不会回退旧发送通道。
        """

        unavailable = self._binding_check()
        if unavailable is not None:
            return unavailable
        settings = self._service_settings()
        signature = settings.signature()
        if isinstance(self._service, RemoteServiceSession):
            if self._service_signature == signature:
                return self._service
            # 配置指向了另一个服务/数据集：旧会话立即退役（不再经它签发任何新阶段
            # 授权），其传输在本次替换时仍在途的本插件任务退出后关闭。
            self._retire_service(self._service)
            self._service = None
        transport = self._transport_factory(self.config.service)
        session = RemoteServiceSession(
            settings=settings,
            receipt_path=self.ctx.paths.data_dir / SERVICE_BINDING_RECEIPT_FILE_NAME,
            transport=transport,
            environ=self._environ,
            request_timeout_seconds=self._request_timeout_seconds,
        )
        self._service = session
        self._service_signature = signature
        return session

    def _retire_service(self, session: RemoteServiceSession) -> None:
        """退役旧服务会话：先停止经它签发新授权，再等在途任务退出后关闭其传输。

        顺序固定为「`self._service` 不再指向旧会话（新阶段授权一律走新会话）→ 等本次
        替换时仍在途的本插件任务退出 → `close()` 释放 aiohttp 会话（连接池/socket）」：
        在途投递可能仍持有旧会话并已提交阶段调用，提前关闭会中止可能已产生平台副作用
        的请求，把结果误报成传输不可达。旧会话保留在 `_retired_services`，`on_unload`
        会再逐个关闭（`close()` 幂等，兜住「退役后才被在途调用重新建连」的窄窗口）；
        没有运行中的事件循环时无法安排异步关闭，直接留给卸载处理。
        """

        self._retired_services.append(session)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        pending = [task for task in self._tasks if not task.done()]

        async def close_after_inflight() -> None:
            if pending:
                await asyncio.wait(pending)
            await session.close()

        task = loop.create_task(close_after_inflight())
        self._tasks[task] = "退役服务会话收尾（关闭旧传输）"
        task.add_done_callback(self._forget_task)

    @staticmethod
    def _aiohttp_transport(section: Any) -> HttpTransport:
        """生产传输：按代码常量 `DEFAULT_REQUEST_TIMEOUT_SECONDS` 设置 aiohttp 会话超时。

        `[service]` 配置节只承载地址/身份/凭证变量名/账本绑定，不承载超时；测试注入的
        `request_timeout_seconds` 由客户端 `_call` 的外层等待收口，不改写生产传输的
        缺省值（延迟导入网络库，缺依赖时如实报告不可用）。
        """

        del section
        return AiohttpTransport(timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS)

    def _policy_snapshot(self) -> ServicePolicy:
        """把最新配置归一成发送许可快照（总开关 + 两级白名单）。"""

        return ServicePolicy(
            enabled=self._is_live(),
            source_groups=whitelist_entries(self.config.scope.source_groups),
            target_groups=whitelist_entries(self.config.scope.target_groups),
        )

    # ------------------------------------------------------------------ 尝试登记

    def prepared(self, attempt: PendingAttempt) -> None:
        """登记已准备、尚未完成的尝试（供卸载/权限收缩时请求远端撤销）。"""

        self._active_attempts[attempt.delivery_id] = attempt

    def finished(self, delivery_id: str) -> None:
        """本次调用结束时释放尝试登记（结果以服务端账本为准，不在这里落任何真相）。"""

        self._active_attempts.pop(delivery_id, None)

    def _attempt_still_allowed(self, attempt: PendingAttempt) -> bool:
        """按最新配置判断该待执行尝试是否仍被允许（源群与目标群都在白名单）。"""

        return self._is_live() and source_group_is_allowed(
            self.config, attempt.source_group_id
        ) and target_is_allowed(self.config, attempt.target_group_id)

    async def _revoke_disallowed_attempts(self, reason: str) -> bool:
        """请求远端撤销已不再允许的待执行尝试；返回是否全部确认（False = 撤销未确认）。

        整段撤销共享总时长上限 `REVOKE_TOTAL_WAIT_SECONDS`：每个尝试至多发一次
        `cancel` RPC，总预算用尽后不再发起新请求，剩余尝试按「撤销未确认」如实告警
        ——不因尝试数变多而让卸载/热更新无界阻塞，也不宣称远端已停止。
        """

        service = self._service
        if not isinstance(service, RemoteServiceSession):
            return True
        deadline = asyncio.get_running_loop().time() + REVOKE_TOTAL_WAIT_SECONDS
        confirmed = True
        for attempt in list(self._active_attempts.values()):
            if self._attempt_still_allowed(attempt):
                continue
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                confirmed = False
                self.ctx.logger.warning(
                    f"{reason}：撤销 {attempt.delivery_id} 的未消费授权超出总时长"
                    f" {REVOKE_TOTAL_WAIT_SECONDS:g} 秒仍未获确认，不宣称远端已停止"
                )
                continue
            try:
                outcome = await asyncio.wait_for(
                    service.cancel(
                        delivery_id=attempt.delivery_id,
                        attempt_id=attempt.attempt_id,
                    ),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                outcome = RemoteUnavailable("撤销请求超出整段总时长上限")
            if isinstance(outcome, RemoteUnavailable):
                confirmed = False
                self.ctx.logger.warning(
                    f"{reason}：撤销 {attempt.delivery_id} 的未消费授权未获确认"
                    f"（{outcome.reason}），不宣称远端已停止"
                )
        return confirmed

    # ------------------------------------------------------------------ 组件与生命周期

    def get_components(self) -> list[dict[str, Any]]:
        """把转发 Tool 的 `chat_scope` 提到组件声明**顶层**（承载性修正）。

        Runner 只读组件声明顶层的 `chat_scope`（缺省回落 `"all"`），而
        `@Tool(chat_scope=…)` 经 SDK 只会落进组件 metadata；第 1 组已核实该
        metadata 回退分支不可达。不覆写这一层，群聊专用的转发工具会在私聊会话中
        同样可见（可见性过滤失效）。
        """
        components = super().get_components()
        for component in components:
            if str(component.get("name") or "") != FORWARD_TOOL_NAME:
                continue
            component["chat_scope"] = "group"
            metadata = component.get("metadata")
            if isinstance(metadata, dict):
                metadata.pop("chat_scope", None)
        return components

    async def on_load(self) -> None:
        """生命周期：插件加载完成。

        只做**本地**绑定检查（design D6/D10）：地址、身份、凭证变量名、预期账本与
        迁移确认逐项核对；不通过时如实告警，工具调用随后一律拒绝，且不建立任何
        服务连接、不回退旧发送通道。
        """

        unavailable = self._binding_check()
        if unavailable is not None:
            self.ctx.logger.warning(
                f"转发服务未就绪（{unavailable.reason}）：工具调用将被拒绝，不退回旧发送通道"
            )
        else:
            self.ctx.logger.info(
                f"转发服务绑定已就绪：预期账本={self._service_settings().expected_ledger_id}，"
                f"地址={self._service_settings().endpoint}"
            )
        self.ctx.logger.info("麦麦搬运喵已加载")

    async def on_unload(self) -> None:
        """生命周期：插件卸载。

        顺序固定为「本地先停止签发新阶段授权 → 尽力请求远端撤销 → 有界等待本插件任务
        退出 → 释放传输」：撤销不可达时如实报告「撤销未确认」，不伪称远端已停止；
        已经发生的平台发送保留在服务端账本里，不回滚。
        """

        self._closed = True
        await self._revoke_disallowed_attempts("插件卸载")
        service = self._service
        if isinstance(service, RemoteServiceSession):
            deregistered = await service.deregister()
            if isinstance(deregistered, RemoteUnavailable):
                self.ctx.logger.warning(
                    f"插件卸载：远端注销未获确认（{deregistered.reason}），不宣称远端已停止"
                )
        await self._drain_tasks("插件卸载")
        if isinstance(service, RemoteServiceSession):
            await service.close()
        # 退役会话再逐个关闭一次：close() 幂等，兜住「退役后才被在途调用重新建连」的
        # 窄窗口，确保旧连接池最终释放。
        for retired in self._retired_services:
            await retired.close()
        self._retired_services.clear()
        self._service = None
        self._service_signature = ()
        self.ctx.logger.info("麦麦搬运喵已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """生命周期：插件配置热更新。

        宿主在 scope 为 `self` 时先注入最新配置（`runner_main.py:2273-2281` 调
        `_apply_plugin_config` → `set_plugin_config`，`runner_main.py:899-901`）再调用
        本入口，因此这里按只读的 `self.config` 读到的就是新配置。

        行为：
        1. 取消失效的待执行自动判断（开关/自动入口关闭、候选类型被移出、来源群或目标
           白名单不再允许）；**仅标准变化不取消仍合法的候选**；
        2. 发送许可（总开关 + 两级白名单）变化时推进远端权限代次，并请求远端撤销已不再
           允许的待执行尝试——撤销不可达时如实告警「撤销未确认」；
        3. 投递不整体取消：在途发送可能已产生副作用，源阶段与评论阶段授权前会按最新
           配置复验。
        """

        del scope, config_data, version
        self._cancel_invalid_auto_entries()
        service = self._service
        if not isinstance(service, RemoteServiceSession):
            return
        policy = self._policy_snapshot()
        unavailable = await service.advance_policy(policy)
        if isinstance(unavailable, RemoteUnavailable):
            self.ctx.logger.warning(
                f"配置热更新：远端权限收缩未获确认（{unavailable.reason}），不宣称远端已停止"
            )
        await self._revoke_disallowed_attempts("配置热更新")

    async def _drain_tasks(self, reason: str) -> None:
        """取消并等待本插件创建的后台任务退出（台账带可定位描述）。

        只处理 `self._tasks` 里的任务（全部由本插件创建，值是「源消息 / 目标群 / 阶段」
        的可定位描述）：不触碰其他插件或宿主的任务。等待有界：超出
        `TASK_EXIT_WAIT_SECONDS` 的任务已被取消但可能仍卡在宿主调用里，此时按任务描述
        逐条告警说明，不再无限阻塞卸载。
        """

        pending = [task for task in self._tasks if not task.done()]
        if not pending:
            return
        for task in pending:
            task.cancel()
        _, still_running = await asyncio.wait(pending, timeout=TASK_EXIT_WAIT_SECONDS)
        if still_running:
            labels = "；".join(sorted(self._tasks.get(task, "未知任务") for task in still_running))
            self.ctx.logger.warning(
                f"{reason}：{len(still_running)} 个后台任务在 {TASK_EXIT_WAIT_SECONDS:g} 秒内未退出"
                f"（已取消；插件侧不会再发起新的发送，在途宿主调用可能仍完成）：{labels}"
            )
            return
        self.ctx.logger.info(f"{reason}：已取消并等待 {len(pending)} 个后台任务退出")

    def _forget_task(self, task: asyncio.Task[Any]) -> None:
        """任务结束后从本插件台账移除：卸载只等待仍在运行的任务。"""

        self._tasks.pop(task, None)

    def _cancel_invalid_auto_entries(self) -> None:
        """取消已失效的待执行自动判断（按最新配置判定，不依赖旧配置快照）。"""

        for task, candidate in list(self._auto_tasks.items()):
            if task.done() or not self._auto_entry_invalid(candidate):
                continue
            task.cancel()
            self.ctx.logger.debug(
                "配置热更新后取消已失效的自动判断：源消息=%s，源群=%s",
                candidate.source_message_id,
                candidate.group_id,
            )

    def _auto_entry_invalid(self, candidate: ForwardCandidate) -> bool:
        """待执行自动判断是否已失效：已卸载，或最新配置不再允许对该候选唤起。

        判定复用 `auto_entry.auto_entry_invalid_reason`（与等待结束后的重验同一谓词，
        含候选类型：类型被移出自动候选类型同样失效）；本方法只决定「现在要不要取消」，
        不负责取消时机之外的事情。
        """

        return self._closed or bool(
            auto_entry_invalid_reason(
                self.config,
                candidate.group_id,
                candidate.message_type,
            )
        )

    # ------------------------------------------------------------------ 工具入口

    @Tool(
        FORWARD_TOOL_NAME,
        description=(
            "把本群的一条消息转发到允许的目标群，可附带一条评论。"
            "只接受 message_id、target_group_id 和可选 comment 三个字符串参数，"
            "不支持或无法完整恢复的内容会被明确拒绝。"
        ),
        parameters=FORWARD_TOOL_PARAMETERS,
        chat_scope="group",
    )
    async def forward_message(self, **kwargs: Any) -> dict[str, Any]:
        """群聊转发工具入口：总开关 + 本地预检 + 服务客户端投递编排。

        工具调用参数里的会话、平台与授权字段一律不读：来源可信性只由本插件在
        宿主 Hook 上签发的一次性凭据决定（见 `source_binding.py`）。本地预检（含
        本地绑定检查）通过后创建投递任务并交给 `forward_delivery.deliver_forward`：
        远端准备 → 最新复验 → 源阶段 → 固定延迟评论阶段。

        投递在插件自己的后台任务里执行并登记：卸载时可以取消它，也可以等待它退出
        （design D6）。任务的取消与阻塞不影响服务端账本里的既成事实。
        """
        if not self._is_live():
            reason = "插件已卸载" if self._closed else "插件总开关关闭"
            return self._finish(self._render_rejection(f"{reason}，{NOTHING_SENT_NOTE}", kwargs))

        service = self._service_ready()
        if isinstance(service, RemoteUnavailable):
            return self._finish(
                self._render_rejection(f"转发服务未就绪（{service.reason}）", kwargs)
            )

        outcome = await precheck_forward_request(
            ctx=self.ctx,
            config=self.config,
            bindings=self._bindings,
            arguments=kwargs,
        )
        if isinstance(outcome, Rejected):
            return self._finish(self._render_rejection(outcome.reason, kwargs))
        if self._closed:
            # 预检期间可能发生卸载：此时不得创建发送任务或提交任何阶段授权。
            return self._finish(self._render_rejection(f"插件已卸载，{NOTHING_SENT_NOTE}", kwargs))
        task = asyncio.create_task(self._deliver(outcome, service))
        self._tasks[task] = (
            f"投递（源消息={outcome.arguments.message_id}，目标群={outcome.arguments.target_group_id}）"
        )
        task.add_done_callback(self._forget_task)
        result = await task
        return self._finish(
            render_forward_result(
                result,
                material=outcome.material,
                source_message_id=outcome.arguments.message_id,
                target_group_id=outcome.arguments.target_group_id,
            )
        )

    async def _deliver(
        self,
        prepared: PreparedForward,
        service: RemoteServiceSession,
    ) -> DeliveryResult:
        """在插件自己的后台任务里执行一次投递（卸载时可取消并被等待）。"""

        unavailable = await service.ensure_ready(self._policy_snapshot())
        if isinstance(unavailable, RemoteUnavailable):
            return DeliveryResult(
                status=TOOL_STATUS_REJECTED,
                stage=STAGE_REJECTED,
                reason=Rejected.not_sent(f"转发服务未就绪（{unavailable.reason}）").reason,
            )
        return await deliver_forward(
            session=service,
            prepared=prepared,
            clock=self._clock,
            request_id=secrets.token_urlsafe(18),
            source_allowed=lambda: self._send_allowed(prepared),
            comment_allowed=lambda: self._send_allowed(prepared),
            attempts=self,
        )

    def _send_allowed(self, prepared: PreparedForward) -> bool:
        """阶段授权前的权限复验：按**提交时**的最新配置核对开关与两级白名单。

        工具入口的预检结果只是「可以尝试」的资格；源阶段与评论阶段提交授权前都再走
        一次本判定（design D4）：卸载标记、总开关、来源群白名单、目标群白名单任一
        不满足都不得再提交授权。
        """

        return self._is_live() and source_group_is_allowed(
            self.config, prepared.source.group_id
        ) and target_is_allowed(self.config, prepared.arguments.target_group_id)

    def _finish(self, rendered: dict[str, Any]) -> dict[str, Any]:
        """记录可定位的失败信息并返回结果（完整成功不额外记录）。

        日志只含阶段、源消息 ID、目标群 ID、状态与中文原因：不回显评论正文、
        不输出一次性来源凭据，也不生成任何摘要或评论副本。
        """

        if not rendered["success"]:
            self.ctx.logger.warning(
                f"转发未完成：阶段={rendered['stage'] or 'none'}，"
                f"源消息={rendered['source_message_id'] or '未知'}，"
                f"目标群={rendered['target_group_id'] or '未知'}，"
                f"状态={rendered['status']}，原因={rendered['reason']}"
            )
        return rendered

    def _render_rejection(self, reason: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """渲染拒绝结果：与投递结果同形，并尽力带回模型声明的源消息与目标群用于定位。

        只读取调用参数里的字符串（模型声明值），不做任何取值、查询或发送；预检未
        通过时没有任何已准备的材料，因此结果里不含材料键。
        """

        return render_forward_result(
            DeliveryResult(status=TOOL_STATUS_REJECTED, stage=STAGE_REJECTED, reason=reason),
            source_message_id=_argument_text(arguments, "message_id"),
            target_group_id=_argument_text(arguments, "target_group_id"),
        )

    # ------------------------------------------------------------------ Hook

    @HookHandler(
        PLANNER_AFTER_RESPONSE_HOOK,
        name=BIND_FORWARD_SOURCE_HANDLER,
        description="模型输出工具调用后：为本轮 forward_message 签发一次性可信来源凭据。",
        mode=BLOCKING_HOOK_MODE,
        order=EARLY_HOOK_ORDER,
    )
    async def bind_forward_source(self, **kwargs: Any) -> dict[str, Any]:
        """在本轮 Planner 输出上签发来源凭据（模型参数里带不进可信会话）。

        载荷由宿主构造（`session_id` + `output_items`），处理器只把转发调用改写为
        「公开参数 + 本次一次性凭据」；`modified_kwargs` 随后成为工具调用的参数
        （`runner_main.py:2214`）。插件关闭或已卸载时不改写、不签发，工具调用自然拒绝。
        """
        if not self._is_live():
            return {"action": "continue", "modified_kwargs": kwargs}
        session_id = str(kwargs.get("session_id") or "").strip()
        output_items = kwargs.get("output_items")
        if session_id and isinstance(output_items, list):
            kwargs["output_items"] = self._bindings.sanitize_and_bind_output_items(
                session_id,
                output_items,
            )
        return {"action": "continue", "modified_kwargs": kwargs}

    @HookHandler(
        RECEIVE_AFTER_PROCESS_HOOK,
        description="观察已完成预处理的消息，不拦截原处理链。",
        mode=OBSERVE_HOOK_MODE,
    )
    async def observe_received_message(self, **kwargs: Any) -> None:
        """观察已处理入站消息：筛选自动入口候选并在后台给出一次判断机会。

        观察模式由宿主后台分发（`hook_dispatcher.py:632-662`），返回值不参与聚合，
        因此这里既不拦截也不改写原消息处理。默认关闭或自动入口关闭时直接返回，
        不产生任何宿主调用；候选筛选与去重登记都在本协程内同步完成，随后把
        「确认可读取 → 唤起源群判断」交给本插件自己的后台任务（design D2）。
        """
        if self._auto_entry_config() is None:
            return
        candidate = screen_forward_candidate(kwargs.get("message"), self.config)
        if isinstance(candidate, NotACandidate):
            self.ctx.logger.debug("消息不进入自动判断：%s", candidate.reason)
            return
        if not self._auto_dedup.claim(candidate):
            self.ctx.logger.debug(
                "同一候选已在去重窗口内上报过，本次不重复唤起：%s",
                candidate.source_message_id,
            )
            return
        self._spawn_auto_entry(candidate)

    def _spawn_auto_entry(self, candidate: ForwardCandidate) -> None:
        """把候选的等待与唤起登记成插件自己的后台任务（卸载/热更新时取消）。"""

        task = asyncio.create_task(self._run_auto_entry(candidate))
        self._auto_tasks[task] = candidate
        self._tasks[task] = (
            f"自动判断（源消息={candidate.source_message_id}，源群={candidate.group_id}）"
        )

        def _forget(done: asyncio.Task[None]) -> None:
            self._auto_tasks.pop(done, None)
            self._forget_task(done)

        task.add_done_callback(_forget)

    async def _run_auto_entry(self, candidate: ForwardCandidate) -> None:
        """执行一次自动判断唤起，并记录可定位结果（后台任务不得丢失异常）。"""

        try:
            outcome = await run_auto_entry(
                ctx=self.ctx,
                candidate=candidate,
                clock=self._clock,
                current_config=self._auto_entry_config,
            )
        except asyncio.CancelledError:
            self.ctx.logger.debug(
                "自动判断的本地等待已取消（只结束本插件等待，不代表宿主侧判断结果）："
                "源消息=%s，源群=%s",
                candidate.source_message_id,
                candidate.group_id,
            )
            raise
        except Exception as error:
            self.ctx.logger.warning(
                f"自动判断未完成：源消息={candidate.source_message_id}，"
                f"源群={candidate.group_id}，原因={type(error).__name__}"
            )
            return
        if outcome.triggered:
            self.ctx.logger.info(
                f"已向源群提供一次分享判断机会：源消息={candidate.source_message_id}，"
                f"源群={candidate.group_id}，源会话={outcome.stream_id or candidate.session_id}，"
                f"任务={outcome.task_id or '未知'}"
            )
            return
        self.ctx.logger.warning(
            f"未提供分享判断机会：源消息={candidate.source_message_id}，"
            f"源群={candidate.group_id}，原因={outcome.reason}"
        )

    def _auto_entry_config(self) -> CourierConfig | None:
        """等待结束后读一次最新配置快照；不可继续自动判断时返回 None。

        自动判断不缓存配置：等待期间的热更新必须对「还要不要唤起」生效，目标白名单
        的增删也按同一份最新快照重新计算。观察 Hook 入口也经本判定早退（与等待结束
        后的判定同源），卸载标记同样在此生效。
        """

        if not self._is_live() or not self.config.auto.enabled:
            return None
        return self.config


def create_plugin(
    clock: Clock | None = None,
    transport_factory: TransportFactory | None = None,
    environ: Mapping[str, str] | None = None,
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
) -> CourierPlugin:
    """SDK 工厂入口：Runner 通过本函数（无参）创建插件实例。

    `clock` / `transport_factory` / `environ` / `request_timeout_seconds` 只为测试注入
    预留，宿主调用时保持缺省。
    """

    return CourierPlugin(
        clock=clock,
        transport_factory=transport_factory,
        environ=environ,
        request_timeout_seconds=request_timeout_seconds,
    )


def _argument_text(arguments: Mapping[str, Any], name: str) -> str:
    """取调用参数里的短文本用于结果定位（只接受字符串，不做任何转换）。"""

    value = arguments.get(name)
    return value.strip() if isinstance(value, str) else ""
