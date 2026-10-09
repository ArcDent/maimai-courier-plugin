"""共享层：纯投递类型、发送路径词汇与结果渲染只有一份定义，且无 SDK 环境可导入。

- 投递身份是五字段（账号 + 源群 + 源消息 ID + 目标 + 目标形态），评论与请求 ID 不
  参与；`target_kind` 只有 `group` / `private` 两个取值、且 MUST NOT 有默认值；
- 账本状态仍是 `sending` / `sent` / `failed` / `unknown` 四态，发送状态与账本状态
  共用同一声明；
- 发送路径词汇 `SourceKind` 恰为三值（`forward` / `image` / `text`），与配置
  `MessageType` 同一词汇来源，`SOURCE_KIND_VALUES` 不漂移；
- 结果面的完整成功口径不变（只有 `sent` / `already_forwarded` 是完整成功），
  已投递事实与完整成功分离；
- 共享层在无 `maibot_sdk` 的解释器里可以直接导入（服务进程同款环境）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import fields
from pathlib import Path
from typing import Any, get_args

ROOT = Path(__file__).resolve().parent.parent

SHARED_MODULE_CORE_NAMES = {
    "shared.clock": ("Clock", "SystemClock"),
    "shared.delivery": ("DeliveryKey", "LedgerStatus", "LedgerUnavailable",
                        "LedgerUnavailableError", "SendStatus",
                        "TargetKind", "TARGET_KIND_VALUES",
                        "TARGET_KIND_GROUP", "TARGET_KIND_PRIVATE"),
    "shared.result": ("DeliveryResult", "SourceSendResult", "ToolStatus",
                      "ErrorStage", "render_forward_result", "COMMENT_DELAY_SECONDS"),
    "shared.send_steps": ("SourceKind", "SourceMaterial", "PrepareRejected",
                          "PreparedSource", "SourceSendRequest", "CommentSendRequest",
                          "SOURCE_KIND_VALUES"),
    "shared.identifiers": ("safe_identifier", "verbatim_identifier",
                           "IDENTIFIER_FORBIDDEN_CHARACTERS", "MAX_IDENTIFIER_LENGTH"),
    "shared.mentions": ("flatten_mention_text", "mention_display_text",
                        "ALL_MENTION_IDENTIFIER", "ALL_MENTION_TEXT"),
    "shared.config_rules": ("valid_env_var_name", "is_loopback_host", "is_private_host",
                            "onebot_endpoint_rejection", "ENDPOINT_SCHEMES"),
    "shared": (),
}
"""共享层要求的子模块与各自核心名字。

