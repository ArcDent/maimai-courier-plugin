"""`type_check` 卡片内容类型检查验收（判定族单点，design D1）。

断言面只经模块公开导出：`unsupported_card_content` 输入 `get_forward_msg` 展开
查询取得的应答 `data`（含 `messages` 键的信封形态）或 `messages` 节点列表本身
（每项含 `sender` 映射与 `message` 段数组），两种形态归一后同一判定：命中「任一
节点 content 段数组含其它合并转发卡片」返回 reason 常量，否则返回空串。固定对外
措辞的字面量以 delta spec 定稿为准，reason 常量在本文件按约定字面量钉住，
两者必须是独立常量（reason 给代码/日志定位，措辞给工具结果）。

纯函数：无 IO、不接触传输与客户端；畸形输入不抛异常、不改写输入由稳定性
用例覆盖（见 `test_畸形展开输入稳定返回且输入零变更`）。
"""

from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path

import pytest

from maimai_courier_plugin.type_check import (
    NESTED_FORWARD_DETAIL, NESTED_FORWARD_REASON, unsupported_card_content,
)

_PLUGIN_MODULES = frozenset({
    "shared", "send_material", "snowluma_client", "snowluma_transport", "auto_entry",
    "forward_precheck", "forward_delivery", "plugin", "delivery_ledger", "config",
    "source_binding", "clock", "config_rules", "delivery", "identifiers", "mentions",
    "result", "send_steps",
})


def test_嵌套卡片命中返回固定reason() -> None:
    messages = [
        {"sender": {"user_id": "10001", "nickname": "群友甲"}, "message": [
            {"type": "text", "data": {"text": "看这个"}},
            {"type": "forward", "data": {"id": "F-2026-0001"}},
        ]},
    ]
    assert unsupported_card_content(messages) == "forward_nested_unsupported"


def test_固定对外措辞与reason是两个独立常量() -> None:
    # 措辞字面量来自 delta spec「嵌套卡片发送前拒绝」场景；reason 字面量由本变更定义。
    assert NESTED_FORWARD_DETAIL == "源卡片包含嵌套聊天记录，无法完整转发，不建议继续尝试"
    assert NESTED_FORWARD_REASON == "forward_nested_unsupported"
    assert NESTED_FORWARD_REASON != NESTED_FORWARD_DETAIL


def test_非嵌套卡片返回空串() -> None:
    messages = [
        {"sender": {"user_id": "10001"}, "message": [
            {"type": "text", "data": {"text": "普通文字"}},
            {"type": "at", "data": {"qq": "all"}},
            {"type": "image", "data": {"url": "https://images.example.org/a"}},
            {"type": "reply", "data": {"id": "9001"}},
        ]},
        {"sender": {"user_id": "10002"}, "message": [{"type": "text", "data": {"text": "第二条"}}]},
    ]
    assert unsupported_card_content(messages) == ""


def test_信封形态含嵌套返回拒绝() -> None:
    # 客户端 prepare_source 交来的正是应答 data 信封（get_forward_msg 应答 data 的
    # 原形态）：归一后必须与裸列表同一判定，不能因输入形态差异让嵌套检查静默失效。
    envelope = {"messages": [
        {"sender": {"user_id": "10001", "nickname": "群友甲"}, "message": [
            {"type": "text", "data": {"text": "看这个"}},
            {"type": "forward", "data": {"id": "F-2026-0003"}},
        ]},
    ]}
    assert unsupported_card_content(envelope) == NESTED_FORWARD_REASON


def test_信封非嵌套内容返回空串() -> None:
    # 对称契约：同一信封归一后不含嵌套段时与裸列表一样放行，不因形态变化误拒。
    envelope = {"messages": [
        {"sender": {"user_id": "10001"}, "message": [
            {"type": "text", "data": {"text": "普通文字"}},
            {"type": "at", "data": {"qq": "all"}},
        ]},
        {"sender": {"user_id": "10002"}, "message": [{"type": "text", "data": {"text": "第二条"}}]},
    ]}
    assert unsupported_card_content(envelope) == ""


def test_空展开返回空串() -> None:
    assert unsupported_card_content([]) == ""
    assert unsupported_card_content({"messages": []}) == ""


@pytest.mark.parametrize(
    "messages",
    [
        None,
        "节点序列",
        42,
        {},
        {"messages": None},
        {"messages": "文字"},
        [None],
        ["节点"],
        [{}],
        [{"sender": {"user_id": "10001"}}],
        [{"message": None}],
        [{"message": "文字"}],
        [{"message": [None]}],
        [{"message": [42]}],
        [{"message": [{}]}],
        [{"message": [{"type": None}]}],
        [{"message": [{"type": 42}]}],
        [{"message": [{"type": "text", "data": "不是映射"}]}],
        [{"sender": "不是映射", "message": [{"type": "forward"}]}],
    ],
)
def test_畸形展开输入稳定返回且输入零变更(messages: object) -> None:
    # 段不是字典、data 不是映射、标识字段缺失或类型错误、信封里 messages 不是
    # 列表：一律稳定放行或拒绝，不抛异常；深拷贝比对确认输入对象零变更（不改写、不净化）。
    original = deepcopy(messages)
    rejection = unsupported_card_content(messages)
    assert rejection in ("", NESTED_FORWARD_REASON)
    assert messages == original


def test_嵌套命中也不改写输入() -> None:
    messages = [{"sender": {"user_id": "10001"}, "message": [{"type": "forward", "data": {"id": "F-2026-0002"}}]}]
    original = deepcopy(messages)
    assert unsupported_card_content(messages) == NESTED_FORWARD_REASON
    assert messages == original


def test_模块不依赖插件侧模块() -> None:
    # 判定族单点的单向依赖（design D1）：只做纯判断，不 import 传输 / 客户端 /
    # auto_entry / send_material，当前实现也没有 import shared 子模块的必要。
    source = (Path(__file__).resolve().parents[1] / "type_check.py").read_text(encoding="utf-8")
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source, filename="type_check.py")):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.update(node.module.split("."))
    plugin_modules = roots & _PLUGIN_MODULES
    assert plugin_modules == set(), f"type_check 不得依赖插件侧模块: {sorted(plugin_modules)}"
