"""5.5 只读结果查询与原调用续接（真实 HTTP 路由 + 真实账本 + 模拟网关）。

判据来自 design D4/D8 与 tasks 5.5：

- `GET /v1/forwards/{delivery_id}` 返回权威源/评论状态与已知目标 ID，不含任何正文；
- 仅原活动调用可取得此前签发且未过期的评论挑战；新会话、重启或过期后不得重新生成；
- 连续查询、恢复查询与未知查询都不增加平台发送次数，查询也不恢复发送权。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from shared.protocol import delivery_id_for
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

SOURCE_TEXT = "源消息正文哨兵"
COMMENT_TEXT = "评论正文哨兵"


def _service(tmp_path: Path) -> AuthorizedService:
    harness = launch_authorized_service(tmp_path / "state")
    store_source_message(
        harness.onebot,
        message_id=SOURCE_MESSAGE_ID,
        group_id=SOURCE_GROUP_ID,
        message=[{"type": "text", "data": {"text": SOURCE_TEXT}}],
    )
    return harness


def _send_source(harness: AuthorizedService, session_id: str, *, comment: str = COMMENT_TEXT) -> dict[str, object]:
    """建立一次源投递成功（评论待执行）并返回源阶段结果。"""

    prepared = result_of(harness.prepare(str(session_id), generation=1, comment=comment))
    executed = result_of(
        harness.execute(
            str(prepared["delivery_id"]),
            stage="source",
            attempt_id=str(prepared["attempt_id"]),
            session_id=str(session_id),
            generation=1,
            challenge=str(prepared["challenge"]),
        )
    )
    assert executed["status"] == "sent", executed
    return executed


def test_查询返回权威状态与已知目标ID且不含正文(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    executed = _send_source(harness, str(session_id))

    response = harness.query(str(executed["delivery_id"]))
    result = result_of(response)

    assert result["status"] == "sent"
    assert result["target_message_id"] == executed["target_message_id"]
    assert result["comment_status"] == "pending"
    assert result["comment_challenge"] == executed["comment_challenge"], (
        "原活动调用仍可取回此前签发且未过期的评论挑战"
    )
    serialized = json.dumps(response.payload, ensure_ascii=False)
    assert SOURCE_TEXT not in serialized, "查询响应不得包含源正文"
    assert COMMENT_TEXT not in serialized, "查询响应不得包含评论正文"


def test_连续查询与恢复查询都不增加平台发送次数(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    executed = _send_source(harness, str(session_id))
    sends_before = harness.onebot.send_count

    for _ in range(3):
        assert harness.query(str(executed["delivery_id"])).status == 200

    assert harness.onebot.send_count == sends_before, "查询绝不通过再次发送确认"

    # 服务重启后的恢复查询：只读已持久化结果，不重发、也不签发新挑战
    harness.restart()
    session_id = authorize_session(harness, generation=2)
    restored = result_of(harness.query(str(executed["delivery_id"])))

    assert restored["status"] == "sent"
    assert restored["target_message_id"] == executed["target_message_id"]
    assert restored["comment_challenge"] == "", "重启后旧会话与挑战已失效，不得重新生成"
    assert harness.onebot.send_count == sends_before


def test_未知投递查询明确不存在且零发送(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    authorize_session(harness)

    response = harness.query(delivery_id_for(_dummy_key()))

    assert response.status == 404, response.payload
    assert error_of(response)[0] == "not_found"
    assert harness.onebot.send_count == 0


def _dummy_key() -> object:
    from shared.delivery import DeliveryKey

    return DeliveryKey(
        account_id=BOUND_ACCOUNT_ID,
        source_group_id=SOURCE_GROUP_ID,
        source_message_id="m-从未准备",
        target_group_id=TARGET_GROUP_ID,
    )


def test_新会话不再取得评论挑战(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    executed = _send_source(harness, str(session_id))

    authorize_session(harness, generation=2)
    after = result_of(harness.query(str(executed["delivery_id"])))

    assert after["comment_challenge"] == "", "新会话不得取得旧调用的评论挑战"
    assert after["comment_status"] == "skipped", (
        "旧实例的未消费评论授权随会话失效：登记为跳过（源已投递事实保留）"
    )
    assert after["target_message_id"] == executed["target_message_id"]
    assert harness.onebot.send_count == 1, "只允许此前那一次源发送"


def test_评论挑战过期后查询登记跳过且不自动补发(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    executed = _send_source(harness, str(session_id))

    # 心跳维持会话，但跨过 30 秒评论挑战窗口
    for _ in range(3):
        harness.clock.advance(10.0)
        assert harness.heartbeat(str(session_id)).status == 200
    harness.clock.advance(0.1)

    queried = result_of(harness.query(str(executed["delivery_id"])))

    assert queried["comment_status"] == "skipped", (
        "未提交评论的挑战到期后必须登记跳过，不形成永久待发送队列"
    )
    assert queried["comment_challenge"] == ""
    assert harness.onebot.send_count == 1, "跳过不等于发送，也不自动补发"
    snapshot = asyncio.run(harness.ledger.lookup_delivery(str(executed["delivery_id"])))
    assert snapshot is not None and snapshot.comment_status == "skipped"
    assert snapshot.status == "sent" and snapshot.target_message_id == executed["target_message_id"]


def test_挑战未过期的最后一刻仍可续接查询(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    executed = _send_source(harness, str(session_id))

    for _ in range(2):
        harness.clock.advance(10.0)
        assert harness.heartbeat(str(session_id)).status == 200
    harness.clock.advance(9.9)  # 第 29.9 秒：挑战仍在有效期
    assert harness.heartbeat(str(session_id)).status == 200

    queried = result_of(harness.query(str(executed["delivery_id"])))

    assert queried["comment_status"] == "pending"
    assert queried["comment_challenge"] == executed["comment_challenge"]
    assert queried["comment_challenge_expires_in_seconds"] > 0
    assert asyncio.run(harness.ledger.lookup_delivery(str(executed["delivery_id"]))).comment_status == "pending"  # type: ignore[union-attr]
