"""第 7.2/7.3 组跨进程验收：真实子进程服务、真实客户端子进程与真实回环 OneBot。

覆盖（不得只用同进程模拟）：

- 真实服务子进程（`python -m service start --config …`）→ 真实回环 WebSocket 模拟
  OneBot；真实插件子进程（`python tests/client_process.py`）→ 真实 HTTP 客户端；
- 客户端重启：第二次调用按账本事实阻断重发；
- 客户端子进程被强杀：服务端仍完成已开始的平台调用与落账（任务退出不冒充终态）；
- 服务端在源/评论回执处被强杀：重启后遗留 `sending` 归并为待核实并继续阻断；
- 源成功后未提交的评论：重启后不补发，旧会话/旧挑战不能续用；
- 服务端在评论回执处崩溃：评论结果按待核实，源投递事实保留。

每条场景记录：输入、平台发送内容与次数、服务只读查询/独立账本口径与工具结果。
全部数据为合成值，只连接本机回环，不连接真实 QQ、不发送真实消息。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

pytest.importorskip("aiohttp", reason="跨进程验收用例需要 aiohttp（按需装载）")

import aiohttp  # noqa: E402

from delivery_ledger import DeliveryKey  # noqa: E402
from shared.protocol import delivery_id_for  # noqa: E402

from tests.cross_process_support import (  # noqa: E402
    DEFAULT_ONEBOT_TOKEN,
    DEFAULT_ONEBOT_TOKEN_ENV,
    DEFAULT_SERVICE_TOKEN,
    DEFAULT_SERVICE_TOKEN_ENV,
    MockOneBotServer,
    ServiceProcess,
    http_json,
    kill_process,
    ledger_identity_from,
    run_client_process,
    run_service_cli,
    source_message,
    start_client_process,
    start_service_process,
    wait_for,
)

SOURCE_TEXT = "跨进程验收的源消息"
TARGET_GROUP_ID = "20002"
SOURCE_GROUP_ID = "10001"
ACCOUNT_ID = "10000"
MIGRATION_CONFIRMATION = "e2e-confirmation"


def _delivery_id(message_id: str) -> str:
    """四字段投递身份摘要（与服务端账本同一公开算法）。"""

    return delivery_id_for(
        DeliveryKey(
            account_id=ACCOUNT_ID,
            source_group_id=SOURCE_GROUP_ID,
            source_message_id=message_id,
            target_group_id=TARGET_GROUP_ID,
        )
    )


def _store_source(mock: MockOneBotServer, message_id: str) -> None:
    mock.store(
        source_message(
            message_id=message_id,
            group_id=SOURCE_GROUP_ID,
            message=[{"type": "text", "data": {"text": SOURCE_TEXT}}],
        )
    )


async def _fresh_state(state_dir: Path) -> str:
    """真实 CLI 新装初始化，返回账本身份（跨进程解析 CLI 输出）。"""

    code, out, err = await run_service_cli("ledger", "fresh", "--state-dir", str(state_dir))
    assert code == 0, f"ledger fresh 退出码 {code}：{out}\n{err}"
    return ledger_identity_from(out)


def _job(
    tmp_path: Path,
    *,
    identity: str,
    port: int,
    message_id: str,
    comment: str = "",
    sequence: int = 0,
    raw_message: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """客户端子进程作业描述（凭证只经环境变量传递，不写进作业文件）。"""

    return {
        "job_path": str(tmp_path / f"job-{message_id}-{sequence}.json"),
        "endpoint": f"http://127.0.0.1:{port}",
        "token_env_var": DEFAULT_SERVICE_TOKEN_ENV,
        "expected_ledger_id": identity,
        "migration_confirmation": MIGRATION_CONFIRMATION,
        "data_dir": str(tmp_path / f"client-{message_id}-{sequence}"),
        "message_id": message_id,
        "comment": comment,
        "raw_message": [dict(segment) for segment in (raw_message or ({"type": "text", "data": SOURCE_TEXT},))],
        "request_timeout_seconds": 30.0,
    }


async def _query(
    session: aiohttp.ClientSession, service: ServiceProcess, message_id: str
) -> tuple[int, Mapping[str, Any]]:
    return await http_json(
        session,
        "GET",
        f"{service.base_url}/v1/forwards/{_delivery_id(message_id)}",
        token=DEFAULT_SERVICE_TOKEN,
    )


async def _wait_ledger(
    session: aiohttp.ClientSession,
    service: ServiceProcess,
    message_id: str,
    *,
    expected: str,
) -> Mapping[str, Any]:
    """轮询只读查询直到投递状态到达期望值（真实短间隔；不做长时间等待）。"""

    latest: Mapping[str, Any] = {}
    for _ in range(400):
        status, payload = await _query(session, service, message_id)
        if status == 200:
            latest = payload.get("result") or {}
            if latest.get("status") == expected:
                return latest
        await asyncio.sleep(0.02)
    raise AssertionError(f"投递 {message_id} 未在等待期内到达 {expected}：{latest}")


def _env() -> dict[str, str]:
    """客户端子进程环境：只注入合成服务凭证。"""

    return {DEFAULT_SERVICE_TOKEN_ENV: DEFAULT_SERVICE_TOKEN}


# --------------------------------------------------------------------------- 7.3 客户端重启


def test_真实子进程贯通一次转发且客户端重启后阻断重发(tmp_path: Path) -> None:
    async def scenario() -> dict[str, Any]:
        mock = await MockOneBotServer().start()
        _store_source(mock, "m-source-1")
        try:
            state_dir = tmp_path / "state"
            identity = await _fresh_state(state_dir)
            service = await start_service_process(
                state_dir=state_dir, onebot_endpoint=mock.endpoint, ledger_id=identity
            )
            try:
                first = await run_client_process(
                    _job(tmp_path, identity=identity, port=service.port, message_id="m-source-1"),
                    env_extra=_env(),
                )
                async with aiohttp.ClientSession() as session:
                    record = await _wait_ledger(session, service, "m-source-1", expected="sent")
                second = await run_client_process(
                    _job(
                        tmp_path,
                        identity=identity,
                        port=service.port,
                        message_id="m-source-1",
                        sequence=1,
                    ),
                    env_extra=_env(),
                )
            finally:
                exit_code = await service.stop()
            return {
                "first": first,
                "second": second,
                "record": record,
                "sends": mock.sends(),
                "send_count": mock.send_count,
                "exit_code": exit_code,
            }
        finally:
            await mock.stop()

    outcome = asyncio.run(scenario())
    first = outcome["first"]["result"]
    second = outcome["second"]["result"]

    assert first["status"] == "sent" and first["success"] is True, first
    assert outcome["sends"] == [[{"type": "text", "data": {"text": SOURCE_TEXT}}]], outcome["sends"]
    assert outcome["record"]["status"] == "sent" and outcome["record"]["target_message_id"], outcome
    assert second["status"] == "already_forwarded" and second["success"] is True, second
    assert outcome["send_count"] == 1, "客户端重启后的重复调用不得再发送"
    assert outcome["first"]["host"]["old_send_calls"] == [], "宿主旧发送通道必须零调用"
    assert outcome["exit_code"] == 0, "SIGTERM 后服务端应有界退出并以 0 结束"
    assert DEFAULT_SERVICE_TOKEN not in str(outcome["first"]["host"]), (
        "服务凭证不得出现在客户端进程报告或日志里",
        outcome["first"]["host"],
    )


# --------------------------------------------------------------------------- 7.2 客户端强杀


def test_客户端子进程强杀后服务端仍完成平台调用与落账(tmp_path: Path) -> None:
    async def scenario() -> dict[str, Any]:
        mock = await MockOneBotServer().start()
        _store_source(mock, "m-source-1")
        try:
            state_dir = tmp_path / "state"
            identity = await _fresh_state(state_dir)
            service = await start_service_process(
                state_dir=state_dir, onebot_endpoint=mock.endpoint, ledger_id=identity
            )
            try:
                mock.hold("send_msg")
                process = await start_client_process(
                    _job(tmp_path, identity=identity, port=service.port, message_id="m-source-1"),
                    env_extra=_env(),
                )
                await wait_for(lambda: mock.send_count == 1)
                assert process.returncode is None, "客户端此刻仍应在等待响应"
                await kill_process(process)
                mock.release()
                async with aiohttp.ClientSession() as session:
                    record = await _wait_ledger(session, service, "m-source-1", expected="sent")
                    repeat = await run_client_process(
                        _job(
                            tmp_path,
                            identity=identity,
                            port=service.port,
                            message_id="m-source-1",
                            sequence=1,
                        ),
                        env_extra=_env(),
                    )
            finally:
                await service.stop()
            return {
                "record": record,
                "repeat": repeat,
                "send_count": mock.send_count,
                "sends": mock.sends(),
            }
        finally:
            await mock.stop()

    outcome = asyncio.run(scenario())

    assert outcome["send_count"] == 1, "被强杀的客户端不得触发重发"
    assert outcome["sends"] == [[{"type": "text", "data": {"text": SOURCE_TEXT}}]], outcome["sends"]
    assert outcome["record"]["status"] == "sent" and outcome["record"]["target_message_id"], (
        "已开始的平台调用必须完成并落账（任务退出不冒充平台终态）",
        outcome["record"],
    )
    assert outcome["repeat"]["result"]["status"] == "already_forwarded", outcome["repeat"]
    assert outcome["repeat"]["host"]["old_send_calls"] == []


# --------------------------------------------------------------------------- 7.3 服务端崩溃


def test_服务端崩溃遗留的源占位重启后按待核实并阻断重发(tmp_path: Path) -> None:
    async def scenario() -> dict[str, Any]:
        mock = await MockOneBotServer().start()
        _store_source(mock, "m-source-1")
        _store_source(mock, "m-source-2")
        try:
            state_dir = tmp_path / "state"
            identity = await _fresh_state(state_dir)
            service = await start_service_process(
                state_dir=state_dir, onebot_endpoint=mock.endpoint, ledger_id=identity
            )
            try:
                succeeded = await run_client_process(
                    _job(tmp_path, identity=identity, port=service.port, message_id="m-source-1"),
                    env_extra=_env(),
                )
                mock.hold("send_msg")
                crashed_client = await start_client_process(
                    _job(tmp_path, identity=identity, port=service.port, message_id="m-source-2"),
                    env_extra=_env(),
                )
                await wait_for(lambda: mock.send_count == 2)
                await service.kill()  # 平台已收到发送、服务端尚未取得回执
                mock.release()
                await kill_process(crashed_client)
            finally:
                if service.alive:
                    await service.stop()

            restarted = await start_service_process(
                state_dir=state_dir, onebot_endpoint=mock.endpoint, ledger_id=identity
            )
            try:
                unknown = await run_client_process(
                    _job(
                        tmp_path,
                        identity=identity,
                        port=restarted.port,
                        message_id="m-source-2",
                        sequence=1,
                    ),
                    env_extra=_env(),
                )
                blocked = await run_client_process(
                    _job(
                        tmp_path,
                        identity=identity,
                        port=restarted.port,
                        message_id="m-source-1",
                        sequence=1,
                    ),
                    env_extra=_env(),
                )
                async with aiohttp.ClientSession() as session:
                    record_unknown = await _query(session, restarted, "m-source-2")
                    record_sent = await _query(session, restarted, "m-source-1")
            finally:
                exit_code = await restarted.stop()
            return {
                "succeeded": succeeded,
                "unknown": unknown,
                "blocked": blocked,
                "record_unknown": record_unknown,
                "record_sent": record_sent,
                "send_count": mock.send_count,
                "exit_code": exit_code,
            }
        finally:
            await mock.stop()

    outcome = asyncio.run(scenario())

    assert outcome["succeeded"]["result"]["status"] == "sent", outcome["succeeded"]
    assert outcome["unknown"]["result"]["status"] == "unknown", (
        "崩溃遗留的在途占位必须按待核实报告",
        outcome["unknown"],
    )
    assert outcome["unknown"]["result"]["sent"] is False, outcome["unknown"]
    assert outcome["blocked"]["result"]["status"] == "already_forwarded", outcome["blocked"]
    assert outcome["send_count"] == 2, ("重启前后都不得重发", outcome["send_count"])
    assert outcome["record_unknown"][0] == 200 and outcome["record_unknown"][1]["result"]["status"] == "unknown", (
        "独立账本口径：源在途占位归并为待核实",
        outcome["record_unknown"],
    )
    assert outcome["record_sent"][1]["result"]["status"] == "sent", outcome["record_sent"]


# --------------------------------------------------------------------------- 7.3 评论不补发


def test_源成功后未提交评论在服务重启后不补发且旧挑战失效(tmp_path: Path) -> None:
    async def scenario() -> dict[str, Any]:
        mock = await MockOneBotServer().start()
        _store_source(mock, "m-source-1")
        try:
            state_dir = tmp_path / "state"
            identity = await _fresh_state(state_dir)
            service = await start_service_process(
                state_dir=state_dir, onebot_endpoint=mock.endpoint, ledger_id=identity
            )
            async with aiohttp.ClientSession() as session:
                status, payload = await http_json(
                    session,
                    "POST",
                    f"{service.base_url}/v1/sessions",
                    token=DEFAULT_SERVICE_TOKEN,
                    payload={"protocol_version": 1, "client_identity": "maibot-plugin"},
                )
                assert status == 200, payload
                session_id = payload["result"]["session_id"]
                status, payload = await http_json(
                    session,
                    "PUT",
                    f"{service.base_url}/v1/sessions/{session_id}/policy",
                    token=DEFAULT_SERVICE_TOKEN,
                    payload={
                        "protocol_version": 1,
                        "policy_generation": 1,
                        "enabled": True,
                        "source_groups": [SOURCE_GROUP_ID],
                        "target_groups": [TARGET_GROUP_ID],
                    },
                )
                assert status == 200, payload
                status, payload = await http_json(
                    session,
                    "POST",
                    f"{service.base_url}/v1/forwards/prepare",
                    token=DEFAULT_SERVICE_TOKEN,
                    payload={
                        "protocol_version": 1,
                        "request_id": "req-e2e-1",
                        "session_id": session_id,
                        "policy_generation": 1,
                        "source_group_id": SOURCE_GROUP_ID,
                        "source_message_id": "m-source-1",
                        "target_group_id": TARGET_GROUP_ID,
                        "comment": "未提交的评论",
                    },
                )
                assert status == 200, payload
                prepared = payload["result"]
                status, payload = await http_json(
                    session,
                    "POST",
                    f"{service.base_url}/v1/forwards/{prepared['delivery_id']}/source",
                    token=DEFAULT_SERVICE_TOKEN,
                    payload={
                        "protocol_version": 1,
                        "attempt_id": prepared["attempt_id"],
                        "session_id": session_id,
                        "policy_generation": 1,
                        "challenge": prepared["challenge"],
                    },
                )
                assert status == 200 and payload["result"]["status"] == "sent", payload
                comment_challenge = payload["result"]["comment_challenge"]
                assert comment_challenge, payload
            await service.stop()

            restarted = await start_service_process(
                state_dir=state_dir, onebot_endpoint=mock.endpoint, ledger_id=identity
            )
            try:
                async with aiohttp.ClientSession() as session:
                    status, payload = await http_json(
                        session,
                        "POST",
                        f"{restarted.base_url}/v1/forwards/{prepared['delivery_id']}/comment",
                        token=DEFAULT_SERVICE_TOKEN,
                        payload={
                            "protocol_version": 1,
                            "attempt_id": prepared["attempt_id"],
                            "session_id": session_id,
                            "policy_generation": 1,
                            "challenge": comment_challenge,
                        },
                    )
                    old_challenge_status = status
                    old_challenge_error = str(payload.get("error") or "")
                    repeat = await run_client_process(
                        _job(
                            tmp_path,
                            identity=identity,
                            port=restarted.port,
                            message_id="m-source-1",
                            sequence=2,
                        ),
                        env_extra=_env(),
                    )
                    record = await _query(session, restarted, "m-source-1")
            finally:
                exit_code = await restarted.stop()
            return {
                "old_challenge_status": old_challenge_status,
                "old_challenge_error": old_challenge_error,
                "repeat": repeat,
                "record": record,
                "send_count": mock.send_count,
                "sends": mock.sends(),
                "exit_code": exit_code,
            }
        finally:
            await mock.stop()

    outcome = asyncio.run(scenario())

    assert outcome["send_count"] == 1, ("源成功后未提交的评论不得补发", outcome["sends"])
    assert outcome["old_challenge_status"] != 200, (
        "服务重启后旧会话/旧挑战必须失效",
        outcome["old_challenge_status"],
        outcome["old_challenge_error"],
    )
    assert outcome["old_challenge_error"] in (
        "session_invalid",
        "expired_authorization",
        "execution_window_expired",
        "not_found",
        "forbidden",
    ), outcome
    assert outcome["repeat"]["result"]["status"] == "already_forwarded", outcome["repeat"]
    assert outcome["repeat"]["result"]["sent"] is True, outcome["repeat"]
    assert outcome["repeat"]["result"]["comment_status"] in ("", "failed", "unknown"), outcome["repeat"]
    assert outcome["record"][1]["result"]["comment_status"] == "skipped", (
        "未提交评论在恢复时必须登记为跳过（不形成待发送队列）",
        outcome["record"],
    )
    assert outcome["exit_code"] == 0


# --------------------------------------------------------------------------- 7.2 评论回执崩溃


def test_服务端在评论回执处崩溃后重启不补发评论(tmp_path: Path) -> None:
    async def scenario() -> dict[str, Any]:
        mock = await MockOneBotServer().start()
        _store_source(mock, "m-source-1")
        try:
            state_dir = tmp_path / "state"
            identity = await _fresh_state(state_dir)
            service = await start_service_process(
                state_dir=state_dir, onebot_endpoint=mock.endpoint, ledger_id=identity
            )
            process = await start_client_process(
                _job(
                    tmp_path,
                    identity=identity,
                    port=service.port,
                    message_id="m-source-1",
                    comment="被崩溃打断的评论",
                ),
                env_extra=_env(),
            )
            await wait_for(lambda: mock.send_count == 1)
            mock.hold("send_msg")  # 下一次发送（评论）先记录、回执悬停
            await wait_for(lambda: mock.send_count == 2)
            await service.kill()
            mock.release()
            await kill_process(process)

            restarted = await start_service_process(
                state_dir=state_dir, onebot_endpoint=mock.endpoint, ledger_id=identity
            )
            try:
                repeat = await run_client_process(
                    _job(
                        tmp_path,
                        identity=identity,
                        port=restarted.port,
                        message_id="m-source-1",
                        comment="不补发",
                        sequence=3,
                    ),
                    env_extra=_env(),
                )
                async with aiohttp.ClientSession() as session:
                    record = await _query(session, restarted, "m-source-1")
            finally:
                exit_code = await restarted.stop()
            return {
                "repeat": repeat,
                "record": record,
                "send_count": mock.send_count,
                "sends": mock.sends(),
                "exit_code": exit_code,
            }
        finally:
            await mock.stop()

    outcome = asyncio.run(scenario())

    assert outcome["send_count"] == 2, ("评论平台调用已发生：不得自动补发", outcome["sends"])
    assert outcome["sends"][0] == [{"type": "text", "data": {"text": SOURCE_TEXT}}], outcome["sends"]
    assert outcome["sends"][1] == [{"type": "text", "data": {"text": "被崩溃打断的评论"}}], outcome["sends"]
    assert outcome["record"][1]["result"]["status"] == "sent", (
        "源投递事实必须保留",
        outcome["record"],
    )
    assert outcome["record"][1]["result"]["comment_status"] == "unknown", (
        "评论在途占位必须归并为待核实（可能已发）",
        outcome["record"],
    )
    assert outcome["repeat"]["result"]["status"] == "already_forwarded", outcome["repeat"]
    assert outcome["repeat"]["result"]["comment_status"] in ("failed", "unknown"), outcome["repeat"]


def test_错误OneBot凭证被拒绝且正确凭证才就绪(tmp_path: Path) -> None:
    """设施自检：错误 OneBot 凭证连接被拒（不进入就绪）；凭证值不进入服务日志。"""

    async def scenario() -> dict[str, Any]:
        mock = await MockOneBotServer().start()
        try:
            state_dir = tmp_path / "state"
            identity = await _fresh_state(state_dir)
            rejected: str = ""
            try:
                wrong = await start_service_process(
                    state_dir=state_dir,
                    onebot_endpoint=mock.endpoint,
                    ledger_id=identity,
                    onebot_token="wrong-token",
                    onebot_token_env=DEFAULT_ONEBOT_TOKEN_ENV,
                    ready_timeout=6.0,
                )
            except AssertionError as error:
                rejected = str(error)
            else:
                await wrong.kill()
                raise AssertionError("错误 OneBot 凭证不得进入就绪状态")
            accepted = await start_service_process(
                state_dir=state_dir,
                onebot_endpoint=mock.endpoint,
                ledger_id=identity,
                onebot_token=DEFAULT_ONEBOT_TOKEN,
                onebot_token_env=DEFAULT_ONEBOT_TOKEN_ENV,
            )
            try:
                stderr = accepted.stderr_text()
            finally:
                await accepted.stop()
            return {"rejected": rejected, "stderr": stderr}
        finally:
            await mock.stop()

    outcome = asyncio.run(scenario())

    assert outcome["rejected"], outcome
    assert "提前退出" in outcome["rejected"] or "未在期限内就绪" in outcome["rejected"], outcome
    assert DEFAULT_ONEBOT_TOKEN not in outcome["stderr"], ("连接凭证不得写入服务日志", outcome["stderr"])
    assert "wrong-token" not in outcome["stderr"], ("错误凭证值同样不得回显", outcome["stderr"])
