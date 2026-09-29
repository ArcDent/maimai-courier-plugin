"""麦麦搬运喵：麦麦在群聊间分享消息的独立插件。

本模块是组合根：承载 SDK 生命周期、一个群聊 Tool（`forward_message`）与两个 Hook
的声明——`maisaka.planner.after_response`（为工具调用签发一次性可信来源凭据）与
`chat.receive.after_process`（观察已处理入站消息，交给源群自动判断入口），并把
它们与持久账本、发送接缝装配在一起：

- `forward_precheck.py` / `send_material.py`：参数、来源、目标与内容预检；
- `delivery_ledger.py`：`data_dir` 内的 SQLite 投递账本（持久防重）；
- `forward_delivery.py`：投递编排（占位 → 源发送 → 回执落账 → 固定延迟评论 →
  统一结果面 `render_forward_result`）；
- `host_send.py`：两个发送接缝的真实实现（源消息 `send.text` / `send.image` /
  `send.hybrid` / `send.forward`；评论 `send.text`，均解释详细回执）；
- `auto_entry.py`：源群自动判断入口（候选筛选、可读性有限等待、有界去重、意图构造
  与一次 `maisaka.proactive.trigger` 唤起）；它只唤起判断，不发送任何消息。

生命周期（design D7）：`on_load` 检查一次账本；`on_unload` 取消并**等待**本插件创建
的全部后台任务（自动等待与在途投递）后释放账本连接；`on_config_update` 立即取消失效
的待执行自动判断，投递则在源发送前与评论发送前按最新配置复验 `_send_allowed`。取消
只作用于本插件自己的任务，不触碰其他插件的任务。

术语见 CONTEXT.md，设计取舍见同级 change 的 design.md。
"""

from __future__ import annotations

import asyncio
from typing import Any, Final, Mapping

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
from .delivery_ledger import (
    LEDGER_FILE_NAME,
    DeliveryLedger,
    LedgerOpener,
    LedgerUnavailable,
    LedgerUnavailableError,
    open_ledger as open_delivery_ledger,
)
from .forward_delivery import (
    STAGE_REJECTED,
    TOOL_STATUS_REJECTED,
    CommentSendStep,
    DeliveryResult,
    SourceSendResult,
    SourceSendStep,
    deliver_forward,
    render_forward_result,
)
from .forward_precheck import (
    PreparedForward,
    precheck_forward_request,
    source_group_is_allowed,
    target_is_allowed,
)
from .host_send import send_comment_text, send_prepared_forward
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


