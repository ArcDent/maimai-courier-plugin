"""插件侧准备与发送请求的公开类型。

本模块只定义类型：可信源材料、准备结果与两个发送请求，不发送、不连接、不导入
SDK，也不声明任何发送步骤协议——HTTP 直连的真实发送由 `snowluma_client.py`
按固定七动作执行（契约为 `SourceSendRequest` / `CommentSendRequest` 的接收面）。

- 发送结果复用共享层 `SourceSendResult`（`sent` / `failed` / `unknown`），不新增
  第二套结果类型；
- 材料只描述「已核实的可信源材料」：卡片带按源消息卡片标识展开查询构造的节点
  数组（投递时平台按目标群上下文重建卡片），普通消息带段序列或单节点卡片节点；
  不从客户端提交接受正文、资源 ID 或媒体路径。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Literal, get_args

SourceKind = Literal["forward", "image", "text"]
"""可信源材料的发送路径种类（三值分派）：

- `forward`：唯一顶层合并转发卡片，材料带按该源消息卡片标识展开查询构造的
  节点数组（发送者标识与显示名经压平限长，内容段按查询结果原样）；
- `image`：普通图片（含图文混排），材料带已校验段序列（提及按普通消息口径
  文本化、不触发顶层提醒）；
- `text`：其余普通消息，材料带单节点卡片节点（内容段按源消息原样提交，
  含 at 原样、不做文本化）。
"""

SOURCE_KIND_VALUES: Final[frozenset[str]] = frozenset(get_args(SourceKind))


@dataclass(frozen=True, slots=True)
class SourceMaterial:
    """已核实的可信源材料（来自绑定账号的同账号查询，不来自客户端提交）。

    - `forward`：带按源消息卡片标识展开查询构造的卡片节点数组（发送者标识与
      显示名经压平限长、内容段按查询结果原样，投递时平台按目标群上下文重建
      卡片，插件不持有卡片资源 ID）；
    - `image`：带 OneBot 形态的已校验段序列（普通提及已文本化）；
    - `text`：带单节点卡片节点 `{"type":"node","data":{…}}`（内容段原样含 at）。
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

    `kind` 由 `material.kind` 派生（同一事实一处持有，避免两字段手工同步）。
    HTTP 无连接代次：准备阶段的账号与归属核验在每次发送动作前以最新查询复验，
    配置失效由配置代次 `config_generation` 承载。
    """

    material: SourceMaterial = field(repr=False)
    source_group_id: str
    source_message_id: str
    target_group_id: str
    account_id: str
    config_generation: int

    @property
    def kind(self) -> SourceKind:
        """发送路径分派值（`forward` / `image` / `text`）。"""

        return self.material.kind


@dataclass(frozen=True, slots=True)
class SourceSendRequest:
    """源发送只接收本次可信准备结果，不接受模型材料。"""

    prepared: PreparedSource


@dataclass(frozen=True, slots=True)
class CommentSendRequest:
    """评论发送只接收可信准备结果与本次普通文本。"""

    prepared: PreparedSource
    comment: str = field(repr=False)
