"""共享层：纯投递类型、发送路径词汇与结果渲染只有一份定义，且无 SDK 环境可导入。

- 投递身份仍是四字段（账号 + 源群 + 源消息 ID + 目标群），评论与请求 ID 不参与；
- 账本状态仍是 `sending` / `sent` / `failed` / `unknown` 四态，发送状态与账本状态
  共用同一声明；
- 发送路径词汇 `SourceKind` 恰为三值（`forward` / `image` / `text`），与配置
  `MessageType` 同一词汇来源，`SOURCE_KIND_VALUES` 不漂移；
- 结果面的完整成功口径不变（只有 `sent` / `already_forwarded` 是完整成功），
  渲染覆盖全部工具状态与错误阶段；
- 插件侧既有导入入口（`shared.clock` / `delivery_ledger` / `forward_delivery`）与
  共享层必须是**同一对象**，不允许出现第二套并行定义；
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
                        "LedgerUnavailableError", "SendStatus"),
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

ERROR_STAGES = ("", "rejected", "source_send", "source_persist", "comment_send", "comment_persist")
"""错误阶段封闭值域（空串表示没有故障）。"""


def test_投递身份是四字段且源目标不互作填充() -> None:
    import shared.delivery

    assert [field.name for field in fields(shared.delivery.DeliveryKey)] == [
        "account_id",
        "source_group_id",
        "source_message_id",
        "target_group_id",
    ], "投递身份必须是账号 + 源群 + 源消息 ID + 目标群四字段"
    assert set(get_args(shared.delivery.LedgerStatus)) == {"sending", "sent", "failed", "unknown"}
    assert shared.delivery.SendStatus is shared.delivery.LedgerStatus, (
        "发送状态值域必须与账本状态共用同一声明"
    )


def test_发送路径词汇恰为三值且与配置候选同一来源() -> None:

    import shared.send_steps

    assert get_args(shared.send_steps.SourceKind) == ("forward", "image", "text")
    assert shared.send_steps.SOURCE_KIND_VALUES == frozenset(get_args(shared.send_steps.SourceKind))
    for kind in shared.send_steps.SOURCE_KIND_VALUES:
        material = shared.send_steps.SourceMaterial(kind=kind)
        assert material.kind == kind, "三值都必须能构造可信源材料"


def test_共享层时钟与插件侧时钟是同一对象() -> None:
    import delivery_ledger
    import forward_delivery
    import plugin
    import shared.clock

    assert shared.clock.Clock is delivery_ledger.Clock is forward_delivery.Clock is plugin.Clock
    assert shared.clock.SystemClock is plugin.SystemClock


def test_插件侧导入入口与共享层同一对象() -> None:
    import delivery_ledger
    import forward_delivery
    import shared.delivery
    import shared.result

    assert not hasattr(delivery_ledger, "SendStatus"), "账本不得重导出共享类型（0 兼容层）"
    assert forward_delivery.SendStatus is shared.delivery.LedgerStatus
    assert forward_delivery.DeliveryResult is shared.result.DeliveryResult
    assert forward_delivery.SourceSendResult is shared.result.SourceSendResult
    assert forward_delivery.render_forward_result is shared.result.render_forward_result


def test_结果面完整成功口径与已投递事实分离() -> None:
    from shared.result import DeliveryResult, render_forward_result

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


def test_结果面渲染覆盖全部错误阶段与评论文段() -> None:

    from shared.result import (
        COMMENT_DELAY_SECONDS,
        STAGE_COMMENT_PERSIST,
        STAGE_COMMENT_SEND,
        STAGE_NONE,
        STAGE_REJECTED,
        STAGE_SOURCE_PERSIST,
        STAGE_SOURCE_SEND,
        DeliveryResult,
        render_forward_result,
    )

    assert (STAGE_NONE, STAGE_REJECTED, STAGE_SOURCE_SEND, STAGE_SOURCE_PERSIST,
            STAGE_COMMENT_SEND, STAGE_COMMENT_PERSIST) == ERROR_STAGES
    assert COMMENT_DELAY_SECONDS == 0.5, "评论固定延迟是代码常量，不做成配置项"

    for stage in ERROR_STAGES:
        rendered = render_forward_result(DeliveryResult(status="failed", stage=stage))
        assert rendered["stage"] == stage, stage

    rendered = render_forward_result(
        DeliveryResult(status="partial", sent=True, target_message_id="m-target-1",
                       comment_status="unknown", comment_message_id=""),
    )
    assert (rendered["comment_status"], rendered["comment_message_id"]) == ("unknown", "")
    assert rendered["target_message_id"] == "m-target-1", "已知真实目标 ID 必须如实带回"


def test_结果面源消息与目标群各归其位() -> None:
    from shared.result import DeliveryResult, render_forward_result

    rendered = render_forward_result(
        DeliveryResult(status="rejected"),
        source_message_id="m-source-1",
        target_group_id="g-target-2",
    )

    assert rendered["source_message_id"] == "m-source-1"
    assert rendered["target_group_id"] == "g-target-2"


def test_结果面材料摘要按结构渲染且不依赖插件材料类() -> None:
    from shared.result import DeliveryResult, render_forward_result

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
