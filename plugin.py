"""插件组合根：宿主可信入口、本地账本及自己的 SnowLuma HTTP 客户端。"""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import Future as WorkerFuture, ThreadPoolExecutor
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Awaitable, Callable, Final, Literal, Mapping

from maibot_sdk import HookHandler, MaiBotPlugin, Tool

from .auto_entry import (AutoEntryDedup, ForwardCandidate, NotACandidate,
                         auto_entry_invalid_reason, run_auto_entry, screen_forward_candidate)
from .shared.clock import Clock, SystemClock
from .config import CourierConfig
from .delivery_ledger import LEDGER_FILE_NAME, DeliveryLedger, LedgerUnavailable, open_ledger
from .forward_delivery import deliver_forward
from .forward_precheck import (PreparedForward, precheck_forward_request, resolve_source_session,
                               shared_forward_group, source_group_is_allowed,
                               source_group_private_allowed)
from .shared.delivery import TARGET_KIND_GROUP, TARGET_KIND_PRIVATE, TargetKind
from .shared.result import (DeliveryResult, STAGE_REJECTED, TOOL_STATUS_REJECTED,
                            render_forward_result)
from .send_material import friend_entry_identifier, friend_entry_nickname, friend_entry_remark
from .snowluma_client import SnowLumaClient, business_failure, response_payload
from .snowluma_transport import (AiohttpOneBotTransport, OneBotConnection, OneBotTransport,
                                 OneBotUnavailable, OneBotUnresolved, resolve_onebot_connection)
from .source_binding import (
    FRIEND_LIST_TOOL_PARAMETERS,
    FORWARD_TOOL_NAME,
    FORWARD_TOOL_NAMES,
    FORWARD_TOOL_PARAMETERS,
    LIST_FORWARD_FRIENDS_TOOL_NAME,
    NOTHING_SENT_NOTE,
    Rejected,
    SourceBindingTable,
)

RECEIVE_AFTER_PROCESS_HOOK = "chat.receive.after_process"
"""观察已处理入站消息的宿主 Hook 名。"""

PLANNER_AFTER_RESPONSE_HOOK = "maisaka.planner.after_response"
"""签发一次性来源凭据的宿主 Hook（允许改写 `output_items`，工具侧唯一不可伪造的来源）。"""

BIND_FORWARD_SOURCE_HANDLER = "forward_message_source_binding"
"""来源绑定处理器组件名（模型不可见，仅供宿主分发与测试定位）。"""

OBSERVE_HOOK_MODE = "observe"
"""观察模式字面量：`HookMode` 不在 SDK 根导出面，插件按字符串声明（SDK 会归一）。"""

BLOCKING_HOOK_MODE = "blocking"
"""阻塞模式字面量：需要改写 Hook 载荷（签发来源凭据）时使用。"""

EARLY_HOOK_ORDER = "early"
"""顺序槽位字面量：来源绑定必须早于其它处理器改写本轮输出。"""

TASK_EXIT_WAIT_SECONDS: Final[float] = 5.0
"""本插件所有退出工作共享的总预算。"""

MAX_INFLIGHT_DELIVERIES: Final[int] = 8
"""包括预检和等待在内的工具容量；不驱逐既有投递。"""

MAX_FRIEND_ENTRIES: Final[int] = 500
"""名册工具单次返回的条目上限（代码常量，不做成配置）：超出即截断并在结果中明示。"""

TransportFactory = Callable[[OneBotConnection], OneBotTransport]
LedgerFactory = Callable[[Path], DeliveryLedger | LedgerUnavailable]


def _target_label(target_group_id: str, target_user_id: str) -> str:
    """任务表与告警里的目标定位文本：按目标形态区分「目标群=…」与「私聊=…」。

    群号与好友 QQ 号是两个重叠的数字空间（ADR 0012），两种形态互斥、同一时刻只有
    一个目标字段非空；两个目标字段都为空时按未知目标定位。名册备注与昵称从不进入。
    """

    if not target_group_id.strip() and not target_user_id.strip():
        return "目标=未知"
    return f"私聊={target_user_id}" if target_user_id.strip() else f"目标群={target_group_id}"


