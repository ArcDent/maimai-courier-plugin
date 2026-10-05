## Why

2026-10-05 真实部署排障实测（`docs/snowluma-http-api-catalog.md` 6.5/6.6）证明：
当前卡片转发路径（`forward_group_single_msg`，平台按源消息标识重建）对**跨群转来的
含图卡片**必然失败——平台重建时下载卡片内部旧签名 URL 得到
`retcode=100 "HTTP download failed: 400"`；而 `send_msg` 直引旧 res_id 是假成功
（信封 ok、四项回执全过、群内却是降级空壳），永不可用。唯一实测可用的通道是
`send_group_forward_msg` 投递 `get_forward_msg` 展开后的节点（平台按目标群上下文
重新上传卡片内媒体）。跨群含图卡片恰是群里最高频的「转发的转发」，当前路径下这类
消息全部明确失败，产品可用性缺口明确。私有排障报告（含 Q2 嵌套实测）在
`.local/report/card-forward-rebuild-report.md`，证据已同步公开文档。

## What Changes

- **BREAKING（契约）**：卡片消息转发的机制从「只提交源消息标识、平台按标识重建」
  改为「`get_forward_msg` 展开源卡片 → 节点结构校验与发送者署名拍平 →
  `send_group_forward_msg` 一步投递」；插件不递归展开、不重建内层、不改写节点
  内容；嵌套内层 forward 段原样提交，内层在目标群的展示不保证（归 U6）。
- **固定动作表变更**：移除 `forward_group_single_msg`，新增只读
  `get_forward_msg`；发送仍是一次源一个发送动作（卡片 `send_group_forward_msg`
  一步完成上传+发送，不引入 upload 中间态）。
- **材料边界**：卡片节点 content 与其内媒体引用只来自 `get_forward_msg` 的可信
  查询结果，MUST NOT 接受调用者任意 URL/资源 ID；显式禁止 `send_msg` 直引旧
  res_id（假成功陷阱）写入契约。
- **契约补缺**：发送动作额外返回的 `res_id`/`forward_id` MUST NOT 作为成功证据、
  不入账、不进结果。
- **术语**：CONTEXT.md「卡片消息转发」保留术语名，机制定义改为「按源消息展开的
  节点重组发送，平台按目标群上下文重新上传卡片内媒体」。
- 普通文字/图片路径、评论段、账本与结果口径**不变**。

## Capabilities

### New Capabilities

（无。全部改动落在既有两个能力的需求上。）

### Modified Capabilities

- `cross-group-forward`：「支持消息类型与完整性」的卡片条款重写（展开重组路径、
  嵌套内层原样提交、禁止 send_msg 直引旧 res_id、展开查询失败即整条拒绝）。
- `snowluma-direct-client`：「同账号查询与源材料可信性」新增卡片展开查询与节点
  材料口径；「真实回执与安全结果分类」补额外资源标识证据排除；「材料最小化与
  隐私」补卡片节点媒体来源边界。

## Impact

- **代码**：`snowluma_transport.py`（动作表 6→6：移除 `forward_group_single_msg`、
  新增 `get_forward_msg`）；`snowluma_client.py`（prepare 的 forward 分支、`_submit`
  分派）；`send_material.py`（卡片节点载荷构造与校验）；`forward_delivery.py` 编排
  不变；`plugin.py` 装配不变（动作面经 transport/client）。
- **配置/账本**：`[snowluma]` 无变化；四字段账本键不变、无迁移；`config_version`
  不需要因行为键变化（无新增配置项）——实现时复核。
- **测试**：transport 七动作、client 卡片分支（展开/校验/拍平/嵌套/拒绝）、
  acceptance 回环端点卡片全链、既有卡片用例改新契约。
- **文档**：AGENTS.md 模块职责与关键规则、README 支持类型、运维手册、联验清单
  （U4/U6 更新）、catalog 引用本变更归档件。
- **未确认项**：U3–U8 维持未确认；本变更不宣称联验完成——真实部署复验（尤其
  跨群含图卡片端到端与内层展示）仍待授权。
