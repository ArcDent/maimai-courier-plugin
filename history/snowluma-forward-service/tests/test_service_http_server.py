"""5.9 真实 HTTP 服务端：真实路由、认证、监听与启动/退出（需要 aiohttp）。

缺失 `aiohttp`（标准验证命令不装网络库）时整份用例跳过；带 `--with aiohttp` 运行时
给出真实回环 socket 上的证据：

- `start_http_server` 真正监听受控地址，请求经真实路由与认证层应答（401/200/404）；
- 停止后端口释放；
- `python -m service start` 经注入传输工厂在回环端口上真正提供服务并如实报告就绪，
  停止后以 0 退出（不连接真实 OneBot、不发送任何真实消息）。
"""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("aiohttp", reason="真实 HTTP 服务端用例需要 aiohttp（按需装载）")

import aiohttp  # noqa: E402

from service.cli import EXIT_OK, ServiceCommandRunner, main  # noqa: E402
from service.config import ServiceConfig  # noqa: E402
from service.server import (  # noqa: E402
    RunningService,
    ServerUnavailable,
    build_running_service,
    start_http_server,
)
from tests.service_support import FakeOneBot, launch_authorized_service  # noqa: E402


TOKEN_ENV_VAR = "MC_TEST_HTTP_SERVICE_TOKEN"
SERVICE_TOKEN = "real-socket-token"
ONEBOT_TOKEN_ENV_VAR = "MC_TEST_HTTP_ONEBOT_TOKEN"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _config(state_dir: Path, *, port: int, ledger_id: str = "") -> ServiceConfig:
    return ServiceConfig(
        account_id="10000",
        state_dir=str(state_dir),
        client_identity="maibot-plugin",
        ledger_id=ledger_id,
        listen_host="127.0.0.1",
        listen_port=port,
        token_env_var=TOKEN_ENV_VAR,
        allowed_source_groups=("10001",),
        allowed_target_groups=("20002",),
    )


def test_真实HTTP服务端经真实路由与认证应答(tmp_path: Path) -> None:
    harness = launch_authorized_service(tmp_path / "state")
    scenario_sends = harness.onebot.send_count

    async def scenario() -> dict[str, Any]:
        identity = await harness.ledger.identity()
        built = build_running_service(
            config=_config(harness.state_dir, port=0),
            ledger=harness.ledger,
            gateway=harness.gateway,
            clock=harness.clock,
            ledger_identity=identity,
            environ={TOKEN_ENV_VAR: SERVICE_TOKEN},
        )
        assert isinstance(built, RunningService), built
        started = await start_http_server(built.app, built.config)
        assert not isinstance(started, ServerUnavailable), started
        base = f"http://{started.address}"
        outcome: dict[str, Any] = {"address": started.address}
        try:
            async with aiohttp.ClientSession() as session:
                unauthorised = await session.get(f"{base}/v1/forwards/d-1")
                outcome["unauthorised_status"] = unauthorised.status
                outcome["unauthorised_error"] = (await unauthorised.json())["error"]

                created = await session.post(
                    f"{base}/v1/sessions",
                    json={"protocol_version": 1, "client_identity": "maibot-plugin"},
                    headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
                )
                outcome["created_status"] = created.status
                outcome["created"] = (await created.json())["result"]

                missing = await session.get(
                    f"{base}/v1/并没有这个接口",
                    headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
                )
                outcome["missing_status"] = missing.status
        finally:
            address = started.address
            await started.stop()
        outcome["address_after_stop"] = address
        return outcome

    outcome = asyncio.run(scenario())

    assert outcome["unauthorised_status"] == 401
    assert outcome["unauthorised_error"] == "unauthorized"
    assert outcome["created_status"] == 200
    assert outcome["created"]["account_id"] == "10000"
    assert outcome["created"]["ledger_identity"]
    assert outcome["missing_status"] == 404
    assert harness.onebot.send_count == scenario_sends, "真实服务端应答不产生任何发送"

    async def closed() -> int:
        session = aiohttp.ClientSession()
        try:
            response = await session.get(f"http://{outcome['address_after_stop']}/v1/sessions")
            return response.status
        finally:
            await session.close()

    with pytest.raises(aiohttp.ClientConnectorError):
        asyncio.run(closed())


def test_启动CLI真正启动HTTP服务并如实报告就绪(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    harness = launch_authorized_service(tmp_path / "state")
    identity = asyncio.run(harness.ledger.identity())
    harness.close()
    port = _free_port()
    config = tmp_path / "service.toml"
    config.write_text(
        "\n".join(
            [
                "[service]",
                'client_identity = "maibot-plugin"',
                'account_id = "10000"',
                f'state_dir = "{harness.state_dir}"',
                f"ledger_id = {json.dumps(identity)}",
                f'listen_host = "127.0.0.1"',
                f"listen_port = {port}",
                f'token_env_var = "{TOKEN_ENV_VAR}"',
                'onebot_endpoint = "ws://127.0.0.1:9/onebot"',
                f'onebot_token_env_var = "{ONEBOT_TOKEN_ENV_VAR}"',
                'allowed_source_groups = ["10001"]',
                'allowed_target_groups = ["20002"]',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(TOKEN_ENV_VAR, SERVICE_TOKEN)
    monkeypatch.setenv(ONEBOT_TOKEN_ENV_VAR, "onebot-token")
    onebot = FakeOneBot(account_id=10000)
    observed: dict[str, Any] = {}

    async def serve_once(service: RunningService) -> int:
        """真实服务循环替身：对真实监听地址发一次请求后立即返回（不等待信号）。"""

        observed["address"] = service.address
        async with aiohttp.ClientSession() as session:
            response = await session.post(
                f"http://{service.address}/v1/sessions",
                json={"protocol_version": 1, "client_identity": "maibot-plugin"},
                headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
            )
            observed["status"] = response.status
            observed["payload"] = await response.json()
        return EXIT_OK

    runner = ServiceCommandRunner(
        transport_factory=lambda config, clock: onebot, serve=serve_once
    )
    code = main(["start", "--config", str(config)], commands=runner)
    output = capsys.readouterr().err

    assert code == EXIT_OK, output
    assert f"HTTP 服务端已就绪并监听 127.0.0.1:{port}" in output, output
    assert "账号已就绪" in output, output
    assert observed["address"] == f"127.0.0.1:{port}"
    assert observed["status"] == 200, observed
    assert observed["payload"]["result"]["ledger_identity"] == identity
    assert onebot.send_count == 0, "启动与接口就绪不产生任何发送"
