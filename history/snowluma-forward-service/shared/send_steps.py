"""服务端两个发送接缝的公开签名（design D3/D9）。

真实平台发送只出现在服务端 SnowLuma 网关模块（第 4 组）；本模块只定义输入、输出与
契约外状态的收窄口径，不发送、不连接、不导入 SDK。

- 发送结果复用共享层 `SourceSendResult`（`sent` / `failed` / `unknown`），不新增
  第二套结果类型；
- 材料只描述「已核实的可信源材料」：卡片带资源引用，普通消息带段序列；
  服务端不从请求接受正文、资源 ID 或媒体路径；
- `normalize_send_status` 是契约外状态的唯一兜底口径：未识别取值一律待核实。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Final, Literal, cast, get_args

from .delivery import SendStatus
from .result import SourceSendResult

SourceKind = Literal["forward", "text", "image", "mixed"]
"""可信源材料种类：单张卡片资源引用 / 纯文字 / 纯图片 / 图文混排。"""

SOURCE_KIND_VALUES: Final[frozenset[str]] = frozenset(get_args(SourceKind))

SEND_STAGE_SOURCE: Final[str] = "source"
"""源发送的阶段提示（7.2 验收补丁）：**非协议必需字段**，用途只有调用归属与可观测性
（让可控传输/测试设施能区分「源发送」与「评论发送」两个接缝）；传输实现可忽略，
真实 OneBot 协议载荷不带该字段（`AiohttpOneBotTransport` 显式忽略它）。"""

SEND_STAGE_COMMENT: Final[str] = "comment"
"""评论发送的阶段提示（与源阶段同一口径）：非协议必需字段，只在控制面标识调用归属，
传输实现可忽略、不上线路。"""


@dataclass(frozen=True, slots=True)
class SourceMaterial:
    """已核实的可信源材料（来自绑定账号的网关查询，不来自客户端提交）。

    - `forward`：只带唯一顶层卡片资源引用 `forward_resource_id`，不展开内部节点；
    - `text` / `image` / `mixed`：带 OneBot 形态的已校验段序列（普通提及已文本化）。
    """

    kind: SourceKind
    forward_resource_id: str = ""
    segments: tuple[dict[str, Any], ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class SourceSendRequest:
    """源发送接缝的输入：投递/尝试定位 + 目标群 + 可信源材料。"""

    delivery_id: str
    attempt_id: str
    target_group_id: str
    material: SourceMaterial


@dataclass(frozen=True, slots=True)
class CommentSendRequest:
    """评论发送接缝的输入：投递/尝试定位 + 目标群 + 本次评论文本。"""

    delivery_id: str
    attempt_id: str
    target_group_id: str
    comment: str


SourceSendStep = Callable[[SourceSendRequest], Awaitable[SourceSendResult]]
"""服务端源发送步骤：真实实现只在服务端网关模块（`send_msg` 的资源/文字/图片段）。"""

CommentSendStep = Callable[[CommentSendRequest], Awaitable[SourceSendResult]]
"""服务端评论发送步骤：评论文本只成为单个普通文字段，不解析成消息段或命令。"""


def normalize_send_status(raw: object) -> SendStatus:
    """把发送步骤返回的状态收窄到契约值域：未识别取值一律按待核实（`unknown`）。

    契约外的字面量（空串、大小写不符、`cancelled` / `timeout` 等）可能已经产生平台
    副作用，绝不能降级成可安全重试的 `failed`。
    """

    text = raw if isinstance(raw, str) else str(raw)
    if text in get_args(SendStatus):
        return cast(SendStatus, text)
    return "unknown"
