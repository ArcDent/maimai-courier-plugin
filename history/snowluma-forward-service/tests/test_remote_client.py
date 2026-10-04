"""6.2 专用 HTTP 客户端与有界结果恢复（design D2/D4/D5/D8）。

seam：`remote_client.py` 的公开客户端接口与真实/模拟 HTTP 服务。用例只经插件的
`forward_message` 公共入口与可注入传输观察端点调用、认证头、重定向处理、有界超时
与响应丢失后的恢复动作；不直接调用客户端私有方法，也不连接任何真实地址。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Mapping

from remote_client import (
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    InvalidEndpoint,
    ServiceEndpoint,
    TransportReply,
    parse_service_endpoint,
)

from tests.client_support import DEFAULT_SERVICE_TOKEN_ENV, make_client_service, make_scripted_plugin
from tests.support import issue_source_call, run_bound_forward


class _RedirectTransport:
    """脚本化传输：所有请求返回 302，并记录收到的请求头（断言认证头不外发）。"""

    ledger_identity = "scripted-ledger-1"

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, str]]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
    ) -> TransportReply:
        self.requests.append((method, url, dict(headers)))
        return TransportReply(
            status=302,
            payload={"location": "https://evil.example.invalid/steal"},
        )

    async def close(self) -> None:
        return None


def test_客户端按顺序调用会话准备源评论端点(tmp_path: Path) -> None:
    fixture = make_client_service(tmp_path)

    result, _ = run_bound_forward(
        fixture.plugin, message_id="m-source-1", comment="评论只提交一次", clock=fixture.clock
    )

    assert result["status"] == "sent", result
    paths = fixture.transport.paths()
    assert [path for path in paths if path.startswith("/v1/sessions")][0] == "/v1/sessions", paths
    assert any(path.endswith("/policy") for path in paths), paths
    assert any(path.endswith("/prepare") for path in paths), paths
    assert fixture.transport.count("/source") == 1, paths
    assert fixture.transport.count("/comment") == 1, paths
    # 准备之后才可能提交阶段；评论只在源成功并等待后才提交。
    assert paths.index("/v1/forwards/prepare") < next(
        index for index, path in enumerate(paths) if path.endswith("/source")
    ), paths
    assert next(index for index, path in enumerate(paths) if path.endswith("/source")) < next(
        index for index, path in enumerate(paths) if path.endswith("/comment")
    ), paths

    # 客户端提交面最小化：四个受控标识符 + 请求 ID/会话/代次 + 评论原文。
    prepare_body = fixture.transport.body_of("/v1/forwards/prepare")
    assert set(prepare_body) == {
        "protocol_version",
        "request_id",
        "session_id",
        "policy_generation",
        "source_group_id",
        "source_message_id",
        "target_group_id",
        "comment",
    }, prepare_body
    comment_paths = [path for path in paths if path.endswith("/comment")]
    comment_body = fixture.transport.body_of(comment_paths[0])
    assert set(comment_body) == {
        "protocol_version",
        "attempt_id",
        "session_id",
        "policy_generation",
        "challenge",
    }, comment_body
    assert "评论只提交一次" not in str(comment_body), "评论原文不得在评论阶段重复提交"
    fixture.close()


def test_认证头只发给配置的固定地址且不跟随重定向(tmp_path: Path) -> None:
    """302 不跟随：客户端不从重定向取信息，也不把认证头转交其它地址。"""

    redirect = _RedirectTransport()
    plugin, _ctx, _transport, clock = make_scripted_plugin(tmp_path, transport=redirect)  # type: ignore[arg-type]

    result, _ = run_bound_forward(plugin, message_id="m-source-1", clock=clock)

    assert result["status"] == "rejected", result
    assert "重定向" in str(result["reason"]), result
    assert len(redirect.requests) == 1, ("客户端不得跟随重定向到其它地址", redirect.requests)
    method, url, headers = redirect.requests[0]
    assert method == "POST", redirect.requests
    assert url.startswith("http://127.0.0.1:8787/v1/"), url
    assert headers.get("Authorization", "").startswith("Bearer "), (
        "认证头只发给配置的固定地址",
        headers,
    )
    assert "evil.example.invalid" not in url


def test_单次请求有界超时按不可用报告(tmp_path: Path) -> None:
    import asyncio

    plugin, _ctx, transport, clock = make_scripted_plugin(
        tmp_path,
        request_timeout_seconds=0.02,
    )
    transport.hang_paths.append("/v1/sessions")
    arguments = issue_source_call(plugin, message_id="m-source-1")

    started = time.monotonic()
    result = asyncio.run(plugin.forward_message(**arguments))
    elapsed = time.monotonic() - started

    assert result["status"] == "rejected", result
    assert "超时" in str(result["reason"]), result
    assert elapsed < 2.0, f"超时必须由配置上界收口，不允许真实长等待（{elapsed:.3f}s）"


def test_缺凭证时不构造传输也不发起请求(tmp_path: Path) -> None:
    fixture = make_client_service(tmp_path, environ={})

    result, _ = run_bound_forward(fixture.plugin, message_id="m-source-1", clock=fixture.clock)

    assert result["status"] == "rejected", result
    assert DEFAULT_SERVICE_TOKEN_ENV in str(result["reason"]), result
    assert fixture.transport.paths() == [], "缺凭证时不得建立任何连接或请求"
    assert fixture.old_send_calls() == []
    fixture.close()


def test_执行响应丢失后只查询同一投递不再次提交发送(tmp_path: Path) -> None:
    plugin, _ctx, transport, clock = make_scripted_plugin(tmp_path)
    transport.source_reply = {
        "status": "sent",
        "target_message_id": "m-target-recovered",
        "comment_status": "pending",
        "comment_challenge": "",
        "reason": "",
    }
    transport.lose_response_paths.append("/source")

    result, _ = run_bound_forward(plugin, message_id="m-source-1", clock=clock)

    assert result["status"] == "sent", result
    assert result["target_message_id"] == "m-target-recovered", result
    assert transport.count("/source") == 1, "响应丢失后不得再次提交发送探测"
    query_paths = [
        path
        for path in transport.paths()
        if path.startswith("/v1/forwards/")
        and path.count("/") == 3
        and not path.endswith("/prepare")
    ]
    assert len(query_paths) == 1, ("恢复只查询同一投递一次", transport.paths())
    assert transport.count("/comment") == 0, "未请求评论时不得提交评论阶段"


def test_查询失败时如实返回未知且不换通路重发(tmp_path: Path) -> None:
    plugin, _ctx, transport, clock = make_scripted_plugin(tmp_path)
    transport.lose_response_paths.append("/source")
    transport.fail_paths.append(f"/v1/forwards/{_expected_delivery_id()}")

    result, _ = run_bound_forward(plugin, message_id="m-source-1", clock=clock)

    assert result["status"] == "unknown", result
    assert "待核实" in str(result["reason"]), result
    assert transport.count("/source") == 1, "查询不可达也不得再次提交发送"


def test_缺少必填响应字段按协议不符报告(tmp_path: Path) -> None:
    plugin, _ctx, transport, clock = make_scripted_plugin(tmp_path)
    transport.session_reply = {"account_id": "10000", "ledger_identity": "scripted-ledger-1"}

    result, _ = run_bound_forward(plugin, message_id="m-source-1", clock=clock)

    assert result["status"] == "rejected", result
    assert "session_id" in str(result["reason"]), result
    assert transport.count("/prepare") == 0, "会话不符不得继续提交准备"


def test_远程明文地址与非法地址在客户端拒绝() -> None:
    assert parse_service_endpoint("https://forward.example.invalid").base_url == (
        "https://forward.example.invalid"
    )
    for raw, label in (
        ("http://10.0.0.9:8787", "跨主机明文"),
        ("ftp://127.0.0.1:1", "非 HTTP(S)"),
        ("http://user:pass@127.0.0.1:8787", "含凭证"),
        ("http://127.0.0.1:8787/path?token=x", "含查询串"),
        ("", "空地址"),
    ):
        outcome = parse_service_endpoint(raw)
        assert isinstance(outcome, InvalidEndpoint), (label, outcome)
        assert outcome.reason
    # 回环地址允许明文 http（受控隧道/本机场景），但客户端仍不跟随重定向。
    assert parse_service_endpoint("http://127.0.0.1:8787").base_url == "http://127.0.0.1:8787"
    assert DEFAULT_REQUEST_TIMEOUT_SECONDS > 0


def _expected_delivery_id(
    *,
    message_id: str = "m-source-1",
    source_group_id: str = "10001",
    target_group_id: str = "20002",
    account_id: str = "10000",
) -> str:
    """四字段投递 ID（与服务端同一公开算法，供故障路径匹配）。"""

    from delivery_ledger import DeliveryKey
    from shared.protocol import delivery_id_for

    return delivery_id_for(
        DeliveryKey(
            account_id=account_id,
            source_group_id=source_group_id,
            source_message_id=message_id,
            target_group_id=target_group_id,
        )
    )


def test_回环主机尾点形态在客户端与服务端判定一致() -> None:
    """复核修复：`127.0.0.1.` 是同一回环地址，两侧必须复用同一定义（design D9）。"""

    from service.config import onebot_endpoint_rejection
    from shared.config_rules import is_loopback_host

    assert is_loopback_host("127.0.0.1.") is True
    assert is_loopback_host("127.0.0.1") is True
    assert is_loopback_host("[::1].") is True
    assert is_loopback_host("localhost.") is True
    assert is_loopback_host("10.0.0.9") is False

    # 客户端：带尾点的回环地址与 `127.0.0.1` 同判，允许受控隧道明文 http。
    endpoint = parse_service_endpoint("http://127.0.0.1.:8787")
    assert isinstance(endpoint, ServiceEndpoint), endpoint
    assert endpoint.base_url == "http://127.0.0.1.:8787"

    # 服务端：同一地址的 OneBot 明文 ws 与 `127.0.0.1` 同判（不要求 wss）。
    assert onebot_endpoint_rejection("ws://127.0.0.1.:8787") is None
    assert onebot_endpoint_rejection("ws://127.0.0.1:8787") is None
