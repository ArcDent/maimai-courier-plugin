"""5.2 单调策略更新及权限交集（真实 HTTP 路由 + 真实账本 + 模拟网关）。

判据来自 design D4/D5 与 tasks 5.2：

- 旧权限代次不能覆盖新代次；客户端许可不得越过服务端上限；
- 关闭开关、移除源或目标后，新阶段被拒绝；
- 仅自动标准或候选类型变化不改变发送许可；
- 已创建投递所有者仍可只读查询其结果，查询不恢复发送权。
"""

from __future__ import annotations

from pathlib import Path

from tests.service_support import (
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

OUTSIDE_SOURCE_GROUP = "30003"
OUTSIDE_TARGET_GROUP = "40004"


def _service(tmp_path: Path) -> AuthorizedService:
    harness = launch_authorized_service(tmp_path / "state")
    store_source_message(
        harness.onebot,
        message_id=SOURCE_MESSAGE_ID,
        group_id=SOURCE_GROUP_ID,
        message=[{"type": "text", "data": {"text": "看看这个"}}],
    )
    return harness


def test_客户端许可不得越过服务端上限(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(
        harness,
        source_groups=(SOURCE_GROUP_ID, OUTSIDE_SOURCE_GROUP),
        target_groups=(TARGET_GROUP_ID, OUTSIDE_TARGET_GROUP),
    )

    allowed = harness.prepare(str(session_id), generation=1)
    assert result_of(allowed)["status"] == "prepared", allowed.payload

    outside_source = harness.prepare(
        str(session_id), generation=1, source_group_id=OUTSIDE_SOURCE_GROUP, request_id="req-out-source"
    )
    assert outside_source.status == 403, outside_source.payload
    assert error_of(outside_source)[0] == "permission_denied"

    outside_target = harness.prepare(
        str(session_id),
        generation=1,
        target_group_id=OUTSIDE_TARGET_GROUP,
        request_id="req-out-target",
    )
    assert outside_target.status == 403, outside_target.payload
    assert error_of(outside_target)[0] == "permission_denied"
    assert harness.onebot.send_count == 0, "许可之外的准备不得产生任何发送"


def test_旧权限代次不能覆盖新代次(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness, generation=2)

    stale = harness.update_policy(str(session_id), generation=1)
    assert stale.status == 409, stale.payload
    assert error_of(stale)[0] == "stale_policy"

    same = harness.update_policy(str(session_id), generation=2)
    assert same.status == 409, same.payload

    # 新代次仍然是 2：用代次 2 准备成功，用代次 1 一律按旧代次拒绝
    assert result_of(harness.prepare(str(session_id), generation=2))["status"] == "prepared"
    old = harness.prepare(str(session_id), generation=1, request_id="req-old")
    assert old.status == 409, old.payload
    assert error_of(old)[0] == "stale_policy"
    assert harness.onebot.send_count == 0


def test_关闭开关后新阶段与在途准备都被拒绝(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    prepared = result_of(harness.prepare(str(session_id), generation=1))

    harness.update_policy(str(session_id), generation=2, enabled=False)

    blocked = harness.prepare(str(session_id), generation=2, request_id="req-off")
    assert blocked.status == 403, blocked.payload
    assert error_of(blocked)[0] == "permission_denied"

    in_flight = harness.execute(
        str(prepared["delivery_id"]),
        stage="source",
        attempt_id=str(prepared["attempt_id"]),
        session_id=str(session_id),
        generation=2,
        challenge=str(prepared["challenge"]),
    )
    assert in_flight.status == 403, in_flight.payload
    assert error_of(in_flight)[0] == "permission_denied"
    assert harness.onebot.send_count == 0


def test_移除目标后新阶段被拒绝(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    prepared = result_of(harness.prepare(str(session_id), generation=1))

    harness.update_policy(
        str(session_id), generation=2, target_groups=(OUTSIDE_TARGET_GROUP,)
    )

    removed = harness.prepare(str(session_id), generation=2, request_id="req-removed")
    assert removed.status == 403, removed.payload
    assert error_of(removed)[0] == "permission_denied"

    queued = harness.execute(
        str(prepared["delivery_id"]),
        stage="source",
        attempt_id=str(prepared["attempt_id"]),
        session_id=str(session_id),
        generation=1,
        challenge=str(prepared["challenge"]),
    )
    assert queued.status in (409, 403), queued.payload
    assert harness.onebot.send_count == 0


def test_自动标准或候选类型不改变发送许可(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)

    for extra in (
        {"auto_message_types": ["forward", "text"]},
        {"auto_share_criteria": "只要消息有趣就转发"},
        {"auto": {"message_types": ["forward"]}},
    ):
        payload = {
            "protocol_version": 1,
            "policy_generation": 2,
            "enabled": True,
            "source_groups": [SOURCE_GROUP_ID],
            "target_groups": [TARGET_GROUP_ID],
            **extra,
        }
        response = harness.call("PUT", f"/v1/sessions/{session_id}/policy", payload=payload)
        assert response.status == 400, response.payload
        assert error_of(response)[0] == "unknown_field"

    # 许可未被这些字段改变：代次 1 的准备仍然成功，且不存在代次 2
    assert result_of(harness.prepare(str(session_id), generation=1))["status"] == "prepared"
    stale = harness.update_policy(str(session_id), generation=1)
    assert stale.status == 409, stale.payload


def test_已创建投递仍可只读查询且查询不恢复发送权(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = authorize_session(harness)
    prepared = result_of(harness.prepare(str(session_id), generation=1, comment="评论"))
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
    assert executed["status"] == "sent"

    # 权限收缩：目标群被移除
    harness.update_policy(str(session_id), generation=2, target_groups=(OUTSIDE_TARGET_GROUP,))

    queried = result_of(harness.query(str(prepared["delivery_id"])))
    assert queried["status"] == "sent"
    assert queried["target_message_id"] == executed["target_message_id"], (
        "已创建投递的认证所有者仍可只读查询其结果"
    )

    blocked = harness.prepare(str(session_id), generation=2, request_id="req-after-shrink")
    assert blocked.status == 403, blocked.payload
    assert harness.onebot.send_count == 1, "查询与拒绝都不产生新的发送"
