"""插件组合根：宿主可信入口、本地账本及自己的 SnowLuma HTTP 客户端。"""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import Future as WorkerFuture, ThreadPoolExecutor
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable, Final, Mapping

from maibot_sdk import HookHandler, MaiBotPlugin, Tool

from .auto_entry import (AutoEntryDedup, ForwardCandidate, NotACandidate,
                         auto_entry_invalid_reason, run_auto_entry, screen_forward_candidate)
from .shared.clock import Clock, SystemClock
from .config import CourierConfig
from .delivery_ledger import LEDGER_FILE_NAME, DeliveryLedger, LedgerUnavailable, open_ledger
from .forward_delivery import deliver_forward
from .forward_precheck import PreparedForward, precheck_forward_request, shared_forward_group
from .shared.result import DeliveryResult, STAGE_REJECTED, TOOL_STATUS_REJECTED, render_forward_result
from .snowluma_client import SnowLumaClient
from .snowluma_transport import (AiohttpOneBotTransport, OneBotConnection, OneBotTransport,
                                 OneBotUnresolved, resolve_onebot_connection)
from .source_binding import (
    FORWARD_TOOL_NAME,
    FORWARD_TOOL_PARAMETERS,
    NOTHING_SENT_NOTE,
    Rejected,
    SourceBindingTable,
)

RECEIVE_AFTER_PROCESS_HOOK = "chat.receive.after_process"
"""观察已处理入站消息的宿主 Hook 名。"""

