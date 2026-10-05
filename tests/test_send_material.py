"""`send_material` 公开转换入口验收（普通消息材料 + 卡片节点载荷构造）。

断言面只经 `send_material` 公开导出（`build_source_material` /
`source_outbound_segments` / `build_card_nodes`）与替身材料输入；卡片节点构造
只从展开查询结果取字段，不回调客户端、不接触平台。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from maimai_courier_plugin.send_material import (
    build_card_nodes, build_source_material, source_outbound_segments,
)
from maimai_courier_plugin.shared.send_steps import PrepareRejected, SourceMaterial


def test_图片路径图文顺序保留且提及文本化三形态() -> None:
    raw = [
        {"type": "text", "data": {"text": "甲\n"}},
        {"type": "text", "data": {"text": "乙"}},
        {"type": "at", "data": {"qq": "20002", "card": "群友甲"}},
        {"type": "at", "data": {"qq": 20003}},
        {"type": "at", "data": {"qq": "all"}},
        {"type": "text", "data": {"text": "\n尾"}},
    ]
    original = deepcopy(raw)
    material = build_source_material(raw)
    assert not isinstance(material, PrepareRejected)
    assert material.kind == "text"  # 无图片段：文字按卡片路径发送
    assert source_outbound_segments(material) == (
        {"type": "text", "data": {"text": "甲\n"}},
        {"type": "text", "data": {"text": "乙［提及：群友甲（20002）］［提及：20003］［提及：全体成员］\n尾"}},
    )
    assert raw == original


def test_图片路径图文混排kind归image且顺序保留() -> None:
    raw = [
        {"type": "text", "data": {"text": "看图"}},
        {"type": "image", "data": {"url": "https://images.example.org/a"}},
        {"type": "image", "data": {"url": "https://images.example.org/b"}},
    ]
    material = build_source_material(raw)
    assert not isinstance(material, PrepareRejected)
    assert material.kind == "image"
    assert source_outbound_segments(material) == (
        {"type": "text", "data": {"text": "看图"}},
        {"type": "image", "data": {"url": "https://images.example.org/a"}},
        {"type": "image", "data": {"url": "https://images.example.org/b"}},
    )


def test_文字路径at按源消息原样提交() -> None:
    raw = [
        {"type": "at", "data": {"qq": "all"}},
        {"type": "at", "data": {"qq": "20002", "card": "群友甲"}},
        {"type": "text", "data": {"text": "正文"}},
    ]
    material = build_source_material(raw, verbatim_mentions=True)
    assert not isinstance(material, PrepareRejected)
    assert material.kind == "text"
    assert source_outbound_segments(material) == tuple(raw)


@pytest.mark.parametrize("segment", [
    {"type": "at", "data": {}}, {"type": "at", "data": {"qq": True}},
    {"type": "at", "data": {"qq": ""}}, {"type": "at", "data": {"qq": "0"}},
    {"type": "at", "data": {"qq": "谁"}}, {"type": "reply", "data": {"id": "r1"}},
    {"type": "record", "data": {}}, {"type": "file", "data": {}},
    {"type": "video", "data": {}},
])
def test_不完整或不受支持段整条拒绝且两条路径共用(segment: dict[str, object]) -> None:
    for verbatim in (False, True):
        result = build_source_material([{"type": "text", "data": {"text": "头"}}, segment],
                                       verbatim_mentions=verbatim)
        assert isinstance(result, PrepareRejected), (verbatim, result)


@pytest.mark.parametrize("data", [
    {}, {"file": "/private/image.png"}, {"url": "file:///private/image.png"},
    {"url": "http://images.example.org/x"}, {"url": "https://127.0.0.1/x"},
    {"url": "https://10.0.0.1/x"}, {"url": "https://169.254.169.254/x"},
    {"url": "https://[::1]/x"}, {"url": "https://localhost/x"},
    {"url": "https://2130706433/x"}, {"url": "https://0x7f000001/x"},
    {"url": "https://127.1/x"}, {"url": "https://user:secret@images.example.org/x"},
    {"url": "https://images.example.org\\@127.0.0.1/x"},
    {"url": "https://images.example.org/\u200bx"}, {"url": "https://100.64.0.1/x"},
])
def test_媒体不可重放整条拒绝且不读文件或内网(data: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins
    import socket

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("拒绝材料不能读取文件或访问网络")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(builtins, "open", forbidden)
    result = build_source_material([{"type": "text", "data": {"text": "头"}},
                                    {"type": "image", "data": data}])
    assert isinstance(result, PrepareRejected)
    assert "secret" not in result.reason


def test_卡片路径不再由本模块构造材料() -> None:

    segments = [{"type": "forward", "data": {"id": "resource-test-A", "content": [
        {"type": "reply"}, {"type": "at", "data": {}}, {"type": "unknown", "data": "内部私密"},
    ]}}]
    assert isinstance(build_source_material(segments), PrepareRejected)
    assert isinstance(build_source_material(segments, verbatim_mentions=True), PrepareRejected)
    assert "forward_resource_id" not in SourceMaterial.__slots__
    rejected = source_outbound_segments(SourceMaterial(kind="forward"))
    assert isinstance(rejected, PrepareRejected)


def test_输出段是冻结拷贝不共享材料对象() -> None:
    raw = [{"type": "text", "data": {"text": "正文"}}]
    material = build_source_material(raw, verbatim_mentions=True)
    assert not isinstance(material, PrepareRejected)
    segments = source_outbound_segments(material)
    assert not isinstance(segments, PrepareRejected)
    segments[0]["data"]["text"] = "改写"
    assert material.segments[0]["data"]["text"] == "正文"


def test_卡片节点逐字段构造且内容段深拷贝隔离() -> None:
    expanded: dict[str, Any] = {"messages": [
        {"sender": {"user_id": "10001", "card": "群友甲", "nickname": "甲的昵称"},
         "message": [{"type": "text", "data": {"text": "内层正文"}}]},
        {"sender": {"user_id": 10002, "nickname": "群友乙"},
         "message": [{"type": "image", "data": {"url": "https://multimedia.example/x.png", "file": "x.png"}}]},
    ]}
    original = deepcopy(expanded)
    nodes = build_card_nodes(expanded)
    assert not isinstance(nodes, PrepareRejected)
    assert nodes == (
        {"type": "node", "data": {"uin": "10001", "name": "群友甲",
                                  "content": [{"type": "text", "data": {"text": "内层正文"}}]}},
        {"type": "node", "data": {"uin": "10002", "name": "群友乙",
                                  "content": [{"type": "image", "data": {
                                      "url": "https://multimedia.example/x.png", "file": "x.png"}}]}},
    )
    assert expanded == original
    assert nodes[0]["data"]["content"][0] is not expanded["messages"][0]["message"][0]
    nodes[0]["data"]["content"][0]["data"]["text"] = "改写"
    assert expanded["messages"][0]["message"][0]["data"]["text"] == "内层正文"


def test_展开查询的messages段列表直接可用() -> None:
    messages: list[dict[str, Any]] = [
        {"sender": {"user_id": "10001", "nickname": "甲"},
         "message": [{"type": "text", "data": {"text": "正文"}}]},
    ]
    nodes = build_card_nodes(messages)
    assert not isinstance(nodes, PrepareRejected)
    assert nodes[0]["data"] == {"uin": "10001", "name": "甲",
                                "content": [{"type": "text", "data": {"text": "正文"}}]}


@pytest.mark.parametrize("sender", [
    None, {}, "10001", 10001, True, {"user_id": None}, {"user_id": ""}, {"user_id": " "},
    {"user_id": "12 34"}, {"user_id": True}, {"user_id": ["10001"]},
    {"user_id": "1" * 129}, {"user_id": '10001"'}, {"user_id": "100\n01"},
])
def test_卡片节点发送者不可确认整条拒绝(sender: object) -> None:
    expanded: dict[str, Any] = {"messages": [
        {"sender": sender, "nickname": "甲", "message": [{"type": "text", "data": {"text": "正文"}}]},
    ]}
    result = build_card_nodes(expanded)
    assert isinstance(result, PrepareRejected)
    assert result.error == "sender_unconfirmed"


def test_卡片节点显示名取card优先回退nickname并压平限长() -> None:
    expanded: dict[str, Any] = {"messages": [
        {"sender": {"user_id": "10001", "card": "  卡片名\t", "nickname": "昵称"},
         "message": [{"type": "text", "data": {"text": "正文"}}]},
        {"sender": {"user_id": "10002", "card": "  ", "nickname": "昵称乙"},
         "message": [{"type": "text", "data": {"text": "正文"}}]},
        {"sender": {"user_id": "10003", "nickname": "面\n在"},
         "message": [{"type": "text", "data": {"text": "正文"}}]},
        {"sender": {"user_id": "10004", "nickname": "群" * 200},
         "message": [{"type": "text", "data": {"text": "正文"}}]},
    ]}
    nodes = build_card_nodes(expanded)
    assert not isinstance(nodes, PrepareRejected)
    assert [node["data"]["name"] for node in nodes] == ["卡片名", "昵称乙", "面 在", "群" * 128]


@pytest.mark.parametrize("sender", [
    {}, {"nickname": "  "}, {"card": ""}, {"card": None}, {"nickname": 123},
    {"card": "\n\t", "nickname": None},
])
def test_卡片节点显示名不可确认整条拒绝(sender: dict[str, object]) -> None:
    expanded: dict[str, Any] = {"messages": [
        {"sender": {**sender, "user_id": "10001"}, "message": [{"type": "text", "data": {"text": "正文"}}]},
    ]}
    result = build_card_nodes(expanded)
    assert isinstance(result, PrepareRejected)
    assert result.error == "sender_name_unconfirmed"


@pytest.mark.parametrize("expanded", [
    None, [], "messages", {}, {"messages": None}, {"messages": []}, {"messages": {}},
    {"messages": [None]}, {"messages": ["节点"]},
    {"messages": [{"sender": {"user_id": "10001", "nickname": "甲"}}]},
    {"messages": [{"sender": {"user_id": "10001", "nickname": "甲"}, "message": []}]},
    {"messages": [{"sender": {"user_id": "10001", "nickname": "甲"}, "message": "正文"}]},
    {"messages": [{"sender": {"user_id": "10001", "nickname": "甲"}, "message": [None]}]},
])
def test_展开查询结构不符整条拒绝(expanded: object) -> None:
    assert isinstance(build_card_nodes(expanded), PrepareRejected)


def test_嵌套forward段随内容原样保留() -> None:
    inner: dict[str, Any] = {"type": "forward", "data": {"id": "old-res-id-xyz", "content": [
        {"type": "text", "data": {"text": "内层"}},
    ]}}
    expanded: dict[str, Any] = {"messages": [{"sender": {"user_id": "10001", "nickname": "甲"}, "message": [
        {"type": "text", "data": {"text": "头"}},
        inner,
        {"type": "mface", "data": {"emoji_id": "x"}},
        {"type": "json", "data": "{\"app\":\"com.tencent.gift\"}"},
    ]}]}
    original = deepcopy(expanded)
    nodes = build_card_nodes(expanded)
    assert not isinstance(nodes, PrepareRejected)
    assert nodes[0]["data"]["content"] == expanded["messages"][0]["message"]
    assert nodes[0]["data"]["content"][1] is not inner
    assert expanded == original
    nodes[0]["data"]["content"][1]["data"]["id"] = "改写"
    assert inner["data"]["id"] == "old-res-id-xyz"
