"""组4：本地投递编排的顺序、阶段授权与落账收口（直连口径，内存替身全链）。

断言面只经 `forward_delivery.deliver_forward` 公开协程与共享类型：账本先查 →
准备 → 复验 → 占位 → 发送 → 落账 → 固定 0.5 秒 → 评论占位 → 发送 → 落账；
评论延迟只来自 `COMMENT_DELAY_SECONDS` 代码常量并经注入时钟。替身设施：
`tests/snowluma_client_support` 的内存传输与可控时钟 + 本文件的内存账本与脚本化
客户端；不触真实网络、不真实等待。
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from typing import Any, Callable

import pytest

from maimai_courier_plugin.shared.clock import SystemClock
from maimai_courier_plugin.delivery_ledger import open_ledger
from maimai_courier_plugin.forward_delivery import deliver_forward
from maimai_courier_plugin.send_material import SendMaterial
from maimai_courier_plugin.shared.delivery import (
    DeliveryKey, LedgerStatus,
    LedgerUnavailable, LedgerUnavailableError,
    STATUS_FAILED, STATUS_SENDING, STATUS_SENT, STATUS_UNKNOWN,
)
from maimai_courier_plugin.shared.result import (
    COMMENT_DELAY_SECONDS, DeliveryResult, SourceSendResult, render_forward_result,
)
from maimai_courier_plugin.shared.send_steps import (
    PrepareRejected, PreparedSource, SourceKind, SourceMaterial, get_args,
)
from maimai_courier_plugin.snowluma_client import SnowLumaClient
from tests.snowluma_client_support import (
    CARD_RESOURCE_ID, MemoryOneBotTransport, SOURCE_MESSAGE_ID, TARGET_MESSAGE_ID,
    trusted_forward,
)
from tests.support import (
    SOURCE_ACCOUNT_ID, SOURCE_GROUP_ID, TARGET_GROUP_ID,
    FakeClock, envelope, source_record, spin_until,
)

COMMENT_MESSAGE_ID = "-34567"


def delivery_key() -> DeliveryKey:

    return DeliveryKey(SOURCE_ACCOUNT_ID, SOURCE_GROUP_ID, SOURCE_MESSAGE_ID, TARGET_GROUP_ID)


@dataclasses.dataclass(frozen=True)
class DeliveryRecordStub:
    """替身记录快照：字段与 `DeliveryRecord` 对齐（供 `_existing` 读取）。"""

    key: DeliveryKey
    status: LedgerStatus
    target_message_id: str = ""
    comment_status: str = ""
    comment_message_id: str = ""
    reason: str = ""


@dataclasses.dataclass(frozen=True)
class _Reservation:
    granted: bool
    record: Any


class LedgerStub:
    """内存账本替身：与 `DeliveryLedger` 同接口语义，可注入落盘失败。

    授予规则同真实账本（`failed` 允许显式重试，`sent`/`sending`/`unknown` 阻断）；
    `fail` 里的方法名返回 `False`（「没有可更新的在途占位」），固化「落账失败不得
    伪称结果可靠」的编排分支；`deny_comment` 模拟评论占位未取得。
    """

    def __init__(self) -> None:
        self.records: dict[DeliveryKey, DeliveryRecordStub] = {}
        self.fail: set[str] = set()
        self.deny_comment = False
        self.calls: list[str] = []

    def get(self, key: DeliveryKey) -> Any:
        self.calls.append("get")
        return self.records.get(key)

    def reserve(self, key: DeliveryKey) -> Any:
        self.calls.append("reserve")
        record = self.records.get(key)
        if record is not None and record.status != STATUS_FAILED:
            return _Reservation(False, record)
        record = DeliveryRecordStub(key=key, status=STATUS_SENDING)
        self.records[key] = record
        return _Reservation(True, record)

    def reserve_comment(self, key: DeliveryKey) -> Any:
        self.calls.append("reserve_comment")
        record = self.records.get(key)
        if self.deny_comment or record is None or record.status != STATUS_SENT or record.comment_status:
            return _Reservation(False, record)
        record = dataclasses.replace(record, comment_status=STATUS_SENDING)
        self.records[key] = record
        return _Reservation(True, record)

    def mark_sent(self, key: DeliveryKey, target_message_id: str) -> bool:
        self.calls.append("mark_sent")
        if "mark_sent" in self.fail:
            return False
        return self._finalize(key, STATUS_SENT, target_message_id=target_message_id)

    def mark_failed(self, key: DeliveryKey, reason: str) -> bool:
        self.calls.append("mark_failed")
        if "mark_failed" in self.fail:
            return False
        return self._finalize(key, STATUS_FAILED, reason=reason)

    def mark_unknown(self, key: DeliveryKey, reason: str) -> bool:
        self.calls.append("mark_unknown")
        if "mark_unknown" in self.fail:
            return False
        return self._finalize(key, STATUS_UNKNOWN, reason=reason)

    def record_comment(self, key: DeliveryKey, *, status: LedgerStatus,
                       message_id: str = "") -> bool:
        self.calls.append("record_comment")
        if "record_comment" in self.fail:
            return False
        record = self.records.get(key)
        if record is None or record.status != STATUS_SENT or record.comment_status != STATUS_SENDING:
            return False
        self.records[key] = dataclasses.replace(record, comment_status=status,
                                                comment_message_id=message_id)
        return True

    def _finalize(self, key: DeliveryKey, status: LedgerStatus, *,
                  target_message_id: str = "", reason: str = "") -> bool:
        record = self.records.get(key)
        if record is None or record.status != STATUS_SENDING:
            return False
        self.records[key] = dataclasses.replace(record, status=status,
                                                target_message_id=target_message_id,
                                                reason=reason)
        return True


class ScriptedClient:
    """脚本化客户端替身：把编排分支从真实发送隔离，只验证编排本身。"""

    def __init__(self, prepare: Any, *, source: SourceSendResult, comment: Any = None) -> None:
        self._prepare = prepare
        self._source = source
        self._comment = comment
        self.source_calls = 0
        self.comment_calls = 0

    async def prepare_source(self, prepared: Any) -> Any:
        return self._prepare

    async def send_source(self, request: Any, *, before_submit: Any) -> SourceSendResult:
        self.source_calls += 1
        return self._source

    async def send_comment(self, request: Any, *, before_submit: Any) -> SourceSendResult:
        self.comment_calls += 1
        return self._comment


def text_prepared() -> PreparedSource:

    node = {"type": "node", "data": {"uin": "20002", "name": "名片甲",
                                     "content": [{"type": "text", "data": {"text": "正文"}}]}}
    return PreparedSource(SourceMaterial(kind="text", segments=(node,)),
                          SOURCE_GROUP_ID, SOURCE_MESSAGE_ID, TARGET_GROUP_ID,
                          SOURCE_ACCOUNT_ID, 1)


def sent_source() -> SourceSendResult:
    return SourceSendResult(STATUS_SENT, target_message_id=TARGET_MESSAGE_ID)


async def drive(client: Any, ledger: Any, prepared: Any, *,
                allowed: bool | Callable[[], bool] = True,
                clock: Any = None) -> tuple[DeliveryResult, list[str], FakeClock]:

    clock = FakeClock() if clock is None else clock
    stages: list[str] = []
    permitted = allowed if callable(allowed) else (lambda: bool(allowed))
    task = asyncio.create_task(deliver_forward(
        prepared=prepared, ledger=ledger, client=client, clock=clock,
        send_allowed=permitted, stage_changed=stages.append))
    for _ in range(20000):
        if task.done():
            break
        # FakeClock 的 sleep 在虚拟时间推进前挂起：只推进固定评论延迟这一个唤醒点。
        if clock.sleeps and clock.sleeps[-1] == COMMENT_DELAY_SECONDS:
            clock.advance(COMMENT_DELAY_SECONDS)
        await asyncio.sleep(0)
    return await task, stages, clock


def sent_comment() -> SourceSendResult:
    return SourceSendResult(STATUS_SENT, target_message_id=COMMENT_MESSAGE_ID)


# --- 4.2：编排顺序与评论延迟 -----------------------------------------------------


@pytest.mark.parametrize(("message", "action"), [
    ([{"type": "forward", "data": {"id": CARD_RESOURCE_ID, "content": [{"type": "reply"}]}}],
     "send_group_forward_msg"),
    ([{"type": "text", "data": {"text": "普通文字"}}], "send_forward_msg"),
    ([{"type": "text", "data": {"text": "图"}},
      {"type": "image", "data": {"url": "https://images.example.org/a"}}], "send_msg"),
])
def test_全链内存替身绿且按类型分派(message: list[dict[str, Any]], action: str) -> None:
    async def scenario() -> None:
        prepared = await trusted_forward()
        transport = MemoryOneBotTransport()
        transport.source = source_record(message)
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        ledger = LedgerStub()
        result, stages, _ = await drive(client, ledger, prepared)
        assert result.status == "sent"
        assert result.target_message_id == TARGET_MESSAGE_ID
        assert stages == ["源准备", "源发送", "源落账"]
        assert [record["action"] for record in transport.sent] == [action]
        record = ledger.records[delivery_key()]
        assert record.status == STATUS_SENT and record.target_message_id == TARGET_MESSAGE_ID
        await client.close()
    asyncio.run(scenario())


def test_评论经固定延迟后独立发送并落账() -> None:
    async def scenario() -> None:
        prepared = await trusted_forward(extra={"comment": "评论文本"})
        transport = MemoryOneBotTransport()
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        ledger = LedgerStub()
        result, stages, clock = await drive(client, ledger, prepared)
        assert result.status == "sent"
        assert result.comment_status == STATUS_SENT
        # 评论同样只经 send_msg 普通文本，回执经同一次 get_msg 核验。
        assert result.comment_message_id == TARGET_MESSAGE_ID
        # 固定顺序：源落账后才等待，占位先于评论提交，发送后落账。
        assert stages == ["源准备", "源发送", "源落账",
                          "评论等待", "评论占位", "评论发送", "评论落账"]
        # 延迟恰为代码常量 0.5 秒（注入时钟，不是配置、不是随机时长）。
        assert COMMENT_DELAY_SECONDS == 0.5
        assert clock.sleeps.count(COMMENT_DELAY_SECONDS) == 1
        assert [record["action"] for record in transport.sent][-1] == "send_msg"
        record = ledger.records[delivery_key()]
        assert record.status == STATUS_SENT and record.comment_status == STATUS_SENT
        assert record.comment_message_id == TARGET_MESSAGE_ID
        await client.close()
    asyncio.run(scenario())


def test_空白评论不等待不占位不发送() -> None:
    async def scenario() -> None:
        prepared = await trusted_forward(extra={"comment": "   "})
        transport = MemoryOneBotTransport()
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        ledger = LedgerStub()
        result, stages, clock = await drive(client, ledger, prepared)
        assert result.status == "sent"
        assert result.comment_status == ""
        assert stages == ["源准备", "源发送", "源落账"]
        assert COMMENT_DELAY_SECONDS not in clock.sleeps
        assert ledger.calls.count("reserve_comment") == 0
        assert [record["action"] for record in transport.sent] == ["send_forward_msg"]
        await client.close()
    asyncio.run(scenario())


def test_账本先查与既有记录零平台交互() -> None:
    async def scenario() -> None:
        prepared = await trusted_forward()
        transport = MemoryOneBotTransport()
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        ledger = LedgerStub()
        ledger.records[delivery_key()] = DeliveryRecordStub(
            key=delivery_key(), status=STATUS_SENT, target_message_id=TARGET_MESSAGE_ID)
        result, stages, _ = await drive(client, ledger, prepared)
        assert result.status == "already_forwarded"
        assert stages == []  # 账本先查：不进入准备与发送
        assert transport.queries == [] and transport.sent == []
        await client.close()
    asyncio.run(scenario())


def test_明确失败既可登记又允许后续显式重试() -> None:
    async def scenario() -> None:
        prepared = await trusted_forward()
        transport = MemoryOneBotTransport()
        async def reply(action: str, params: object) -> object:
            if action == "send_forward_msg":
                return envelope(None, status="failed", retcode=100, message="not found")
            return None
        transport.on_action = reply
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        ledger = LedgerStub()
        result, _, _ = await drive(client, ledger, prepared)
        assert result.status == "failed"
        assert result.stage == "source_send"
        assert ledger.records[delivery_key()].status == STATUS_FAILED
        # 既有 failed 不阻断新尝试：撤掉注入故障后同一装配可再次成功。
        transport.on_action = None
        result, _, _ = await drive(client, ledger, prepared)
        assert result.status == "sent"
        await client.close()
    asyncio.run(scenario())


def test_在途与待核实记录阻断重发() -> None:
    async def scenario() -> None:
        prepared = await trusted_forward()
        transport = MemoryOneBotTransport()
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        for status in (STATUS_SENDING, STATUS_UNKNOWN):
            ledger = LedgerStub()
            ledger.records[delivery_key()] = DeliveryRecordStub(key=delivery_key(), status=status)
            result, _, _ = await drive(client, ledger, prepared)
            assert result.status == ("sending" if status == STATUS_SENDING else "unknown")
            assert transport.queries == [] and transport.sent == []
        await client.close()
    asyncio.run(scenario())


def test_遗留sending重启恢复为待核实且不盲目重发(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "delivery_ledger.sqlite3"
        ledger = open_ledger(path, clock=SystemClock())
        assert not isinstance(ledger, LedgerUnavailable)
        ledger.reserve(delivery_key())  # 只占位、不落终态：模拟进程中断
        ledger.close()
        reopened = open_ledger(path, clock=SystemClock())
        assert not isinstance(reopened, LedgerUnavailable)
        record = reopened.get(delivery_key())
        assert record is not None and record.status == STATUS_UNKNOWN
        transport = MemoryOneBotTransport()
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        prepared = await trusted_forward()
        result, _, _ = await drive(client, reopened, prepared)
        assert result.status == "unknown"  # 待核实：阻断重发
        assert transport.sent == []
        reopened.close()
    asyncio.run(scenario())


def test_准备拒绝在发送前按拒绝报告() -> None:
    async def scenario() -> None:
        prepared = await trusted_forward()
        client = ScriptedClient(PrepareRejected("account_mismatch", "实际账号无法对应"),
                                source=sent_source())
        ledger = LedgerStub()
        result, stages, _ = await drive(client, ledger, prepared)
        assert result.status == "rejected"
        assert result.stage == "rejected"
        assert stages == ["源准备"]  # 只读准备否决：未进入发送与落账
        assert client.source_calls == 0
        assert ledger.calls == ["get"]
    asyncio.run(scenario())


def test_源发送前复验未通过拒绝且零占位零发送() -> None:
    async def scenario() -> None:
        prepared = await trusted_forward()
        client = ScriptedClient(text_prepared(), source=sent_source())
        ledger = LedgerStub()
        result, stages, _ = await drive(client, ledger, prepared, allowed=False)
        assert result.status == "rejected"
        assert stages == ["源准备"]  # 复验在占位前：未进入发送
        assert ledger.calls == ["get"]  # 复验在占位之前：未取得发送资格
        assert client.source_calls == 0
    asyncio.run(scenario())


def test_占位未取得时按既有记录汇报() -> None:
    async def scenario() -> None:
        prepared = await trusted_forward()
        client = ScriptedClient(text_prepared(), source=sent_source())
        ledger = LedgerStub()
        ledger.records[delivery_key()] = DeliveryRecordStub(key=delivery_key(), status=STATUS_SENDING)
        result, _, _ = await drive(client, ledger, prepared)
        assert result.status == "sending"
        assert client.source_calls == 0
    asyncio.run(scenario())


def test_源落账失败保留已知ID并按待核实报告() -> None:
    async def scenario() -> None:
        prepared = await trusted_forward(extra={"comment": "评论文本"})
        transport = MemoryOneBotTransport()
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        ledger = LedgerStub()
        ledger.fail.add("mark_sent")
        result, stages, _ = await drive(client, ledger, prepared)
        assert result.status == "unknown"
        assert result.sent is True  # 已取得真实回执
        assert result.target_message_id == TARGET_MESSAGE_ID
        assert result.stage == "source_persist"
        assert stages == ["源准备", "源发送", "源落账"]  # 未进入评论段
        assert not any(record["action"] == "send_msg" for record in transport.sent)
        await client.close()
    asyncio.run(scenario())


def test_源结果待核实不进入评论段() -> None:
    async def scenario() -> None:
        prepared = await trusted_forward(extra={"comment": "评论文本"})
        transport = MemoryOneBotTransport()
        async def reply(action: str, params: object) -> object:
            if action == "send_forward_msg":
                return envelope({})  # 缺可核实目标 ID
            return None
        transport.on_action = reply
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        result, stages, clock = await drive(client, LedgerStub(), prepared)
        assert result.status == "unknown"
        assert result.comment_status == ""
        assert stages == ["源准备", "源发送", "源落账"]
        assert COMMENT_DELAY_SECONDS not in clock.sleeps
        await client.close()
    asyncio.run(scenario())


def test_契约外发送状态一律按待核实且不降级失败() -> None:
    async def scenario() -> None:
        prepared = await trusted_forward()
        client = ScriptedClient(text_prepared(),
                                source=SourceSendResult("cancelled"),  # 契约外字面量
                                comment=sent_comment())
        ledger = LedgerStub()
        result, _, _ = await drive(client, ledger, prepared)
        assert result.status == "unknown"
        assert ledger.records[delivery_key()].status == STATUS_UNKNOWN
        assert client.comment_calls == 0  # 源未成功：不发送评论
    asyncio.run(scenario())


def test_评论发送前复验失败按部分成功且不提交() -> None:
    async def scenario() -> None:
        prepared = await trusted_forward(extra={"comment": "评论文本"})
        checks = {"count": 0}

        def allowed() -> bool:
            checks["count"] += 1
            return checks["count"] == 1  # 源前放行、评论前撤销

        client = ScriptedClient(text_prepared(), source=sent_source(), comment=sent_comment())
        ledger = LedgerStub()
        result, stages, _ = await drive(client, ledger, prepared, allowed=allowed)
        assert result.status == "partial"
        assert result.sent is True
        assert result.comment_status == STATUS_FAILED
        assert stages[-1] == "评论等待"  # 复验失败点：未到评论占位
        assert client.comment_calls == 0
        assert ledger.records[delivery_key()].comment_status == ""
    asyncio.run(scenario())


def test_评论占位未取得零评论提交() -> None:
    async def scenario() -> None:
        prepared = await trusted_forward(extra={"comment": "评论文本"})
        client = ScriptedClient(text_prepared(), source=sent_source(), comment=sent_comment())
        ledger = LedgerStub()
        ledger.deny_comment = True
        result, _, _ = await drive(client, ledger, prepared)
        assert result.status == "partial"
        assert result.comment_status == STATUS_UNKNOWN
        assert client.comment_calls == 0
    asyncio.run(scenario())


def test_评论落账失败不声称评论可靠() -> None:
    async def scenario() -> None:
        prepared = await trusted_forward(extra={"comment": "评论文本"})
        client = ScriptedClient(text_prepared(), source=sent_source(), comment=sent_comment())
        ledger = LedgerStub()
        ledger.fail.add("record_comment")
        result, _, _ = await drive(client, ledger, prepared)
        assert result.status == "partial"
        assert result.stage == "comment_persist"
        assert result.comment_status == STATUS_UNKNOWN
        assert client.comment_calls == 1
    asyncio.run(scenario())


def test_评论结果待核实按部分成功且不补发() -> None:
    async def scenario() -> None:
        prepared = await trusted_forward(extra={"comment": "评论文本"})
        client = ScriptedClient(text_prepared(), source=sent_source(),
                                comment=SourceSendResult(STATUS_UNKNOWN, reason="待核实"))
        result, _, _ = await drive(client, LedgerStub(), prepared)
        assert result.status == "partial"
        assert result.comment_status == STATUS_UNKNOWN
        assert result.comment_message_id == ""
    asyncio.run(scenario())


def test_评论等待取消不提交也不补发() -> None:
    class CancelAtDelayClock(FakeClock):
        """评论延迟点直接抛取消：等价「等待期间卸载」，不依赖任务取消语义。"""

        async def sleep(self, seconds: float) -> None:
            if seconds == COMMENT_DELAY_SECONDS:
                raise asyncio.CancelledError
            await super().sleep(seconds)

    async def scenario() -> None:
        prepared = await trusted_forward(extra={"comment": "评论文本"})
        client = ScriptedClient(text_prepared(), source=sent_source(), comment=sent_comment())
        ledger = LedgerStub()
        result, _, _ = await drive(client, ledger, prepared, clock=CancelAtDelayClock())
        assert result.status == "partial"
        assert result.sent is True
        assert result.comment_status == STATUS_FAILED
        assert client.comment_calls == 0
        assert ledger.records[delivery_key()].comment_status == ""
    asyncio.run(scenario())


def test_账本不可用时关闭发送且零平台交互() -> None:
    async def scenario() -> None:
        prepared = await trusted_forward()
        transport = MemoryOneBotTransport()
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        result, stages, _ = await drive(client, LedgerUnavailable("状态库缺失"), prepared)
        assert result.status == "ledger_unavailable"
        assert result.stage == "source_persist"
        assert stages == []
        assert transport.queries == [] and transport.sent == []
        await client.close()
    asyncio.run(scenario())


def test_账本读取异常按不可用关闭发送() -> None:
    async def scenario() -> None:
        prepared = await trusted_forward()
        transport = MemoryOneBotTransport()
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)

        class BrokenLedger(LedgerStub):
            def get(self, key: DeliveryKey) -> Any:
                raise LedgerUnavailableError("读取失败")

        result, _, _ = await drive(client, BrokenLedger(), prepared)
        assert result.status == "ledger_unavailable"
        assert transport.queries == [] and transport.sent == []
        await client.close()
    asyncio.run(scenario())


# --- 4.3：共享类型收尾：kind 三值与结果渲染全覆盖 -------------------------------


def test_发送路径kind三值透传共享类型() -> None:
    assert frozenset(get_args(SourceKind)) == {"forward", "image", "text"}
    for kind in get_args(SourceKind):
        prepared = PreparedSource(SourceMaterial(kind=kind), SOURCE_GROUP_ID,
                                  SOURCE_MESSAGE_ID, TARGET_GROUP_ID, SOURCE_ACCOUNT_ID, 1)
        assert prepared.kind == kind and prepared.material.kind == kind


@pytest.mark.parametrize(("status", "success", "sent"), [
    ("sent", True, True),
    ("partial", False, True),
    ("already_forwarded", True, True),
    ("sending", False, False),
    ("rejected", False, False),
    ("failed", False, False),
    ("unknown", False, False),
    ("ledger_unavailable", False, False),
])
def test_结果渲染覆盖全部调用状态(status: str, success: bool, sent: bool) -> None:
    result = DeliveryResult(status=status, sent=sent,
                            already_forwarded=(status == "already_forwarded"),
                            target_message_id="1" if sent else "",
                            comment_status="sent" if status == "sent" else "",
                            stage="source_send" if status == "rejected" else "",
                            reason="定位原因")
    rendered = render_forward_result(result, source_message_id=SOURCE_MESSAGE_ID,
                                     target_group_id=TARGET_GROUP_ID)
    assert rendered["status"] == status
    assert rendered["success"] is success
    assert rendered["sent"] is sent and rendered["delivered"] is sent
    assert rendered["already_forwarded"] is (status == "already_forwarded")
    assert rendered["source_message_id"] == SOURCE_MESSAGE_ID
    assert rendered["target_group_id"] == TARGET_GROUP_ID
    assert rendered["reason"] == "定位原因"
    assert rendered["stage"] == ("source_send" if status == "rejected" else "")
    # 拒绝分支没有已准备材料：不得声称材料完整。
    assert "send_kind" not in rendered and "material_shape" not in rendered


def test_结果渲染按结构面展示材料() -> None:
    result = DeliveryResult(status="sent", sent=True, target_message_id=TARGET_MESSAGE_ID)
    rendered = render_forward_result(result, material=SendMaterial(kind="text", segments=(),
                                                                   shape=("text",)))
    assert rendered["send_kind"] == "text"
    assert rendered["material_shape"] == ["text"]