def _flattened_group_name(name: str) -> str:
    """分组名压平为单行定位文本（空白折叠为空格、剔除控制字符）；判定只认群号。"""

    characters: list[str] = []
    for character in name:
        if character.isspace():
            characters.append(" ")
        elif ord(character) < 32 or 127 <= ord(character) < 160:
            continue
        else:
            characters.append(character)
    return "".join(characters).strip()


class CourierPlugin(MaiBotPlugin):
    """插件主类：三生命周期 + 两个群聊 Tool + 来源绑定与观察两个 Hook。"""

    config_model = CourierConfig

    def __init__(self, *, clock: Clock | None = None,
                 transport_factory: TransportFactory | None = None,
                 ledger_opener: LedgerFactory | None = None,
                 environ: Mapping[str, str] | None = None) -> None:
        """构造不联网；公开注入时钟、固定传输与账本打开函数。"""
        super().__init__()
        self._clock = clock or SystemClock()
        self._bindings = SourceBindingTable()
        self._environ = environ if environ is not None else os.environ
        self._transport_factory = transport_factory or self._aiohttp_transport
        self._ledger_opener = ledger_opener or (lambda path: open_ledger(path, clock=self._clock))
        self._ledger: DeliveryLedger | LedgerUnavailable | None = None
        self._client: SnowLumaClient | None = None
        self._client_close_task: asyncio.Task[None] | None = None
        self._client_refreshing = False
        self._client_generation = 0
        self._connection: OneBotConnection | None = None
        self._config_generation = 0
        self._seen_connection: tuple[str, str, str] | None = None
        self._auto_dedup = AutoEntryDedup(self._clock)
        self._closed = False
        self._shutdown_task: asyncio.Task[None] | None = None
        self._ledger_close_work: WorkerFuture[None] | None = None
        self._inflight = 0
        self._tasks: dict[asyncio.Future[Any], str] = {}
        # 每个子任务复制创建时的不可变来源；父投递结束或换阶段不覆盖旧快照。
        self._task_origin: ContextVar[str] = ContextVar("courier_task_origin", default="")
        self._delivery_tasks: set[asyncio.Task[Any]] = set()
        self._auto_tasks: dict[asyncio.Task[None], ForwardCandidate] = {}

    def _is_live(self) -> bool:
        return not self._closed and bool(self.config.plugin.enabled)

    def _aiohttp_transport(self, connection: OneBotConnection) -> OneBotTransport:
        # 无连接 HTTP 传输不再登记后台任务：动作各自有界，卸载只等组合根自己的任务。
        return AiohttpOneBotTransport(connection, clock=self._clock, environ=self._environ)

    def _register_task(self, task: asyncio.Future[Any], description: str) -> None:
        if origin := self._task_origin.get():
            description += f"：{origin}"
        self._tasks[task] = description
        task.add_done_callback(self._forget_task)

    def get_components(self) -> list[dict[str, Any]]:
        """把两个群聊 Tool 的 `chat_scope` 提到组件声明顶层（承载性修正）。

        Runner 只读顶层 `chat_scope`（缺省回落 `"all"`），`@Tool(chat_scope=…)` 经
        SDK 只落进 metadata；不覆写这一层，群聊专用工具会在私聊可见。按
        `FORWARD_TOOL_NAMES` 清单统一判定，不逐个写死工具名。
        """
        components = super().get_components()
        for component in components:
            if str(component.get("name") or "") not in FORWARD_TOOL_NAMES:
                continue
            component["chat_scope"] = "group"
            metadata = component.get("metadata")
            if isinstance(metadata, dict):
                metadata.pop("chat_scope", None)
        return components

    async def on_load(self) -> None:
        """加载只做本地配置检查，不创建直连或执行平台查询。"""
        connection = resolve_onebot_connection(self.config, self._environ)
        if isinstance(connection, OneBotUnresolved):
            self.ctx.logger.warning(f"SnowLuma 未就绪：{connection.reason}")
        self.ctx.logger.info("麦麦搬运喵已加载")

    async def on_unload(self) -> None:
        """立即停提交；所有清理共享一个五秒预算，重复卸载不重复启动清理。"""
        self._closed = True
        if self._shutdown_task is None:
            deadline = self._clock.now() + TASK_EXIT_WAIT_SECONDS
            self._shutdown_task = asyncio.create_task(self._shutdown(deadline))
            self._register_task(self._shutdown_task, "卸载收尾")
        await asyncio.shield(self._shutdown_task)

    async def _shutdown(self, deadline: float) -> None:
        """只取消并有界等待本插件自己的任务；HTTP 无连接，卸载不关连接、只结束在途请求。"""
        current = asyncio.current_task()
        pending = {task for task in self._tasks
                   if task is not current and not task.done()}
        labels = {task: self._tasks[task] for task in pending}
        for task in pending:
            task.cancel()
        timer = asyncio.create_task(self._clock.sleep(max(0.0, deadline - self._clock.now())))
        try:
            while pending and self._clock.now() < deadline:
                done, _ = await asyncio.wait(pending | {timer}, return_when=asyncio.FIRST_COMPLETED)
                pending.difference_update(done)
                # 收尾协程自身可能继续登记资源收尾，每项仍须独立等待、定位，不能只保留父任务。
                pending.update(task for task in self._tasks if task is not current and not task.done())
                labels.update({task: self._tasks[task] for task in pending})
                if timer in done:
                    break
            incomplete = bool(pending)
            for task in pending:
                task.cancel()
                self.ctx.logger.warning(f"插件卸载：五秒预算内未退出：{labels[task]}；保持发送关闭及账本独占")
            if not incomplete and self._ledger is not None and not isinstance(self._ledger, LedgerUnavailable):
                label = "账本关闭（源消息/目标群=本插件账本，阶段=存储关闭）"
                if self._clock.now() >= deadline:
                    incomplete = True
                    self.ctx.logger.warning(f"插件卸载：预算已耗尽，{label}未开始，保持账本独占")
                else:
                    try:
                        # 专用执行器不占宿主默认线程池；实际 Future 保留到底层完成。
                        # 不取消等待 Future、不重试 close，避免提前释放或关错复用的描述符。
                        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="courier-ledger-close")
                        try:
                            self._ledger_close_work = executor.submit(self._ledger.close)
                        finally:
                            executor.shutdown(wait=False)
                        storage = asyncio.wrap_future(self._ledger_close_work)
                        self._register_task(storage, label)
                        await asyncio.wait((storage, timer), return_when=asyncio.FIRST_COMPLETED)
                        if not storage.done() or self._clock.now() > deadline:
                            incomplete = True
                            self.ctx.logger.warning(f"插件卸载：五秒预算内{label}未确认；底层工作未被取消，使用权以真实关闭为准")
                        elif storage.cancelled() or storage.exception() is not None:
                            incomplete = True
                            self.ctx.logger.warning(f"插件卸载：{label}未确认，未宣称释放独占")
                    except Exception:
                        incomplete = True
                        self.ctx.logger.warning(f"插件卸载：{label}未确认，未宣称释放独占")
            if self._clock.now() > deadline:
                incomplete = True
                self.ctx.logger.warning("插件卸载：清理超出五秒总预算")
            if incomplete:
                self.ctx.logger.warning("麦麦搬运喵已停止接受新提交，清理未完成，不宣称全部退出")
            else:
                self.ctx.logger.info("麦麦搬运喵已卸载")
        finally:
            timer.cancel()

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """宿主先注入新配置；仅取消失效候选，不整体取消在途投递。"""
        del scope, config_data, version
        self._cancel_invalid_auto_entries()
        signature = self._connection_signature()
        if self._seen_connection is not None and signature != self._seen_connection:
            self._config_generation += 1
        self._seen_connection = signature

    def _start_client_close(self, label: str) -> asyncio.Task[None]:
        """每个客户端只创建一个关闭任务，只由连接替换路径触发（卸载不再关闭连接）。"""
        if self._client_close_task is None:
            assert self._client is not None
            self._client_close_task = asyncio.create_task(self._client.close())
            self._register_task(self._client_close_task, label)
        return self._client_close_task

    def _connection_signature(self) -> tuple[str, str, str]:
        return (self.config.snowluma.http_url, self.config.snowluma.token_env_var,
                self.config.snowluma.token)

    def _adopt_client(self, connection: OneBotConnection | OneBotUnresolved) -> SnowLumaClient | None:
        """客户端实例的唯一写点：按连接构造并写入 `_client`/`_connection`/`_client_generation`。

        连接不可解析（端点未配置、地址非法或凭证缺失）时返回 `None`，不构造、不改写
        既有实例；刷新策略（连接替换与代次比较）留在各自调用方。
        """

        if isinstance(connection, OneBotUnresolved):
            return None
        client = SnowLumaClient(transport=self._transport_factory(connection), clock=self._clock,
                                config_generation=self._config_generation)
        self._client = client
        self._connection = connection
        self._client_generation = self._config_generation
        return client

    def _forget_task(self, task: asyncio.Future[Any]) -> None:
        """任务结束后从本插件台账移除：卸载只等待仍在运行的任务。"""

        self._tasks.pop(task, None)
        if not task.cancelled():
            task.exception()

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

        复用 `auto_entry.auto_entry_invalid_reason`（与等待结束后的重验同一谓词）。
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
            "把本群的一条消息转发到允许的目标：目标群或私聊对端，可附带一条评论。"
            "只接受 message_id、target_group_id 与 target_user_id 二选一，以及可选 comment "
            "四个字符串参数，不支持或无法完整恢复的内容会被明确拒绝。"
        ),
        parameters=FORWARD_TOOL_PARAMETERS,
        chat_scope="group",
    )
    async def forward_message(self, **kwargs: Any) -> dict[str, Any]:
        """有限在途容量覆盖预检到结束；所有退出路径都释放名额。"""
        return await self._inflight_guard(
            f"投递繁忙：已有 {MAX_INFLIGHT_DELIVERIES} 个在途请求，本次未发送任何消息",
            lambda reason: self._finish(self._render_rejection(reason, kwargs)),
            lambda: self._forward_request(**kwargs),
        )

    async def _inflight_guard(
        self,
        busy_reason: str,
        reject: Callable[[str], dict[str, Any]],
        operation: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        """有限在途容量（代码常量，不驱逐既有投递）：判满按调用方拒绝面返回。

        预检与等待都算在途；转发工具与名册工具共用同一名额，拒绝面经 `reject` 工厂
        由调用方给出。
        """

        if self._inflight >= MAX_INFLIGHT_DELIVERIES:
            return reject(busy_reason)
        self._inflight += 1
        try:
            return await operation()
        finally:
            self._inflight -= 1

    def _offline_reason(self) -> str:
        """插件不可用的统一原因文案：卸载与总开关关闭共用一句，不各自复制。"""

        return "插件已卸载" if self._closed else "插件总开关关闭"

    async def _forward_request(self, **kwargs: Any) -> dict[str, Any]:
        """可信来源预检后交给本地账本与直连编排；所有权限从最新配置读取。

        连接替换只在旧投递结束后进行，关闭等待结束后仍须复验；发送的最后许可交给
        传输同步回调，不只靠本处预检。
        """
        if not self._is_live():
            reason = self._offline_reason()
            return self._finish(self._render_rejection(f"{reason}，{NOTHING_SENT_NOTE}", kwargs))

        signature = self._connection_signature()
        if self._seen_connection is None:
            self._seen_connection = signature
        generation = self._config_generation
        outcome = await precheck_forward_request(ctx=self.ctx, config=self.config,
                                                 bindings=self._bindings, arguments=kwargs)
        if isinstance(outcome, Rejected):
            return self._finish(self._render_rejection(outcome.reason, kwargs))
        group_label = self._group_label(outcome)
        # 目标定位按形态展示：两个目标字段同一时刻只有一个非空（参数解析时已判
        # 二选一），名册备注与昵称从不进入任务表。
        target = _target_label(
            outcome.arguments.target_group_id,
            outcome.arguments.target_user_id,
        )
        if (not self._send_allowed(outcome) or generation != self._config_generation
                or signature != self._connection_signature()):
            return self._finish(self._render_rejection(Rejected.not_sent("源发送前权限复验未通过（插件已卸载、开关或配置变化）").reason, kwargs), group_label)
        connection = resolve_onebot_connection(self.config, self._environ)
        if isinstance(connection, OneBotUnresolved):
            return self._finish(self._render_rejection(Rejected.not_sent(connection.reason).reason, kwargs), group_label)
        if self._ledger is None:
            self._ledger = self._ledger_opener(self.ctx.paths.data_dir / LEDGER_FILE_NAME)
        if self._client is not None and (self._connection != connection or self._client_generation != generation):
            if self._client_refreshing or any(not task.done() for task in self._delivery_tasks):
                return self._finish(self._render_rejection("旧连接仍有在途工作，拒绝新投递", kwargs), group_label)
            self._client_refreshing = True
            try:
                close_task = self._start_client_close(
                    f"旧连接关闭（源消息={outcome.arguments.message_id}，{target}，阶段=连接替换）")
                await asyncio.shield(close_task)
            except (Exception, asyncio.CancelledError):
                return self._finish(self._render_rejection("旧连接关闭未确认，拒绝新投递", kwargs), group_label)
            finally:
                self._client_refreshing = False
            if (not self._send_allowed(outcome) or generation != self._config_generation
                    or signature != self._connection_signature()):
                return self._finish(self._render_rejection("连接关闭等待后许可已失效，本次未发送任何消息", kwargs), group_label)
            self._client = None
            self._client_close_task = None
        if self._client is None:
            self._adopt_client(connection)
        assert self._client is not None
        prefix = f"分组={group_label}，" if group_label else ""
        label = f"投递（{prefix}源消息={outcome.arguments.message_id}，{target}"
        def stage_changed(stage: str) -> None:
            description = f"{label}，阶段={stage}）"
            self._task_origin.set(description)
            current = asyncio.current_task()
            if current is not None:
                self._tasks[current] = description
        task = asyncio.create_task(deliver_forward(
            prepared=outcome, ledger=self._ledger, client=self._client, clock=self._clock,
            stage_changed=stage_changed,
            send_allowed=lambda: (self._send_allowed(outcome) and generation == self._config_generation
                                  and signature == self._connection_signature()),
        ))
        self._delivery_tasks.add(task)
        task.add_done_callback(self._delivery_tasks.discard)
        self._register_task(task, f"{label}，阶段=源准备）")
        result = await task
        return self._finish(render_forward_result(result, material=outcome.material,
            source_message_id=outcome.arguments.message_id,
            target_group_id=outcome.arguments.target_group_id,
            target_kind=outcome.arguments.target_kind,
            target_user_id=outcome.arguments.target_user_id),
            group_label)

    def _send_allowed(self, prepared: PreparedForward) -> bool:
        """同步复验最新开关与目标判定；源和评论实际提交前均复用（按形态分治）。

        都从 `self.config` 现算，不共享中间缓存：群目标走成对判定
        （`shared_forward_group`，目标被移出来源群所属分组在评论提交前同样拦截，
        已提交的源投递不回滚）；私聊目标改判分组私聊开关（`source_group_private_allowed`，
        与预检私聊分支同一单点判定）。私聊目标的 `target_group_id` 是空串，群成对
        判定对它必然不命中；反过来，`private` 开关也不放宽任何群目标判定。
        """

        if not self._is_live():
            return False
        if prepared.arguments.target_kind == TARGET_KIND_PRIVATE:
            return source_group_private_allowed(self.config, prepared.source.group_id)
        return shared_forward_group(
            self.config,
            prepared.source.group_id,
            prepared.arguments.target_group_id,
        ) is not None

    def _group_label(self, prepared: PreparedForward) -> str:
        """本次投递所属转发分组的压平显示名（得不到分组名时返回空串）。

        分组名只作日志与任务描述定位，不作为任何判定输入；私聊目标没有目标群号，
        成对判定必然不命中，直接早退空串。
        """

        if prepared.arguments.target_kind == TARGET_KIND_PRIVATE:
            return ""
        name = shared_forward_group(
            self.config,
            prepared.source.group_id,
            prepared.arguments.target_group_id,
        )
        return _flattened_group_name(name) if name else ""

    def _finish(self, rendered: dict[str, Any], group_label: str = "") -> dict[str, Any]:
        """记录可定位的失败信息并返回结果（完整成功不额外记录）。

        日志只含压平分组名、阶段、源消息 ID、按形态的目标定位、状态与中文原因：
        不回显评论正文、不输出一次性来源凭据。
        """

        if not rendered["success"]:
            prefix = f"分组={group_label}，" if group_label else ""
            target = _target_label(
                str(rendered["target_group_id"] or ""),
                str(rendered["target_user_id"] or ""),
            )
            self.ctx.logger.warning(
                f"转发未完成：{prefix}阶段={rendered['stage'] or 'none'}，"
                f"源消息={rendered['source_message_id'] or '未知'}，"
                f"{target}，"
                f"状态={rendered['status']}，原因={rendered['reason']}"
            )
        return rendered

    def _render_rejection(self, reason: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """渲染拒绝结果：与投递结果同形，并尽力带回模型声明的源消息与目标用于定位。

        只读取调用参数里的字符串，不做任何取值、查询或发送；预检未通过时尚无材料，
        结果不含材料键。目标按形态带回，两个目标参数都缺失或非法时形态留空、不猜测。
        """

        target_group_id = _argument_text(arguments, "target_group_id")
        target_user_id = _argument_text(arguments, "target_user_id")
        target_kind: TargetKind | Literal[""] = (
            TARGET_KIND_PRIVATE if target_user_id
            else TARGET_KIND_GROUP if target_group_id
            else ""
        )
        return render_forward_result(
            DeliveryResult(status=TOOL_STATUS_REJECTED, stage=STAGE_REJECTED, reason=reason),
            source_message_id=_argument_text(arguments, "message_id"),
            target_group_id=target_group_id,
            target_kind=target_kind,
            target_user_id=target_user_id,
        )

    # ------------------------------------------------------------------ 名册工具入口

    @Tool(
        LIST_FORWARD_FRIENDS_TOOL_NAME,
        description=(
            "只读列出当前 QQ 账号的好友名册：仅当本群所在转发分组开启私聊转发（private）时"
            "可用，不发送任何消息。只返回好友 QQ 号、备注与昵称，不代做匹配、排序或候选收敛，"
            "选择私聊转发目标由调用方自行完成。无参数。"
        ),
        parameters=FRIEND_LIST_TOOL_PARAMETERS,
        chat_scope="group",
    )
    async def list_forward_friends(self, **kwargs: Any) -> dict[str, Any]:
        """与转发工具共用同一在途容量；所有退出路径都释放名额，任何路径都不发送。"""
        return await self._inflight_guard(
            f"名册查询繁忙：已有 {MAX_INFLIGHT_DELIVERIES} 个在途请求",
            self._friend_list_rejection,
            lambda: self._list_forward_friends(**kwargs),
        )

    async def _list_forward_friends(self, **kwargs: Any) -> dict[str, Any]:
        """凭据换可信来源 → 分组私聊开关 → 只读名册查询；所有拒绝都早于任何平台请求。

        查询走固定只读动作 `get_friend_list`，只用一次即关（本次调用自建传输），
        不进入任何发送阶段、不重试；条目数超过 `MAX_FRIEND_ENTRIES` 即截断并在
        结果中明示。
        """

        if not self._is_live():
            reason = self._offline_reason()
            return self._friend_list_rejection(reason)
        # 名册凭据只绑会话：哨兵消息 ID 收在凭据表内（`consume_roster`），调用方
        # 不需要知道它，也不会把转发工具的 message_id 绑定语义带到名册路径上。
        session_id = self._bindings.consume_roster(kwargs)
        if session_id is None:
            return self._friend_list_rejection(
                "无法确认本次调用的可信来源（不接受模型提供的会话或授权字段）"
            )
        # 会话→来源群解析与转发预检共用 `resolve_source_session` 单点定义（查找、
        # 字段归一、群聊/平台校验同一口径）；账号核对是转发侧步骤，名册不需要。
        source = await resolve_source_session(self.ctx, session_id)
        if isinstance(source, Rejected):
            return self._friend_list_rejection(source.reason)
        source_group_id = source.group_id
        if not source_group_is_allowed(self.config, source_group_id):
            return self._friend_list_rejection("调用来源群不在任何转发分组内")
        # 私聊许可判定与预检私聊分支、提交前复验三处共用 `source_group_private_allowed`。
        if not source_group_private_allowed(self.config, source_group_id):
            return self._friend_list_rejection(
                "来源群所属分组未开启私聊转发（private 开关为假），拒绝提供好友名册"
            )
        connection = resolve_onebot_connection(self.config, self._environ)
        if isinstance(connection, OneBotUnresolved):
            return self._friend_list_rejection(connection.reason)
        transport = self._transport_factory(connection)
        try:
            response = await transport.get_friend_list()
        except OneBotUnavailable as error:
            return self._friend_list_rejection(f"好友名册查询不可用（类别={error.category}）")
        finally:
            await self._close_roster_transport(transport)
        roster = response_payload(response)
        if not isinstance(roster, list):
            return self._friend_list_rejection(
                "平台明确拒绝好友名册查询（业务失败）" if business_failure(response)
                else "好友名册查询结果不可识别（未返回条目数组）"
            )
        entries: list[dict[str, str]] = []
        for item in roster[:MAX_FRIEND_ENTRIES]:
            identifier = friend_entry_identifier(item)
            if not identifier:
                return self._friend_list_rejection("好友名册条目字段不可识别（QQ 号不可确认）")
            entries.append({
                "user_id": identifier,
                "remark": friend_entry_remark(item),
                "nickname": friend_entry_nickname(item),
            })
        truncated = len(roster) > MAX_FRIEND_ENTRIES
        # 名册数据最小暴露面：日志只记查询类别与条目数，不记任何条目字段。
        self.ctx.logger.info(
            f"好友名册只读查询：类别=好友名册，条目数={len(entries)}，已截断={truncated}"
        )
        result: dict[str, Any] = {"success": True, "friends": entries, "truncated": truncated}
        if truncated:
            result["note"] = (
                f"名册共 {len(roster)} 条，仅返回前 {MAX_FRIEND_ENTRIES} 条（已截断）"
            )
        return result

    async def _close_roster_transport(self, transport: OneBotTransport) -> None:
        """名册查询的传输只用一次即关：关闭失败不影响已取得的数据，也不重试。"""

        try:
            await transport.close()
        except Exception:
            pass

    def _friend_list_rejection(self, reason: str) -> dict[str, Any]:
        """名册工具的拒绝面：只含可定位原因，不含任何名册条目。"""

        located = Rejected.not_sent(reason).reason
        self.ctx.logger.warning(f"好友名册查询未完成：原因={located}")
        return {"success": False, "reason": located}

    # ------------------------------------------------------------------ Hook

    @HookHandler(
        PLANNER_AFTER_RESPONSE_HOOK,
        name=BIND_FORWARD_SOURCE_HANDLER,
        description="模型输出工具调用后：为本轮本插件工具调用（转发与名册）签发一次性可信来源凭据。",
        mode=BLOCKING_HOOK_MODE,
        order=EARLY_HOOK_ORDER,
    )
    async def bind_forward_source(self, **kwargs: Any) -> dict[str, Any]:
        """在本轮 Planner 输出上签发来源凭据（模型参数里带不进可信会话）。

        载荷由宿主构造（`session_id` + `output_items`），只把指向本插件两个工具的
        调用改写为「公开参数 + 本次一次性凭据」（同批其它工具原样保留且不获凭据）。
        插件关闭或已卸载时不改写、不签发，工具调用自然拒绝。
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

        观察模式由宿主后台分发，返回值不参与聚合，因此既不拦截也不改写原消息
        处理；候选筛选与去重登记都在本协程内同步完成，随后把「确认可读取 →
        唤起源群判断」交给本插件自己的后台任务。
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
            f"自动判断（源消息={candidate.source_message_id}，源群={candidate.group_id}，"
            "目标群=待源群选择，阶段=可读性与名称等待）"
        )

        def _forget(done: asyncio.Task[None]) -> None:
            self._auto_tasks.pop(done, None)
            self._forget_task(done)

        task.add_done_callback(_forget)

    def _auto_entry_client(self) -> SnowLumaClient | None:
        """自动路径只读查询用的客户端：复用工具路径实例，连接不符时按当前配置重建。

        与 `_forward_request` 同一惰性创建口径；自动路径只创建/复用、不关闭旧连接
        （HTTP 无长连接，旧客户端的在途请求由各自持有的引用自行收尾，连接替换与
        关闭竞裁仍只由工具路径负责）。
        """

        connection = resolve_onebot_connection(self.config, self._environ)
        if self._client is not None and self._connection == connection:
            return self._client
        return self._adopt_client(connection)

    async def _presearch_card_content(self, candidate: ForwardCandidate) -> str | None:
        """自动入口的卡片内容预搜索（`run_auto_entry` 的 `presearch` 缝隙）。

        命中嵌套内层卡片返回拒绝原因（据此不提供判断机会）；客户端不可用与任何
        查询异常都返回 `None` 放行，能否发送由工具路径在发送前权威拒绝。查询只读、
        有界，不设置提交许可闸门。
        """

        if self._closed:
            return None
        client = self._auto_entry_client()
        if client is None:
            return None
        try:
            return await client.card_content_rejection(candidate.source_message_id)
        except Exception:
            return None

    async def _run_auto_entry(self, candidate: ForwardCandidate) -> None:
        """执行一次自动判断唤起，并记录可定位结果（后台任务不得丢失异常）。"""

        try:
            outcome = await run_auto_entry(
                ctx=self.ctx,
                candidate=candidate,
                clock=self._clock,
                current_config=self._auto_entry_config,
                presearch=self._presearch_card_content,
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

        自动判断不缓存配置：等待期间的热更新必须对「还要不要唤起」生效，同组目标
        的增删也按同一份最新快照重新计算；观察 Hook 入口也经本判定早退。
        """

        if not self._is_live() or not self.config.auto.enabled:
            return None
        return self.config


def create_plugin(clock: Clock | None = None,
                  transport_factory: TransportFactory | None = None,
                  environ: Mapping[str, str] | None = None,
                  ledger_opener: LedgerFactory | None = None) -> CourierPlugin:
    """宿主无参工厂；测试经公开参数注入依赖，不安装真实 SDK。"""
    return CourierPlugin(clock=clock, transport_factory=transport_factory,
                         environ=environ, ledger_opener=ledger_opener)


def _argument_text(arguments: Mapping[str, Any], name: str) -> str:
    """取调用参数里的短文本用于结果定位（只接受字符串，不做任何转换）。"""

    value = arguments.get(name)
    return value.strip() if isinstance(value, str) else ""
