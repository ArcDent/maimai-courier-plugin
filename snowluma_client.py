"""插件直连执行入口：可信准备与固定动作，服务端字段仍受核实附件 U1–U8 门槛约束。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from .forward_precheck import PreparedForward
from .send_material import build_source_material, source_outbound_segments
from .shared.clock import Clock
from .shared.identifiers import verbatim_identifier
from .shared.result import SourceSendResult
from .shared.send_steps import (
    PrepareRejected, PreparedSource, SourceSendRequest, CommentSendRequest,
    SEND_STAGE_SOURCE, SEND_STAGE_COMMENT,
)
from .snowluma_transport import (
    ONEBOT_ACTION_TIMEOUT_SECONDS, ONEBOT_CLOSE_TIMEOUT_SECONDS,
    OneBotTransport, OneBotUnavailable,
)

RECEIPT_VERIFY_TIMEOUT_SECONDS = 3.0
RECEIPT_VERIFY_INTERVAL_SECONDS = 0.5
MAX_RECEIPT_QUERY_ATTEMPTS = 8

T = TypeVar("T")


def response_payload(response: object) -> Any:
    """附件04的严格成功结构；事件、受理和契约外状态均不提供成功证据。"""
    if not isinstance(response, Mapping) or "post_type" in response or response.get("status") != "ok":
        return None
    if "retcode" in response and (type(response["retcode"]) is not int or response["retcode"] != 0):
        return None
    return response.get("data")


def response_data(response: object) -> Mapping[str, Any] | None:
    """只接受成功响应的映射数据。"""
    data = response_payload(response)
    return data if isinstance(data, Mapping) else None


def canonical_message_id(value: object) -> str:
    """只供比对的整数十进制或原文标识；不从格式推断平台身份。"""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    return verbatim_identifier(str(value))


def _consume_exception(task: asyncio.Future[Any]) -> None:
    """回收已取消工作异常，不回显原始载荷。"""
    if not task.cancelled():
        task.exception()


@dataclass(slots=True)
class _Submission:
    """当前调用的提交证据与取消闸门；不持有任何正文。"""

    stopped: bool = False
    submitted: bool = False
    candidate_id: str = ""


class SnowLumaClient:
    """小执行接口；账号取可信宿主会话，材料仅来自同连接查询。"""

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
            generation = self._transport.connection_generation
            if login is None or canonical_message_id(login.get("user_id")) != prepared.source.account_id:
                return PrepareRejected("account_mismatch", "实际账号无法与可信宿主账号对应，发送前拒绝")
            groups = response_payload(await self._transport.get_group_list(expected_generation=generation))
            if not isinstance(groups, list) or not any(
                isinstance(group, Mapping) and canonical_message_id(group.get("group_id")) == prepared.arguments.target_group_id
                for group in groups
            ):
                return PrepareRejected("target_unavailable", "目标群不在该账号可确认的群名册内，发送前拒绝")
            stored = response_data(await self._transport.get_msg(prepared.onebot_message_id, expected_generation=generation))
            if stored is None:
                return PrepareRejected("source_not_found", "源消息缓存缺失或查询无数据，发送前拒绝")
            if (canonical_message_id(stored.get("message_id")) != prepared.onebot_message_id
                    or stored.get("message_type") != "group"
                    or canonical_message_id(stored.get("group_id")) != prepared.source.group_id):
                return PrepareRejected("source_mismatch", "源消息记录或群归属不一致，发送前拒绝")
            material = build_source_material(stored.get("message"))
            if isinstance(material, PrepareRejected):
                return material
            return PreparedSource(material, prepared.source.group_id, prepared.onebot_message_id,
                                  prepared.arguments.target_group_id, prepared.source.account_id,
                                  generation, self._config_generation)

    async def send_source(self, request: SourceSendRequest, *, before_submit: Callable[[], bool]) -> SourceSendResult:
        """源材料只转成一次固定群动作，无替代通路。"""
        segments = source_outbound_segments(request.prepared.material)
        if isinstance(segments, PrepareRejected):
            return SourceSendResult("failed", reason=segments.reason)
        return await self._send(request.prepared, segments, before_submit, SEND_STAGE_SOURCE)

    async def send_comment(self, request: CommentSendRequest, *, before_submit: Callable[[], bool]) -> SourceSendResult:
        """空白评论在任何查询前结束；正文只形成一个普通文本段。"""
        if not request.comment.strip():
            return SourceSendResult("failed", reason="评论内容为空，未发送")
        return await self._send(request.prepared, ({"type": "text", "data": {"text": request.comment}},), before_submit, SEND_STAGE_COMMENT)

    async def _send(self, prepared: PreparedSource, segments: Sequence[Mapping[str, Any]],
                    before_submit: Callable[[], bool], stage: str) -> SourceSendResult:
        state = _Submission()
        deadline = self._clock.now() + ONEBOT_ACTION_TIMEOUT_SECONDS
        try:
            return await self._bounded(self._execute_send(prepared, segments, before_submit, stage, state, deadline), deadline)
        except (Exception, asyncio.CancelledError):
            return SourceSendResult("unknown" if state.submitted else "failed", target_message_id=state.candidate_id, reason="发送工作未完成；已提交部分待核实，未提交部分停止")
        finally:
            state.stopped = True

    def _current(self, prepared: PreparedSource) -> bool:
        return (not self._closed and prepared.config_generation == self._config_generation
                and prepared.connection_generation == self._transport.connection_generation)

    async def _execute_send(self, prepared: PreparedSource, segments: Sequence[Mapping[str, Any]],
                            before_submit: Callable[[], bool], stage: str, state: _Submission,
                            deadline: float) -> SourceSendResult:
        async with self._lock:
            if not self._current(prepared) or state.stopped or self._clock.now() >= deadline:
                return SourceSendResult("failed", reason="配置或连接已失效，未发送")
            login = response_data(await self._transport.get_login_info(expected_generation=prepared.connection_generation))
            if login is None or canonical_message_id(login.get("user_id")) != prepared.account_id:
                return SourceSendResult("failed", reason="实际账号无法与可信源账号对应，未发送")

            def permit() -> bool:
                if (state.stopped or self._clock.now() >= deadline or not self._current(prepared)
                        or not before_submit()):
                    return False
                state.submitted = True
                return True

            try:
                response = await self._transport.send_msg(target_group_id=prepared.target_group_id, message=segments,
                                               expected_generation=prepared.connection_generation,
                                               before_submit=permit, stage=stage)
            except OneBotUnavailable as error:
                state.submitted = state.submitted or error.submitted
                return SourceSendResult("unknown" if state.submitted else "failed", reason="平台调用未完成，本次不重试")
            except (Exception, asyncio.CancelledError):
                return SourceSendResult("unknown", reason="平台调用结果待核实，本次不重试")
            data = response_data(response)
            state.candidate_id = canonical_message_id(data.get("message_id")) if data is not None else ""
            if not state.candidate_id:
                return SourceSendResult("unknown", reason="平台响应缺少可核实的目标消息 ID，本次不重试")
            return await self._verify_target(prepared, state.candidate_id)

    async def _verify_target(self, prepared: PreparedSource, message_id: str) -> SourceSendResult:
        """只查本次候选ID；次数和总预算共同约束，四项证据与群消息形态缺一不可。"""
        deadline = self._clock.now() + RECEIPT_VERIFY_TIMEOUT_SECONDS
        for _ in range(MAX_RECEIPT_QUERY_ATTEMPTS):
            if self._clock.now() >= deadline or not self._current(prepared):
                break
            try:
                response = await self._bounded(self._transport.get_msg(message_id, expected_generation=prepared.connection_generation), deadline)
            except Exception:
                response = None
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
