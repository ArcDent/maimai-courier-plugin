"""第 7 组验收支持：场景报告（每条场景记录输入、发送次数、权威账本与工具结果）。

验收要求「不仅断言函数返回布尔值」：本模块把一次场景的观察点收成 `ScenarioReport`：

- `input`：本场景的输入材料（合成值）；
- `source_sends` / `comment_sends`：模拟网关实际收到的源/评论发送（按阶段区分）；
- `ledger_rows`：经**独立连接**读到的服务账本投递表事实；
- `record`：服务端只读查询返回的权威投递结果（`None` 表示没有记录）；
- `tool`：`forward_message` 的工具结果面。

报告只读取公共观察点（模拟网关的发送记录、独立账本连接、服务只读查询、工具返回），
不依赖任何私有函数；全部为本地合成数据，不连接真实群、不发送真实消息。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from tests.service_support import STAGE_COMMENT, STAGE_PLATFORM_COMMIT, state_ledger_rows

SOURCE_SEND_STAGE = STAGE_PLATFORM_COMMIT
"""源发送在模拟网关上记录的阶段名（`FakeOneBot.send_msg` 的源闸门，复用既有常量）。"""

COMMENT_SEND_STAGE = STAGE_COMMENT
"""评论发送在模拟网关上记录的阶段名。"""


@dataclass(frozen=True, slots=True)
class ScenarioReport:
    """一条验收场景的报告：输入、源/评论发送、权威账本、工具结果。"""

    label: str
    input: Any
    source_sends: tuple[tuple[Mapping[str, Any], ...], ...]
    comment_sends: tuple[str, ...]
    ledger_rows: tuple[tuple[str, str, str], ...]
    record: Mapping[str, Any] | None
    tool: Mapping[str, Any]

    @property
    def source_send_count(self) -> int:
        """模拟网关实际收到的源发送次数。"""

        return len(self.source_sends)

    @property
    def comment_send_count(self) -> int:
        """模拟网关实际收到的评论发送次数。"""

        return len(self.comment_sends)

    def as_dict(self) -> dict[str, Any]:
        """把报告转成可打印的纯数据（证据记录用）。"""

        return {
            "label": self.label,
            "input": self.input,
            "source_sends": [list(segments) for segments in self.source_sends],
            "comment_sends": list(self.comment_sends),
            "ledger_rows": [list(row) for row in self.ledger_rows],
            "record": dict(self.record) if self.record is not None else None,
            "tool": dict(self.tool),
        }

    def summary(self) -> str:
        """一行摘要：输入、发送次数、账本状态与工具状态（断言信息用）。"""

        ledger = ",".join(f"{row[1]}:{row[2]}" for row in self.ledger_rows) or "无记录"
        record = str(self.record.get("status")) if self.record else "无"
        return (
            f"[{self.label}] 输入={self.input!r} 源发送={self.source_send_count} "
            f"评论发送={self.comment_send_count} 账本={ledger} 查询={record} "
            f"工具={self.tool.get('status')}/{self.tool.get('comment_status', '')}"
        )


def source_sends_of(fixture: Any, *, target_group_id: str | None = None) -> tuple[tuple[Mapping[str, Any], ...], ...]:
    """模拟网关记录的源发送段序列（按阶段识别，不依赖调用顺序）。"""

    wanted = target_group_id or fixture.target_group_id
    return tuple(
        tuple(call.detail.get("message") or ())
        for call in fixture.onebot.send_calls
        if call.target == wanted and call.detail.get("stage") == SOURCE_SEND_STAGE
    )


def comment_sends_of(fixture: Any, *, target_group_id: str | None = None) -> tuple[str, ...]:
    """模拟网关记录的评论发送文本（评论阶段恰为单文本段）。"""

    wanted = target_group_id or fixture.target_group_id
    texts: list[str] = []
    for call in fixture.onebot.send_calls:
        if call.target != wanted or call.detail.get("stage") != COMMENT_SEND_STAGE:
            continue
        segments = list(call.detail.get("message") or ())
        if len(segments) == 1 and segments[0].get("type") == "text":
            texts.append(str((segments[0].get("data") or {}).get("text") or ""))
        else:
            texts.append(f"非单文本段：{segments!r}")
    return tuple(texts)


def scenario_report(
    fixture: Any,
    *,
    label: str,
    tool: Mapping[str, Any],
    input: Any = None,
    record: Mapping[str, Any] | None = None,
    message_id: str = "m-source-1",
) -> ScenarioReport:
    """收齐一条场景的观察点：发送、独立账本行、服务只读查询与工具结果。"""

    rows = tuple(state_ledger_rows(fixture.service.state_dir, message_id=message_id))
    return ScenarioReport(
        label=label,
        input=input,
        source_sends=source_sends_of(fixture),
        comment_sends=comment_sends_of(fixture),
        ledger_rows=rows,
        record=record,
        tool=dict(tool),
    )


def assert_report_consistent(report: ScenarioReport) -> None:
    """报告自洽检查：发送次数必须能被账本与工具状态解释（防止漏记观察点）。

    - 有源发送且工具不是明确失败/拒绝时，账本必须有该源消息的投递行；
    - 没有源发送时，账本不得出现 `sent` 行（未发送不得伪造成功事实）；
    - 评论发送次数不得超过源发送次数（评论附属于源投递事实）。
    """

    statuses = {row[1] for row in report.ledger_rows}
    if report.source_send_count and report.tool.get("status") not in ("failed", "rejected"):
        assert report.ledger_rows, f"有源发送却没有账本记录：{report.summary()}"
    if not report.source_send_count:
        assert "sent" not in statuses, f"未发送却出现成功记录：{report.summary()}"
    assert report.comment_send_count <= report.source_send_count, (
        f"评论发送次数不得超过源发送次数：{report.summary()}"
    )


def delivery_query_count(transport: Any) -> int:
    """客户端发起的「同一投递只读查询」次数（路径恰为 `/v1/forwards/<id>`）。"""

    import re

    return sum(
        1
        for path in transport.paths()
        if re.fullmatch(r"/v1/forwards/(?!prepare)[^/]+", str(path))
    )


def rendered(report: ScenarioReport) -> str:
    """把报告渲染成文本（断言「敏感值不出现在发送材料里」用）。"""

    import json

    return json.dumps(report.as_dict(), ensure_ascii=False)


def sequence_texts(report: ScenarioReport) -> Sequence[str]:
    """源发送里的文本段文本（按发送顺序展平）。"""

    texts: list[str] = []
    for segments in report.source_sends:
        for segment in segments:
            if segment.get("type") == "text":
                texts.append(str((segment.get("data") or {}).get("text") or ""))
    return tuple(texts)