PLANNER_AFTER_RESPONSE_HOOK = "maisaka.planner.after_response"
"""签发一次性来源凭据的宿主 Hook。

该 Hook 在模型输出工具调用后触发且允许改写 `output_items`
（`allow_kwargs_mutation` 为 True），是工具侧唯一不可被模型伪造的来源：载荷由
宿主构造，模型写不到 `session_id`。
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
"""本插件所有退出工作共享的总预算。"""

MAX_INFLIGHT_DELIVERIES: Final[int] = 8
"""包括预检和等待在内的工具容量；不驱逐既有投递。"""

TransportFactory = Callable[[OneBotConnection], OneBotTransport]
LedgerFactory = Callable[[Path], DeliveryLedger | LedgerUnavailable]


def _flattened_group_name(name: str) -> str:
    """把分组名压平为单行定位文本（空白折叠为空格，剔除控制字符，其余原样保留）。

    分组名不限长与字符集，但日志与任务描述必须保持单行；只压平显示形态，判定只认
    群号，不用本函数的结果。
    """

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
    """插件主类：三生命周期 + 一个群聊 Tool + 来源绑定与观察两个 Hook。"""

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
        """把转发 Tool 的 `chat_scope` 提到组件声明**顶层**（承载性修正）。

        Runner 只读组件声明顶层的 `chat_scope`（缺省回落 `"all"`），而
        `@Tool(chat_scope=…)` 经 SDK 只会落进组件 metadata。不覆写这一层，群聊
        专用的转发工具会在私聊会话中同样可见（可见性过滤失效）。
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
        """按连接构造客户端，并写入 `_client`/`_connection`/`_client_generation` 三字段。

        客户端实例的唯一写点：工具路径连接替换尾段与自动路径惰性创建都经本方法落账；
        刷新策略（工具路径的连接替换/关闭竞裁与代次比较）留在各自调用方，不在这里合并
        语义。连接不可解析（端点未配置、地址非法或凭证缺失）时返回 `None`，不构造、
        不改写既有实例。
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
        """有限在途容量覆盖预检到结束；所有退出路径都释放名额。"""
        if self._inflight >= MAX_INFLIGHT_DELIVERIES:
            return self._finish(self._render_rejection("投递繁忙：已有八个在途请求，本次未发送任何消息", kwargs))
        self._inflight += 1
        try:
            return await self._forward_request(**kwargs)
        finally:
            self._inflight -= 1

    async def _forward_request(self, **kwargs: Any) -> dict[str, Any]:
        """可信来源预检后交给本地账本与直连编排；所有权限从最新配置读取。

        连接替换只在旧投递结束后进行，关闭等待结束后仍须复验，不能借卸载竞态
        创建新任务。发送的最后许可交给传输同步回调，而不是只靠本处预检。
        """
        if not self._is_live():
            reason = "插件已卸载" if self._closed else "插件总开关关闭"
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
                    f"旧连接关闭（源消息={outcome.arguments.message_id}，目标群={outcome.arguments.target_group_id}，阶段=连接替换）")
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
        label = f"投递（{prefix}源消息={outcome.arguments.message_id}，目标群={outcome.arguments.target_group_id}"
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
            source_message_id=outcome.arguments.message_id, target_group_id=outcome.arguments.target_group_id),
            group_label)

    def _send_allowed(self, prepared: PreparedForward) -> bool:
        """同步复验最新开关与同组判定；源和评论实际提交前均复用。

        成对判定（`shared_forward_group`）从 `self.config` 现算：预检与复验不共享中间
        缓存，分组收紧（目标被移出来源群所属分组）在评论提交前同样拦截，已提交的源
        投递不回滚。
        """

        return self._is_live() and shared_forward_group(
            self.config,
            prepared.source.group_id,
            prepared.arguments.target_group_id,
        ) is not None

    def _group_label(self, prepared: PreparedForward) -> str:
        """本次投递所属转发分组的压平显示名（得不到分组名时返回空串）。

        分组名只作日志与任务描述定位：压平空白与控制字符成单行文本，不作为任何判定
        输入（判定只认群号，`shared_forward_group` 返回的是原始去空白组名）。
        """

        name = shared_forward_group(
            self.config,
            prepared.source.group_id,
            prepared.arguments.target_group_id,
        )
        return _flattened_group_name(name) if name else ""

    def _finish(self, rendered: dict[str, Any], group_label: str = "") -> dict[str, Any]:
        """记录可定位的失败信息并返回结果（完整成功不额外记录）。

        日志只含分组名（压平后）、阶段、源消息 ID、目标群 ID、状态与中文原因：不回显
        评论正文、不输出一次性来源凭据，也不生成任何摘要或评论副本。
        """

        if not rendered["success"]:
            prefix = f"分组={group_label}，" if group_label else ""
            self.ctx.logger.warning(
                f"转发未完成：{prefix}阶段={rendered['stage'] or 'none'}，"
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
        「公开参数 + 本次一次性凭据」；`modified_kwargs` 随后成为工具调用的参数。
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

        观察模式由宿主后台分发，返回值不参与聚合，因此这里既不拦截也不改写原
        消息处理。默认关闭或自动入口关闭时直接返回，不产生任何宿主调用；候选
        筛选与去重登记都在本协程内同步完成，随后把「确认可读取 → 唤起源群判断」
        交给本插件自己的后台任务。
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

        与 `_forward_request` 同一惰性创建口径：连接不可解析（端点未配置、地址非法或
        凭证缺失）时返回 `None`，组合根据此跳过预搜索。创建后的客户端与工具路径复用
        同一实例（连接相同不触发替换）；自动路径只创建/复用、不关闭旧连接——HTTP 无
        长连接，旧客户端的在途请求由各自持有的引用自行收尾，连接替换与关闭竞裁仍只由
        工具路径负责。构造与三字段写入都走 `_adopt_client` 单写点。
        """

        connection = resolve_onebot_connection(self.config, self._environ)
        if self._client is not None and self._connection == connection:
            return self._client
        return self._adopt_client(connection)

    async def _presearch_card_content(self, candidate: ForwardCandidate) -> str | None:
        """自动入口的卡片内容预搜索（`run_auto_entry` 的 `presearch` 缝隙）。

        命中嵌套内层卡片返回拒绝原因（据此不提供判断机会并记录可定位日志）；客户端
        不可用（连接不可解析）与任何查询异常都返回 `None` 放行：照常给源群
        一次判断机会，能否发送由工具路径在发送前权威拒绝。查询只读、有界，与工具路径
        共用同一传输替身，不设置提交许可闸门。
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
        的增删也按同一份最新快照重新计算。观察 Hook 入口也经本判定早退（与等待结束
        后的判定同源），卸载标记同样在此生效。
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
