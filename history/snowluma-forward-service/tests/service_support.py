"""测试支持：服务端的可控 HTTP、OneBot 与时钟设施（2.4 起用，2.6 补齐）。

公开设施：

- `ServiceHarness`：把 `service.http_api.ServiceApp`、不发送后端替身、阶段闸门、
  模拟 OneBot、账本替身与注入时钟装配在一起；
- `InMemoryHttpClient`：在**内存里**驱动一次 HTTP 请求（无网络、无真实等待）；
- `FakeOneBot`：模拟 OneBot 服务（`get_login_info` / `get_msg` / `get_group_list` /
  `send_msg`），记录查询与发送调用次数与目标，可注入断线、错误响应与异常；
- `group_source_message` / `store_source_message`：按宿主存储副本形态合成群源消息
  （第 4 组源准备与发送用例共用），全部为合成值，不对应任何真实消息；
- `StageGates`：五个阶段（准备 / 平台提交 / 回执 / 落账 / 评论）的暂停与放行，
  超时用注入时钟推进；
- `open_state_ledger` / `close_ledger`：第 3 组起按公开打开入口装配真实服务账本
  （`tmp_path` 合成目录，不触碰真实运行状态）；
- `AuthorizedService`（第 5 组起）：真实协调器 + 真实账本 + 模拟 OneBot + 内存 HTTP，
  全部经真实路由与认证中间件驱动，可模拟服务重启与换账号；
- `FakeClock`：直接复用 `tests.support.FakeClock`（不新增第二份时钟替身）。

默认传输只在本进程内存里（`memory://onebot`），绝不连接真实群或外部地址。
"""

from __future__ import annotations

import asyncio
import json
import secrets
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, cast
from urllib.parse import urlsplit

from remote_client import TransportReply
from service.backends import NonSendingBackends
from service.coordinator import ServiceCoordinator
from service.gateway import AccountUnavailable, SnowLumaGateway
from service.http_api import HttpRequest, HttpResponse, ServiceApp
from service.ledger_migration import FreshOutcome, initialize_fresh
from service.ledger_store import (
    DELIVERY_TABLE,
    LEGACY_LEDGER_FILE_NAME,
    SERVICE_LEDGER_FILE_NAME,
    ServiceLedger,
    StateDatabase,
    open_state_database,
)
from service.onebot import OneBotUnavailable
from service.sessions import SessionRegistry
from shared.delivery import (
    DeliveryKey,
    LedgerStatus,
    LedgerUnavailableError,
    SendStatus,
)
from shared.ledger import (
    AttemptSnapshot,
    CommentReservation,
    DeliverySnapshot,
    RecoveryReport,
    SourceReservation,
)
from shared.protocol import CommentStatus, delivery_id_for
from shared.send_steps import SEND_STAGE_COMMENT, SEND_STAGE_SOURCE

from tests.cross_process_support import authoritative_message_id
from tests.support import FakeClock

ALLOW_ALL_SUBJECT = "client-test"
"""认证替身返回的主体标识（真实主体来自凭证绑定，见 2.5）。"""

IN_MEMORY_ENDPOINT = "memory://onebot"
"""模拟 OneBot 的内存地址：设施默认不连接任何真实地址。"""

STAGE_PREPARE = "prepare"
"""准备阶段（只校验、查源与登记有界准备，不发送）。"""

STAGE_PLATFORM_COMMIT = "platform_commit"
"""平台提交阶段（进入唯一一次平台 RPC 之前）。"""

STAGE_RECEIPT = "receipt"
"""回执阶段（平台调用之后、取得回执之前）。"""

STAGE_LEDGER_WRITE = "ledger_write"
"""落账阶段（账本占位或终态写入之前）。"""

STAGE_COMMENT = "comment"
"""评论阶段（评论发送之前）。"""

STAGE_NAMES: tuple[str, ...] = (
    STAGE_PREPARE,
    STAGE_PLATFORM_COMMIT,
    STAGE_RECEIPT,
    STAGE_LEDGER_WRITE,
    STAGE_COMMENT,
)
"""可分别暂停的阶段名（2.6 验收列出的五个阶段）。"""

SYNTHETIC_KEY = DeliveryKey(
    account_id="10000",
    source_group_id="10001",
    source_message_id="m-source-1",
    target_group_id="20002",
)
"""设施自检用合成投递身份（全部为合成值，不对应任何真实群或消息）。"""

BOUND_ACCOUNT_ID = "10000"
"""第 4 组用例的绑定账号（模拟网关默认登录账号的十进制文本）。"""

SOURCE_GROUP_ID = "10001"
"""第 4 组用例的源群号（合成值）。"""

TARGET_GROUP_ID = "20002"
"""第 4 组用例的目标群号（模拟网关名册默认包含该群）。"""

SOURCE_MESSAGE_ID = "m-source-1"
"""第 4 组用例的源消息 ID（合成值）。"""


def _known_stage(stage: str) -> None:
    """阶段名拼错时立即失败，避免用例静默地什么都没暂停。"""

    if stage not in STAGE_NAMES:
        raise ValueError(f"未知阶段 {stage!r}；可用阶段：{', '.join(STAGE_NAMES)}")


def _stage_for(stage: str) -> str:
    """把发送阶段提示映射到可控闸门名。

    - 空串是 `OneBotTransport.send_msg` 的**协议缺省**（`stage: str = ""`）：评论发送
      总会显式带 `SEND_STAGE_COMMENT`，因此空串按源发送处理、停在「平台提交」闸门；
    - 控制面提示（`shared.send_steps.SEND_STAGE_SOURCE` / `SEND_STAGE_COMMENT`）来自
      `SnowLumaGateway._platform_send`；映射后源发送停在「平台提交」、评论发送停在
      「评论」闸门；
    - 闸门名（旧口径的直接调用）原样保留；
    - 其余取值立即报错，不静默当成源发送。
    """

    if stage in ("", SEND_STAGE_SOURCE, STAGE_PLATFORM_COMMIT):
        return STAGE_PLATFORM_COMMIT
    if stage == SEND_STAGE_COMMENT:  # 与 STAGE_COMMENT 同值：一个分支即覆盖两种口径
        return STAGE_COMMENT
    _known_stage(stage)
    return stage


