"""真实发送接缝：把预检通过的源消息交给宿主正常发送链（design D4）。

通路（第 1 / 5 组在 Host `src/plugin_runtime/capabilities/core.py` 与 SDK
`maibot_sdk/capabilities/send.py` 核实）：

- 纯文字 → `send.text`（多个文本段按换行拼成一条普通文本消息）；
- 图片 → `send.image`（单张）或 `send.hybrid`（多张图片）；
- 文字与图片混排 → `send.hybrid`（段序与段内容原样透传）；
- 合并转发卡片 → `send.forward`（节点作者与段内容原样透传）。

所有发送都显式开启 `storage_message` 与 `sync_to_maisaka_history`、并请求详细回执
（`return_details=True`）：前者保证消息落进目标群记录与 Maisaka 可读历史，后者才
拿得到真实目标消息 ID（`core.py:300` 的 `sent_message.platform_message_id`）。

**成功证据只能是非空真实目标消息 ID**：`sent=True` 而回执没有
`platform_message_id` 时按待核实处理——宿主存在「SENT 但无 external id」的合法分支
（`src/platform_io/drivers/plugin_driver.py:207-213`），bool 不能充当成功证据。

**未成功回执不等于明确未发送**：宿主把平台失败与发送成功之后的异常（如落库失败）
都归为未成功回执并吞掉原因（`src/services/send_service.py:915-919`、`:1054-1141`），
插件无法区分「明确没发出」与「可能已发出但回执丢失」——一律按待核实（`unknown`）
保守登记，绝不改走另一通路重发。只有调用宿主之前就能确定「没有发出任何消息」
（本地材料不可发送）时才返回 `failed`。
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Final

from .forward_delivery import SourceSendResult
from .forward_precheck import PreparedForward

SEND_OPTIONS: Final[dict[str, Any]] = {
    "return_details": True,
    "storage_message": True,
    "sync_to_maisaka_history": True,
}
"""一次发送的固定选项：五个调用点共用同一来源（design D4）。

- `return_details=True`：请求详细回执，只有它才带回真实目标消息 ID；
- `storage_message=True`：显式开启消息存储，发送成功的消息必须落进目标群消息记录；
- `sync_to_maisaka_history=True`：显式开启 Maisaka 历史同步（宿主默认值为 False，
  必须逐次显式打开）。
"""


async def send_prepared_forward(ctx: Any, prepared: PreparedForward) -> SourceSendResult:
    """按材料种类选择已核实的发送通路，返回发送步骤结果（本插件唯一的源发送实现）。"""

    material = prepared.material
    stream_id = prepared.target.session_id
    if material.kind == "forward":
        return await _deliver(
            ctx.send.forward([dict(node) for node in material.nodes], stream_id, **SEND_OPTIONS)
        )
    if material.kind == "text":
        text = _join_text_segments(material.segments)
        if not text.strip():
            # 调用宿主之前即可确定没有可发送内容：明确未发送，不产生任何副作用。
            return SourceSendResult(status="failed", reason="源消息没有可发送的文本内容")
        return await _deliver(ctx.send.text(text, stream_id, **SEND_OPTIONS))

    images = [
        str(segment.get("binary_data_base64") or "").strip()
        for segment in material.segments
        if str(segment.get("type") or "") == "image"
    ]
    # 单图、多图与图文混排共用同一条本地兜底：预检已要求图片带非空
    # `binary_data_base64`（`send_material._has_image_material`），但纯空白字符串仍会
    # 漏过预检；此时在调用宿主之前即可确定没有可恢复的媒体数据 → 明确未发送。
    # 多图/混排也必须整条拒绝：不得只丢这一段，也不得把不可恢复的段交给宿主。
    if any(not image for image in images):
        return SourceSendResult(status="failed", reason="源消息的图片缺少可恢复的媒体数据")
    if material.kind == "image" and len(images) == 1:
        return await _deliver(ctx.send.image(images[0], stream_id, **SEND_OPTIONS))
    return await _deliver(
        ctx.send.hybrid([dict(segment) for segment in material.segments], stream_id, **SEND_OPTIONS)
    )


async def send_comment_text(ctx: Any, stream_id: str, text: str) -> SourceSendResult:
    """评论发送步骤：按普通文本发送，不解析成命令或消息段（spec 可选评论固定延迟）。"""

    if not text.strip():
        return SourceSendResult(status="failed", reason="评论内容为空，未发送")
    return await _deliver(
        ctx.send.text(text, stream_id, **SEND_OPTIONS),
        phase="评论",
    )


async def _deliver(call: Awaitable[Any], *, phase: str = "源消息") -> SourceSendResult:
    """执行一次宿主发送调用并解释详细回执；不吞掉取消，不换通路重发。

    `phase` 只用于中文原因里定位故障阶段：源消息发送取默认值，评论发送显式传「评论」。
    """

    try:
        result = await call
    except asyncio.CancelledError:
        raise
    except Exception as error:
        return SourceSendResult(
            status="unknown",
            reason=f"{phase}调用宿主发送能力异常（{type(error).__name__}），无法确认是否已发出",
        )
    return interpret_send_result(result, phase=phase)


def interpret_send_result(result: Any, *, phase: str) -> SourceSendResult:
    """把详细回执（SDK 归一后的 `{"sent", "message_id"}`）解释成发送步骤结果。

    真实发送链的回执经 SDK 归一后只保留 `sent` 与 `message_id` 两个键
    （`maibot_sdk/context.py:287-291`），因此这里只认 `sent`：契约外回执（例如只带
    `success`）没有可核实的成功回执语义，一律走保守的待核实分支——bool 与随附的
    `message_id` 都不能充当成功证据。
    """

    if isinstance(result, dict):
        sent = bool(result.get("sent"))
        message_id = str(result.get("message_id") or "").strip()
        if sent and message_id:
            return SourceSendResult(status="sent", target_message_id=message_id)
        if sent:
            return SourceSendResult(
                status="unknown",
                reason=f"{phase}已发出但详细回执没有真实目标消息 ID",
            )
    return SourceSendResult(
        status="unknown",
        reason=f"{phase}未获得成功回执（宿主把发送异常与平台失败都归为未成功），无法确认是否已发出",
    )


def _join_text_segments(segments: tuple[dict[str, Any], ...]) -> str:
    """把普通消息的文本段拼成一条文本消息：段序不变、段内容不删改。"""

    return "\n".join(str(segment.get("data") or "") for segment in segments)
