"""第 7.4 组验收：认证、数据最小化与隔离边界（真实路由 + 真实账本 + 模拟网关）。

seam：全部 v1 HTTP 接口（经真实认证中间件与路由）、服务 CLI 输出/子进程、模拟网关与
宿主能力调用记录。插件↔服务端的 HTTP 请求走**内存传输**（`ServiceAppTransport`，
不经真实套接字），账本 CLI 仍是真实子进程；真实子进程 + 真实回环套接字的跨进程覆盖
见 `tests/test_acceptance_processes.py`。覆盖：

- 未认证、跨主体、越权群、任意动作/地址/路径字段、畸形与超长请求、准备过载均安全
  拒绝，且拒绝/查询路径平台发送次数不变；
- 用合成敏感标记验证：凭证、源正文、评论与卡片资源 ID 不进入日志、错误响应或持久
  控制数据（服务账本、迁移/导出/状态 CLI 输出）；
- 临时材料清理（过期）不删除任何投递事实。

全部为本地合成数据；不连接真实 QQ、不读取任何真实运行账本或凭证。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from service.coordinator import MAX_PENDING_PREPARES
from service.http_api import MAX_BODY_BYTES
from shared.protocol import PREPARE_CHALLENGE_SECONDS

from tests.acceptance_support import delivery_query_count, scenario_report
from tests.client_support import (
    DEFAULT_MEDIA_URL,
    make_client_service,
)
from tests.cross_process_support import run_service_cli
from tests.service_support import (
    SECOND_SUBJECT,
    SOURCE_GROUP_ID,
    SOURCE_MESSAGE_ID,
    TARGET_GROUP_ID,
    AuthorizedService,
    authorize_session,
    error_of,
    launch_authorized_service,
    result_of,
    state_ledger_rows,
    store_source_message,
)
from tests.support import (
    forward_node,
    forward_segment,
    image_segment,
    run_bound_forward,
    text_segment,
)

SERVICE_TOKEN = "synthetic-service-token-SENTINEL0001"
"""合成服务凭证：用于验证它不出现在日志/错误响应/持久控制数据里。"""

SECOND_TOKEN = "synthetic-second-token-SENTINEL0002"
"""第二个认证主体的合成凭证。"""

SOURCE_MARKER = "SYNTHETICSOURCEWORD0001"
"""合成源正文标记（不得进入持久控制数据或错误响应）。"""

COMMENT_MARKER = "SYNTHETICCOMMENTWORD0002"
"""合成评论标记（不得进入持久控制数据或错误响应）。"""

CARD_MARKER = "SYNTHETICCARDRES0003"
"""合成卡片资源引用标记（只允许出现在平台发送段里）。"""


def _service(tmp_path: Path) -> AuthorizedService:
    harness = launch_authorized_service(tmp_path / "state")
    store_source_message(
        harness.onebot,
        message_id=SOURCE_MESSAGE_ID,
        group_id=SOURCE_GROUP_ID,
        message=[{"type": "text", "data": {"text": SOURCE_MARKER}}],
    )
    return harness


def _state_bytes(state_dir: Path) -> bytes:
    """服务状态目录下全部文件的字节（持久控制数据的原始观察面）。"""

    chunks: list[bytes] = []
    for path in sorted(state_dir.rglob("*")):
        if path.is_file():
            chunks.append(path.read_bytes())
    return b"\n".join(chunks)


# --------------------------------------------------------------------------- 认证与隔离


def test_未认证跨主体与越权访问全部被拒绝且零发送(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = str(authorize_session(harness))
    prepared = result_of(harness.prepare(session_id, generation=1))
    other_token = harness.add_subject(SECOND_SUBJECT, token=SECOND_TOKEN)
    cases: list[dict[str, Any]] = []

    unauthorised: list[tuple[str, str]] = [
        ("POST", "/v1/sessions"),
        ("POST", f"/v1/sessions/{session_id}/heartbeat"),
        ("PUT", f"/v1/sessions/{session_id}/policy"),
        ("DELETE", f"/v1/sessions/{session_id}"),
        ("POST", "/v1/forwards/prepare"),
        ("POST", f"/v1/forwards/{prepared['delivery_id']}/source"),
        ("POST", f"/v1/forwards/{prepared['delivery_id']}/comment"),
        ("GET", f"/v1/forwards/{prepared['delivery_id']}"),
        ("POST", f"/v1/forwards/{prepared['delivery_id']}/cancel"),
    ]
    for method, path in unauthorised:
        response = harness.call(method, path, payload={"protocol_version": 1}, authenticated=False)
        error, reason = error_of(response)
        cases.append({"case": f"未认证 {method} {path}", "status": response.status, "error": error})
        assert response.status == 401 and error == "unauthorized", (method, path, response.payload)
        assert SERVICE_TOKEN not in str(response.payload), response.payload

    # 跨主体：另一个合法主体既不能读取也不能执行他人投递。
    for method, path, payload in (
        ("GET", f"/v1/forwards/{prepared['delivery_id']}", None),
        (
            "POST",
            f"/v1/forwards/{prepared['delivery_id']}/source",
            {
                "protocol_version": 1,
                "attempt_id": prepared["attempt_id"],
                "session_id": session_id,
                "policy_generation": 1,
                "challenge": prepared["challenge"],
            },
        ),
        (
            "POST",
            f"/v1/forwards/{prepared['delivery_id']}/cancel",
            {"protocol_version": 1, "attempt_id": prepared["attempt_id"]},
        ),
    ):
        response = harness.call(method, path, payload=payload, subject=SECOND_SUBJECT)
        error, _reason = error_of(response)
        cases.append({"case": f"跨主体 {method} {path}", "status": response.status, "error": error})
        assert response.status == 403 and error in (
            "forbidden",
            "delivery_forbidden",
            "session_invalid",
        ), (method, path, response.payload)

    assert harness.onebot.send_count == 0, "认证与隔离用例不得产生任何平台发送"
    assert harness.onebot.send_calls == []
    assert len(cases) == len(unauthorised) + 3, cases
    assert all(case["status"] in (401, 403) for case in cases), cases
    harness.close()


def test_越权群任意字段畸形与超长请求都安全拒绝(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = str(authorize_session(harness))
    baseline = harness.onebot.send_count

    def prepare(**overrides: Any) -> dict[str, Any]:
        payload = {
            "protocol_version": 1,
            "request_id": "req-security",
            "session_id": session_id,
            "policy_generation": 1,
            "source_group_id": SOURCE_GROUP_ID,
            "source_message_id": SOURCE_MESSAGE_ID,
            "target_group_id": TARGET_GROUP_ID,
            "comment": "",
        }
        payload.update(overrides)
        return payload

    rejected: list[dict[str, Any]] = []
    for label, payload, expected_status in (
        ("源群越权", prepare(source_group_id="99999"), 403),
        ("目标群越权", prepare(target_group_id="99998"), 403),
        (
            "任意动作字段",
            prepare(action="send_msg", endpoint="ws://evil.invalid/onebot"),
            400,
        ),
        (
            "任意地址与路径字段",
            prepare(url="http://127.0.0.1:1/onebot", path="/etc/passwd"),
            400,
        ),
        ("受控字段覆盖", prepare(account_id="99999", state_dir="/tmp"), 400),
    ):
        response = harness.call("POST", "/v1/forwards/prepare", payload=payload)
        error, reason = error_of(response)
        rejected.append(
            {"case": label, "status": response.status, "error": error, "reason": reason}
        )
        assert response.status == expected_status, (label, response.payload)
        assert "evil.invalid" not in reason and "/etc/passwd" not in reason, reason

    # 源群与目标群相同：客户许可与服务端上限都允许把源群当目标时，仍在发送前拒绝。
    same_group_harness = launch_authorized_service(
        tmp_path / "same-group-state",
        server_source_groups=(SOURCE_GROUP_ID,),
        server_target_groups=(SOURCE_GROUP_ID, TARGET_GROUP_ID),
    )
    store_source_message(
        same_group_harness.onebot,
        message_id=SOURCE_MESSAGE_ID,
        group_id=SOURCE_GROUP_ID,
        message=[{"type": "text", "data": {"text": SOURCE_MARKER}}],
    )
    same_group_session = str(
        authorize_session(same_group_harness, target_groups=(SOURCE_GROUP_ID,))
    )
    same_group = same_group_harness.call(
        "POST",
        "/v1/forwards/prepare",
        payload={
            "protocol_version": 1,
            "request_id": "req-same-group",
            "session_id": same_group_session,
            "policy_generation": 1,
            "source_group_id": SOURCE_GROUP_ID,
            "source_message_id": SOURCE_MESSAGE_ID,
            "target_group_id": SOURCE_GROUP_ID,
            "comment": "",
        },
    )
    error, reason = error_of(same_group)
    rejected.append(
        {"case": "源群等于目标群", "status": same_group.status, "error": error, "reason": reason}
    )
    assert same_group.status == 400 and error == "target_unavailable", same_group.payload
    assert same_group_harness.onebot.send_count == 0, "源群当目标必须在发送前拒绝"
    same_group_harness.close()

    malformed = harness.call("POST", "/v1/forwards/prepare", body=b"{not json")
    assert error_of(malformed)[0] == "invalid_json", malformed.payload
    not_object = harness.call("POST", "/v1/forwards/prepare", body=b"[1, 2, 3]")
    assert error_of(not_object)[0] == "invalid_field", not_object.payload
    oversized = harness.call(
        "POST", "/v1/forwards/prepare", body=b"{" + b"#" * (MAX_BODY_BYTES + 1) + b"}"
    )
    assert oversized.status == 413 and error_of(oversized)[0] == "body_too_large", oversized.payload
    unknown_path = harness.call("GET", "/v1/并没有这个接口")
    assert unknown_path.status == 404, unknown_path.payload
    wrong_method = harness.call("PATCH", "/v1/forwards/prepare", payload=prepare())
    assert wrong_method.status == 405, wrong_method.payload
    traversal = harness.call("GET", "/v1/forwards/%2E%2E%2Fetc%2Fpasswd")
    assert traversal.status in (400, 404), traversal.payload

    assert harness.onebot.send_count == baseline == 0, "全部负例不得产生任何平台发送"
    assert harness.onebot.send_calls == []
    assert len(rejected) == 6, rejected
    harness.close()


def test_准备过载明确繁忙且已成功记录不被逐出(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = str(authorize_session(harness))
    for index in range(1, MAX_PENDING_PREPARES):
        store_source_message(
            harness.onebot,
            message_id=f"m-cap-{index}",
            group_id=SOURCE_GROUP_ID,
            message=[{"type": "text", "data": {"text": f"容量 {index}"}}],
        )
    for index in range(1, MAX_PENDING_PREPARES):
        result_of(harness.prepare(session_id, generation=1, source_message_id=f"m-cap-{index}"))
    assert harness.coordinator.pending_prepares == MAX_PENDING_PREPARES - 1

    # 恰好用满容量，再请求一条：明确繁忙，零发送。
    full = result_of(harness.prepare(session_id, generation=1))
    assert full["status"] == "prepared", full
    store_source_message(
        harness.onebot,
        message_id="m-cap-over",
        group_id=SOURCE_GROUP_ID,
        message=[{"type": "text", "data": {"text": "超载"}}],
    )
    busy = harness.prepare(session_id, generation=1, source_message_id="m-cap-over")
    error, reason = error_of(busy)
    assert busy.status == 429 and error == "busy", (busy.status, busy.payload)
    assert "容量" in reason or "繁忙" in reason, reason
    assert harness.onebot.send_count == 0, "准备过载不得产生平台发送"

    executed = result_of(
        harness.execute(
            str(full["delivery_id"]),
            stage="source",
            attempt_id=str(full["attempt_id"]),
            session_id=session_id,
            generation=1,
            challenge=str(full["challenge"]),
        )
    )
    assert executed["status"] == "sent" and harness.onebot.send_count == 1, executed
    repeat = result_of(harness.prepare(session_id, generation=1, request_id="req-after-success"))
    assert repeat["status"] == "already_forwarded", repeat
    assert harness.onebot.send_count == 1, "成功记录不得被容量逐出"
    harness.close()


# --------------------------------------------------------------------------- 数据最小化


def test_敏感标记不进入日志错误响应与持久控制数据(tmp_path: Path) -> None:
    # 卡片资源 ID 用合成标记；源正文与评论各带独立标记。
    fixture = make_client_service(
        tmp_path,
        raw_message=[forward_segment(forward_node(content=[text_segment(SOURCE_MARKER)]))],
        resource_id=CARD_MARKER,
    )
    result, _ = run_bound_forward(
        fixture.plugin, message_id="m-source-1", comment=COMMENT_MARKER, clock=fixture.clock
    )
    report = scenario_report(
        fixture, label="数据最小化·卡片+评论", tool=result, input=CARD_MARKER, record=fixture.record()
    )

    assert result["status"] == "sent", report.summary()
    state_bytes = _state_bytes(fixture.service.state_dir)
    for marker in (SOURCE_MARKER, COMMENT_MARKER, CARD_MARKER):
        assert marker.encode("utf-8") not in state_bytes, (
            "源正文/评论/资源 ID 不得进入持久控制数据",
            marker,
        )
    assert SERVICE_TOKEN.encode("utf-8") not in state_bytes, "凭证不得进入持久控制数据"

    # 平台发送记录必须真实存在（源卡片 + 评论各一次，都落在目标群）。
    assert len(fixture.onebot.send_calls) == 2, fixture.onebot.send_calls
    assert {call.target for call in fixture.onebot.send_calls} == {TARGET_GROUP_ID}, (
        fixture.onebot.send_calls
    )
    texts = [segment for segments in fixture.sent_segments() for segment in segments]
    assert {"type": "forward", "data": {"id": CARD_MARKER}} in texts, texts
    assert {"type": "text", "data": {"text": COMMENT_MARKER}} in texts, texts

    # 客户端提交面与宿主日志不含原文/资源 ID/凭证；评论是唯一允许提交的本次文本参数
    # （工具三参数之一，服务端据此执行评论阶段），但同样不得进入日志或宿主能力记录。
    prepare_body = fixture.transport.body_of("/v1/forwards/prepare")
    rendered_prepare = json.dumps(prepare_body, ensure_ascii=False)
    assert SOURCE_MARKER not in rendered_prepare, "源正文不得由客户端提交"
    assert CARD_MARKER not in rendered_prepare, "卡片资源 ID 不得由客户端提交"
    assert COMMENT_MARKER in rendered_prepare, "评论是本次唯一允许提交的文本参数"
    client_visible = json.dumps(
        {
            "requests": fixture.transport.paths(),
            "logs": fixture.ctx.logger.records,
            "host": {
                "chat_calls": fixture.ctx.chat.calls,
                "message_calls": fixture.ctx.message.calls,
                "old_send_calls": fixture.ctx.send.calls,
            },
        },
        ensure_ascii=False,
    )
    for marker in (SOURCE_MARKER, COMMENT_MARKER, CARD_MARKER):
        assert marker not in client_visible, ("敏感标记不得进入客户端日志或宿主记录", marker)
    assert SERVICE_TOKEN not in client_visible, "凭证不得进入日志或请求记录"

    # 只读查询不返回任何正文或评论，且查询本身不发送。
    query_sends = fixture.onebot.send_count
    record = fixture.record()
    assert record is not None and record["status"] == "sent", record
    rendered_query = json.dumps(record, ensure_ascii=False)
    for marker in (SOURCE_MARKER, COMMENT_MARKER, CARD_MARKER):
        assert marker not in rendered_query, ("查询结果不得返回正文/评论/资源 ID", marker)
    assert fixture.onebot.send_count == query_sends == 2, "查询不得产生新的发送"

    # 服务 CLI（status / export）的输出同样不含敏感标记；导出失败不改写账本。
    status_code, status_out, status_err = asyncio.run(
        run_service_cli("ledger", "status", "--state-dir", str(fixture.service.state_dir))
    )
    assert status_code == 0, status_out + status_err
    exported = tmp_path / "compat.sqlite3"
    fixture.close()
    export_code, export_out, export_err = asyncio.run(
        run_service_cli(
            "ledger", "export", "--state-dir", str(fixture.service.state_dir), "--output", str(exported)
        )
    )
    assert export_code == 0, export_out + export_err
    combined = status_out + status_err + export_out + export_err
    for marker in (SOURCE_MARKER, COMMENT_MARKER, CARD_MARKER, SERVICE_TOKEN):
        assert marker not in combined, ("服务 CLI 输出不得包含敏感标记", marker, combined)
    for marker in (SOURCE_MARKER, COMMENT_MARKER, CARD_MARKER):
        assert marker.encode("utf-8") not in exported.read_bytes(), (
            "兼容导出不得携带源正文/评论/资源 ID",
            marker,
        )
    fixture.close()


def test_临时材料过期清理不删除任何投递事实(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = str(authorize_session(harness))
    delivered = result_of(harness.prepare(session_id, generation=1))
    executed = result_of(
        harness.execute(
            str(delivered["delivery_id"]),
            stage="source",
            attempt_id=str(delivered["attempt_id"]),
            session_id=session_id,
            generation=1,
            challenge=str(delivered["challenge"]),
        )
    )
    assert executed["status"] == "sent", executed
    sent_rows = {row[0]: row[1] for row in state_ledger_rows(harness.state_dir)}

    # 再准备若干条（临时材料在内存里），推进到挑战过期后触发一次清理。
    store_source_message(
        harness.onebot,
        message_id="m-expire-1",
        group_id=SOURCE_GROUP_ID,
        message=[{"type": "text", "data": {"text": "临时材料 A"}}],
    )
    result_of(harness.prepare(session_id, generation=1, source_message_id="m-expire-1"))
    assert harness.coordinator.pending_prepares == 1
    for _ in range(7):
        harness.clock.advance(5.0)
        result_of(harness.heartbeat(session_id))
    assert harness.coordinator.pending_prepares == 0, "过期临时材料必须被清理（不长期驻留）"

    after_rows = {row[0]: row[1] for row in state_ledger_rows(harness.state_dir)}
    assert after_rows == sent_rows, ("清理临时材料不得删除投递事实", sent_rows, after_rows)
    record = result_of(harness.query(str(delivered["delivery_id"])))
    assert record["status"] == "sent" and record["target_message_id"], record
    repeat = result_of(harness.prepare(session_id, generation=1, request_id="req-after-expiry"))
    assert repeat["status"] == "already_forwarded", repeat
    assert harness.onebot.send_count == 1, "清理与查询都不得产生新的平台发送"
    harness.close()


# --------------------------------------------------------------------------- 辅助


def test_错账号时关闭发送且零平台调用(tmp_path: Path) -> None:
    """网关实际登录账号与绑定账号不符：准备与执行都拒绝，平台发送次数不变。"""

    harness = _service(tmp_path)
    session_id = str(authorize_session(harness))
    harness.onebot.account_id = 99999  # 网关换号（绑定账号仍是 10000）

    prepared = harness.prepare(session_id, generation=1)
    error, reason = error_of(prepared)
    assert prepared.status in (400, 401, 503), prepared.payload
    assert error in ("account_mismatch", "account_unavailable", "account_changed"), prepared.payload
    assert "不符" in reason or "关闭发送" in reason or "未确认" in reason or "已变化" in reason, reason
    assert harness.onebot.send_count == 0, ("账号不符不得产生平台发送", harness.onebot.send_calls)

    # 换回绑定账号后必须重建服务实例并重新建立会话（旧会话与代次在账号变化时已失效，
    # 绝不自动切换账号）。
    harness.onebot.account_id = 10000
    harness.restart()
    fresh_session = str(authorize_session(harness))
    recovered = result_of(harness.prepare(fresh_session, generation=1))
    assert recovered["status"] == "prepared", recovered
    executed = result_of(
        harness.execute(
            str(recovered["delivery_id"]),
            stage="source",
            attempt_id=str(recovered["attempt_id"]),
            session_id=fresh_session,
            generation=1,
            challenge=str(recovered["challenge"]),
        )
    )
    assert executed["status"] == "sent" and harness.onebot.send_count == 1, executed
    harness.close()


def test_普通图片媒体地址不进入持久控制数据(tmp_path: Path) -> None:
    """补充边界：普通图片的媒体地址不进入服务账本等持久控制数据。"""

    fixture = make_client_service(tmp_path, raw_message=[image_segment()])
    result, _ = run_bound_forward(fixture.plugin, message_id="m-source-1")
    assert result["status"] == "sent", result
    state_bytes = _state_bytes(fixture.service.state_dir)
    assert DEFAULT_MEDIA_URL.encode("utf-8") not in state_bytes, (
        "媒体地址不得进入持久控制数据",
        DEFAULT_MEDIA_URL,
    )
    assert fixture.sent_segments() == [[{"type": "image", "data": {"url": DEFAULT_MEDIA_URL}}]]
    fixture.close()
