"""4.8 统一发送失败与未知的分类。

判据（tasks.md 4.8 与 design D6/D8）：

- 调用前可证明未发的错误（材料不可发送、账号未就绪、空白评论）返回 `failed`；
- 平台调用之后的模糊错误（失败响应、媒体加载失败、异常、断线、超时、契约外状态）
  一律按待核实（`unknown`），绝不降级为可重试失败；
- 取消不吞：步骤传播 `CancelledError`，编排侧用公开口径得到待核实结果；
- 同一请求只产生一次平台动作、无宿主旧发送回退；错误原因不泄露正文、资源标识
  或凭证。

对端一律是 2.6 的模拟 OneBot（内存），不连接任何真实地址。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from service.gateway import (
    GATEWAY_CALL_TIMEOUT_SECONDS,
    SnowLumaGateway,
    cancelled_platform_result,
)
from service.materials import PrepareRejected
from shared.send_steps import CommentSendRequest, SourceMaterial, SourceSendRequest
from tests.service_support import (
    BOUND_ACCOUNT_ID,
    STAGE_RECEIPT,
    TARGET_GROUP_ID,
    FakeClock,
    FakeOneBot,
    OneBotUnavailable,
)

FORWARD_MATERIAL = SourceMaterial(kind="forward", forward_resource_id="res-secret-abc")
COMMENT_TEXT = "秘密评论正文"
ALLOWED_OPERATIONS = frozenset({"get_login_info", "get_group_list", "get_msg", "send_msg"})
ROOT = Path(__file__).resolve().parent.parent


def _source_request() -> SourceSendRequest:
    return SourceSendRequest(
        delivery_id="d-1",
        attempt_id="a-1",
        target_group_id=TARGET_GROUP_ID,
        material=FORWARD_MATERIAL,
    )


def _comment_request(text: str = COMMENT_TEXT) -> CommentSendRequest:
    return CommentSendRequest(
        delivery_id="d-1", attempt_id="a-1", target_group_id=TARGET_GROUP_ID, comment=text
    )


def _gateway(onebot: Any, clock: FakeClock) -> SnowLumaGateway:
    return SnowLumaGateway(transport=onebot, account_id=BOUND_ACCOUNT_ID, clock=clock)


class _StubTransport:
    """只回固定响应/异常的传输替身（契约外状态与异常注入用；不模拟真实网关）。"""

    def __init__(self, *, receipt: Any = None, error: BaseException | None = None) -> None:
        self._receipt = receipt
        self._error = error
        self.send_count = 0
        self.queries: list[str] = []

    async def get_login_info(self) -> Mapping[str, Any]:
        return {"status": "ok", "retcode": 0, "data": {"user_id": int(BOUND_ACCOUNT_ID)}}

    async def get_group_list(self) -> Mapping[str, Any]:
        return {"status": "ok", "retcode": 0, "data": [{"group_id": TARGET_GROUP_ID}]}

    async def get_msg(self, message_id: str) -> Mapping[str, Any]:
        self.queries.append(message_id)
        return {"status": "failed", "retcode": 100, "message": "message not found"}

    async def send_msg(
        self,
        *,
        target_group_id: str,
        message: Sequence[Mapping[str, Any]],
        stage: str = "",
    ) -> Mapping[str, Any]:
        del stage  # 阶段提示只供可控设施区分接缝：本替身不记录阶段
        self.send_count += 1
        if self._error is not None:
            raise self._error
        assert self._receipt is not None
        return self._receipt


def test_调用前可证明未发的错误返回failed() -> None:
    cases: list[tuple[str, Any, Any, Any]] = [
        (
            "空资源引用",
            FakeOneBot(),
            SourceMaterial(kind="forward", forward_resource_id=""),
            _source_request(),
        ),
        (
            "未知材料种类",
            FakeOneBot(),
            SourceMaterial(kind="text", segments=()),
            _source_request(),
        ),
    ]
    for label, onebot, material, _ in cases:
        gateway = _gateway(onebot, FakeClock())
        request = SourceSendRequest(
            delivery_id="d-1", attempt_id="a-1", target_group_id=TARGET_GROUP_ID, material=material
        )

        result = asyncio.run(gateway.send_source(request))

        assert result.status == "failed", (label, result)
        assert onebot.send_count == 0, (label, "可证明未发不得进入平台调用")
        assert onebot.calls == [], (label, "材料无效连只读查询都不应发生")

    mismatched = FakeOneBot(account_id=99999)
    result = asyncio.run(_gateway(mismatched, FakeClock()).send_source(_source_request()))
    assert result.status == "failed", result
    assert mismatched.send_count == 0, "账号不符是可证明的未发送"

    blank = FakeOneBot()
    result = asyncio.run(_gateway(blank, FakeClock()).send_comment(_comment_request("   ")))
    assert result.status == "failed", result
    assert blank.calls == []


def test_平台调用后的模糊响应与异常一律unknown() -> None:
    fuzzy_responses = [
        {"status": "failed", "retcode": 100, "message": "send group message rejected: result=79"},
        {"status": "failed", "retcode": 100, "message": "failed to fetch media: invalid rkey"},
        {"status": "failed", "retcode": 1404, "message": "unknown error"},
    ]
    for response in fuzzy_responses:
        onebot = FakeOneBot()
        onebot.fail_next("send_msg", response)
        result = asyncio.run(_gateway(onebot, FakeClock()).send_source(_source_request()))
        assert result.status == "unknown", (response, result)
        assert onebot.send_count == 1, (response, "模糊失败绝不重发")

    exceptions: list[BaseException] = [
        RuntimeError("平台内部错误"),
        OneBotUnavailable("模拟断线：未取得回执"),
        TimeoutError("模拟传输超时"),
    ]
    for error in exceptions:
        onebot = FakeOneBot()
        onebot.raise_next("send_msg", error)
        result = asyncio.run(_gateway(onebot, FakeClock()).send_source(_source_request()))
        assert result.status == "unknown", (error, result)
        assert onebot.send_count == 1, (error, "异常绝不降级为可重试失败")


def test_平台调用超时按unknown处理() -> None:
    onebot = FakeOneBot()
    clock = FakeClock()
    gateway = _gateway(onebot, clock)
    onebot.gates.pause(STAGE_RECEIPT)

    async def scenario() -> Any:
        task = asyncio.ensure_future(gateway.send_source(_source_request()))
        for _ in range(200):
            if onebot.gates.arrivals.get(STAGE_RECEIPT):
                break
            await asyncio.sleep(0)
        assert onebot.gates.arrivals.get(STAGE_RECEIPT) == 1, "平台调用已到达回执阶段"
        clock.advance(GATEWAY_CALL_TIMEOUT_SECONDS)
        result = await task
        onebot.gates.release(STAGE_RECEIPT)
        return result

    result = asyncio.run(scenario())

    assert result.status == "unknown", result
    assert result.status != "failed", "超时后可能已发出，绝不标成可重试失败"
    assert onebot.send_count == 1, "超时不通过再次发送确认"
    assert "待核实" in result.reason


def test_取消不吞掉且按待核实口径报告() -> None:
    onebot = FakeOneBot()
    onebot.raise_next("send_msg", asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_gateway(onebot, FakeClock()).send_source(_source_request()))
    assert onebot.send_count == 1, "取消发生在平台调用之后：步骤必须原样传播取消"

    result = cancelled_platform_result()
    assert result.status == "unknown", result
    assert "待核实" in result.reason


def test_契约外状态一律unknown() -> None:
    out_of_contract: list[Any] = [
        "不是映射结构",
        [],
        {"ok": True, "data": {"message_id": "x"}},
        {"status": "failed", "retcode": 100},
        {"status": "ok", "retcode": 1, "data": {"message_id": "x"}},
        {"status": "ok", "retcode": 0},
        {"status": "ok", "retcode": 0, "data": {"message_id": ""}},
        {"status": "ok", "retcode": 0, "data": {"message_id": True}},
        {"status": "ok", "retcode": 0, "data": {"message_id": ["x"]}},
        {"status": "error", "retcode": 0, "data": {"message_id": "x"}},
    ]
    for receipt in out_of_contract:
        transport = _StubTransport(receipt=receipt)
        result = asyncio.run(_gateway(transport, FakeClock()).send_source(_source_request()))
        assert result.status == "unknown", (receipt, result)
        assert transport.send_count == 1, (receipt, "契约外状态不得重发、不得降级为 failed")


def test_同一请求只有一种平台动作且无宿主旧通道() -> None:
    onebot = FakeOneBot()
    for _ in range(2):  # 源与评论各注入一次模糊失败响应
        onebot.fail_next(
            "send_msg",
            {"status": "failed", "retcode": 100, "message": "send group message rejected"},
        )
    gateway = _gateway(onebot, FakeClock())

    source_result = asyncio.run(gateway.send_source(_source_request()))
    comment_result = asyncio.run(gateway.send_comment(_comment_request()))

    assert source_result.status == "unknown" and comment_result.status == "unknown", (
        source_result,
        comment_result,
    )
    assert onebot.send_count == 2, "每个请求恰一次平台动作，无换通路重发"
    operations = {call.operation for call in onebot.calls}
    assert operations <= ALLOWED_OPERATIONS, operations

    for module in ("service/gateway.py", "service/materials.py", "service/onebot.py"):
        source = (ROOT / module).read_text(encoding="utf-8")
        for pattern in (
            r"\bimport\s+host_send\b",
            r"\bfrom\s+host_send\b",
            r"\bhost_send\s*\.",
            r"\bctx\.send\s*\.",
            r"\bctx\.api\s*\.",
            r"\bsend\.(?:forward|hybrid|text|image)\s*\(",
        ):
            assert not re.search(pattern, source), (module, pattern, "服务端不得回退宿主旧发送通道")


def test_错误原因不泄露原文资源标识与凭证() -> None:
    onebot = FakeOneBot()
    onebot.fail_next("send_msg", {"status": "failed", "retcode": 100, "message": "rejected"})
    source_result = asyncio.run(_gateway(onebot, FakeClock()).send_source(_source_request()))
    assert "res-secret-abc" not in source_result.reason, source_result.reason

    commenting = FakeOneBot()
    commenting.fail_next("send_msg", {"status": "failed", "retcode": 100, "message": "rejected"})
    comment_result = asyncio.run(
        _gateway(commenting, FakeClock()).send_comment(_comment_request())
    )
    assert COMMENT_TEXT not in comment_result.reason, comment_result.reason

    credential = "synthetic-onebot-token"
    leaking = _StubTransport(error=RuntimeError(f"connect failed token={credential}"))
    leaked_result = asyncio.run(_gateway(leaking, FakeClock()).send_source(_source_request()))
    assert leaked_result.status == "unknown", leaked_result
    assert credential not in leaked_result.reason, leaked_result.reason
    assert "res-secret-abc" not in leaked_result.reason, leaked_result.reason


def test_准备拒绝原因同样不含资源标识与正文() -> None:
    rejected = PrepareRejected(error="source_resource_missing", reason="源卡片缺少可用的资源引用")
    assert "res-secret-abc" not in rejected.reason
