"""2.2 共享协议：请求/响应模型、阶段授权、账本仓储协议与服务端发送接缝。

判据来自 design D4/D5/D7/D8：

- 协议是 HTTP/JSON v1；请求必须声明版本，错误版本与未知字段一律拒绝；
- 请求 ID、投递 ID、会话 ID、权限代次与阶段是**各自独立**的模型，都不是防重键：
  唯一投递身份仍是四字段 `DeliveryKey`，`delivery_id` 只是它的稳定摘要；
- 阶段授权绑定投递、尝试、会话、权限代次、阶段与到期时间，不能跨阶段或跨尝试互用；
- 评论状态是封闭值域（`not_requested` / `pending` / `sending` / `sent` / `failed` /
  `unknown` / `skipped`）；
- 账本仓储协议覆盖投递、尝试、源/评论占位与结果查询，只读不到记录返回 `None`；
- 发送接缝的结果复用共享 `SourceSendResult`；契约外状态一律收窄为待核实。
"""

from __future__ import annotations

from typing import Any, get_args

VALID_PREPARE_PAYLOAD: dict[str, Any] = {
    "protocol_version": 1,
    "request_id": "req-1",
    "session_id": "sess-1",
    "policy_generation": 3,
    "source_group_id": "10001",
    "source_message_id": "m-source-1",
    "target_group_id": "20002",
    "comment": "看看这个",
}
"""合法准备请求：字段名与 design D4 的准备接口一一对应。"""


def _prepare(**overrides: Any) -> Any:
    from shared.protocol import parse_prepare_request

    payload = dict(VALID_PREPARE_PAYLOAD)
    payload.update(overrides)
    return parse_prepare_request(payload)


def test_协议版本是v1且请求ID不参与投递身份() -> None:
    from dataclasses import fields

    import shared.protocol as protocol

    assert protocol.PROTOCOL_VERSION == 1, "HTTP/JSON 协议固定为 v1（design D4）"

    parsed = _prepare()
    assert not hasattr(parsed, "error"), parsed
    other_request = _prepare(request_id="req-2", session_id="sess-2", policy_generation=9)
    assert other_request.request_id != parsed.request_id

    key = protocol.DeliveryKey(
        account_id="10000",
        source_group_id="10001",
        source_message_id="m-source-1",
        target_group_id="20002",
    )
    same_key = protocol.DeliveryKey(
        account_id="10000",
        source_group_id="10001",
        source_message_id="m-source-1",
        target_group_id="20002",
    )
    other_target = protocol.DeliveryKey(
        account_id="10000",
        source_group_id="10001",
        source_message_id="m-source-1",
        target_group_id="20003",
    )
    assert protocol.delivery_id_for(key) == protocol.delivery_id_for(same_key), (
        "投递 ID 只由四字段决定，换请求 ID／会话／权限代次都不改变它"
    )
    assert protocol.delivery_id_for(key) != protocol.delivery_id_for(other_target)
    assert (
        len(protocol.delivery_id_for(key)) <= protocol.MAX_IDENTIFIER_LENGTH
    ), "投递 ID 必须满足标识符长度上限（可安全进请求与日志）"

    # 四字段仍是唯一防重身份：`DeliveryKey` 字段集合与共享投递身份一致。
    assert [field.name for field in fields(protocol.DeliveryKey)] == [
        "account_id",
        "source_group_id",
        "source_message_id",
        "target_group_id",
    ]


