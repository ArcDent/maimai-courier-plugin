"""5.1 认证客户端会话与心跳过期（真实 HTTP 路由 + 认证中间件 + 注入时钟）。

判据来自 design D4/D5 与 tasks 5.1：

- `POST /v1/sessions` 建立会话，返回绑定账号、账本身份与服务启动代次；
- 同一认证主体同时只允许一个活动实例：新实例替换旧实例，旧实例不能继续用授权；
- 心跳只续期（5 秒间隔 / 15 秒失联到期），不发送、不更新权限；
- 旧服务代次（重启）与旧实例（换会话、换账号）不能继续使用会话与未消费授权；
- 全部超时用注入时钟推进，不做任何真实等待。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from shared.protocol import HEARTBEAT_INTERVAL_SECONDS, SESSION_TIMEOUT_SECONDS
from tests.service_support import AuthorizedService, error_of, launch_authorized_service, result_of


def _service(tmp_path: Path) -> AuthorizedService:
    """装配会话夹具（真实账本 + 模拟 OneBot + 内存 HTTP）。"""

    return launch_authorized_service(tmp_path / "state")


def test_建立会话返回绑定账号与账本身份(tmp_path: Path) -> None:
    harness = _service(tmp_path)

    result = result_of(harness.open_session())

    assert result["account_id"] == harness.account_id
    assert result["ledger_identity"] == asyncio.run(harness.ledger.identity()) != ""
    assert result["server_generation"], result
    assert result["protocol_version"] == 1
    assert result["heartbeat_interval_seconds"] == HEARTBEAT_INTERVAL_SECONDS == 5.0
    assert result["session_timeout_seconds"] == SESSION_TIMEOUT_SECONDS == 15.0
    assert harness.onebot.send_count == 0, "建立会话不得产生任何平台发送"


def test_缺少凭证不能建立会话(tmp_path: Path) -> None:
    harness = _service(tmp_path)

    unauthenticated = harness.call(
        "POST",
        "/v1/sessions",
        payload={"protocol_version": 1, "client_identity": "maibot-plugin"},
        authenticated=False,
    )

    assert unauthenticated.status == 401, unauthenticated.payload
    assert harness.onebot.send_count == 0


def test_同一主体只允许一个活动实例且旧实例失效(tmp_path: Path) -> None:
    harness = _service(tmp_path)

    first = result_of(harness.open_session())
    second = result_of(harness.open_session())

    assert first["session_id"] != second["session_id"], "新实例必须得到新的会话 ID"
    old_heartbeat = harness.heartbeat(str(first["session_id"]))
    assert old_heartbeat.status == 401, old_heartbeat.payload
    assert error_of(old_heartbeat)[0] == "session_invalid"
    old_policy = harness.update_policy(str(first["session_id"]), generation=2)
    assert old_policy.status == 401, old_policy.payload
    assert harness.heartbeat(str(second["session_id"])).status == 200, "新实例必须保持可用"
    assert harness.onebot.send_count == 0


def test_心跳只续期不发送也不更新权限(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = result_of(harness.open_session())["session_id"]
    accepted = harness.update_policy(str(session_id), generation=1)
    assert accepted.status == 200, accepted.payload

    before = dict(harness.onebot.query_counts)
    alive = result_of(harness.heartbeat(str(session_id)))

    assert alive["session_id"] == session_id
    assert alive["expires_in_seconds"] == SESSION_TIMEOUT_SECONDS
    assert harness.onebot.send_count == 0, "心跳不得发送任何消息"
    assert harness.onebot.query_counts == before, "心跳只续期，不查询网关也不更新权限"

    # 心跳不更新权限：当前权限代次仍是 1，同一代次重复提交按旧代次拒绝
    again = harness.update_policy(str(session_id), generation=1)
    assert again.status == 409, again.payload
    assert error_of(again)[0] == "stale_policy"


def test_失联十五秒到期且过期会话不能复活(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = result_of(harness.open_session())["session_id"]

    harness.clock.advance(SESSION_TIMEOUT_SECONDS - 0.1)
    assert harness.heartbeat(str(session_id)).status == 200, "14.9 秒内必须仍可用"

    harness.clock.advance(15.2)
    expired = harness.heartbeat(str(session_id))
    assert expired.status == 401, expired.payload
    assert error_of(expired)[0] == "session_invalid"
    assert harness.update_policy(str(session_id), generation=2).status == 401, (
        "到期会话不得复活或更新权限"
    )
    assert harness.open_session().status == 200, "到期只影响旧会话，新会话可以重建"
    assert harness.onebot.send_count == 0, "全程没有任何平台发送"


def test_心跳从心跳时刻重新计算有效期(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = result_of(harness.open_session())["session_id"]

    harness.clock.advance(14.0)
    assert harness.heartbeat(str(session_id)).status == 200
    harness.clock.advance(14.0)
    assert harness.heartbeat(str(session_id)).status == 200, "心跳必须重新计算 15 秒有效期"

    harness.clock.advance(SESSION_TIMEOUT_SECONDS + 0.1)
    assert harness.heartbeat(str(session_id)).status == 401, "超过 15 秒未心跳即失效"


def test_会话失效后拒绝签发绑定死会话的挑战(tmp_path: Path) -> None:
    from service.sessions import ERROR_SESSION_INVALID, SessionRejection

    harness = _service(tmp_path)
    first = result_of(harness.open_session())
    replaced_session = str(first["session_id"])
    replacement = str(result_of(harness.open_session())["session_id"])

    def issue(session_id: str) -> Any:
        return harness.sessions.issue(
            stage="comment",
            delivery_id="d-1",
            attempt_id="a-1",
            session_id=session_id,
            policy_generation=0,
        )

    replaced = issue(replaced_session)
    assert isinstance(replaced, SessionRejection), "被新实例替换的会话不得再签发挑战"
    assert replaced.error == ERROR_SESSION_INVALID
    assert harness.sessions.pending_authorizations == 0, "拒绝签发不得占用容量"

    harness.deregister(replacement)
    closed = issue(replacement)
    assert isinstance(closed, SessionRejection), "已注销的会话不得再签发挑战"

    expiring = str(result_of(harness.open_session())["session_id"])
    harness.clock.advance(SESSION_TIMEOUT_SECONDS + 0.1)
    expired = issue(expiring)
    assert isinstance(expired, SessionRejection), "已过期的会话不得再签发挑战"

    assert harness.onebot.send_count == 0


def test_换账号使旧会话与未消费授权失效(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    session_id = result_of(harness.open_session())["session_id"]

    harness.onebot.account_id = 99999
    asyncio.run(harness.gateway.connect())

    stale = harness.heartbeat(str(session_id))
    assert stale.status == 401, stale.payload
    assert error_of(stale)[0] == "account_changed"
    blocked = harness.open_session()
    assert blocked.status == 503, blocked.payload
    assert error_of(blocked)[0] == "account_unavailable"

    harness.onebot.account_id = int(harness.account_id)
    asyncio.run(harness.gateway.connect())
    notice = harness.open_session()
    assert notice.status == 401, notice.payload
    assert error_of(notice)[0] == "account_changed", "换回账号同样是一次账号代次变化，必须先失效"
    rebuilt = result_of(harness.open_session())
    assert rebuilt["account_id"] == harness.account_id
    assert harness.heartbeat(str(session_id)).status == 401, "旧会话在换号后不得恢复"
    assert harness.onebot.send_count == 0


def test_服务重启使旧会话与旧代次失效但账本身份不变(tmp_path: Path) -> None:
    harness = _service(tmp_path)
    established = result_of(harness.open_session())
    identity_before = str(established["ledger_identity"])

    harness.restart()
    stale = harness.heartbeat(str(established["session_id"]))
    assert stale.status == 401, stale.payload
    assert error_of(stale)[0] == "session_invalid"

    rebuilt = result_of(harness.open_session())
    assert rebuilt["ledger_identity"] == identity_before, "账本身份必须跨重启稳定"
    assert rebuilt["server_generation"] != established["server_generation"], (
        "重启后服务启动代次必须变化（旧代次的会话与挑战不能继续使用）"
    )
    assert harness.onebot.send_count == 0
