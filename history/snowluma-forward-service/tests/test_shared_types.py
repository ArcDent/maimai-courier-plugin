"""2.1 共享层：纯投递类型、时钟与结果渲染只有一份定义，且无 SDK 环境可导入。

判据来自 design D7/D8/D9 与第 1 组 1.5 的隔离装载证据：

- 投递身份仍是四字段（账号 + 源群 + 源消息 ID + 目标群），评论与请求 ID 不参与；
- 账本状态仍是 `sending` / `sent` / `failed` / `unknown` 四态，发送状态与账本状态
  共用同一声明；
- 结果面的完整成功口径不变（只有 `sent` / `already_forwarded` 是完整成功）；
- 插件侧既有导入入口（`clock` / `delivery_ledger` / `forward_delivery`）与共享层
  必须是**同一对象**，不允许出现第二套并行定义；
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

SHARED_EXPORT_NAMES = (
    "Clock",
    "DeliveryKey",
    "DeliveryResult",
    "LedgerStatus",
    "render_forward_result",
)
"""2.1 seam 要求共享层公开导出的名字。"""

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
"""结果状态 → 是否完整成功（design D8：只有 sent / already_forwarded 算完整成功）。"""


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


def test_共享层时钟与插件侧时钟是同一对象() -> None:
    import clock as plugin_clock
    import shared.clock

    assert plugin_clock.Clock is shared.clock.Clock
    assert plugin_clock.SystemClock is shared.clock.SystemClock


def test_插件侧兼容导入入口与共享层同一对象() -> None:
    import delivery_ledger
    import forward_delivery
    import shared.delivery
    import shared.result

    assert delivery_ledger.DeliveryKey is shared.delivery.DeliveryKey
    assert delivery_ledger.LedgerStatus is shared.delivery.LedgerStatus
    assert delivery_ledger.LedgerUnavailable is shared.delivery.LedgerUnavailable
    assert delivery_ledger.LedgerUnavailableError is shared.delivery.LedgerUnavailableError
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


def test_共享层在无SDK解释器里可导入() -> None:
    code = "\n".join(
        [
            "import json, sys",
            "import shared, shared.clock, shared.delivery, shared.result",
            "try:",
            "    import maibot_sdk",
            "except ImportError:",
            "    sdk_present = False",
            "else:",
            "    sdk_present = True",
            "print(json.dumps({",
            "    'sdk_present': sdk_present,",
            "    'exports': sorted(name for name in "
            + repr(SHARED_EXPORT_NAMES)
            + " if hasattr(shared, name)),",
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
    assert payload["sdk_present"] is False, "服务进程环境不得依赖 maibot_sdk"
    assert payload["exports"] == list(SHARED_EXPORT_NAMES)