def test_准备请求拒绝未知字段与非法标识() -> None:
    from shared.protocol import (
        ERROR_INVALID_FIELD,
        ERROR_INVALID_ID,
        ERROR_MISSING_FIELD,
        ERROR_PROTOCOL_VERSION,
        ERROR_UNKNOWN_FIELD,
    )

    for label, payload in [
        ("缺字段", {key: value for key, value in VALID_PREPARE_PAYLOAD.items() if key != "session_id"}),
        ("未知字段", {**VALID_PREPARE_PAYLOAD, "extra": "x"}),
        ("协议版本不符", {**VALID_PREPARE_PAYLOAD, "protocol_version": 2}),
        ("协议版本缺失", {key: value for key, value in VALID_PREPARE_PAYLOAD.items() if key != "protocol_version"}),
        ("非法请求 ID", {**VALID_PREPARE_PAYLOAD, "request_id": "bad id"}),
        ("非法源消息 ID", {**VALID_PREPARE_PAYLOAD, "source_message_id": ""}),
        ("控制字符源群", {**VALID_PREPARE_PAYLOAD, "source_group_id": "10001\n"}),
        ("尖括号目标群", {**VALID_PREPARE_PAYLOAD, "target_group_id": "<20002>"}),
        ("权限代次非整数", {**VALID_PREPARE_PAYLOAD, "policy_generation": "3"}),
        ("评论非字符串", {**VALID_PREPARE_PAYLOAD, "comment": {"text": "x"}}),
    ]:
        outcome = _prepare_payload(payload)
        assert hasattr(outcome, "error"), (label, outcome)
        assert outcome.error in {
            ERROR_UNKNOWN_FIELD,
            ERROR_MISSING_FIELD,
            ERROR_INVALID_ID,
            ERROR_INVALID_FIELD,
            ERROR_PROTOCOL_VERSION,
        }, (label, outcome)

    assert _prepare_payload({**VALID_PREPARE_PAYLOAD, "extra": "x"}).error == ERROR_UNKNOWN_FIELD
    assert _prepare_payload(
        {key: value for key, value in VALID_PREPARE_PAYLOAD.items() if key != "session_id"}
    ).error == ERROR_MISSING_FIELD
    assert _prepare_payload({**VALID_PREPARE_PAYLOAD, "protocol_version": 2}).error == (
        ERROR_PROTOCOL_VERSION
    )
    assert _prepare_payload({**VALID_PREPARE_PAYLOAD, "source_group_id": "<10001>"}).error == (
        ERROR_INVALID_ID
    )
    assert _prepare_payload({**VALID_PREPARE_PAYLOAD, "comment": 5}).error == ERROR_INVALID_FIELD
    assert not hasattr(_prepare_payload(VALID_PREPARE_PAYLOAD), "error"), "同一 payload 必须可解析"


def test_准备请求不接受账号许可上限与状态目录() -> None:
    from shared.protocol import ERROR_UNKNOWN_FIELD

    for field in ("account_id", "state_dir", "limits", "listen_host"):
        outcome = _prepare_payload({**VALID_PREPARE_PAYLOAD, field: "x"})
        assert hasattr(outcome, "error") and outcome.error == ERROR_UNKNOWN_FIELD, (field, outcome)


def test_评论状态与尝试阶段是封闭值域() -> None:
    import shared.ledger as ledger
    import shared.protocol as protocol

    assert set(get_args(protocol.CommentStatus)) == {
        "not_requested",
        "pending",
        "sending",
        "sent",
        "failed",
        "unknown",
        "skipped",
    }, "评论状态必须是 design D7 的封闭集合"
    assert set(ledger.ATTEMPT_STAGE_VALUES) == {
        "prepared",
        "sending",
        "sent",
        "failed",
        "unknown",
        "cancelled",
    }, "尝试的源阶段必须是封闭集合（含准备与撤销）"