`shared` 包本身列为空元组：0 兼容层下包顶层不重导出任何符号（`hasattr` 反向断言），
调用方一律显式走子模块导入。
"""

RESULT_SUCCESS_EXPECTATION: dict[str, bool] = {
    "sent": True,
    "already_forwarded": True,
    "partial": False,
    "sending": False,
    "rejected": False,
    "failed": False,
    "unknown": False,
    "ledger_unavailable": False,
}
"""结果状态 → 是否完整成功（只有 sent / already_forwarded 算完整成功）。"""


def test_投递身份是五字段且源目标不互作填充() -> None:
    from maimai_courier_plugin.shared import delivery

    assert [field.name for field in fields(delivery.DeliveryKey)] == [
        "account_id",
        "source_group_id",
        "source_message_id",
        "target_group_id",
        "target_kind",
    ], "投递身份必须是账号 + 源群 + 源消息 ID + 目标 + 目标形态五字段"
    assert set(get_args(delivery.LedgerStatus)) == {"sending", "sent", "failed", "unknown"}
    assert delivery.SendStatus is delivery.LedgerStatus, (
        "发送状态值域必须与账本状态共用同一声明"
    )


def test_目标形态两值且同号群与私聊是不同投递() -> None:
    import pytest
    from maimai_courier_plugin.shared.delivery import (
        TARGET_KIND_GROUP, TARGET_KIND_PRIVATE, TARGET_KIND_VALUES,
        DeliveryKey, TargetKind,
    )

    assert get_args(TargetKind) == ("group", "private"), "目标形态只有群与私聊两个取值"
    assert TARGET_KIND_VALUES == frozenset(get_args(TargetKind))
    assert (TARGET_KIND_GROUP, TARGET_KIND_PRIVATE) == ("group", "private")

    group = DeliveryKey("10001", "111", "m-source-1", "12345", TARGET_KIND_GROUP)
    private = DeliveryKey("10001", "111", "m-source-1", "12345", TARGET_KIND_PRIVATE)
    assert group == DeliveryKey("10001", "111", "m-source-1", "12345", "group"), "构造值必须是字面量本身"
    assert group != private, "同号群与同号好友是两笔独立投递，键不能相等"

    # 目标形态 MUST NOT 有默认值：忘记传 kind 时立刻失败，不能静默按群落账。
    without_kind = ("10001", "111", "m-source-1", "12345")
    with pytest.raises(TypeError):
        DeliveryKey(*without_kind)


def test_发送路径词汇恰为三值且与配置候选同一来源() -> None:

    from maimai_courier_plugin.shared import send_steps

    assert get_args(send_steps.SourceKind) == ("forward", "image", "text")
    assert send_steps.SOURCE_KIND_VALUES == frozenset(get_args(send_steps.SourceKind))
    for kind in send_steps.SOURCE_KIND_VALUES:
        material = send_steps.SourceMaterial(kind=kind)
        assert material.kind == kind, "三值都必须能构造可信源材料"


def test_结果面完整成功口径与已投递事实分离() -> None:
    from maimai_courier_plugin.shared.result import DeliveryResult, render_forward_result

    for status, expected_success in RESULT_SUCCESS_EXPECTATION.items():
        rendered = render_forward_result(DeliveryResult(status=status))
        assert rendered["status"] == status
        assert rendered["success"] is expected_success, (status, rendered)

    partial = render_forward_result(
        DeliveryResult(status="partial", sent=True, target_message_id="m-target-1"),
    )
    assert partial["sent"] is True and partial["delivered"] is True
    assert partial["success"] is False, "评论未完成不是完整成功，但源消息已投递的事实要保留"

    unsaved = render_forward_result(
        DeliveryResult(status="unknown", sent=True, target_message_id="m-target-2"),
    )
    assert unsaved["sent"] is True and unsaved["success"] is False, (
        "已发送但状态未落盘时必须报告已知投递事实，同时不算完整成功"
    )


def test_结果面源消息与目标群各归其位() -> None:
    from maimai_courier_plugin.shared.result import DeliveryResult, render_forward_result

    rendered = render_forward_result(
        DeliveryResult(status="rejected"),
        source_message_id="m-source-1",
        target_group_id="g-target-2",
    )

    assert rendered["source_message_id"] == "m-source-1"
    assert rendered["target_group_id"] == "g-target-2"


def test_结果面材料摘要按结构渲染且不依赖插件材料类() -> None:
    from maimai_courier_plugin.shared.result import DeliveryResult, render_forward_result

    class _Material:
        kind = "forward"
        shape: tuple[Any, ...] = ({"forward": [["text"]]},)

    rendered = render_forward_result(DeliveryResult(status="sent", sent=True), material=_Material())

    assert rendered["send_kind"] == "forward"
    assert rendered["material_shape"] == [{"forward": [["text"]]}]


def test_共享层在无SDK解释器里可导入且包顶层不聚合符号() -> None:
    code = "\n".join(
        [
            "import json, sys",
            "import shared, shared.clock, shared.delivery, shared.result, shared.send_steps",
            "import shared.identifiers, shared.mentions, shared.config_rules",
            "try:",
            "    import maibot_sdk",
            "except ImportError:",
            "    sdk_present = False",
            "else:",
            "    sdk_present = True",
            "required = " + repr(SHARED_MODULE_CORE_NAMES),
            "print(json.dumps({",
            "    'sdk_present': sdk_present,",
            "    'missing': {name: [attr for attr in attrs if not hasattr(sys.modules[name], attr)]",
            "                for name, attrs in required.items() if any(not hasattr(sys.modules[name], attr) for attr in attrs)},",
            "    'aggregated': sorted(attr for attr in ('Clock', 'DeliveryKey', 'DeliveryResult',",
            "                   'SourceKind', 'render_forward_result', 'safe_identifier')",
            "                   if hasattr(shared, attr)),",
            "}))",
        ]
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["sdk_present"] is False, "共享层不得依赖 maibot_sdk"
    assert payload["missing"] == {}, f"共享层子模块缺少核心名字: {payload['missing']}"
    assert payload["aggregated"] == [], "shared 包顶层不得重导出符号（0 兼容层）"
