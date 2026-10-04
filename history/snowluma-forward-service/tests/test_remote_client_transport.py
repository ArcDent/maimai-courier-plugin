"""6.2 真实回环 HTTP 往返：真实 `AiohttpTransport` → 真实服务路由（需要 aiohttp）。

缺失 `aiohttp`（标准验证命令不装网络库）时整份用例跳过；带 `--with aiohttp` 运行时
在本机回环 socket 上验证：插件的真实 HTTP 客户端经真实认证中间件建立会话、准备并
执行源阶段，模拟 OneBot 恰好收到一次发送。不连接真实 QQ、不发送任何真实消息。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("aiohttp", reason="真实回环 HTTP 客户端用例需要 aiohttp（按需装载）")

from remote_client import AiohttpTransport  # noqa: E402

from service.config import ServiceConfig  # noqa: E402
from service.server import (  # noqa: E402
    RunningService,
    ServerUnavailable,
    build_running_service,
    start_http_server,
)

from tests.client_support import (  # noqa: E402
    client_service_config,
    onebot_segments,
)
from tests.service_support import launch_authorized_service, store_source_message  # noqa: E402
from tests.support import (  # noqa: E402
    SOURCE_GROUP_ID,
    TARGET_GROUP_ID,
    issue_source_call,
    make_plugin,
    prepare_source,
    text_segment,
)

TOKEN_ENV_VAR = "MC_TEST_CLIENT_TRANSPORT_TOKEN"
SERVICE_TOKEN = "real-loopback-client-token"


def test_真实回环HTTP客户端经真实路由完成源阶段(tmp_path: Path) -> None:
    """真实 aiohttp 客户端 → 真实回环路由 → 真实协调器 → 模拟 OneBot，一次发送。"""

    service = launch_authorized_service(
        tmp_path / "service-state",
        server_source_groups=(SOURCE_GROUP_ID,),
        server_target_groups=(TARGET_GROUP_ID,),
    )
    identity = asyncio.run(service.ledger.identity())
    store_source_message(
        service.onebot,
        message_id="m-source-1",
        group_id=SOURCE_GROUP_ID,
        message=onebot_segments([text_segment("真实回环正文")]),
    )

    config = client_service_config(
        identity,
        endpoint="http://127.0.0.1:1",  # 启动真实监听后替换为实际端口
        token_env_var=TOKEN_ENV_VAR,
    )
    plugin, ctx = make_plugin(
        tmp_path,
        config=config,
        transport_factory=lambda section: AiohttpTransport(timeout_seconds=5.0),
        environ={TOKEN_ENV_VAR: SERVICE_TOKEN},
    )
    prepare_source(ctx)
    arguments = issue_source_call(plugin, message_id="m-source-1")

    async def scenario() -> dict[str, Any]:
        built = build_running_service(
            config=ServiceConfig(
                account_id="10000",
                state_dir=str(service.state_dir),
                client_identity="maibot-plugin",
                ledger_id=identity,
                listen_host="127.0.0.1",
                listen_port=0,
                token_env_var=TOKEN_ENV_VAR,
                allowed_source_groups=(SOURCE_GROUP_ID,),
                allowed_target_groups=(TARGET_GROUP_ID,),
            ),
            ledger=service.ledger,
            gateway=service.gateway,
            clock=service.clock,
            ledger_identity=identity,
            environ={TOKEN_ENV_VAR: SERVICE_TOKEN},
        )
        assert isinstance(built, RunningService), built
        started = await start_http_server(built.app, built.config)
        assert not isinstance(started, ServerUnavailable), started
        try:
            plugin.set_plugin_config(
                {
                    **config,
                    "service": {**config["service"], "endpoint": f"http://{started.address}"},
                }
            )
            result = await asyncio.wait_for(plugin.forward_message(**arguments), timeout=10)
            await plugin.on_unload()
            return result
        finally:
            await started.stop()

    result = asyncio.run(scenario())

    assert result["status"] == "sent", result
    assert result["success"] is True, result
    assert result["target_message_id"], result
    assert service.onebot.send_count == 1, "真实回环链上恰一次平台发送"
    assert service.onebot.send_calls[0].target == TARGET_GROUP_ID
    assert service.onebot.send_calls[0].detail["message"] == [
        {"type": "text", "data": {"text": "真实回环正文"}}
    ]
    assert plugin.ctx.send.calls == [], "真实回环链仍不得触碰宿主旧发送通道"
    service.close()