class CourierPlugin(MaiBotPlugin):
    """插件主类：三生命周期 + 一个群聊 Tool + 来源绑定与观察两个 Hook。"""

    config_model = CourierConfig

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        send_source: SourceSendStep | None = None,
        open_ledger: LedgerOpener | None = None,
    ) -> None:
        """构造插件实例；`clock` / `send_source` / `open_ledger` 供测试注入。

        生产缺省：真实时钟、真实 `send.*` 发送步骤（`host_send.send_prepared_forward`）
        与 `data_dir` 下的 SQLite 账本。
        """

        super().__init__()
        self._clock: Clock = clock or SystemClock()
        self._bindings = SourceBindingTable()
        self._send_source: SourceSendStep = send_source or self._send_prepared_forward
        self._send_comment: CommentSendStep = self._send_comment_text
        if open_ledger is not None:
            self._open_ledger: LedgerOpener = open_ledger
        else:
            clock_source = self._clock
            self._open_ledger = lambda path: open_delivery_ledger(path, clock=clock_source)
        self._ledger_state: DeliveryLedger | LedgerUnavailable | None = None
        self._auto_dedup = AutoEntryDedup(self._clock)
        self._closed = False
        self._tasks: dict[asyncio.Task[Any], str] = {}
        self._auto_tasks: dict[asyncio.Task[None], ForwardCandidate] = {}

    def _is_enabled(self) -> bool:
        """插件总开关是否开启；关闭时不得产生任何发送。"""

        return bool(self.config.plugin.enabled)

    def _is_live(self) -> bool:
        """插件是否仍在工作：未卸载且总开关开启。

        工具入口、来源绑定、发送前复验与自动入口共用本谓词，避免「卸载标记 + 总开关」
        这一组合在各处各写一遍（各处的后续判定与措辞仍各自保留）。
        """

        return not self._closed and self._is_enabled()

    def _ledger(self) -> DeliveryLedger | LedgerUnavailable:
        """打开（或复用）投递账本；不可用时只报一次错误并关闭发送。

        路径固定为宿主授予的 `ctx.paths.data_dir`；打开失败或库文件损坏时不视为
        空库、不覆盖也不删除既有数据，只是拒绝发送。打开成功即完成一次启动恢复：
        上一次进程中断遗留的 `sending` 归并为待核实，避免盲目重发；恢复本身失败时
        至少告警一次（失败与「归并零条」可区分），遗留 `sending` 继续阻止重发。
        """

        if self._ledger_state is None:
            state = self._open_ledger(self.ctx.paths.data_dir / LEDGER_FILE_NAME)
            if isinstance(state, LedgerUnavailable):
                self.ctx.logger.error(
                    "投递账本不可用，已关闭发送：%s",
                    state.reason,
                )
            else:
                try:
                    recovered = state.recover_interrupted()
                except LedgerUnavailableError as error:
                    self.ctx.logger.warning(
                        "启动恢复遗留投递记录失败（遗留的发送中记录仍会阻止重发）：%s",
                        error.reason,
                    )
                else:
                    if recovered:
                        self.ctx.logger.warning("发现进程中断遗留的投递记录，已标记为待核实")
            self._ledger_state = state
        return self._ledger_state

    async def _send_prepared_forward(self, prepared: PreparedForward) -> SourceSendResult:
        """默认发送接缝：真实宿主发送链（`host_send.send_prepared_forward`）。

        只做上下文绑定，不新增抽象层：通路选择与详细回执解释都在 `host_send.py`；
        `create_plugin(send_source=…)` 可注入替身替换本接缝（测试与第 4 组的替身）。
        """

        return await send_prepared_forward(self.ctx, prepared)

    async def _send_comment_text(self, stream_id: str, text: str) -> SourceSendResult:
        """默认评论接缝：按普通文本发送本次评论（不解析成命令或消息段）。"""

        return await send_comment_text(self.ctx, stream_id, text)

    def _send_allowed(self, prepared: PreparedForward) -> bool:
        """发送前的权限复验：按**发送时**的最新配置核对开关与两级白名单。

        工具入口的预检结果只是「进入投递」的资格；实际源发送前与评论发送前都再走
        一次本判定（design D7）。卸载标记、总开关、来源群白名单、目标群白名单任一
        不满足都不得再发送：不使用调用开始时的旧快照，也不依赖宿主是否已通知过配置
        热更新。
        """

        return (
            self._is_live()
            and source_group_is_allowed(self.config, prepared.source.group_id)
            and target_is_allowed(self.config, prepared.target.group_id)
        )

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

        加载即检查一次投递账本（第 4 组 4.6）：库文件损坏或不可读时只记录明确
        错误并关闭发送，不视为空库继续，也不覆盖或删除既有状态数据。
        """

        self._ledger()
        self.ctx.logger.info("麦麦搬运喵已加载")

    async def on_unload(self) -> None:
        """生命周期：插件卸载。

        取消并等待本插件创建的全部后台任务（自动等待与在途投递），再释放账本连接：
        卸载后不再存在继续发送的孤立任务；已经发生的成功发送保留在账本里，不回滚。
        等待有上限（`TASK_EXIT_WAIT_SECONDS`），超出即告警，不静默留下孤立任务。
        """

        self._closed = True
        await self._drain_tasks("插件卸载")
        if isinstance(self._ledger_state, DeliveryLedger):
            self._ledger_state.close()
            self._ledger_state = None
        self.ctx.logger.info("麦麦搬运喵已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """生命周期：插件配置热更新。

        宿主在 scope 为 `self` 时先注入最新配置（`runner_main.py:2273-2281` 调
        `_apply_plugin_config` → `set_plugin_config`，`runner_main.py:899-901`）再调用
        本入口，因此这里按只读的 `self.config` 读到的就是新配置，不需要也无法自行
        改写配置属性。

        热更新后立即取消**已失效**的待执行自动判断（总开关/自动入口关闭、来源群或
        目标白名单不再允许）：待执行工作不得继续按开始时的旧快照走。投递工作不做
        整体取消——在途发送已经可能产生副作用，正确做法是源发送前与评论发送前按最新
        配置逐次复验（`_send_allowed`），未发送的一律不发送、已成功的如实保留。
        """

        del scope, config_data, version
        self._cancel_invalid_auto_entries()

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
        """待执行自动判断是否已失效：已卸载，或最新配置不再允许对来源群唤起。

        判定复用 `auto_entry.auto_entry_invalid_reason`（与等待结束后的重验同一谓词）；
        本方法只决定「现在要不要取消」，不负责取消时机之外的事情。
        """

        return self._closed or bool(auto_entry_invalid_reason(self.config, candidate.group_id))

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
        """群聊转发工具入口：总开关 + 参数/权限/内容预检 + 投递编排。

        工具调用参数里的会话、平台与授权字段一律不读：来源可信性只由本插件在
        宿主 Hook 上签发的一次性凭据决定（见 `source_binding.py`）。预检通过后交给
        `forward_delivery.deliver_forward`：持久占位 → 源发送步骤 → 回执落账。

        投递在插件自己的后台任务里执行并登记：卸载时可以取消它，也可以等待它退出
        （design D7）。任务的取消与阻塞不影响已落账的事实。
        """
        if not self._is_live():
            reason = "插件已卸载" if self._closed else "插件总开关关闭"
            return self._finish(self._render_rejection(f"{reason}，{NOTHING_SENT_NOTE}", kwargs))
        outcome = await precheck_forward_request(
            ctx=self.ctx,
            config=self.config,
            bindings=self._bindings,
            arguments=kwargs,
        )
        if isinstance(outcome, Rejected):
            return self._finish(self._render_rejection(outcome.reason, kwargs))
        if self._closed:
            # 预检期间可能发生卸载：此时不得再打开账本、创建发送任务或发送任何消息。
            return self._finish(self._render_rejection(f"插件已卸载，{NOTHING_SENT_NOTE}", kwargs))
        task = asyncio.create_task(self._deliver(outcome))
        self._tasks[task] = (
            f"投递（源消息={outcome.arguments.message_id}，目标群={outcome.target.group_id}）"
        )
        task.add_done_callback(self._forget_task)
        result = await task
        return self._finish(
            render_forward_result(
                result,
                material=outcome.material,
                source_message_id=outcome.arguments.message_id,
                target_group_id=outcome.target.group_id,
            )
        )

    async def _deliver(self, prepared: PreparedForward) -> DeliveryResult:
        """在插件自己的后台任务里执行一次投递（卸载时可取消并被等待）。

        源发送前与评论发送前的权限复验共用 `_send_allowed`（按发送时最新配置）。
        """

        return await deliver_forward(
            ledger=self._ledger(),
            prepared=prepared,
            send_source=self._send_source,
            clock=self._clock,
            send_comment=self._send_comment,
            source_allowed=lambda: self._send_allowed(prepared),
            comment_allowed=lambda: self._send_allowed(prepared),
        )

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
                "自动判断已取消：源消息=%s，源群=%s",
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
    send_source: SourceSendStep | None = None,
    open_ledger: LedgerOpener | None = None,
) -> CourierPlugin:
    """SDK 工厂入口：Runner 通过本函数（无参）创建插件实例。

    `clock` / `send_source` / `open_ledger` 只为测试注入预留，宿主调用时保持缺省。
    """

    return CourierPlugin(clock=clock, send_source=send_source, open_ledger=open_ledger)


def _argument_text(arguments: Mapping[str, Any], name: str) -> str:
    """取调用参数里的短文本用于结果定位（只接受字符串，不做任何转换）。"""

    value = arguments.get(name)
    return value.strip() if isinstance(value, str) else ""

