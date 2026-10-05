"""2.4 v1 HTTP 路由与参数校验骨架：合法请求抵达后端边界，非法请求一律拒绝。

判据来自 design D4（v1 接口清单）与 D5（认证、字段/请求体受限、不返回底层正文）：

- 会话建立/心跳/策略/注销、准备、源/评论执行、查询、撤销九个接口都可达后端边界；
- 非法 JSON、未知字段（含任意动作/连接地址字段）、错误协议版本与非法路径标识符
  一律拒绝；
- 仅受理请求绝不返回源投递成功；骨架与全部负例的平台发送计数必须为零。

本组注入**不发送的公开后端替身**（`service.backends.NonSendingBackends`），后端
边界由该替身的调用记录证明。
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.service_support import ServiceHarness

VALID_PREPARE: dict[str, Any] = {
    "protocol_version": 1,
    "request_id": "req-1",
    "session_id": "sess-1",
    "policy_generation": 3,
    "source_group_id": "10001",
    "source_message_id": "m-source-1",
    "target_group_id": "20002",
    "comment": "看看这个",
}

STAGE_BODY: dict[str, Any] = {
    "protocol_version": 1,
    "attempt_id": "a-1",
    "session_id": "sess-1",
    "policy_generation": 3,
    "challenge": "ch-1",
}

ROUTES: tuple[tuple[str, str, dict[str, Any] | None, str], ...] = (
    ("POST", "/v1/sessions", {"protocol_version": 1, "client_identity": "client-a"}, "create_session"),
    ("POST", "/v1/sessions/sess-1/heartbeat", {"protocol_version": 1}, "heartbeat"),
    (
        "PUT",
        "/v1/sessions/sess-1/policy",
        {
            "protocol_version": 1,
            "policy_generation": 2,
            "enabled": True,
            "source_groups": ["10001"],
            "target_groups": ["20002"],
        },
        "update_policy",
    ),
    ("DELETE", "/v1/sessions/sess-1", None, "deregister"),
    ("POST", "/v1/forwards/prepare", VALID_PREPARE, "prepare"),
    ("POST", "/v1/forwards/d-1/source", STAGE_BODY, "execute_source"),
    ("POST", "/v1/forwards/d-1/comment", STAGE_BODY, "execute_comment"),
    ("GET", "/v1/forwards/d-1", None, "query"),
    ("POST", "/v1/forwards/d-1/cancel", {"protocol_version": 1}, "cancel"),
)
"""design D4 的九个接口：方法 + 路径 + 合法请求体 + 对应后端方法名。"""


def test_合法请求抵达对应后端边界() -> None:
    harness = ServiceHarness()

    for method, path, payload, backend_method in ROUTES:
        response = harness.call(method, path, payload=payload)
        assert response.status == 200, (method, path, response)
        assert response.payload.get("ok") is True, (method, path, response.payload)
        assert harness.backends.calls[-1][0] == backend_method, (
            method,
            path,
            harness.backends.calls,
        )

    assert [name for name, _ in harness.backends.calls] == [route[3] for route in ROUTES]
    assert harness.backends.platform_send_count == 0, "骨架不得产生任何平台发送"


def test_受理请求不返回源投递成功() -> None:
    harness = ServiceHarness()

    prepared = harness.call("POST", "/v1/forwards/prepare", payload=VALID_PREPARE)
    prepared_result = prepared.payload["result"]
    assert prepared_result["status"] == "prepared", prepared_result
    assert "success" not in prepared_result, "准备只登记，不得声称发送成功"

    executed = harness.call("POST", "/v1/forwards/d-1/source", payload=STAGE_BODY)
    executed_result = executed.payload["result"]
    assert executed_result["status"] != "sent", "不发送替身绝不能报告已投递"
    assert harness.backends.platform_send_count == 0


def test_非法请求一律拒绝() -> None:
    harness = ServiceHarness()

    broken_json = harness.call("POST", "/v1/forwards/prepare", body=b"{not json")
    assert broken_json.status == 400 and broken_json.payload["error"] == "invalid_json"

    non_object = harness.call("POST", "/v1/forwards/prepare", body=b"[1, 2]")
    assert non_object.status == 400 and non_object.payload["error"] == "invalid_field"

    for label, payload in [
        ("未知字段", {**VALID_PREPARE, "extra": "x"}),
        ("任意动作字段", {**VALID_PREPARE, "action": "send_group_msg"}),
        ("任意连接地址字段", {**VALID_PREPARE, "endpoint": "http://127.0.0.1:9999"}),
        ("协议版本错误", {**VALID_PREPARE, "protocol_version": 2}),
        ("非法源消息 ID", {**VALID_PREPARE, "source_message_id": "<bad>"}),
    ]:
        response = harness.call("POST", "/v1/forwards/prepare", payload=payload)
        assert response.status == 400, (label, response)
        assert response.payload.get("ok") is False, (label, response.payload)
        assert "reason" in response.payload, (label, response.payload)

    bad_path = harness.call("POST", "/v1/forwards/<bad>/source", payload=STAGE_BODY)
    assert bad_path.status == 400 and bad_path.payload["error"] == "invalid_id"
    bad_session = harness.call("POST", "/v1/sessions/bad%20id/heartbeat", payload={"protocol_version": 1})
    assert bad_session.status == 400, bad_session


def test_未知路由与错误方法明确区分() -> None:
    harness = ServiceHarness()

    unknown = harness.call("GET", "/v1/不存在")
    assert unknown.status == 404 and unknown.payload["error"] == "not_found"

    wrong_method = harness.call("PUT", "/v1/sessions", payload={"protocol_version": 1})
    assert wrong_method.status == 405 and wrong_method.payload["error"] == "method_not_allowed"


def test_全部负例之后平台发送计数仍为零() -> None:
    harness = ServiceHarness()

    harness.call("POST", "/v1/forwards/prepare", body=b"x")
    harness.call("POST", "/v1/forwards/prepare", payload={**VALID_PREPARE, "url": "http://x"})
    harness.call("GET", "/v1/nope")
    harness.call("DELETE", "/v1/forwards/d-1")

    assert harness.backends.platform_send_count == 0
    assert harness.backends.calls == [], "负例不得抵达任何后端边界"


def test_准备挑战窗口取自共享层常量而非写死字面量(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """常量漂移防线：替身必须复用 `shared/protocol.py` 的 `PREPARE_CHALLENGE_SECONDS`。"""

    import asyncio

    import service.backends as backends_module
    import shared.protocol as protocol

    assert backends_module.PREPARE_CHALLENGE_SECONDS is protocol.PREPARE_CHALLENGE_SECONDS, (
        "准备挑战窗口必须与共享层同源，服务端不得另写一份（第 3–5 组还要复用该常量）"
    )

    monkeypatch.setattr(backends_module, "PREPARE_CHALLENGE_SECONDS", 7.5)
    backends = backends_module.NonSendingBackends()
    request = protocol.PrepareRequest(
        request_id="req-1",
        session_id="sess-1",
        policy_generation=1,
        source_group_id="10001",
        source_message_id="m-source-1",
        target_group_id="20002",
    )

    result = asyncio.run(backends.prepare(request, subject="client-test"))

    assert result.challenge_expires_in_seconds == 7.5, (
        "准备响应必须读共享层常量；写死 30.0 时这里仍会得到 30.0"
    )


def test_未登记后端适配器的路由明确报错而不静默查询(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新增路由漏登记适配器时必须显式失败，绝不静默落到 `query` 之类的默认后端调用。"""

    import service.http_api as http_api

    harness = ServiceHarness()
    ghost = http_api._Route(
        "POST", ("v1", "forwards", "{delivery_id}", "ghost"), "ghost_backend_method"
    )
    monkeypatch.setattr(http_api, "_ROUTES", (*http_api._ROUTES, ghost))

    response = harness.call("POST", "/v1/forwards/d-1/ghost", payload={"protocol_version": 1})

    assert response.status == 500, response
    assert response.payload["error"] == "unimplemented_backend", response.payload
    assert harness.backends.calls == [], "未登记适配器不得触达任何后端调用"
