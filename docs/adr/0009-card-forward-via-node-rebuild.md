---
status: accepted
date: 2026-10-05
---

# 卡片消息转发改走展开查询与节点重组（send_group_forward_msg）

## 背景与动机

2026-10-05 真实部署排障：源群里由**其他群**转来的含图合并转发卡片，经当前路径
`forward_group_single_msg`（按源消息标识重建）转发全部失败——平台返回
`retcode=100 "HTTP download failed: 400"`，即平台重建卡片时要下载卡片内部的旧
签名 URL 被拒。对同一条消息的四条通道实测（证据
`docs/snowluma-http-api-catalog.md` 6.5/6.6，私有报告
`.local/report/card-forward-rebuild-report.md`）：

| 通道 | 结果 |
| --- | --- |
| `forward_group_single_msg`（原路径） | `failed`/100（下载旧签名 URL 被拒） |
| `send_msg` 直引旧卡片 res_id | 信封 `ok` 且四项回执全过，群内却是降级空壳（假成功） |
| `upload_forward_msg` 单独一步 | `failed`/100（`private image target uid is missing`） |
| `send_group_forward_msg` 投展开节点 | `ok`，目标群内容完整 |

跨群含图卡片（群里「转发的转发」）是最高频形态，原路径下这类消息全部明确失败，
产品可用性缺口明确。

## 决策

1. 卡片路径改为「`get_forward_msg` 展开查询 → `build_card_nodes` 结构校验与发送者
   署名 → `send_group_forward_msg` 一次提交目标群与节点数组」。固定动作表变七动作：
   移除 `forward_group_single_msg`，新增只读 `get_forward_msg`；一次投递动作数不
   增加（展开是只读，发送仍一个）。
2. 契约层显式禁止 `send_msg` 直引旧卡片 res_id：实测证明该通道信封成功、四项回执
   （message_id/群/账号/正整数 message_seq）全部通过，但群内展示降级空壳——回执
   核验无法识别内容降级，属假成功陷阱，永不作为卡片路径。
3. 嵌套卡片不拒绝：展开只取一层，内层 forward 段作为节点内容原样提交（实测平台
   接受），插件不递归展开、不重建内层；内层在目标群的展示不作保证（归 U6）。
4. 媒体边界收紧：节点 content 与其内媒体引用只来自 `get_forward_msg` 的可信查询
   结果，MUST NOT 接受调用者任意 URL、本机路径或资源 ID。
5. `res_id`/`forward_id` 只是平台资源标识：不入账、不进工具结果、不作成功证据。

## 代价与不变量

- 代价：重组是「结构性搬运」而非字节级原资源引用；外层预览与内层展示以真实联验
  为准，不写进产品承诺。
- 不变量：一次源仍最多一个发送动作；不自动重试、不切换动作或通道；四项回执核验
  （目标消息 ID 按原值逐字、负值照比）；`build_card_nodes` 的 sender 标量化与
  name 呈现（card 优先、压平限长）与文字路径同口径；顶层唯一卡片判定不变。

## 未确认项

U3–U8 维持未确认；内层卡片在目标群客户端的真实展示、卡片内 at 是否触发客户端
提醒、大卡片的展开耗时与 body 上限，均待授权联验。本决策不授权部署或发群；离线
测试通过不等于真实联验完成。
