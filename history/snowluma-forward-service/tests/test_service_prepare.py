"""5.3 无发送副作用的源准备（真实 HTTP 路由 + 真实账本 + 模拟网关）。

判据来自 design D4/D6 与 tasks 5.3：

- `POST /v1/forwards/prepare` 只校验、查源与登记有界准备，平台发送计数为零；
- 先返回已存在的成功/在途/未知阻断结果，不因源缓存后来失效放行（也不查源）；
- 新准备绑定确定尝试与源挑战，挑战固定 30 秒到期，过期不自动发送、也不重建。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from shared.delivery import DeliveryKey
from shared.protocol import PREPARE_CHALLENGE_SECONDS, delivery_id_for
from tests.service_support import (
    BOUND_ACCOUNT_ID,
    SOURCE_GROUP_ID,
    SOURCE_MESSAGE_ID,
    TARGET_GROUP_ID,
    AuthorizedService,
    authorize_session,
    error_of,
    launch_authorized_service,
    result_of,
    store_source_message,
)


def _service(tmp_path: Path) -> AuthorizedService:
    harness = launch_authorized_service(tmp_path / "state")
    store_source_message(
        harness.onebot,
        message_id=SOURCE_MESSAGE_ID,
        group_id=SOURCE_GROUP_ID,
        message=[{"type": "text", "data": {"text": "看看这个"}}],
    )
    return harness


def _key(source_message_id: str = SOURCE_MESSAGE_ID) -> DeliveryKey:
    return DeliveryKey(
        account_id=BOUND_ACCOUNT_ID,
        source_group_id=SOURCE_GROUP_ID,
        source_message_id=source_message_id,
        target_group_id=TARGET_GROUP_ID,
    )


def _seed_blocking(harness: AuthorizedService, status: str, *, target_message_id: str = "") -> str:
    """用账本公开接口预置一条投递事实（只用于夹具，断言仍走 HTTP 路由）。"""

    key = _key()
    attempt_id = f"seed-{status}"
    granted = asyncio.run(
        harness.ledger.reserve_source(
            key,
            attempt_id=attempt_id,
            request_id="seed-request",
            session_id="seed-session",
            policy_generation=1,
        )
    )
    assert granted.granted is True, granted
    if status != "sending":
        finalized = asyncio.run(
            harness.ledger.finalize_source(
                key,
                attempt_id=attempt_id,
                status=status,  # type: ignore[arg-type]
                target_message_id=target_message_id,
                reason="夹具预置",
            )
        )
        assert finalized is True
    return delivery_id_for(key)


def test_准备只校验查源与登记且平台发送为零(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)

    response = harness.prepare(str(session_id), generation=1, comment="顺手评论")
    result = result_of(response)

    assert result["status"] == "prepared"
    assert result["delivery_id"] == delivery_id_for(_key())
    assert result["attempt_id"], result
    assert result["challenge"], result
    assert result["challenge_expires_in_seconds"] == PREPARE_CHALLENGE_SECONDS == 30.0
    assert result["comment_status"] == "pending", "本次评论已登记为待执行（仍未发送）"
    assert harness.onebot.send_count == 0, "准备不得调用任何平台发送"
    assert "send_msg" not in {call.operation for call in harness.onebot.calls}
    assert harness.onebot.query_counts.get("get_msg") == 1, "准备只查源一次"


def test_准备先返回既有阻断结果且不因源缓存失效放行(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    delivery_id = _seed_blocking(harness, "sent", target_message_id="target-msg-1")

    # 源缓存此后失效（例如过期或被清理）：既有成功事实仍必须先返回
    harness.onebot.messages.clear()
    before = dict(harness.onebot.query_counts)

    already = result_of(harness.prepare(str(session_id), generation=1))

    assert already["status"] == "already_forwarded"
    assert already["delivery_id"] == delivery_id
    assert already["target_message_id"] == "target-msg-1"
    assert already["challenge"] == "", "既有成功事实不得再签发可发送的挑战"
    assert harness.onebot.query_counts == before, "阻断态下连源查询都不发生"
    assert harness.onebot.send_count == 0

    for status in ("sending", "unknown"):
        other = launch_authorized_service(tmp_path / f"state-{status}")
        store_source_message(
            other.onebot,
            message_id=SOURCE_MESSAGE_ID,
            group_id=SOURCE_GROUP_ID,
            message=[{"type": "text", "data": {"text": "看看这个"}}],
        )
        other_session = authorize_session(other)
        _seed_blocking(other, status)
        blocked = result_of(other.prepare(str(other_session), generation=1))
        assert blocked["status"] == status, blocked
        assert blocked["challenge"] == ""
        assert other.onebot.send_count == 0


def test_准备为每次合法调用绑定独立尝试与挑战(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)

    first = result_of(harness.prepare(str(session_id), generation=1))
    second = result_of(harness.prepare(str(session_id), generation=1, request_id="req-2"))

    assert first["attempt_id"] != second["attempt_id"]
    assert first["challenge"] != second["challenge"]
    assert first["delivery_id"] == second["delivery_id"], "同一四字段投递身份不变"
    assert harness.onebot.send_count == 0


def test_准备拒绝时零发送且不登记可发送状态(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    # 私聊源：网关查询得到 message_type=private，准备必须整条拒绝
    store_source_message(
        harness.onebot,
        message_id="m-private",
        group_id=SOURCE_GROUP_ID,
        message_type="private",
        message=[{"type": "text", "data": {"text": "私聊内容"}}],
    )

    rejected = harness.prepare(str(session_id), generation=1, source_message_id="m-private")

    assert rejected.status == 400, rejected.payload
    assert error_of(rejected)[0] == "source_not_group"
    assert harness.onebot.send_count == 0
    assert asyncio.run(harness.ledger.lookup_delivery(delivery_id_for(_key("m-private")))) is None, (
        "被拒绝的准备不得在账本留下投递记录"
    )


def test_准备挑战固定三十秒到期且不自动发送(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)

    prepared = result_of(harness.prepare(str(session_id), generation=1))
    assert prepared["challenge_expires_in_seconds"] == PREPARE_CHALLENGE_SECONDS

    # 用注入时钟越过 30 秒准备窗口（期间按 5 秒心跳纪律续期会话，不真实等待）
    for _ in range(3):
        harness.clock.advance(10.0)
        assert harness.heartbeat(str(session_id)).status == 200
    harness.clock.advance(0.1)

    assert harness.onebot.send_count == 0, "准备过期不自动发送、不自动重建"
    later = result_of(harness.prepare(str(session_id), generation=1, request_id="req-later"))
    assert later["challenge"] != prepared["challenge"], "过期后只由新的显式调用签发新挑战"
    assert harness.onebot.send_count == 0
