"""4.5 单图、多图和图文材料发送。

判据（tasks.md 4.5 与 design D3、cross-group-forward「支持消息类型与完整性」）：

- 只使用 1.3 核实过的安全媒体表示：`{"type": "image", "data": {"url": <https…>}}`；
- 覆盖单图、多图、图文与带提及图片，保持原顺序，不以描述代替原图；
- 任一图片缺失/失效或媒体引用不安全（`file://`、`base64://`、本机路径、明文 http、
  回环/内网地址）时整条拒绝；服务端不读取任意本机文件或内网资源。

对端一律是 2.6 的模拟 OneBot（内存），不连接任何真实地址。
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

from service.gateway import PrepareRejected, PreparedSource, SnowLumaGateway
from service.materials import ERROR_SOURCE_MEDIA_UNSAFE
from shared.send_steps import SourceMaterial, SourceSendRequest
from tests.service_support import (
    BOUND_ACCOUNT_ID,
    SOURCE_GROUP_ID,
    SOURCE_MESSAGE_ID,
    TARGET_GROUP_ID,
    FakeClock,
    FakeOneBot,
    store_source_message,
)

SAFE_URL = "https://cdn.example.com/media/a.jpg?rkey=synthetic&ttl=3600"
"""合成 https 媒体引用（含 rkey 查询串；域名示例，不对应真实资源）。"""

ALLOWED_OPERATIONS = frozenset({"get_login_info", "get_group_list", "get_msg", "send_msg"})


def _gateway(onebot: FakeOneBot) -> SnowLumaGateway:
    return SnowLumaGateway(transport=onebot, account_id=BOUND_ACCOUNT_ID, clock=FakeClock())


def _prepare(onebot: FakeOneBot) -> PreparedSource | PrepareRejected:
    return asyncio.run(
        _gateway(onebot).prepare_source(
            source_group_id=SOURCE_GROUP_ID,
            source_message_id=SOURCE_MESSAGE_ID,
            target_group_id=TARGET_GROUP_ID,
        )
    )


def _with_message(message: list[Mapping[str, Any]]) -> FakeOneBot:
    onebot = FakeOneBot()
    store_source_message(
        onebot, message_id=SOURCE_MESSAGE_ID, group_id=SOURCE_GROUP_ID, message=message
    )
    return onebot


def _image(url: str = SAFE_URL, **extra: Any) -> dict[str, Any]:
    """合成 OneBot 图片段：收侧形态含 `url/file/sub_type/summary`（1.3 核实）。"""

    data: dict[str, Any] = {"url": url, "file": "media/a.jpg", "sub_type": 0, "summary": "[图片]"}
    data.update(extra)
    return {"type": "image", "data": data}


def _send(gateway: SnowLumaGateway, prepared: PreparedSource) -> Any:
    return asyncio.run(
        gateway.send_source(
            SourceSendRequest(
                delivery_id="d-1",
                attempt_id="a-1",
                target_group_id=TARGET_GROUP_ID,
                material=prepared.material,
            )
        )
    )


def test_单图多图与图文按原顺序发送() -> None:
    second_url = "https://cdn.example.com/media/b.jpg?rkey=synthetic&ttl=3600"
    cases: list[tuple[str, list[Mapping[str, Any]], tuple[dict[str, Any], ...], str]] = [
        (
            "单图",
            [_image()],
            ({"type": "image", "data": {"url": SAFE_URL}},),
            "image",
        ),
        (
            "多图",
            [_image(), _image(second_url)],
            (
                {"type": "image", "data": {"url": SAFE_URL}},
                {"type": "image", "data": {"url": second_url}},
            ),
            "image",
        ),
        (
            "图文",
            [
                {"type": "text", "data": {"text": "看图"}},
                _image(),
                {"type": "text", "data": {"text": "完毕"}},
            ],
            (
                {"type": "text", "data": {"text": "看图"}},
                {"type": "image", "data": {"url": SAFE_URL}},
                {"type": "text", "data": {"text": "完毕"}},
            ),
            "mixed",
        ),
    ]
    for label, message, expected, kind in cases:
        onebot = _with_message(message)
        gateway = _gateway(onebot)

        prepared = _prepare(onebot)
        assert isinstance(prepared, PreparedSource), (label, prepared)
        assert prepared.material.kind == kind, (label, prepared.material.kind)
        assert prepared.material.segments == expected, (label, prepared.material.segments)

        result = _send(gateway, prepared)
        assert result.status == "sent", (label, result)
        assert onebot.send_count == 1, (label, "同一请求只允许一次平台发送")
        assert onebot.send_calls[0].detail["message"] == list(expected), (
            label,
            "媒体只以 https 引用重建：不携带 file/sub_type/summary，不以描述代替原图",
        )
        assert {call.operation for call in onebot.calls} <= ALLOWED_OPERATIONS, label


def test_带提及图片保持顺序且不生成实际提及段() -> None:
    onebot = _with_message(
        [
            _image(),
            {"type": "at", "data": {"qq": "12345"}},
            {"type": "text", "data": {"text": "看图"}},
        ]
    )
    gateway = _gateway(onebot)

    prepared = _prepare(onebot)

    assert isinstance(prepared, PreparedSource), prepared
    assert prepared.material.kind == "mixed"
    assert prepared.material.segments == (
        {"type": "image", "data": {"url": SAFE_URL}},
        {"type": "text", "data": {"text": "［提及：12345］看图"}},
    ), "图片顺序不变，提及转普通文本"

    result = _send(gateway, prepared)
    assert result.status == "sent", result
    assert onebot.send_calls[0].detail["message"] == [
        {"type": "image", "data": {"url": SAFE_URL}},
        {"type": "text", "data": {"text": "［提及：12345］看图"}},
    ]
    assert all(segment["type"] != "at" for segment in onebot.send_calls[0].detail["message"])


def test_不安全媒体引用整条拒绝() -> None:
    unsafe_urls: list[tuple[str, Any]] = [
        ("本机文件协议", "file:///tmp/secret.jpg"),
        ("base64 协议", "base64://aGVsbG8="),
        ("本机路径", "/tmp/secret.jpg"),
        ("明文 http", "http://cdn.example.com/a.jpg"),
        ("回环地址", "https://127.0.0.1/a.jpg"),
        ("本机名", "https://localhost/a.jpg"),
        ("内网地址", "https://192.168.1.5/a.jpg"),
        ("IPv6 回环", "https://[::1]/a.jpg"),
        ("传统十进制 IPv4", "https://2130706433/a.jpg"),
        ("传统十六进制 IPv4", "https://0x7f000001/a.jpg"),
        ("传统短写 IPv4", "https://127.1/a.jpg"),
        ("传统八进制 IPv4", "https://0177.0.0.1/a.jpg"),
        ("IPv6 内网", "https://[fd00::1]/a.jpg"),
        ("IPv6 链路本地", "https://[fe80::1]/a.jpg"),
        ("IPv4 映射 IPv6 回环", "https://[::ffff:127.0.0.1]/a.jpg"),
        ("零宽空格", "https://127.0.0.1\u200b/a.jpg"),
        ("软连字符域名", "https://cd\u00adn.example.com/a.jpg"),
        ("本机名后缀点", "https://localhost./a.jpg"),
        ("纯数字非 IP 主机", "https://99999999999/a.jpg"),
        ("反斜杠解析歧义", "https://127.0.0.1\\@cdn.example.com/a.jpg"),
        ("空 url", ""),
        ("非字符串 url", 12345),
        ("含空白 url", "https://cdn.example.com/a b.jpg"),
        ("缺 url", None),
    ]
    for label, url in unsafe_urls:
        segment: dict[str, Any] = {"type": "image", "data": {"file": "media/a.jpg"}}
        if url is not None:
            segment["data"]["url"] = url
        onebot = _with_message([segment])

        prepared = _prepare(onebot)

        assert isinstance(prepared, PrepareRejected), (label, prepared)
        assert prepared.error == ERROR_SOURCE_MEDIA_UNSAFE, (label, prepared)
        assert onebot.send_count == 0, (label, "不安全媒体必须在发送前整条拒绝")


def test_发送材料里的额外媒体字段不进入发送() -> None:
    onebot = FakeOneBot()
    gateway = _gateway(onebot)
    material = SourceMaterial(
        kind="image",
        segments=(
            {"type": "image", "data": {"url": SAFE_URL, "file": "/tmp/secret.jpg", "path": "/tmp/x"}},
        ),
    )

    result = asyncio.run(
        gateway.send_source(
            SourceSendRequest(
                delivery_id="d-1",
                attempt_id="a-1",
                target_group_id=TARGET_GROUP_ID,
                material=material,
            )
        )
    )

    assert result.status == "sent", result
    assert onebot.send_calls[0].detail["message"] == [
        {"type": "image", "data": {"url": SAFE_URL}}
    ], "发送段由服务端重建：本机路径与 file 字段不进入平台调用"


def test_发送材料里的不安全媒体不进入平台调用() -> None:
    onebot = FakeOneBot()
    gateway = _gateway(onebot)
    material = SourceMaterial(
        kind="image",
        segments=({"type": "image", "data": {"url": "file:///tmp/secret.jpg"}},),
    )

    result = asyncio.run(
        gateway.send_source(
            SourceSendRequest(
                delivery_id="d-1",
                attempt_id="a-1",
                target_group_id=TARGET_GROUP_ID,
                material=material,
            )
        )
    )

    assert result.status == "failed", result
    assert onebot.send_count == 0, "不安全的媒体引用不得进入平台调用"
