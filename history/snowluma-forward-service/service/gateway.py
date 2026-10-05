"""SnowLuma 网关接缝：固定连接、账号就绪与两个发送接缝（design D2/D3/D5/D8）。

本模块是服务端与 SnowLuma 网关之间的**唯一**通道（第 4 组）：

- `connect()`：服务启动/重连入口，用 `get_login_info` 核对绑定账号；账号不符或
  无法确认时不进入可发送状态；观测到的账号变化使 `account_generation` 递增
  （第 5 组据此让未消费授权失效），绑定账号本身绝不自动切换；
- `prepare_source()`：源准备公开边界（4.2 起）；只从绑定账号的网关查询核对源归属，
  不接受客户端提交的正文、资源 ID 或媒体地址；
- `send_source()` / `send_comment()`：`SourceSendStep` / `CommentSendStep` 的真实
  实现；调用前可证明未发的错误返回 `failed`，平台调用之后的一切模糊结果返回
  `unknown`（待核实，绝不降级为可重试失败）。

本模块不导入 SDK、不读文件、不记录正文；错误原因只含定位与类别。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Final, Mapping, Sequence

from shared.clock import Clock
from shared.delivery import STATUS_FAILED, STATUS_SENT, STATUS_UNKNOWN
from shared.identifiers import verbatim_identifier
from shared.mentions import flatten_mention_text
from shared.result import SourceSendResult
from shared.send_steps import (
    SEND_STAGE_COMMENT,
    SEND_STAGE_SOURCE,
    CommentSendRequest,
    SourceMaterial,
    SourceSendRequest,
)

from .materials import (
    ERROR_INVALID_ID,
    ERROR_SOURCE_GROUP_MISMATCH,
    ERROR_SOURCE_NOT_FOUND,
    ERROR_SOURCE_NOT_GROUP,
    ERROR_SOURCE_QUERY_UNAVAILABLE,
    ERROR_TARGET_UNAVAILABLE,
    PrepareRejected,
    PreparedSource,
    build_source_material,
    source_outbound_segments,
)
from .onebot import OneBotTransport

ERROR_ACCOUNT_MISMATCH: Final[str] = "account_mismatch"
"""网关实际登录账号与服务端绑定账号不符（不自动切换账号）。"""

ERROR_ACCOUNT_UNCONFIRMED: Final[str] = "account_unconfirmed"
"""无法从网关响应确认登录账号（缺字段、非成功响应等）。"""

ERROR_CONNECTION_UNAVAILABLE: Final[str] = "connection_unavailable"
"""连接不可用：未取得回执，无法确认账号，也不允许发送。"""

GATEWAY_CALL_TIMEOUT_SECONDS: Final[float] = 10.0
"""单次网关调用的上界（秒，代码常量）：超时按结果不明处理，不做长等待。"""

RECEIPT_VERIFY_TIMEOUT_SECONDS: Final[float] = 3.0
"""目标消息身份核实的查询期上限（秒，代码常量；用注入时钟推进）。"""

RECEIPT_VERIFY_INTERVAL_SECONDS: Final[float] = 0.5
"""核实查询的重试间隔（秒，代码常量）：缓存短暂不可见时可续查。"""

MAX_RECEIPT_QUERY_ATTEMPTS: Final[int] = 8
"""单次回执核实的查询次数上限（代码常量）：查询必须有界。"""

RECEIPT_QUERY_MIN_TIMEOUT_SECONDS: Final[float] = 0.05
"""单次核实查询的下限超时（秒，代码常量）：给查询留出可完成的等待，避免空转。"""

MAX_NICKNAME_LENGTH: Final[int] = 64
"""账号昵称的长度上限：昵称属不可信显示文本，压平后限长。"""


@dataclass(frozen=True, slots=True)
class AccountReady:
    """账号就绪：网关实际登录账号与服务端绑定账号一致。"""

    account_id: str
    nickname: str = ""


@dataclass(frozen=True, slots=True)
class AccountUnavailable:
    """账号不可发送：带安全错误类别与可定位中文原因（不含凭证与正文）。"""

    error: str
    reason: str


def response_payload(response: object) -> Any:
    """成功响应的 `data` 面（可能是列表）：非成功响应或结构不符返回 `None`。

    成功面口径来自第 1 组核实（1.4）：只有 `okResponse({...})` 形态可视为成功；
    `status` 大小写不敏感（网关固定小写 `ok`），`retcode` 出现时必须是 0。
    """

    if not isinstance(response, Mapping):
        return None
    if str(response.get("status") or "").strip().lower() != "ok":
        return None
    retcode = response.get("retcode")
    if retcode is not None and (isinstance(retcode, bool) or retcode != 0):
        return None
    return response.get("data")


def response_data(response: object) -> Mapping[str, Any] | None:
    """成功响应的映射数据面：缺 `data` 或不是映射一律 `None`。"""

    data = response_payload(response)
    return data if isinstance(data, Mapping) else None


def canonical_identifier(value: object) -> str:
    """把网关返回的标识符归一成安全可引用的文本：整数转文本，字符串逐字校验。

    与 `canonical_message_id` 同一 `verbatim_identifier` 口径：不合格（空白、控制
    字符、尖括号/引号/反引号、超长）一律返回空串，不裁剪、不净化、不回显原值——
    `" 10001"` 不得被悄悄当成 `"10001"`（对不上真实标识）。只用于**比较**，请求
    字段本身不净化。
    """

    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return verbatim_identifier(str(value))
    if isinstance(value, str):
        return verbatim_identifier(value)
    return ""


def safe_nickname(value: object) -> str:
    """把网关昵称当不可信显示文本处理：压平控制字符与空白并限长。

    昵称不参与路由与比较，只用于展示与原因描述；压平口径与提及显示文本一致
    （`shared/mentions.flatten_mention_text`），避免错误原因里夹带控制字符或超长文本。
    """

    return flatten_mention_text(value)[:MAX_NICKNAME_LENGTH]


def canonical_message_id(value: object) -> str:
    """目标消息 ID 的安全形态：非空、限定长度、无空白/控制字符，否则空串。

    `sent=True` 但没有可引用的目标消息 ID 不构成成功证据（第 1 组 1.4）。
    """

    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return verbatim_identifier(str(value))
    if isinstance(value, str):
        return verbatim_identifier(value)
    return ""


def platform_failure_result(reason: str) -> SourceSendResult:
    """平台调用之后的统一模糊结果口径：一律待核实（`unknown`）。"""

    return SourceSendResult(status=STATUS_UNKNOWN, reason=reason)


def cancelled_platform_result() -> SourceSendResult:
    """平台调用阶段被取消的统一口径：可能已产生副作用，一律待核实。

    发送步骤**原样传播** `asyncio.CancelledError`（不吞取消，与插件侧发送步骤的既有
    约定一致）；编排方捕获取消后必须用本函数登记结果，不得报告「明确未发送」。
    """

    return platform_failure_result("平台调用被取消：可能已产生副作用，按待核实处理")


class GatewayTimeout(RuntimeError):
    """本次网关调用超出注入时钟的上界：结果不明，按待核实处理，不做长等待。"""


def _positive_sequence(value: object) -> bool:
    """可证明的平台群序列：正整数（布尔不是整数，1.4 核实的权威身份判据）。"""

    return isinstance(value, int) and not isinstance(value, bool) and value > 0


class SnowLumaGateway:
    """绑定账号的 SnowLuma 网关接缝：账号就绪、源准备与两个发送步骤。"""

    def __init__(
        self,
        *,
        transport: OneBotTransport,
        account_id: str,
        clock: Clock,
        call_timeout_seconds: float = GATEWAY_CALL_TIMEOUT_SECONDS,
    ) -> None:
        if not verbatim_identifier(account_id):
            raise ValueError("绑定账号必须是安全可引用的标识符（服务端受控配置提供）")
        self._transport = transport
        self._account_id = account_id
        self._clock = clock
        self._call_timeout = float(call_timeout_seconds)
        self._ready: AccountReady | None = None
        self._last_seen_account = ""
        self._account_generation = 0

    # ------------------------------------------------------------------ 账号就绪

    @property
    def bound_account_id(self) -> str:
        """服务端绑定账号（只来自受控配置，绝不自动切换）。"""

        return self._account_id

    @property
    def account_generation(self) -> int:
        """观测到的网关账号代次：账号变化即递增（第 5 组据此失效未消费授权）。"""

        return self._account_generation

    @property
    def ready(self) -> AccountReady | None:
        """当前是否处于可发送状态；`None` 表示尚未通过就绪检查。"""

        return self._ready

    async def connect(self) -> AccountReady | AccountUnavailable:
        """服务启动/重连入口：查询 `get_login_info` 并核对绑定账号。

        重连后再调用一次；账号变化会清空就绪状态并使代次递增，但不改变绑定账号、
        也不触碰任何已保存回执。
        """

        try:
            response = await self._bounded_call(
                self._transport.get_login_info(), self._call_timeout
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._ready = None
            return AccountUnavailable(
                ERROR_CONNECTION_UNAVAILABLE,
                f"无法查询网关登录账号（{type(error).__name__}），已关闭发送",
            )

        data = response_data(response)
        account = canonical_identifier(data.get("user_id") if data is not None else None)
        if not account:
            self._ready = None
            return AccountUnavailable(
                ERROR_ACCOUNT_UNCONFIRMED,
                "网关未返回可确认的登录账号（缺失或形态非法），已关闭发送（不进入可发送状态）",
            )
        if account != self._last_seen_account:
            self._account_generation += 1
            self._last_seen_account = account
        if account != self._account_id:
            self._ready = None
            return AccountUnavailable(
                ERROR_ACCOUNT_MISMATCH,
                f"网关实际登录账号 {account} 与绑定账号不符，已关闭发送（不自动切换账号）",
            )
        nickname = ""
        if data is not None:
            nickname = safe_nickname(data.get("nickname"))
        self._ready = AccountReady(account_id=account, nickname=nickname)
        return self._ready

    async def _send_failure(self) -> SourceSendResult | None:
        """发送前的**全量**就绪复验：每次重新查询账号，不就绪时给出「明确未发送」。

        返回 `None` 表示账号就绪、可以继续；返回结果表示本次未发送任何消息
        （`failed`）。不复用缓存的 `ready`：预检到发送之间账号可能已经变化，
        且绑定账号绝不自动切换（1.4/4.1 核实口径）。
        """

        outcome = await self.connect()
        if isinstance(outcome, AccountUnavailable):
            return SourceSendResult(
                status=STATUS_FAILED, reason=f"{outcome.reason}；本次未发送任何消息"
            )
        return None

    async def _bounded_call(
        self, operation: "Awaitable[Mapping[str, Any]]", timeout_seconds: float
    ) -> Mapping[str, Any]:
        """在**注入时钟**上为一次网关调用设定上界；超时取消调用并抛 `GatewayTimeout`。

        有界等待一律走注入时钟（与评论延迟、准备到期同一时间边界）：超时用注入
        时钟推进，测试无需真实长等待；取消上界内的调用不会吞掉外层任务取消
        （外层取消照常传播 `CancelledError`）。
        """

        call = asyncio.ensure_future(operation)
        timer = asyncio.ensure_future(self._clock.sleep(timeout_seconds))
        try:
            done, _ = await asyncio.wait({call, timer}, return_when=asyncio.FIRST_COMPLETED)
            if call in done:
                return await call
            raise GatewayTimeout(f"网关调用超过 {timeout_seconds:g} 秒上界，结果不明")
        finally:
            for pending in (call, timer):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(call, timer, return_exceptions=True)

    # ------------------------------------------------------------------ 源准备

    async def prepare_source(
        self,
        *,
        source_group_id: str,
        source_message_id: str,
        target_group_id: str,
    ) -> PreparedSource | PrepareRejected:
        """源准备公开边界：只校验、查源并派生材料，不发送、不落盘。

        顺序固定为「标识符 → 目标 → 账号 → 查源 → 归属 → 材料」：权限与目标问题在
        查询源消息**之前**拒绝，因此越权请求不会先取得源正文；客户端无法提交正文、
        资源 ID 或媒体地址（本方法的参数面只有三个受控标识符）。
        """

        source_group = verbatim_identifier(source_group_id)
        if not source_group:
            return PrepareRejected(ERROR_INVALID_ID, "源群号不是安全可引用的标识符，发送前拒绝")
        message_id = verbatim_identifier(source_message_id)
        if not message_id:
            return PrepareRejected(ERROR_INVALID_ID, "源消息 ID 不是安全可引用的标识符，发送前拒绝")
        target_group = verbatim_identifier(target_group_id)
        if not target_group:
            return PrepareRejected(ERROR_INVALID_ID, "目标群号不是安全可引用的标识符，发送前拒绝")
        if target_group == source_group:
            return PrepareRejected(ERROR_TARGET_UNAVAILABLE, "目标群不能是源群自身，发送前拒绝")

        ready = await self.connect()
        if isinstance(ready, AccountUnavailable):
            return PrepareRejected(ready.error, f"{ready.reason}（未查询源正文）")

        target_rejection = await self._target_available(target_group)
        if target_rejection is not None:
            return target_rejection

        stored = await self._query_source(message_id)
        if isinstance(stored, PrepareRejected):
            return stored
        message_type = str(stored.get("message_type") or "").strip()
        if message_type != "group":
            return PrepareRejected(
                ERROR_SOURCE_NOT_GROUP, "源消息不是群聊消息（私聊源不转发），发送前拒绝"
            )
        actual_group = canonical_identifier(stored.get("group_id"))
        if actual_group != source_group:
            return PrepareRejected(
                ERROR_SOURCE_GROUP_MISMATCH, "源消息实际属于另一群，发送前拒绝（不把查询内容返回）"
            )
        material = build_source_material(stored.get("message"))
        if isinstance(material, PrepareRejected):
            return material
        return PreparedSource(
            material=material,
            source_group_id=source_group,
            source_message_id=message_id,
            target_group_id=target_group,
        )

    async def _target_available(self, target_group_id: str) -> PrepareRejected | None:
        """核对目标是绑定账号可见的有效群（1.2 核实的群名册，而非群信息兜底对象）。

        1.2 已核实：群不在名册时 `get_group_info` 仍以兜底对象成功返回、`group_name`
        为空串，因此「目标可用」只能靠 `get_group_list` 的成员资格确认。
        """

        try:
            response = await self._bounded_call(
                self._transport.get_group_list(), self._call_timeout
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return PrepareRejected(
                ERROR_TARGET_UNAVAILABLE,
                f"无法核对目标群（{type(error).__name__}），发送前拒绝",
            )
        payload = response_payload(response)
        if not isinstance(payload, list):
            return PrepareRejected(
                ERROR_TARGET_UNAVAILABLE, "网关未返回可核对的群名册，无法确认目标群，发送前拒绝"
            )
        visible = {
            canonical_identifier(entry.get("group_id") if isinstance(entry, Mapping) else entry)
            for entry in payload
        }
        if target_group_id not in visible:
            return PrepareRejected(
                ERROR_TARGET_UNAVAILABLE,
                "目标群不在绑定账号可见的群名册中，无法确认为有效目标，发送前拒绝",
            )
        return None

    async def _query_source(self, message_id: str) -> "Mapping[str, Any] | PrepareRejected":
        """按源消息 ID 只读查询网关存储副本；缓存缺失与查询不可用分开报告。"""

        try:
            response = await self._bounded_call(
                self._transport.get_msg(message_id), self._call_timeout
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return PrepareRejected(
                ERROR_SOURCE_QUERY_UNAVAILABLE,
                f"源消息查询不可用（{type(error).__name__}），发送前拒绝",
            )
        data = response_data(response)
        if data is None:
            return PrepareRejected(
                ERROR_SOURCE_NOT_FOUND,
                "源消息在网关缓存中不存在或查询未取得数据，发送前拒绝（不重建、不猜造）",
            )
        return data

    # ------------------------------------------------------------------ 发送接缝

    async def send_source(self, request: SourceSendRequest) -> SourceSendResult:
        """源发送步骤：材料不可发送或账号未就绪时明确未发送，否则唯一一次平台调用。"""

        segments = source_outbound_segments(request.material)
        if isinstance(segments, PrepareRejected):
            return SourceSendResult(
                status=STATUS_FAILED, reason=f"{segments.reason}；本次未发送任何消息"
            )
        failure = await self._send_failure()
        if failure is not None:
            return failure
        return await self._platform_send(
            request.target_group_id, segments, stage=SEND_STAGE_SOURCE
        )

    async def send_comment(self, request: CommentSendRequest) -> SourceSendResult:
        """评论发送步骤：评论文本只成为单个普通文字段（不解析成段或命令）。

        空白评论在**任何网关调用之前**拒绝（连只读查询都不发生）：评论缺省/空/全
        空白不等待也不发送（design D4、cross-group-forward「可选评论固定延迟」）。
        """

        if not request.comment.strip():
            return SourceSendResult(status=STATUS_FAILED, reason="评论内容为空，未发送")
        failure = await self._send_failure()
        if failure is not None:
            return failure
        return await self._platform_send(
            request.target_group_id,
            ({"type": "text", "data": {"text": request.comment}},),
            stage=SEND_STAGE_COMMENT,
        )

    async def _platform_send(
        self,
        target_group_id: str,
        message: "Sequence[Mapping[str, Any]]",
        *,
        stage: str = "",
    ) -> SourceSendResult:
        """唯一一次平台调用：成功面只有可引用的目标消息 ID，其余一律待核实。

        `stage` 是控制面阶段提示（源/评论），只透传给传输接缝供可控测试设施区分
        两个发送入口；它是**非协议必需字段**（调用归属/可观测性提示，传输实现可
        忽略），线路参数与调用次数不变。
        """

        try:
            response = await self._bounded_call(
                self._transport.send_msg(
                    target_group_id=target_group_id, message=message, stage=stage
                ),
                self._call_timeout,
            )
        except asyncio.CancelledError:
            raise
        except GatewayTimeout:
            return platform_failure_result(
                "平台调用超出注入时钟上界：可能已产生副作用，按待核实处理"
            )
        except Exception as error:
            return platform_failure_result(
                f"平台调用未取得回执（{type(error).__name__}），本次不重试，按待核实处理"
            )
        data = response_data(response)
        if data is None:
            return platform_failure_result("平台未返回成功回执，可能已产生副作用，按待核实处理")
        message_id = canonical_message_id(data.get("message_id"))
        if not message_id:
            return platform_failure_result(
                "平台成功响应缺少可引用的目标消息 ID，无法核实，按待核实处理"
            )
        return await self._verify_target_message(message_id, target_group_id)

    async def _verify_target_message(
        self, message_id: str, target_group_id: str
    ) -> SourceSendResult:
        """用只读查询核实目标群、绑定发送者与可证明的平台消息身份。

        - 命中目标群 + 绑定账号 + **返回记录就是被查询的那条消息** + 正整数群序列
          （1.4 核实的权威身份）才确认成功；
        - 存储副本存在但身份不符、返回的是另一条记录、缺少可用群序列（本地不透明
          ID）→ 立即待核实，不做无谓重试；
        - 缓存暂时不可见或查询暂不可用 → 在有界查询期内重试，超期按待核实；
        - 全程只读：绝不通过再次发送确认。
        """

        deadline = self._clock.now() + RECEIPT_VERIFY_TIMEOUT_SECONDS
        attempts = 0
        while attempts < MAX_RECEIPT_QUERY_ATTEMPTS:
            attempts += 1
            remaining = max(
                RECEIPT_QUERY_MIN_TIMEOUT_SECONDS,
                min(self._call_timeout, deadline - self._clock.now()),
            )
            try:
                response = await self._bounded_call(
                    self._transport.get_msg(message_id), remaining
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                response = None
            verdict = self._identity_verdict(response, message_id, target_group_id)
            if verdict is not None:
                return verdict
            if self._clock.now() >= deadline:
                break
            await self._clock.sleep(RECEIPT_VERIFY_INTERVAL_SECONDS)
        return platform_failure_result(
            f"有界查询期内未能核实目标消息身份（查询 {attempts} 次），按待核实处理"
        )

    def _identity_verdict(
        self, response: object, message_id: str, target_group_id: str
    ) -> SourceSendResult | None:
        """身份核实判定：`None` 表示缓存暂不可见（可在有界期内重试）。"""

        data = response_data(response)
        if data is None:
            return None
        group = canonical_identifier(data.get("group_id"))
        sender = canonical_identifier(data.get("user_id"))
        if group and group != target_group_id:
            return platform_failure_result(
                "核实查询返回的消息属于另一目标群，目标消息身份不可核实，按待核实处理"
            )
        if sender and sender != self._account_id:
            return platform_failure_result(
                "核实查询返回的发送者不是绑定账号，目标消息身份不可核实，按待核实处理"
            )
        if canonical_message_id(data.get("message_id")) != message_id:
            return platform_failure_result(
                "核实查询返回的记录不是被查询的目标消息，目标消息身份不可核实，按待核实处理"
            )
        sequence = data.get("message_seq")
        if group == target_group_id and sender == self._account_id and _positive_sequence(sequence):
            return SourceSendResult(status=STATUS_SENT, target_message_id=message_id)
        return platform_failure_result(
            "核实查询未返回可证明的平台消息身份（缺群序列或本地不透明 ID），按待核实处理"
        )