def open_state_ledger(state_dir: Path, *, clock: Any = None) -> ServiceLedger:
    """按公开打开入口装配真实服务账本（必要时建库；不触碰真实运行状态）。"""

    database = open_state_database(state_dir, writable=True, create=True)
    assert isinstance(database, StateDatabase), f"服务账本应可打开: {database!r}"
    return ServiceLedger(database.connection, clock or FakeClock(), lock=database.lock)


@dataclass(frozen=True, slots=True)
class LegacyRecordSpec:
    """旧账本合成记录（第 3.7–3.9 组共用）：状态 + 可选的源目标 ID 与评论结果。"""

    key: DeliveryKey
    status: str = "sent"
    target_message_id: str = ""
    comment_status: str = ""
    comment_message_id: str = ""


def build_legacy_snapshot(
    legacy_dir: Path, snapshot: Path, specs: Sequence[LegacyRecordSpec]
) -> Path:
    """用**旧公共接口**合成旧账本，再复制成只读快照（原库保持不动）。

    只调用 `delivery_ledger` 的公开方法（`open_ledger` / `reserve` / `mark_*` /
    `record_comment`），不复制旧 SQL：合成的旧库因此具备旧实现的全部行为事实。
    """

    from tests.support import open_test_ledger

    legacy_dir.mkdir(parents=True, exist_ok=True)
    ledger = open_test_ledger(legacy_dir)
    try:
        for spec in specs:
            assert ledger.reserve(spec.key).granted is True, spec
            if spec.status == "sent":
                assert ledger.mark_sent(spec.key, spec.target_message_id) is True, spec
                if spec.comment_status:
                    comment_status = spec.comment_status
                    assert comment_status in ("sent", "failed", "unknown"), spec
                    assert (
                        ledger.record_comment(
                            spec.key,
                            status=cast(LedgerStatus, comment_status),
                            message_id=spec.comment_message_id,
                        )
                        is True
                    ), spec
            elif spec.status == "unknown":
                assert ledger.mark_unknown(spec.key, "旧库中的待核实记录") is True, spec
            elif spec.status == "failed":
                assert ledger.mark_failed(spec.key, "旧库中明确未发送") is True, spec
            elif spec.status != "sending":
                raise ValueError(f"未知的旧库合成状态：{spec.status!r}")
    finally:
        ledger.close()
    shutil.copyfile(legacy_dir / LEGACY_LEDGER_FILE_NAME, snapshot)
    return snapshot


def close_ledger(ledger: ServiceLedger) -> None:
    """同步关闭账本（协议方法 async；测试用独立事件循环驱动）。"""

    asyncio.run(ledger.close())


def state_ledger_rows(state_dir: Path, *, message_id: str = "") -> list[tuple[str, str, str]]:
    """用**独立连接**观察投递表落盘事实（不经账本对象、不依赖其私有结构）。"""

    connection = sqlite3.connect(
        str(state_dir / SERVICE_LEDGER_FILE_NAME), timeout=5.0, isolation_level=None
    )
    try:
        sql = f"SELECT source_message_id, status, target_message_id FROM {DELIVERY_TABLE}"
        parameters: tuple[str, ...] = ()
        if message_id:
            sql += " WHERE source_message_id = ?"
            parameters = (message_id,)
        return [
            (str(row[0]), str(row[1]), str(row[2]))
            for row in connection.execute(sql, parameters)
        ]
    finally:
        connection.close()


class StageGates:
    """阶段闸门：默认放行；`pause(stage)` 后进入该阶段的调用会停在入口直到 `release`。

    被暂停阶段的调用停在闸门时，任务必须在事件循环里让出（不阻塞循环）；放行后继续。
    `arrivals` 记录每个阶段被走到多少次，供用例等待「已到达」再决定放行或注入故障。
    """

    def __init__(self) -> None:
        self._paused: set[str] = set()
        self._events: dict[str, asyncio.Event] = {}
        self.arrivals: dict[str, int] = {}

    def pause(self, stage: str) -> None:
        """关闭某阶段的闸门（此后到达该阶段的调用会等待）。"""

        _known_stage(stage)
        self._paused.add(stage)
        self._events.setdefault(stage, asyncio.Event()).clear()

    def release(self, stage: str) -> None:
        """放行某阶段（唤醒当前等待者，并让后续到达直接通过）。"""

        _known_stage(stage)
        self._paused.discard(stage)
        self._events.setdefault(stage, asyncio.Event()).set()

    def is_paused(self, stage: str) -> bool:
        _known_stage(stage)
        return stage in self._paused

    async def arrive(self, stage: str) -> None:
        """到达某阶段：登记一次到达；闸门关闭时等待放行。"""

        _known_stage(stage)
        self.arrivals[stage] = self.arrivals.get(stage, 0) + 1
        while stage in self._paused:
            await self._events.setdefault(stage, asyncio.Event()).wait()

    async def wait_for(self, stage: str, *, clock: Any, timeout_seconds: float) -> bool:
        """等待某阶段**再被到达一次**；超时按注入时钟推进（不真实等待），返回是否等到。"""

        _known_stage(stage)
        target = self.arrivals.get(stage, 0) + 1

        async def _arrival() -> None:
            while self.arrivals.get(stage, 0) < target:
                await asyncio.sleep(0)

        waiter = asyncio.ensure_future(_arrival())
        timer = asyncio.ensure_future(clock.sleep(timeout_seconds))
        try:
            done, _ = await asyncio.wait({waiter, timer}, return_when=asyncio.FIRST_COMPLETED)
            return waiter in done
        finally:
            for task in (waiter, timer):
                if not task.done():
                    task.cancel()
            await asyncio.gather(waiter, timer, return_exceptions=True)