def test_阶段授权不能跨阶段或跨尝试互用() -> None:
    from shared.protocol import (
        ERROR_AUTHORIZATION_MISMATCH,
        ERROR_EXPIRED_AUTHORIZATION,
        ERROR_STAGE_MISMATCH,
        StageAuthorization,
        authorization_rejection,
    )

    authorization = StageAuthorization(
        stage="source",
        token="challenge-1",
        delivery_id="d-1",
        attempt_id="a-1",
        session_id="sess-1",
        policy_generation=3,
        expires_at=100.0,
    )

    assert (
        authorization_rejection(
            authorization,
            expected_stage="source",
            delivery_id="d-1",
            attempt_id="a-1",
            session_id="sess-1",
            policy_generation=3,
            now=99.0,
        )
        is None
    ), "绑定完全一致的未过期授权必须通过"

    used_for_comment = authorization_rejection(
        authorization,
        expected_stage="comment",
        delivery_id="d-1",
        attempt_id="a-1",
        session_id="sess-1",
        policy_generation=3,
        now=99.0,
    )
    assert used_for_comment is not None and used_for_comment.error == ERROR_STAGE_MISMATCH

    wrong_attempt = authorization_rejection(
        authorization,
        expected_stage="source",
        delivery_id="d-1",
        attempt_id="a-2",
        session_id="sess-1",
        policy_generation=3,
        now=99.0,
    )
    assert wrong_attempt is not None and wrong_attempt.error == ERROR_AUTHORIZATION_MISMATCH

    expired = authorization_rejection(
        authorization,
        expected_stage="source",
        delivery_id="d-1",
        attempt_id="a-1",
        session_id="sess-1",
        policy_generation=3,
        now=100.5,
    )
    assert expired is not None and expired.error == ERROR_EXPIRED_AUTHORIZATION


def test_契约外发送状态一律收窄为待核实() -> None:
    from shared.send_steps import normalize_send_status

    for raw, expected in [
        ("sent", "sent"),
        ("failed", "failed"),
        ("unknown", "unknown"),
        ("", "unknown"),
        ("seNd", "unknown"),
        ("cancelled", "unknown"),
        ("timeout", "unknown"),
        (None, "unknown"),
    ]:
        assert normalize_send_status(raw) == expected, (raw, expected)


def test_服务端发送接缝输入是投递尝试目标与材料() -> None:
    from dataclasses import fields

    import shared.send_steps as send_steps

    assert [field.name for field in fields(send_steps.SourceSendRequest)] == [
        "delivery_id",
        "attempt_id",
        "target_group_id",
        "material",
    ]
    assert [field.name for field in fields(send_steps.CommentSendRequest)] == [
        "delivery_id",
        "attempt_id",
        "target_group_id",
        "comment",
    ]

    material = send_steps.SourceMaterial(kind="forward", forward_resource_id="res-1")
    request = send_steps.SourceSendRequest(
        delivery_id="d-1",
        attempt_id="a-1",
        target_group_id="20002",
        material=material,
    )
    assert request.material.forward_resource_id == "res-1"


def test_账本仓储协议覆盖投递尝试占位与查询() -> None:
    from shared.delivery import DeliveryKey
    from shared.ledger import (
        AttemptSnapshot,
        CommentReservation,
        DeliverySnapshot,
        LedgerRepository,
        RecoveryReport,
        SourceReservation,
    )

    required = (
        "identity",
        "reserve_source",
        "finalize_source",
        "reserve_comment",
        "finalize_comment",
        "lookup_delivery",
        "lookup_attempt",
        "recover_interrupted",
    )
    for name in required:
        assert callable(getattr(LedgerRepository, name, None)), f"账本仓储协议缺少 {name}"

    key = DeliveryKey(
        account_id="10000",
        source_group_id="10001",
        source_message_id="m-source-1",
        target_group_id="20002",
    )
    snapshot = DeliverySnapshot(delivery_id="d-1", key=key, status="sending")
    granted = SourceReservation(granted=True, snapshot=snapshot)
    blocked = CommentReservation(granted=False, snapshot=snapshot)
    attempt = AttemptSnapshot(
        attempt_id="a-1",
        delivery_id="d-1",
        request_id="req-1",
        session_id="sess-1",
        policy_generation=3,
        source_stage="sending",
        comment_stage="not_requested",
    )
    report = RecoveryReport()

    assert granted.granted is True and blocked.granted is False
    assert snapshot.status == "sending" and snapshot.target_message_id == ""
    assert attempt.source_stage == "sending" and attempt.request_id == "req-1"
    assert report.sources_recovered == 0


def _prepare_payload(payload: dict[str, Any]) -> Any:
    from shared.protocol import parse_prepare_request

    return parse_prepare_request(payload)
