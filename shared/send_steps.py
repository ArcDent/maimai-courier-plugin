"""插件侧准备与发送请求的公开类型。

本模块只定义类型，不发送、不连接、不导入 SDK：HTTP 直连的真实发送由
`snowluma_client.py` 按固定八动作执行（本模块是 `SourceSendRequest` /
`CommentSendRequest` 的接收面）。

- 发送结果复用共享层 `SourceSendResult`，不新增第二套结果类型；
- 目标形态与私聊对端在 `PreparedSource` 单点持有（字面量来自
  `shared.delivery.TargetKind`），两个发送请求经只读属性透出同一份事实；
- 材料只描述已核实的可信源材料，不从客户端提交接受正文、资源 ID 或媒体路径。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Literal, get_args

from .delivery import TARGET_KIND_GROUP, TargetKind

SourceKind = Literal["forward", "image", "text"]
"""可信源材料的发送路径种类（三值分派），各取值的材料形态见 `SourceMaterial`。"""

SOURCE_KIND_VALUES: Final[frozenset[str]] = frozenset(get_args(SourceKind))


@dataclass(frozen=True, slots=True)
class SourceMaterial:
    """已核实的可信源材料（来自绑定账号的同账号查询，不来自客户端提交）。

    - `forward`：按源消息卡片标识展开查询构造的卡片节点数组（内容段按查询结果
      原样，投递时平台按目标群上下文重建卡片，插件不持有卡片资源 ID）；
    - `image`：OneBot 形态的已校验段序列（普通提及已文本化）；
    - `text`：单节点卡片节点（内容段原样含 at）。
    """

    kind: SourceKind
    segments: tuple[dict[str, Any], ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class PrepareRejected:
    """可信源准备拒绝；原因只含安全类别，不含源正文。"""

    error: str
    reason: str


@dataclass(frozen=True, slots=True)
class PreparedSource:
    """本次内存材料和可信路由；材料与发送路径固定，不参与账本键。

    `kind` 由 `material.kind` 派生（同一事实一处持有，避免两字段手工同步）；配置
    失效由配置代次 `config_generation` 承载（HTTP 无连接代次）。

    目标按形态分流，同一时刻只有一个目标字段非空：`target_group_id` 存群号（私聊
    目标为空串），`target_user_id` 存对端 QQ 号（群目标为空串）。`target_kind`
    默认 `group` 只为让群路径构造保持原样；私聊路径必须显式传 `private` 与对端。
    """

    material: SourceMaterial = field(repr=False)
    source_group_id: str
    source_message_id: str
    target_group_id: str
    account_id: str
    config_generation: int
    target_kind: TargetKind = TARGET_KIND_GROUP
    target_user_id: str = ""

    @property
    def kind(self) -> SourceKind:
        """发送路径分派值（`forward` / `image` / `text`）。"""

        return self.material.kind


@dataclass(frozen=True, slots=True)
class SourceSendRequest:
    """源发送只接收本次可信准备结果，不接受模型材料。

    目标形态与私聊对端不是本类的第二份数据，而是 `prepared` 上同一事实的只读
    透出。
    """

    prepared: PreparedSource

    @property
    def target_kind(self) -> TargetKind:
        """目标形态（`group` / `private`）：与准备结果同一事实一处持有。"""

        return self.prepared.target_kind

    @property
    def target_user_id(self) -> str:
        """私聊对端 QQ 号；群目标为空串。"""

        return self.prepared.target_user_id


@dataclass(frozen=True, slots=True)
class CommentSendRequest:
    """评论发送只接收可信准备结果与本次普通文本，随源投递的同一形态与原对端送达。

    目标形态与私聊对端只透出 `prepared` 上的同一份事实，不在请求面复制。
    """

    prepared: PreparedSource
    comment: str = field(repr=False)

    @property
    def target_kind(self) -> TargetKind:
        """目标形态（`group` / `private`）：与准备结果同一事实一处持有。"""

        return self.prepared.target_kind

    @property
    def target_user_id(self) -> str:
        """私聊对端 QQ 号；群目标为空串。"""

        return self.prepared.target_user_id