async def pump(predicate: Callable[[], bool], *, ticks: int = 400) -> None:
    """让出事件循环直到条件成立（不真实等待）；需要推进时钟的用例自行 `clock.advance`。"""

    for _ in range(ticks):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("等待条件超时（测试设施 pump）")


@dataclass(frozen=True, slots=True)
class FakeOneBotCall:
    """一次模拟网关调用记录：动作、目标（群号或消息 ID）与细节。"""

    operation: str
    target: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class FakeOneBot:
    """模拟 OneBot 服务：形状对齐第 1 组核实的网关返回，可暂停、可注入故障。

    - `send_msg` 依次经过「平台提交」与「回执」两个阶段闸门（评论发送用
      `stage=STAGE_COMMENT`，只经过评论闸门）；
    - `authoritative_receipts=False` 时只回本地不透明 ID，`include_message_id=False`
      时回缺失 ID——供第 4.7 组的回执核实用例注入；
    - 成功发送的消息会写入 `messages`（自发消息存储副本形态），供 `get_msg` 复核。
    """

    endpoint: str = IN_MEMORY_ENDPOINT
    account_id: int = 10000
    nickname: str = "搬运喵"
    gates: StageGates = field(default_factory=StageGates)
    authoritative_receipts: bool = True
    include_message_id: bool = True
    groups: tuple[str, ...] = ("20002",)
    messages: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    calls: list[FakeOneBotCall] = field(default_factory=list)
    send_calls: list[FakeOneBotCall] = field(default_factory=list)
    query_counts: dict[str, int] = field(default_factory=dict)
    send_count: int = 0
    _failures: dict[str, list[Mapping[str, Any]]] = field(default_factory=dict, repr=False)
    _permanent_failures: dict[str, Mapping[str, Any]] = field(default_factory=dict, repr=False)
    _errors: dict[str, list[BaseException]] = field(default_factory=dict, repr=False)
    _connected: bool = True

    @property
    def connected(self) -> bool:
        """当前是否处于连接状态（断线由 `disconnect()` 注入）。"""

        return self._connected

    def fail_next(self, operation: str, response: Mapping[str, Any]) -> None:
        """让下一次该动作返回注入的错误响应（调用仍被记录，不自动重试）。"""

        self._failures.setdefault(operation, []).append(dict(response))

    def fail_always(self, operation: str, response: Mapping[str, Any]) -> None:
        """让该动作在此后**每次**调用都返回同一错误响应（模拟持续不可用）。"""

        self._permanent_failures[operation] = dict(response)

    def raise_next(self, operation: str, error: BaseException) -> None:
        """让下一次该动作抛出注入异常（断线、取消、超时等）。"""

        self._errors.setdefault(operation, []).append(error)

    def disconnect(self) -> None:
        """注入断线：此后所有动作明确不可用。"""

        self._connected = False

    def reconnect(self) -> None:
        """恢复连接（不回放断线期间的调用）。"""

        self._connected = True

    async def get_login_info(self) -> Mapping[str, Any]:
        """只读账号查询：返回绑定账号与昵称（第 1 组 1.2 核实形状）。"""

        self._require_connected()
        self._raise_pending("get_login_info")
        self._record("get_login_info")
        injected = self._take_failure("get_login_info")
        if injected is not None:
            return injected
        return {
            "status": "ok",
            "retcode": 0,
            "data": {"user_id": self.account_id, "nickname": self.nickname},
        }

    async def get_msg(self, message_id: str) -> Mapping[str, Any]:
        """只读消息查询：缓存缺失时返回明确失败（不伪造内容）。"""

        self._require_connected()
        self._raise_pending("get_msg")
        self._record("get_msg", target=message_id)
        injected = self._take_failure("get_msg")
        if injected is not None:
            return injected
        stored = self.messages.get(message_id)
        if stored is None:
            return {"status": "failed", "retcode": 100, "message": "message not found"}
        return {"status": "ok", "retcode": 0, "data": dict(stored)}

    async def get_group_list(self) -> Mapping[str, Any]:
        """只读群列表查询：返回绑定账号当前可见的群（第 1 组 1.2 核实形状）。

        第 1 组已核实：群不在名册时 `get_group_info` 仍以兜底对象返回、`group_name`
        为空串，因此「目标可用」只能靠名册成员资格确认，不能靠群信息兜底对象。
        """

        self._require_connected()
        self._raise_pending("get_group_list")
        self._record("get_group_list")
        injected = self._take_failure("get_group_list")
        if injected is not None:
            return injected
        return {
            "status": "ok",
            "retcode": 0,
            "data": [{"group_id": group, "group_name": f"合成群 {group}"} for group in self.groups],
        }

    async def send_msg(
        self,
        *,
        target_group_id: str,
        message: Sequence[Mapping[str, Any]],
        stage: str = "",
    ) -> Mapping[str, Any]:
        """发送消息：进入平台调用即记录，再返回回执或注入的错误响应/异常。

        记录点放在平台提交闸门之后、注入响应/异常之前：暂停在「平台提交」表示尚未
        进入平台 RPC（不计一次发送），而注入的失败响应或异常都代表**已经尝试**过
        一次平台动作（供「不得换通路重发」用例断言）。

        `stage` 的缺省是**协议缺省**（`OneBotTransport.send_msg` 的 `""`，按源发送
        处理）；也可接受闸门名（`platform_commit` / `comment`，直接调用时使用）与
        控制面提示（`source` / `comment`，`SnowLumaGateway` 透传）——提示映射到对应
        闸门，使源/评论两个发送接缝在**同一**模拟网关上可区分、可分别暂停。
        """

        stage = _stage_for(stage)
        self._require_connected()
        if stage == STAGE_COMMENT:
            # 评论发送同时经过评论闸门与平台提交闸门：前者用于区分两个发送接缝，
            # 后者沿用「进入平台 RPC 之前」的既有语义（旧用例仍可在此暂停）。
            await self.gates.arrive(STAGE_COMMENT)
            await self.gates.arrive(STAGE_PLATFORM_COMMIT)
        else:
            await self.gates.arrive(stage)
        call = FakeOneBotCall(
            operation="send_msg",
            target=target_group_id,
            detail={"message": [dict(segment) for segment in message], "stage": stage},
        )
        self.calls.append(call)
        self.send_calls.append(call)
        self.send_count += 1

        self._raise_pending("send_msg")
        injected = self._take_failure("send_msg")
        if injected is not None:
            return injected

        if stage != STAGE_COMMENT:
            await self.gates.arrive(STAGE_RECEIPT)
        return self._receipt(target_group_id, message)

    def _receipt(self, target_group_id: str, message: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """构造成功回执；按用例开关给出权威 ID / 本地不透明 ID / 缺失 ID。

        本地不透明 ID（`authoritative_receipts=False`）对应的自发消息副本没有可核实
        的群序列（`message_seq=0`），对齐第 1 组 1.4 核实的 `finalizeSend` 行为：
        仅当群序列权威时才有可核实 ID，否则回退本地不透明 ID。
        """

        if not self.include_message_id:
            return {"status": "ok", "retcode": 0, "data": {}}
        sequence = self.send_count
        if self.authoritative_receipts:
            message_id: Any = authoritative_message_id(sequence, target_group_id)
            stored_sequence = sequence
        else:
            message_id = f"local-{sequence}"
            stored_sequence = 0
        self.messages[str(message_id)] = {
            "message_type": "group",
            "message_id": message_id,
            "message_seq": stored_sequence,
            "group_id": target_group_id,
            "user_id": self.account_id,
            "sender": {"user_id": self.account_id},
            "message": [dict(segment) for segment in message],
        }
        return {
            "status": "ok",
            "retcode": 0,
            "data": {"message_id": message_id, "message_seq": stored_sequence},
        }

    def _record(self, operation: str, *, target: str = "", detail: Mapping[str, Any] | None = None) -> FakeOneBotCall:
        call = FakeOneBotCall(operation=operation, target=target, detail=dict(detail or {}))
        self.calls.append(call)
        if operation != "send_msg":
            self.query_counts[operation] = self.query_counts.get(operation, 0) + 1
        return call

    def _take_failure(self, operation: str) -> Mapping[str, Any] | None:
        pending = self._failures.get(operation)
        if pending:
            return pending.pop(0)
        return self._permanent_failures.get(operation)

    def _raise_pending(self, operation: str) -> None:
        pending = self._errors.get(operation)
        if pending:
            raise pending.pop(0)

    def _require_connected(self) -> None:
        if not self._connected:
            raise OneBotUnavailable("模拟 OneBot 断线：本次未取得回执，禁止换通路重发")


def group_source_message(
    *,
    message_id: str = "m-source-1",
    group_id: str = "10001",
    user_id: str = "34567",
    message: Sequence[Mapping[str, Any]] | None = None,
    message_type: str = "group",
    message_seq: int = 0,
) -> dict[str, Any]:
    """合成一条 `get_msg` 返回的宿主存储副本（群事件形态，第 1 组 1.2 核实字段）。

    `get_msg` 返回首次收到时的存储副本：群事件含 `message_type` / `message_seq` /
    `group_id` / `user_id` / `message` 段数组，不含 `post_type` / `self_id`。
    """

    return {
        "message_type": message_type,
        "message_id": message_id,
        "message_seq": message_seq,
        "group_id": group_id,
        "user_id": user_id,
        "sender": {"user_id": user_id},
        "message": [dict(segment) for segment in (message if message is not None else ())],
    }


def store_source_message(onebot: FakeOneBot, **kwargs: Any) -> dict[str, Any]:
    """把合成的宿主存储副本写入模拟网关缓存，返回该副本供用例引用。"""

    stored = group_source_message(**kwargs)
    onebot.messages[str(stored["message_id"])] = stored
    return stored


class StubLedgerRepository:
    """最小内存账本替身：只回答固定结果并记录调用（真实实现在第 3 组）。

    它**不**实现持久状态机：本组只需要「落账」阶段可暂停、可注入存储故障。
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._deliveries: dict[str, DeliverySnapshot] = {}

    async def identity(self) -> str:
        self.calls.append("identity")
        return "stub-ledger"

    async def prepare_attempt(
        self,
        key: DeliveryKey,
        *,
        attempt_id: str,
        request_id: str,
        session_id: str,
        policy_generation: int,
        comment: str = "",
    ) -> AttemptSnapshot | None:
        self.calls.append("prepare_attempt")
        return AttemptSnapshot(
            attempt_id=attempt_id,
            delivery_id=delivery_id_for(key),
            request_id=request_id,
            session_id=session_id,
            policy_generation=policy_generation,
            source_stage="prepared",
            comment_stage="pending" if comment.strip() else "not_requested",
            comment=comment,
        )

    async def reserve_source(
        self,
        key: DeliveryKey,
        *,
        attempt_id: str,
        request_id: str,
        session_id: str,
        policy_generation: int,
    ) -> SourceReservation:
        self.calls.append("reserve_source")
        snapshot = DeliverySnapshot(
            delivery_id=delivery_id_for(key),
            key=key,
            status="sending",
        )
        self._deliveries[snapshot.delivery_id] = snapshot
        return SourceReservation(granted=True, snapshot=snapshot)

    async def finalize_source(
        self,
        key: DeliveryKey,
        *,
        attempt_id: str,
        status: SendStatus,
        target_message_id: str = "",
        reason: str = "",
    ) -> bool:
        self.calls.append("finalize_source")
        return True

    async def reserve_comment(self, key: DeliveryKey, *, attempt_id: str) -> CommentReservation:
        self.calls.append("reserve_comment")
        return CommentReservation(
            granted=True,
            snapshot=DeliverySnapshot(
                delivery_id=delivery_id_for(key),
                key=key,
                status="sent",
                comment_status="pending",
            ),
        )

    async def finalize_comment(
        self,
        key: DeliveryKey,
        *,
        attempt_id: str,
        status: CommentStatus,
        comment_message_id: str = "",
        reason: str = "",
    ) -> bool:
        self.calls.append("finalize_comment")
        return True

    async def cancel_attempt(self, key: DeliveryKey, *, attempt_id: str, reason: str) -> bool:
        self.calls.append("cancel_attempt")
        return True

    async def lookup_delivery(self, delivery_id: str) -> DeliverySnapshot | None:
        self.calls.append("lookup_delivery")
        return self._deliveries.get(delivery_id)

    async def lookup_attempt(self, attempt_id: str) -> AttemptSnapshot | None:
        self.calls.append("lookup_attempt")
        return None

    async def recover_interrupted(self) -> RecoveryReport:
        self.calls.append("recover_interrupted")
        return RecoveryReport()

    async def close(self) -> None:
        self.calls.append("close")


@dataclass
class PausingLedgerRepository:
    """账本包装：每次写操作前经过「落账」闸门，并可注入存储故障。

    `fail_next(method)` 让下一次该方法抛 `LedgerUnavailableError`（模拟落盘失败）；
    读取操作不经过闸门，保持只读路径不被暂停影响。
    """

    delegate: Any
    gates: StageGates = field(default_factory=StageGates)
    failures: list[str] = field(default_factory=list)

    def fail_next(self, method: str) -> None:
        """让下一次该方法按存储失败抛错。"""

        self.failures.append(method)

    async def _write_gate(self, method: str) -> None:
        await self.gates.arrive(STAGE_LEDGER_WRITE)
        if method in self.failures:
            self.failures.remove(method)
            raise LedgerUnavailableError("注入的账本写入失败")

    async def identity(self) -> str:
        return await self.delegate.identity()

    async def prepare_attempt(self, key: DeliveryKey, **kwargs: Any) -> AttemptSnapshot | None:
        return await self.delegate.prepare_attempt(key, **kwargs)

    async def lookup_delivery(self, delivery_id: str) -> DeliverySnapshot | None:
        return await self.delegate.lookup_delivery(delivery_id)

    async def lookup_attempt(self, attempt_id: str) -> AttemptSnapshot | None:
        return await self.delegate.lookup_attempt(attempt_id)

    async def recover_interrupted(self) -> RecoveryReport:
        return await self.delegate.recover_interrupted()

    async def close(self) -> None:
        await self.delegate.close()

    async def reserve_source(self, key: DeliveryKey, **kwargs: Any) -> SourceReservation:
        await self._write_gate("reserve_source")
        return await self.delegate.reserve_source(key, **kwargs)

    async def finalize_source(self, key: DeliveryKey, **kwargs: Any) -> bool:
        await self._write_gate("finalize_source")
        return await self.delegate.finalize_source(key, **kwargs)

    async def reserve_comment(self, key: DeliveryKey, **kwargs: Any) -> CommentReservation:
        await self._write_gate("reserve_comment")
        return await self.delegate.reserve_comment(key, **kwargs)

    async def finalize_comment(self, key: DeliveryKey, **kwargs: Any) -> bool:
        await self._write_gate("finalize_comment")
        return await self.delegate.finalize_comment(key, **kwargs)

    async def cancel_attempt(self, key: DeliveryKey, **kwargs: Any) -> bool:
        await self._write_gate("cancel_attempt")
        return await self.delegate.cancel_attempt(key, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)


class AllowAllAuthGate:
    """测试门：任何请求都通过，返回固定的测试主体。"""

    def authenticate(self, request: HttpRequest) -> str | None:
        del request
        return ALLOW_ALL_SUBJECT


@dataclass
class InMemoryHttpClient:
    """内存 HTTP 客户端：直接驱动 `ServiceApp`，不发网络请求、不跟随重定向。

    `call` 用 `asyncio.run` 驱动，供同步用例使用；异步用例应直接 `await app.handle(...)`。
    """

    app: ServiceApp
    token: str = ""
    calls: list[HttpRequest] = field(default_factory=list)

    def call(
        self,
        method: str,
        path: str,
        *,
        payload: Any = None,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        """驱动一次请求：`payload` 序列化 JSON；`body` 直接给原始字节（含非法 JSON）。"""

        merged = dict(headers or {})
        if self.token and not any(key.lower() == "authorization" for key in merged):
            merged["Authorization"] = f"Bearer {self.token}"
        raw = _raw_body(payload, body)
        request = HttpRequest(method=method, path=path, headers=merged, body=raw)
        self.calls.append(request)
        return asyncio.run(self.app.handle(request))


@dataclass
class InMemoryServiceTransport:
    """插件侧客户端的**内存传输**替身：把请求交给真实 `ServiceApp.handle`（无网络）。

    形状对齐 `remote_client.HttpTransport`：从绝对地址取路径（含查询串）、保留方法/
    请求头/请求体，返回状态码与 JSON 载荷；`requests` 记录全部请求供用例断言
    「请求了哪些端点、有没有重复提交发送探测」。默认不连接任何真实地址。
    """

    app: ServiceApp
    requests: list[HttpRequest] = field(default_factory=list)

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
    ) -> TransportReply:
        """驱动一次真实路由（含认证中间件），返回与 aiohttp 同形的原始回应。"""

        parts = urlsplit(url)
        path = parts.path if not parts.query else f"{parts.path}?{parts.query}"
        request = HttpRequest(method=method, path=path, headers=dict(headers), body=body)
        self.requests.append(request)
        response = await self.app.handle(request)
        return TransportReply(status=response.status, payload=dict(response.payload))

    async def close(self) -> None:
        """内存传输无可释放资源（幂等）。"""

        return None

    def paths(self) -> list[str]:
        """已请求的路径列表（断言端点调用序列用）。"""

        return [request.path for request in self.requests]


@dataclass
class ServiceHarness:
    """内存服务夹具：应用 + 不发送后端 + 闸门 + 模拟 OneBot + 账本替身 + 注入时钟。"""

    backends: NonSendingBackends = field(default_factory=NonSendingBackends)
    auth: Any = None
    clock: FakeClock = field(default_factory=FakeClock)
    gates: StageGates = field(default_factory=StageGates)
    onebot: FakeOneBot = field(init=False)
    ledger: PausingLedgerRepository = field(init=False)

    def __post_init__(self) -> None:
        self.onebot = FakeOneBot(gates=self.gates)
        self.ledger = PausingLedgerRepository(delegate=StubLedgerRepository(), gates=self.gates)
        self.app = ServiceApp(
            backends=self.backends,
            auth=self.auth or AllowAllAuthGate(),
        )

    def client(self, *, token: str = "") -> InMemoryHttpClient:
        """取一个内存 HTTP 客户端；`token` 非空时自动附 Bearer 凭证。"""

        return InMemoryHttpClient(app=self.app, token=token)

    def call(
        self,
        method: str,
        path: str,
        *,
        payload: Any = None,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        """等价于 `client().call(...)`（同步用例的便捷入口）。"""

        return self.client().call(method, path, payload=payload, body=body, headers=headers)


def launch_service(
    *,
    backends: NonSendingBackends | None = None,
    auth: Any = None,
    clock: FakeClock | None = None,
    gates: StageGates | None = None,
) -> ServiceHarness:
    """服务启动器：装配一个只在内存里驱动的服务夹具（不连接真实网络）。

    传入的时钟与闸门会被**同一个实例**装配到夹具、模拟 OneBot 与账本替身上，
    因此用例可以统一暂停阶段、推进超时。
    """

    return ServiceHarness(
        backends=backends or NonSendingBackends(),
        auth=auth,
        clock=clock or FakeClock(),
        gates=gates or StageGates(),
    )


async def walk_all_stages(
    harness: ServiceHarness,
    *,
    target_group_id: str = "20002",
    comment: str = "看看这个",
) -> None:
    """按顺序触及五个可暂停阶段（设施自检用骨架流水线；真实链路由第 3–5 组接通）。"""

    await harness.gates.arrive(STAGE_PREPARE)
    await harness.onebot.send_msg(
        target_group_id=target_group_id,
        message=[{"type": "forward", "data": {"id": "res-1"}}],
    )
    await harness.ledger.reserve_source(
        SYNTHETIC_KEY,
        attempt_id="attempt-1",
        request_id="req-1",
        session_id="sess-1",
        policy_generation=1,
    )
    await harness.onebot.send_msg(
        target_group_id=target_group_id,
        message=[{"type": "text", "data": {"text": comment}}],
        stage=STAGE_COMMENT,
    )


def _raw_body(payload: Any, body: bytes | None) -> bytes:
    """请求体：显式字节优先；否则把载荷序列化成 JSON；都为空则空体。"""

    if body is not None:
        return body
    if payload is None:
        return b""
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


# --------------------------------------------------------------------------- 第 5 组设施

DEFAULT_CLIENT_IDENTITY = "maibot-plugin"
"""第 5 组夹具的客户端身份（受控配置的 `client_identity`，同时是认证主体）。"""

DEFAULT_TOKEN = "test-service-token"
"""第 5 组夹具的 Bearer 凭证（合成值，只在内存里使用，不连接任何真实服务）。"""

SECOND_SUBJECT = "another-client"
"""第二个认证主体：只用于「不同主体不能读取或执行他人投递」的隔离用例。"""


def _bearer_token(request: Any) -> str:
    """从请求头取 Bearer 凭证（大小写不敏感；非 Bearer 视为没有凭证）。"""

    headers = getattr(request, "headers", None)
    if not isinstance(headers, Mapping):
        return ""
    for key, value in headers.items():
        if str(key).lower() != "authorization":
            continue
        text = str(value).strip()
        return text[7:].strip() if text.lower().startswith("bearer ") else ""
    return ""


@dataclass
class SubjectAuthGate:
    """测试认证门：Bearer 凭证 → 认证主体；缺凭证时用 `default`（为空即 401）。

    真实实现是 `service.auth.EnvTokenAuthGate`（单主体绑定）；本替身只为隔离用例
    提供第二个主体，认证仍然走真实的 HTTP 中间件，不绕过路由。
    """

    subjects: dict[str, str] = field(default_factory=dict)
    default: str = ""

    def authenticate(self, request: Any) -> str | None:
        provided = _bearer_token(request)
        if provided and provided in self.subjects:
            return self.subjects[provided]
        return self.default or None


@dataclass
class AuthorizedService:
    """第 5 组夹具：真实协调器 + 真实账本 + 模拟 OneBot + 内存 HTTP。

    所有用例都经 `ServiceApp.handle`（真实路由与认证中间件）驱动，不直接调用协调器
    私有方法；`restart()` 用同一状态目录重建协调器与会话，模拟服务进程重启。
    """

    state_dir: Path
    clock: FakeClock
    gates: StageGates
    onebot: FakeOneBot
    ledger: ServiceLedger
    gated_ledger: PausingLedgerRepository
    gateway: SnowLumaGateway
    sessions: SessionRegistry
    coordinator: ServiceCoordinator
    app: ServiceApp
    auth: SubjectAuthGate = field(default_factory=SubjectAuthGate)
    tokens: dict[str, str] = field(default_factory=dict)
    account_id: str = BOUND_ACCOUNT_ID
    client_identity: str = DEFAULT_CLIENT_IDENTITY
    server_source_groups: tuple[str, ...] = (SOURCE_GROUP_ID,)
    server_target_groups: tuple[str, ...] = (TARGET_GROUP_ID,)
    server_generation: str = "gen-1"

    # ------------------------------------------------------------------ 认证与请求

    def add_subject(self, subject: str, *, token: str = "") -> str:
        """为第二个认证主体登记一个凭证（隔离用例用），返回该凭证。"""

        issued = token or f"token-{subject}"
        self.tokens[issued] = subject
        self.auth.subjects[issued] = subject
        return issued

    def token_for(self, subject: str = "") -> str:
        """取某主体对应的凭证；缺省主体用夹具默认凭证。"""

        wanted = subject or self.client_identity
        for token, registered in self.tokens.items():
            if registered == wanted:
                return token
        return ""

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Any = None,
        subject: str = "",
        body: bytes | None = None,
        authenticated: bool = True,
    ) -> HttpRequest:
        """构造一次请求（自动附带该主体凭证；`authenticated=False` 时不带认证头）。"""

        headers: dict[str, str] = {}
        token = self.token_for(subject) if authenticated else ""
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return HttpRequest(
            method=method,
            path=path,
            headers=headers,
            body=_raw_body(payload, body),
        )

    async def call_async(
        self,
        method: str,
        path: str,
        *,
        payload: Any = None,
        subject: str = "",
        body: bytes | None = None,
        authenticated: bool = True,
    ) -> HttpResponse:
        """异步驱动一次请求（可在需要暂停阶段/推进时钟的用例里 `await`）。"""

        return await self.app.handle(
            self.request(
                method,
                path,
                payload=payload,
                subject=subject,
                body=body,
                authenticated=authenticated,
            )
        )

    def call(self, method: str, path: str, **kwargs: Any) -> HttpResponse:
        """同步驱动一次请求（不需要并发/闸门的用例用）。"""

        return asyncio.run(self.call_async(method, path, **kwargs))

    def client(self, *, subject: str = "") -> InMemoryHttpClient:
        """取一个内存 HTTP 客户端（附该主体的凭证）。"""

        return InMemoryHttpClient(app=self.app, token=self.token_for(subject))

    # ------------------------------------------------------------------ 便捷调用

    def heartbeat(self, session_id: str, *, subject: str = "") -> HttpResponse:
        return self.call(
            "POST", f"/v1/sessions/{session_id}/heartbeat", payload={"protocol_version": 1}, subject=subject
        )

    def update_policy(
        self,
        session_id: str,
        *,
        generation: int,
        enabled: bool = True,
        source_groups: Sequence[str] | None = None,
        target_groups: Sequence[str] | None = None,
        subject: str = "",
    ) -> HttpResponse:
        return self.call(
            "PUT",
            f"/v1/sessions/{session_id}/policy",
            payload={
                "protocol_version": 1,
                "policy_generation": generation,
                "enabled": enabled,
                "source_groups": list(source_groups if source_groups is not None else (SOURCE_GROUP_ID,)),
                "target_groups": list(target_groups if target_groups is not None else (TARGET_GROUP_ID,)),
            },
            subject=subject,
        )

    def open_session(self, *, subject: str = "", client_identity: str = "") -> HttpResponse:
        return self.call(
            "POST",
            "/v1/sessions",
            payload={
                "protocol_version": 1,
                "client_identity": client_identity or self.client_identity,
            },
            subject=subject,
        )

    def prepare(
        self,
        session_id: str,
        *,
        generation: int,
        source_message_id: str = SOURCE_MESSAGE_ID,
        source_group_id: str = SOURCE_GROUP_ID,
        target_group_id: str = TARGET_GROUP_ID,
        comment: str = "",
        request_id: str = "",
        subject: str = "",
    ) -> HttpResponse:
        return self.call(
            "POST",
            "/v1/forwards/prepare",
            payload={
                "protocol_version": 1,
                "request_id": request_id or f"req-{source_message_id}-{target_group_id}",
                "session_id": session_id,
                "policy_generation": generation,
                "source_group_id": source_group_id,
                "source_message_id": source_message_id,
                "target_group_id": target_group_id,
                "comment": comment,
            },
            subject=subject,
        )

    def execute(
        self,
        delivery_id: str,
        *,
        stage: str = "source",
        attempt_id: str,
        session_id: str,
        generation: int,
        challenge: str,
        subject: str = "",
    ) -> HttpResponse:
        return self.call(
            "POST",
            f"/v1/forwards/{delivery_id}/{stage}",
            payload={
                "protocol_version": 1,
                "attempt_id": attempt_id,
                "session_id": session_id,
                "policy_generation": generation,
                "challenge": challenge,
            },
            subject=subject,
        )

    def query(self, delivery_id: str, *, subject: str = "") -> HttpResponse:
        return self.call("GET", f"/v1/forwards/{delivery_id}", subject=subject)

    def cancel(self, delivery_id: str, *, attempt_id: str = "", subject: str = "") -> HttpResponse:
        payload: dict[str, Any] = {"protocol_version": 1}
        if attempt_id:
            payload["attempt_id"] = attempt_id
        return self.call("POST", f"/v1/forwards/{delivery_id}/cancel", payload=payload, subject=subject)

    def deregister(self, session_id: str, *, subject: str = "") -> HttpResponse:
        return self.call("DELETE", f"/v1/sessions/{session_id}", subject=subject)

    # ------------------------------------------------------------------ 重启

    def restart(self) -> "AuthorizedService":
        """模拟服务进程重启：旧会话与未消费挑战全部失效，账本事实保留。"""

        asyncio.run(self.ledger.close())
        rebuilt = launch_authorized_service(
            self.state_dir,
            clock=self.clock,
            gates=self.gates,
            onebot=self.onebot,
            account_id=self.account_id,
            client_identity=self.client_identity,
            server_source_groups=self.server_source_groups,
            server_target_groups=self.server_target_groups,
            initialize=False,
        )
        self.ledger = rebuilt.ledger
        self.gated_ledger = rebuilt.gated_ledger
        self.gateway = rebuilt.gateway
        self.sessions = rebuilt.sessions
        self.coordinator = rebuilt.coordinator
        self.app = rebuilt.app
        self.auth = rebuilt.auth
        self.tokens = rebuilt.tokens
        return self

    def close(self) -> None:
        """关闭账本（释放状态目录独占锁），不删除任何投递事实。"""

        asyncio.run(self.ledger.close())


def result_of(response: HttpResponse) -> dict[str, Any]:
    """取成功响应里的 `result`；失败时直接把错误面带进断言信息。"""

    assert response.status == 200, response.payload
    payload = response.payload
    assert payload.get("ok") is True, payload
    result = payload.get("result")
    assert isinstance(result, dict), payload
    return result


def error_of(response: HttpResponse) -> tuple[str, str]:
    """取失败响应的 `(error, reason)`；成功响应按断言失败。"""

    assert response.status != 200, response.payload
    payload = response.payload
    assert payload.get("ok") is False, payload
    return str(payload.get("error")), str(payload.get("reason"))


def launch_authorized_service(
    state_dir: Path,
    *,
    clock: FakeClock | None = None,
    gates: StageGates | None = None,
    onebot: FakeOneBot | None = None,
    account_id: str = BOUND_ACCOUNT_ID,
    client_identity: str = DEFAULT_CLIENT_IDENTITY,
    server_source_groups: Sequence[str] = (SOURCE_GROUP_ID,),
    server_target_groups: Sequence[str] = (TARGET_GROUP_ID,),
    token: str = DEFAULT_TOKEN,
    initialize: bool = True,
) -> AuthorizedService:
    """装配第 5 组夹具：真实账本 + 模拟 OneBot + 真实协调器 + 内存 HTTP。

    `initialize=False` 用于重启场景（账本已存在，不再新装初始化）。
    """

    resolved_clock = clock or FakeClock()
    resolved_gates = gates or StageGates()
    resolved_onebot = onebot or FakeOneBot(gates=resolved_gates, account_id=int(account_id))
    if initialize:
        fresh = initialize_fresh(state_dir, clock=resolved_clock)
        assert isinstance(fresh, FreshOutcome), fresh
    ledger = open_state_ledger(state_dir, clock=resolved_clock)
    gated_ledger = PausingLedgerRepository(delegate=ledger, gates=resolved_gates)
    gateway = SnowLumaGateway(
        transport=resolved_onebot, account_id=account_id, clock=resolved_clock
    )
    ready = asyncio.run(gateway.connect())
    assert not isinstance(ready, AccountUnavailable), ready
    sessions = SessionRegistry(
        clock=resolved_clock,
        account_id=account_id,
        ledger_identity=_ledger_identity_of(ledger),
        server_generation=f"gen-{secrets.token_hex(4)}",
        server_source_groups=server_source_groups,
        server_target_groups=server_target_groups,
        account_generation=gateway.account_generation,
    )
    coordinator = ServiceCoordinator(
        ledger=gated_ledger,
        gateway=gateway,
        sessions=sessions,
        clock=resolved_clock,
        bound_subject=client_identity,
    )
    auth = SubjectAuthGate(subjects={token: client_identity})
    app = ServiceApp(backends=coordinator, auth=auth)
    return AuthorizedService(
        state_dir=state_dir,
        clock=resolved_clock,
        gates=resolved_gates,
        onebot=resolved_onebot,
        ledger=ledger,
        gated_ledger=gated_ledger,
        gateway=gateway,
        sessions=sessions,
        coordinator=coordinator,
        app=app,
        auth=auth,
        tokens={token: client_identity},
        account_id=account_id,
        client_identity=client_identity,
        server_source_groups=tuple(server_source_groups),
        server_target_groups=tuple(server_target_groups),
    )


def _ledger_identity_of(ledger: ServiceLedger) -> str:
    """读取账本身份（只读，不修改数据集）。"""

    return asyncio.run(ledger.identity())


def authorize_session(
    harness: AuthorizedService,
    *,
    generation: int = 1,
    enabled: bool = True,
    source_groups: Sequence[str] | None = None,
    target_groups: Sequence[str] | None = None,
    subject: str = "",
) -> str:
    """建立会话并下发权限代次，返回会话 ID（第 5 组用例的公共前置）。"""

    session_id = str(result_of(harness.open_session(subject=subject))["session_id"])
    accepted = harness.update_policy(
        session_id,
        generation=generation,
        enabled=enabled,
        source_groups=source_groups,
        target_groups=target_groups,
        subject=subject,
    )
    assert accepted.status == 200, accepted.payload
    return session_id
