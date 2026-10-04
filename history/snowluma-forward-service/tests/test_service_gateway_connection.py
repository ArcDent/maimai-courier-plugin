"""4.1 固定 OneBot 连接与账号就绪检查。

判据（tasks.md 4.1 与 design D5/D10）：

- 连接只使用受控地址和服务端凭证；地址与凭证环境变量名只来自服务端配置，
  凭证值只来自环境变量；
- 实际账号与绑定账号不符、或无法确认时不进入可发送状态（发送步骤明确拒绝，
  不调用任何平台发送）；
- 重连换号使未消费授权失效（账号代次变化是给第 5 组的失效信号）、不自动切换
  账号、不修改已保存回执；
- `python -m service start` 在传输接通后如实报告连接/账号就绪状态。

对端一律是 2.6 的模拟 OneBot（内存），不连接任何真实地址。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from service.cli import EXIT_CONFIG, EXIT_UNAVAILABLE, ServiceCommandRunner, main
from service.config import ConfigUnavailable, ServiceConfig, validate_service_config
from service.gateway import (
    ERROR_ACCOUNT_MISMATCH,
    ERROR_ACCOUNT_UNCONFIRMED,
    ERROR_CONNECTION_UNAVAILABLE,
    MAX_NICKNAME_LENGTH,
    AccountReady,
    AccountUnavailable,
    SnowLumaGateway,
    canonical_identifier,
)
from service.ledger_migration import FreshOutcome, initialize_fresh
from service.onebot import OneBotConnection, OneBotUnresolved, resolve_onebot_connection
from shared.send_steps import CommentSendRequest
from tests.service_support import (
    BOUND_ACCOUNT_ID,
    SOURCE_GROUP_ID,
    TARGET_GROUP_ID,
    FakeClock,
    FakeOneBot,
)

SERVICE_TOKEN_ENV_VAR = "MC_TEST_SERVICE_TOKEN"
"""服务端 HTTP 凭证环境变量名（合成名，值为合成值）。"""

ONEBOT_TOKEN_ENV_VAR = "MC_TEST_ONEBOT_TOKEN"
"""OneBot 连接凭证环境变量名（合成名，值为合成值）。"""

SERVICE_TOKEN_VALUE = "synthetic-service-token"
ONEBOT_TOKEN_VALUE = "synthetic-onebot-token"

ONEBOT_ENDPOINT = "ws://127.0.0.1:3001/onebot"
"""受控连接地址（合成回环地址；测试不真正连接它）。"""


def _gateway(onebot: FakeOneBot, *, account_id: str = BOUND_ACCOUNT_ID) -> SnowLumaGateway:
    return SnowLumaGateway(transport=onebot, account_id=account_id, clock=FakeClock())


def test_连接只查询绑定账号并进入可发送状态() -> None:
    onebot = FakeOneBot()
    gateway = _gateway(onebot)

    ready = asyncio.run(gateway.connect())

    assert isinstance(ready, AccountReady), ready
    assert ready.account_id == BOUND_ACCOUNT_ID
    assert gateway.ready == ready, "就绪状态必须可由只读属性复核"
    assert onebot.query_counts["get_login_info"] == 1, "就绪检查应只查询一次登录信息"


def test_就绪检查本身不发送任何消息() -> None:
    onebot = FakeOneBot()
    gateway = _gateway(onebot)

    asyncio.run(gateway.connect())

    assert onebot.send_calls == [], "账号就绪检查不得产生任何平台发送"
    assert onebot.send_count == 0


def test_实际账号不符时不进入可发送状态且拒绝发送() -> None:
    onebot = FakeOneBot(account_id=99999)
    gateway = _gateway(onebot)

    outcome = asyncio.run(gateway.connect())

    assert isinstance(outcome, AccountUnavailable), outcome
    assert outcome.error == ERROR_ACCOUNT_MISMATCH
    assert gateway.ready is None, "账号不符时不得保留可发送状态"

    result = asyncio.run(
        gateway.send_comment(
            CommentSendRequest(
                delivery_id="d-1",
                attempt_id="a-1",
                target_group_id=TARGET_GROUP_ID,
                comment="你好",
            )
        )
    )

    assert result.status == "failed", result
    assert result.status != "unknown", "账号不符时是可证明的未发送，不是待核实"
    assert onebot.send_count == 0, "账号不符时不得调用平台发送"


def test_账号无法确认或连接不可用都不进入可发送状态() -> None:
    unconfirmable = FakeOneBot()
    unconfirmable.fail_next(
        "get_login_info", {"status": "failed", "retcode": 100, "message": "login info unavailable"}
    )
    outcome = asyncio.run(_gateway(unconfirmable).connect())
    assert isinstance(outcome, AccountUnavailable), outcome
    assert outcome.error == ERROR_ACCOUNT_UNCONFIRMED

    no_user_id = FakeOneBot()
    no_user_id.fail_next(
        "get_login_info", {"status": "ok", "retcode": 0, "data": {"nickname": "无账号"}}
    )
    outcome = asyncio.run(_gateway(no_user_id).connect())
    assert isinstance(outcome, AccountUnavailable), outcome
    assert outcome.error == ERROR_ACCOUNT_UNCONFIRMED

    disconnected = FakeOneBot()
    disconnected.disconnect()
    outcome = asyncio.run(_gateway(disconnected).connect())
    assert isinstance(outcome, AccountUnavailable), outcome
    assert outcome.error == ERROR_CONNECTION_UNAVAILABLE
    assert disconnected.send_count == 0


def test_重连换号使未消费授权失效且不修改绑定与已存回执() -> None:
    onebot = FakeOneBot()
    gateway = _gateway(onebot)

    assert isinstance(asyncio.run(gateway.connect()), AccountReady)
    generation = gateway.account_generation
    receipts_before = dict(onebot.messages)

    onebot.account_id = 20002
    outcome = asyncio.run(gateway.connect())

    assert isinstance(outcome, AccountUnavailable), "换号后必须报告账号不符"
    assert outcome.error == ERROR_ACCOUNT_MISMATCH
    assert gateway.ready is None
    assert gateway.account_generation != generation, "换号必须使旧账号代次失效"
    assert gateway.bound_account_id == BOUND_ACCOUNT_ID, "不得自动切换绑定账号"
    assert dict(onebot.messages) == receipts_before, "换号不得修改已保存回执"

    result = asyncio.run(
        gateway.send_comment(
            CommentSendRequest(
                delivery_id="d-1",
                attempt_id="a-1",
                target_group_id=TARGET_GROUP_ID,
                comment="你好",
            )
        )
    )
    assert result.status == "failed", result
    assert onebot.send_count == 0, "换号后不得继续发送"


def test_连接地址与凭证只来自受控配置与环境变量() -> None:
    config = ServiceConfig(
        account_id=BOUND_ACCOUNT_ID,
        state_dir="/tmp/state",
        client_identity="client-test",
        onebot_endpoint=ONEBOT_ENDPOINT,
        onebot_token_env_var=ONEBOT_TOKEN_ENV_VAR,
    )

    connection = resolve_onebot_connection(config, {ONEBOT_TOKEN_ENV_VAR: ONEBOT_TOKEN_VALUE})
    assert isinstance(connection, OneBotConnection), connection
    assert connection.endpoint == ONEBOT_ENDPOINT, "连接地址只取受控配置"
    assert ONEBOT_TOKEN_VALUE not in repr(connection), "连接描述不得回显凭证值"

    missing = resolve_onebot_connection(config, {})
    assert isinstance(missing, OneBotUnresolved), missing
    assert missing.config_failure is True, "凭证缺失属配置级失败"

    unconfigured = resolve_onebot_connection(
        ServiceConfig(account_id=BOUND_ACCOUNT_ID, state_dir="/tmp/state", client_identity="c"),
        {ONEBOT_TOKEN_ENV_VAR: ONEBOT_TOKEN_VALUE},
    )
    assert isinstance(unconfigured, OneBotUnresolved), unconfigured
    assert unconfigured.config_failure is False, "未配置地址属尚未就绪，不是配置拼写错误"


def test_配置校验拒绝非法OneBot地址与凭证变量名() -> None:
    base: dict[str, Any] = {
        "account_id": BOUND_ACCOUNT_ID,
        "state_dir": "/tmp/state",
        "client_identity": "client-test",
    }
    plaintext = validate_service_config(
        ServiceConfig(**base, onebot_endpoint="http://127.0.0.1:3001", onebot_token_env_var=ONEBOT_TOKEN_ENV_VAR)
    )
    assert isinstance(plaintext, ConfigUnavailable), plaintext
    assert "ws" in plaintext.reason

    bad_var = validate_service_config(
        ServiceConfig(**base, onebot_endpoint=ONEBOT_ENDPOINT, onebot_token_env_var="BAD NAME")
    )
    assert isinstance(bad_var, ConfigUnavailable), bad_var

    no_credential = validate_service_config(
        ServiceConfig(**base, onebot_endpoint=ONEBOT_ENDPOINT)
    )
    assert isinstance(no_credential, ConfigUnavailable), no_credential

    assert (
        validate_service_config(
            ServiceConfig(**base, onebot_endpoint=ONEBOT_ENDPOINT, onebot_token_env_var=ONEBOT_TOKEN_ENV_VAR)
        )
        is None
    )


def test_非回环主机必须使用wss并拒绝userinfo与查询串() -> None:
    base: dict[str, Any] = {
        "account_id": BOUND_ACCOUNT_ID,
        "state_dir": "/tmp/state",
        "client_identity": "client-test",
    }

    allowed = (
        "ws://127.0.0.1:3001/onebot",
        "ws://[::1]:3001/onebot",
        "wss://gateway.example.com:3001/onebot",
    )
    for endpoint in allowed:
        assert (
            validate_service_config(
                ServiceConfig(
                    **base, onebot_endpoint=endpoint, onebot_token_env_var=ONEBOT_TOKEN_ENV_VAR
                )
            )
            is None
        ), endpoint

    remote_plaintext = validate_service_config(
        ServiceConfig(
            **base,
            onebot_endpoint="ws://gateway.example.com:3001/onebot",
            onebot_token_env_var=ONEBOT_TOKEN_ENV_VAR,
        )
    )
    assert isinstance(remote_plaintext, ConfigUnavailable), remote_plaintext
    assert "wss" in remote_plaintext.reason, remote_plaintext

    for label, endpoint in (
        ("userinfo", "ws://user:secret@127.0.0.1:3001/onebot"),
        ("查询串", "ws://127.0.0.1:3001/onebot?access_token=secret"),
        ("片段", "ws://127.0.0.1:3001/onebot#secret"),
    ):
        rejected = validate_service_config(
            ServiceConfig(
                **base, onebot_endpoint=endpoint, onebot_token_env_var=ONEBOT_TOKEN_ENV_VAR
            )
        )
        assert isinstance(rejected, ConfigUnavailable), (label, rejected)

    # 同一校验口径经解析入口生效：明文远程与带凭证地址都属配置级失败、不回显凭证
    for label, endpoint in (
        ("远程明文 ws", "ws://gateway.example.com:3001/onebot"),
        ("userinfo", "ws://user:secret@127.0.0.1:3001/onebot"),
        ("查询串", "ws://127.0.0.1:3001/onebot?access_token=secret"),
    ):
        unresolved = resolve_onebot_connection(
            ServiceConfig(
                **base, onebot_endpoint=endpoint, onebot_token_env_var=ONEBOT_TOKEN_ENV_VAR
            ),
            {ONEBOT_TOKEN_ENV_VAR: ONEBOT_TOKEN_VALUE},
        )
        assert isinstance(unresolved, OneBotUnresolved), (label, unresolved)
        assert unresolved.config_failure is True, (label, unresolved)
        assert "secret" not in unresolved.reason, (label, unresolved.reason)


def test_账号标识形态非法时不回显原值并关闭发送() -> None:
    assert canonical_identifier("10000") == "10000", "合法标识符行为不变"
    assert canonical_identifier(10000) == "10000"
    assert canonical_identifier(" 10000") == "", "不再裁剪成对不上真实标识的另一个字符串"
    assert canonical_identifier("10000\n") == ""
    assert canonical_identifier("<10000>") == ""

    poisoned = FakeOneBot()
    poisoned.fail_next(
        "get_login_info",
        {
            "status": "ok",
            "retcode": 0,
            "data": {"user_id": "10000\n\x1b[31m伪造"},
        },
    )
    outcome = asyncio.run(_gateway(poisoned).connect())

    assert isinstance(outcome, AccountUnavailable), outcome
    assert outcome.error == ERROR_ACCOUNT_UNCONFIRMED
    assert "\x1b" not in outcome.reason and "\n" not in outcome.reason, outcome.reason
    assert "伪造" not in outcome.reason, "错误原因不得原样回显不可信标识符"

    bracketed = FakeOneBot()
    bracketed.fail_next(
        "get_login_info",
        {"status": "ok", "retcode": 0, "data": {"user_id": "<10000>"}},
    )
    outcome = asyncio.run(_gateway(bracketed).connect())

    assert isinstance(outcome, AccountUnavailable), outcome
    assert "<10000>" not in outcome.reason, outcome.reason


def test_账号就绪昵称按不可信显示文本压平限长() -> None:
    onebot = FakeOneBot(nickname="搬\n运\x1b喵" + "长" * 200)

    ready = asyncio.run(_gateway(onebot).connect())

    assert isinstance(ready, AccountReady), ready
    assert ready.account_id == BOUND_ACCOUNT_ID
    assert "\n" not in ready.nickname and "\x1b" not in ready.nickname, ready.nickname
    assert len(ready.nickname) <= MAX_NICKNAME_LENGTH, ready.nickname


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    return json.dumps(str(value))


def _write_config(tmp_path: Path, **overrides: Any) -> Path:
    values: dict[str, Any] = {
        "account_id": BOUND_ACCOUNT_ID,
        "state_dir": str(tmp_path / "state"),
        "client_identity": "client-test",
        "token_env_var": SERVICE_TOKEN_ENV_VAR,
        "onebot_endpoint": ONEBOT_ENDPOINT,
        "onebot_token_env_var": ONEBOT_TOKEN_ENV_VAR,
    }
    values.update(overrides)
    lines = ["[service]"]
    lines.extend(f"{key} = {_toml_value(value)}" for key, value in values.items())
    path = tmp_path / "service.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _prepare_state(tmp_path: Path) -> tuple[Path, str]:
    state_dir = tmp_path / "state"
    fresh = initialize_fresh(state_dir, clock=FakeClock())
    assert isinstance(fresh, FreshOutcome), fresh
    return state_dir, fresh.ledger_identity


async def _non_blocking_serve(service: object) -> int:
    """不阻塞的服务循环替身（第 5 组 `start` 的 `serve` 接缝）：只验证就绪报告。"""

    del service
    from service.cli import EXIT_OK

    return EXIT_OK


def test_启动CLI如实报告OneBot连接与账号就绪状态(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir, ledger_id = _prepare_state(tmp_path)
    config_path = _write_config(
        tmp_path, state_dir=str(state_dir), ledger_id=ledger_id
    )
    monkeypatch.setenv(SERVICE_TOKEN_ENV_VAR, SERVICE_TOKEN_VALUE)
    monkeypatch.setenv(ONEBOT_TOKEN_ENV_VAR, ONEBOT_TOKEN_VALUE)

    ready_runner = ServiceCommandRunner(
        transport_factory=lambda config, clock: FakeOneBot(account_id=int(config.account_id)),
        serve=_non_blocking_serve,
    )
    code = main(["start", "--config", str(config_path)], commands=ready_runner)
    output = capsys.readouterr().err

    # 第 5 组起 `start` 会真正监听 HTTP 并等待停止信号：这里注入「不阻塞的服务循环」，
    # 只核对账号就绪报告。未装 aiohttp 时 HTTP 服务端不可用（EXIT_UNAVAILABLE），
    # 装了则由注入的循环立即返回成功（EXIT_OK）；两种环境都必须如实报告账号就绪。
    from service.cli import EXIT_OK

    assert code in (EXIT_UNAVAILABLE, EXIT_OK), output
    assert "账号" in output and "就绪" in output, output

    mismatched_runner = ServiceCommandRunner(
        transport_factory=lambda config, clock: FakeOneBot(account_id=99999)
    )
    code = main(["start", "--config", str(config_path)], commands=mismatched_runner)
    output = capsys.readouterr().err

    assert code != 0, output
    assert "不符" in output, output


def test_启动CLI在OneBot凭证缺失时拒绝启动(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir, ledger_id = _prepare_state(tmp_path)
    config_path = _write_config(tmp_path, state_dir=str(state_dir), ledger_id=ledger_id)
    monkeypatch.setenv(SERVICE_TOKEN_ENV_VAR, SERVICE_TOKEN_VALUE)
    monkeypatch.delenv(ONEBOT_TOKEN_ENV_VAR, raising=False)

    runner = ServiceCommandRunner(
        transport_factory=lambda config, clock: FakeOneBot(account_id=int(config.account_id))
    )
    code = main(["start", "--config", str(config_path)], commands=runner)
    output = capsys.readouterr().err

    assert code == EXIT_CONFIG, output
    assert ONEBOT_TOKEN_ENV_VAR in output, output
