"""卡片内容类型检查：展开查询结果的发送前判定，判定族单点定义（design D1）。

`unsupported_card_content` 输入 `get_forward_msg` 展开查询取得的应答 `data`
（含 `messages` 键的信封形态）或 `messages` 节点列表本身（每项含 `sender` 映射
与 `message` 段数组），两种形态在本模块同一处归一后判定，与
`send_material.build_card_nodes` 的输入口径一致（本模块不 import 它，依赖方向
保持单向）。命中「任一节点的 content 段数组含其它合并转发卡片」时返回 reason
常量，否则返回空串。
当前只含这一种判定；新类型沿同一判定点与同一结果面扩展，不在调用方各自复制。

reason 常量与固定对外措辞是两个独立常量：reason 给代码与日志定位，措辞只随
结果面对用户给出；调用方组装修辞面时用
`PrepareRejected(error=NESTED_FORWARD_REASON, reason=NESTED_FORWARD_DETAIL)`。

本模块只做纯判断：无 IO，不导入传输 / 客户端 / `auto_entry` / `send_material`
（依赖方向保持单向）。嵌套判定的文字契约见 delta spec「嵌套卡片发送前拒绝」：
实测嵌套内层在目标群客户端只能显示为降级空壳，因此发送前整条拒绝，不退化为
递归展开、拍平或摘要。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final


NESTED_FORWARD_REASON: Final[str] = "forward_nested_unsupported"
"""嵌套内层卡片的拒绝 reason：代码与日志定位用，不含正文，也不进对外措辞。"""

NESTED_FORWARD_DETAIL: Final[str] = "源卡片包含嵌套聊天记录，无法完整转发，不建议继续尝试"
"""嵌套内层卡片的固定对外措辞：与 reason 独立的字面量，随工具结果面给出一致说明。"""


def unsupported_card_content(expanded: object) -> str:
    """命中嵌套内层卡片返回 reason 常量；未命中与空展开返回空串。

    `expanded` 是展开查询取得的应答 `data` 或其 `messages` 节点列表：信封映射
    取 `messages` 值、其余输入原样参与判定（同一处归一，客户端 `prepare_source`
    交来的信封与直传节点列表判定一致，不因形态差异漏判）。嵌套判定只看段类型
    字段：任一节点 `message` 段数组里存在 `type == "forward"` 的段即命中，不读
    段 `data`、不递归内层。输入只读：不改写、不净化。畸形输入（非列表、节点非
    映射、`message` 非段数组、段非映射或类型字段异常）一律按「无法判定即放行」
    稳定返回空串，不抛异常；发送前的权威拒绝由调用方在展开查询后独立复判兜底。
    """

    messages = expanded.get("messages") if isinstance(expanded, Mapping) else expanded
    if not isinstance(messages, list):
        return ""
    for node in messages:
        if not isinstance(node, Mapping):
            continue
        content = node.get("message")
        if not isinstance(content, list):
            continue
        for segment in content:
            if (isinstance(segment, Mapping)
                    and str(segment.get("type") or "").strip() == "forward"):
                return NESTED_FORWARD_REASON
    return ""
