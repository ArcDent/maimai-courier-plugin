"""插件直连执行入口：可信准备、类型分派与固定动作，回执按保守分类落结果。

无连接 HTTP 一次性请求-响应，按 `PreparedSource.kind` 分派固定发送动作：卡片
`send_group_forward_msg` 展开节点重组 / 普通图片 `send_msg` 段直发 / 普通文字
`send_forward_msg` 单节点卡片 / 评论 `send_msg` 普通文本。发送前的账号与归属
核验在每次动作前以最新查询复验；回执核验只按 `get_msg` 四项证据比对、不重试、
不外传资源 ID。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, TypeVar

from .forward_precheck import PreparedForward
from .send_material import (
    build_card_nodes, build_source_material, card_node_name, card_node_sender,
    classify_top_level_shape, forward_card_id, source_outbound_segments,
)
from .shared.clock import Clock
from .shared.identifiers import verbatim_identifier
from .shared.result import SourceSendResult
from .shared.send_steps import (
    PrepareRejected, PreparedSource, SourceMaterial, SourceSendRequest,
    CommentSendRequest,
)
from .snowluma_transport import (
    ONEBOT_ACTION_TIMEOUT_SECONDS, ONEBOT_CLOSE_TIMEOUT_SECONDS,
    OneBotTransport, OneBotUnavailable,
)
from .type_check import NESTED_FORWARD_DETAIL, unsupported_card_content

RECEIPT_VERIFY_TIMEOUT_SECONDS = 3.0
RECEIPT_VERIFY_INTERVAL_SECONDS = 0.5
MAX_RECEIPT_QUERY_ATTEMPTS = 8

BUSINESS_NOT_FOUND_RETCODE = 100
"""平台业务失败码：查询目标不存在（实测，见 API 目录）。明确业务结果，
不冒充传输异常；已发送但目标不存在按明确失败分类，不自动重试。"""

T = TypeVar("T")


def response_payload(response: object) -> Any:
    """平台动作应答信封的严格成功结构；事件、受理和契约外状态均不提供成功证据。"""
    if not isinstance(response, Mapping) or "post_type" in response or response.get("status") != "ok":
        return None
    if "retcode" in response and (type(response["retcode"]) is not int or response["retcode"] != 0):
        return None
    return response.get("data")


def response_data(response: object) -> Mapping[str, Any] | None:
    """只接受成功响应的映射数据。"""
    data = response_payload(response)
    return data if isinstance(data, Mapping) else None


def business_failure(response: object) -> bool:
    """平台业务失败信封（`retcode=100` 目标不存在）：明确业务结果，不冒充传输异常。

    已发送但回查不存在、或发送动作本身被平台业务层拒绝，都可证明「这一投递没有
    成功」，按明确失败登记、不自动重试；与传输异常（超时/断连）区分。
    """

    return (isinstance(response, Mapping) and response.get("status") == "failed"
            and type(response.get("retcode")) is int
            and response["retcode"] == BUSINESS_NOT_FOUND_RETCODE)


def canonical_message_id(value: object) -> str:
    """只供比对的整数十进制或原文标识；不从格式推断平台身份。"""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    return verbatim_identifier(str(value))


def _consume_exception(task: asyncio.Future[Any]) -> None:
    """回收已取消工作异常，不回显原始载荷。"""
    if not task.cancelled():
        task.exception()


def _text_card_node(stored: Mapping[str, Any], segments: tuple[dict[str, Any], ...]) -> dict[str, Any] | None:
    """文字单节点卡片载荷：`uin`/`name` 只来自源消息 sender，`content` 为源段原样。

    署名取值与卡片路径共用 `card_node_sender` / `card_node_name`；差异只在空名
    策略：`uin` 不可确认整条拒绝，显示名取不到按文字路径留空、由平台决定展示
    ——卡片路径才整条拒绝，不把该拒绝带进本路径。

    节点只设 `uin` / `name` / `content` 三个字段：不设置 `summary` / `news` /
    `prompt` / `source` 等预览类参数（卡片展示完全由平台决定）。
    """

    uin = card_node_sender(stored.get("sender"))
    if not uin:
        return None
    return {"type": "node", "data": {
        "uin": uin, "name": card_node_name(stored.get("sender")),
        "content": [dict(segment) for segment in segments],
    }}


@dataclass(slots=True)
class _Submission:
    """当前调用的提交证据与取消闸门；不持有任何正文。"""

    stopped: bool = False
    submitted: bool = False
    candidate_id: str = ""


class SnowLumaClient:
    """小执行接口：账号与材料只来自平台查询，发送按准备阶段判定的 kind 分派。"""

    def __init__(self, *, transport: OneBotTransport, clock: Clock, config_generation: int) -> None:
        self._transport = transport
        self._clock = clock
        self._config_generation = config_generation
        self._lock = asyncio.Lock()
        self._closed = False
        self._operations: set[asyncio.Future[Any]] = set()
        self._close_deadline: float | None = None
        self._close_complete = False

    async def _bounded(self, operation: Awaitable[T], deadline: float) -> T:
        """所有等待按注入时钟的剩余预算结束，不等待不合作的取消。"""
        call = asyncio.ensure_future(operation)
        self._operations.add(call)
        call.add_done_callback(self._operations.discard)
        timer = asyncio.create_task(self._clock.sleep(max(0.0, deadline - self._clock.now())))
        try:
            await asyncio.wait((call, timer), return_when=asyncio.FIRST_COMPLETED)
            if self._clock.now() >= deadline:
                raise TimeoutError
            return call.result()
        finally:
            call.cancel()
            timer.cancel()
            call.add_done_callback(_consume_exception)
            timer.add_done_callback(_consume_exception)

    async def prepare_source(self, prepared: PreparedForward) -> PreparedSource | PrepareRejected:
        """按账号→目标名册→同账号源记录只读准备；不会提交发送。"""
        if (self._closed or not isinstance(prepared, PreparedForward)
                or prepared.source.platform != "qq"
                or not all(verbatim_identifier(value) for value in (
                    prepared.source.account_id, prepared.source.group_id,
                    prepared.onebot_message_id, prepared.arguments.target_group_id))
                or prepared.onebot_message_id != prepared.arguments.message_id):
            return PrepareRejected("source_unconfirmed", "无法确认可信源对应，发送前拒绝")
        if prepared.arguments.target_group_id == prepared.source.group_id:
            return PrepareRejected("target_unavailable", "目标群不能是源群自身，发送前拒绝")
        try:
            return await self._bounded(self._prepare(prepared), self._clock.now() + ONEBOT_ACTION_TIMEOUT_SECONDS)
        except (Exception, asyncio.CancelledError):
            return PrepareRejected("source_query_unavailable", "可信源查询未完成，发送前拒绝")

    async def _prepare(self, prepared: PreparedForward) -> PreparedSource | PrepareRejected:
        async with self._lock:
            if self._closed:
                return PrepareRejected("closed", "直连已关闭，发送前拒绝")
            login = response_data(await self._transport.get_login_info())
            if login is None or canonical_message_id(login.get("user_id")) != prepared.source.account_id:
                return PrepareRejected("account_mismatch", "实际账号无法与可信宿主账号对应，发送前拒绝")
            groups = response_payload(await self._transport.get_group_list())
            if not isinstance(groups, list) or not any(
                isinstance(group, Mapping) and canonical_message_id(group.get("group_id")) == prepared.arguments.target_group_id
                for group in groups
            ):
                return PrepareRejected("target_unavailable", "目标群不在该账号可确认的群名册内，发送前拒绝")
            stored = response_data(await self._transport.get_msg(prepared.onebot_message_id))
            if stored is None:
                return PrepareRejected("source_not_found", "源消息缓存缺失或查询无数据，发送前拒绝")
            if (canonical_message_id(stored.get("message_id")) != prepared.onebot_message_id
                    or stored.get("message_type") != "group"
                    or canonical_message_id(stored.get("group_id")) != prepared.source.group_id):
                return PrepareRejected("source_mismatch", "源消息记录或群归属不一致，发送前拒绝")
            # 判定顺序：顶层唯一卡片 → forward（按卡片标识展开查询构造节点材料）；
            # 无卡片且含图片段 → image；否则 → text。
            raw_message = stored.get("message")
            top_level: list[Any] = raw_message if isinstance(raw_message, list) else []
            shape = classify_top_level_shape(top_level) if top_level else "plain"
            if shape == "mixed":
                return PrepareRejected("source_shape_unsupported", "顶层卡片与其它内容混排，整条拒绝")
            if shape == "forward":
                # 卡片链路：顶层唯一卡片段取平台卡片标识 → 只读展开查询 → 发送前
                # 类型检查 → 节点结构校验；材料 = 节点数组，投递时平台按目标群上下文
                # 重建卡片。
                card_id = forward_card_id(top_level[0])
                if not card_id:
                    return PrepareRejected("forward_expand_unavailable", "源卡片标识不可确认，无法展开查询，发送前拒绝")
                response = await self._transport.get_forward_msg(card_id)
                expanded = response_data(response)
                if expanded is None:
                    return PrepareRejected(
                        "forward_expand_unavailable",
                        "平台确认该卡片不存在或不可展开，发送前拒绝" if business_failure(response)
                        else "卡片展开查询无可用数据，发送前拒绝",
                    )
                # 发送前类型检查（判定口径单点定义于 type_check）：嵌套内层卡片在
                # 目标群客户端只能显示为降级空壳，命中即整条拒绝，不进入节点构造、
                # 不退化为递归展开、拍平或摘要。
                nested_reason = unsupported_card_content(expanded)
                if nested_reason:
                    return PrepareRejected(error=nested_reason, reason=NESTED_FORWARD_DETAIL)
                nodes = build_card_nodes(expanded)
                if isinstance(nodes, PrepareRejected):
                    return nodes
                return PreparedSource(SourceMaterial(kind="forward", segments=nodes),
                                      prepared.source.group_id, prepared.onebot_message_id,
                                      prepared.arguments.target_group_id, prepared.source.account_id,
                                      self._config_generation)
            built = build_source_material(top_level)
            if isinstance(built, PrepareRejected):
                return built
            if built.kind == "text":
                # 文字路径的内容段按源消息原样（含 at 文本化豁免）：同一校验口径
                # 只把提及渲染从「文本化」换成「原样」，节点署名只取源消息 sender。
                verbatim = build_source_material(top_level, verbatim_mentions=True)
                if isinstance(verbatim, PrepareRejected):
                    return verbatim
                node = _text_card_node(stored, verbatim.segments)
                if node is None:
                    return PrepareRejected("sender_unconfirmed", "源消息发送者标识不可确认，无法按源身份署名")
                built = SourceMaterial(kind="text", segments=(node,))
            return PreparedSource(built, prepared.source.group_id, prepared.onebot_message_id,
                                  prepared.arguments.target_group_id, prepared.source.account_id,
                                  self._config_generation)

    async def card_content_rejection(self, message_id: str) -> str | None:
        """只读预识别卡片内容：命中嵌套内层卡片返回 reason，未命中或查询失败返回 None。

        自动入口的只读缝隙（design D3）：与工具路径 `prepare_source` 卡片分支同一读取
        序列——`get_msg` 取同账号源消息记录、顶层唯一卡片段取平台卡片标识、
        `get_forward_msg` 按标识展开查询、交 `type_check` 单点判定。用 `message_id` 而非
        宿主载荷里的卡片标识：标识经平台验证，不信任宿主载荷。

        查询失败（信封非法、业务失败、超时、连接不可用）与「确认无嵌套」都返回
        `None`：自动入口只关心是否命中，失败即放行（照常给判断机会），工具路径本来就
        会在发送前独立复判，不会因预搜索放行而漏拒。取消不是失败：`asyncio.CancelledError`
        照常向上传播，不放行、不改写为 `None`（与 `auto_entry._card_presearch_rejection`
        的「取消照常传播」承诺一致）。本方法只读：不设置提交许可闸门、不提交任何发送
        动作、不持有源正文；查询与其它动作共用同一有界预算与单实例串行锁，查询失败不重试。
        """

        if self._closed or not verbatim_identifier(message_id):
            return None
        try:
            return await self._bounded(self._card_content_rejection(message_id),
                                       self._clock.now() + ONEBOT_ACTION_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None

    async def _card_content_rejection(self, message_id: str) -> str | None:
        async with self._lock:
            if self._closed:
                return None
            stored = response_data(await self._transport.get_msg(message_id))
            if stored is None:
                return None
            # 判定顺序与 _prepare 卡片分支同一口径：顶层唯一卡片 → forward（按卡片标识
            # 展开查询交类型检查）；非卡片、取不到标识或展开无数据一律放行。
            raw_message = stored.get("message")
            top_level: list[Any] = raw_message if isinstance(raw_message, list) else []
            if not top_level or classify_top_level_shape(top_level) != "forward":
                return None
            card_id = forward_card_id(top_level[0])
            if not card_id:
                return None
            expanded = response_data(await self._transport.get_forward_msg(card_id))
            if expanded is None:
                return None
            return unsupported_card_content(expanded) or None

    async def send_source(self, request: SourceSendRequest, *, before_submit: Callable[[], bool]) -> SourceSendResult:
        """按准备阶段判定的 `kind` 分派唯一固定群动作，无替代通路、不重试。"""
        prepared = request.prepared
        if prepared.kind == "forward":
            # 卡片材料已是准备阶段按展开查询构造的节点数组：出口只做深拷贝隔离，
            # 不重建、不改写；普通路径才走段材料的输出重建。
            return await self._send(prepared, before_submit,
                                    segments=tuple(deepcopy(node) for node in prepared.material.segments))
        checked = source_outbound_segments(prepared.material)
        if isinstance(checked, PrepareRejected):
            return SourceSendResult("failed", reason=checked.reason)
        return await self._send(prepared, before_submit, segments=checked)

    async def send_comment(self, request: CommentSendRequest, *, before_submit: Callable[[], bool]) -> SourceSendResult:
        """空白评论在任何查询前结束；正文只形成一个普通文本段（不走卡片节点）。"""
        if not request.comment.strip():
            return SourceSendResult("failed", reason="评论内容为空，未发送")
        return await self._send(request.prepared, before_submit, comment=request.comment)

    async def _send(self, prepared: PreparedSource, before_submit: Callable[[], bool],
                    segments: Sequence[Mapping[str, Any]] | None = None,
                    comment: str = "") -> SourceSendResult:
        state = _Submission()
        deadline = self._clock.now() + ONEBOT_ACTION_TIMEOUT_SECONDS
        try:
            return await self._bounded(self._execute_send(prepared, before_submit, state, deadline, segments, comment), deadline)
        except (Exception, asyncio.CancelledError):
            return SourceSendResult("unknown" if state.submitted else "failed", target_message_id=state.candidate_id, reason="发送工作未完成；已提交部分待核实，未提交部分停止")
        finally:
            state.stopped = True

    def _current(self, prepared: PreparedSource) -> bool:
        """HTTP 无连接代次：配置代次一致且客户端未关闭即当前有效。"""
        return not self._closed and prepared.config_generation == self._config_generation

    async def _execute_send(self, prepared: PreparedSource, before_submit: Callable[[], bool],
                            state: _Submission, deadline: float,
                            segments: Sequence[Mapping[str, Any]] | None, comment: str) -> SourceSendResult:
        async with self._lock:
            if not self._current(prepared) or state.stopped or self._clock.now() >= deadline:
                return SourceSendResult("failed", reason="配置已失效，未发送")
            login = response_data(await self._transport.get_login_info())
            if login is None or canonical_message_id(login.get("user_id")) != prepared.account_id:
                return SourceSendResult("failed", reason="实际账号无法与可信源账号对应，未发送")
            if comment:
                send = self._transport.send_msg
                params: dict[str, Any] = {
                    "target_group_id": prepared.target_group_id,
                    "message": [{"type": "text", "data": {"text": comment}}],
                }
            elif prepared.kind == "forward":
                if not segments:
                    return SourceSendResult("failed", reason="卡片发送材料缺失，未发送")
                # 单次卡片发送只提交目标群与展开查询构造的节点数组；
                # 不设 summary/news/prompt/source 预览参数，不接触 res_id/forward_id。
                send = self._transport.send_group_forward_msg
                params = {"target_group_id": prepared.target_group_id, "messages": segments}
            elif prepared.kind == "image":
                if segments is None:
                    return SourceSendResult("failed", reason="图片发送材料缺失，未发送")
                send = self._transport.send_msg
                params = {"target_group_id": prepared.target_group_id, "message": segments}
            elif segments is None:
                return SourceSendResult("failed", reason="文字发送材料缺失，未发送")
            else:
                # kind == "text"：材料已是单节点卡片数组（content 含 at 原样）。
                send = self._transport.send_forward_msg
                params = {"target_group_id": prepared.target_group_id, "messages": segments}

            def permit() -> bool:
                if (state.stopped or self._clock.now() >= deadline or not self._current(prepared)
                        or not before_submit()):
                    return False
                state.submitted = True
                return True

            try:
                response = await send(before_submit=permit, **params)
            except OneBotUnavailable as error:
                state.submitted = state.submitted or error.submitted
                return SourceSendResult("unknown" if state.submitted else "failed", reason="平台调用未完成，本次不重试")
            except (Exception, asyncio.CancelledError):
                return SourceSendResult("unknown", reason="平台调用结果待核实，本次不重试")
            if business_failure(response):
                return SourceSendResult("failed", reason="平台明确拒绝本次发送（业务失败），本次不重试")
            data = response_data(response)
            state.candidate_id = canonical_message_id(data.get("message_id")) if data is not None else ""
            if not state.candidate_id:
                return SourceSendResult("unknown", reason="平台响应缺少可核实的目标消息 ID，本次不重试")
            return await self._verify_target(prepared, state.candidate_id)

    async def _verify_target(self, prepared: PreparedSource, message_id: str) -> SourceSendResult:
        """只查本次候选ID；次数和总预算共同约束，四项证据与群消息形态缺一不可。

        目标消息 ID 可为负：`canonical_message_id` 按原值逐字比对，不假设符号。
        回查得到平台业务失败（目标不存在）属明确业务结果，按明确失败登记；查询超时
        或身份不符按待核实，绝不重发。
        """

        deadline = self._clock.now() + RECEIPT_VERIFY_TIMEOUT_SECONDS
        for _ in range(MAX_RECEIPT_QUERY_ATTEMPTS):
            if self._clock.now() >= deadline or not self._current(prepared):
                break
            try:
                response = await self._bounded(self._transport.get_msg(message_id), deadline)
            except Exception:
                response = None
            if business_failure(response):
                # 平台明确业务结果：目标消息不存在，按明确失败登记（保留已知候选 ID）。
                return SourceSendResult("failed", target_message_id=message_id,
                                        reason="平台确认目标消息不存在，按明确失败登记")
            data = response_data(response)
            if data is not None:
                sequence = data.get("message_seq")
                if (canonical_message_id(data.get("message_id")) == message_id
                        and data.get("message_type") == "group"
                        and canonical_message_id(data.get("group_id")) == prepared.target_group_id
                        and canonical_message_id(data.get("user_id")) == prepared.account_id
                        and type(sequence) is int and sequence > 0):
                    return SourceSendResult("sent", target_message_id=message_id)
                return SourceSendResult("unknown", target_message_id=message_id, reason="目标记录身份或平台群序列不符，按待核实处理")
            remaining = deadline - self._clock.now()
            if remaining <= 0:
                break
            await self._clock.sleep(min(RECEIPT_VERIFY_INTERVAL_SECONDS, remaining))
        return SourceSendResult("unknown", target_message_id=message_id, reason="有界只读查询未能核实目标回执，本次不重试")

    async def close(self) -> None:
        """仅关闭本客户端连接；上层可以用更小的共享卸载预算取消。"""
        self._closed = True
        if self._close_complete:
            return
        if self._close_deadline is None:
            self._close_deadline = self._clock.now() + ONEBOT_CLOSE_TIMEOUT_SECONDS
        for operation in tuple(self._operations):
            operation.cancel()
        if self._clock.now() >= self._close_deadline:
            raise OneBotUnavailable("close_timeout", submitted=False)
        try:
            await self._bounded(self._transport.close(), self._close_deadline)
        except (Exception, asyncio.CancelledError):
            raise OneBotUnavailable("close_incomplete", submitted=False) from None
        if any(not operation.done() for operation in self._operations):
            raise OneBotUnavailable("close_pending", submitted=False)
        self._close_complete = True
