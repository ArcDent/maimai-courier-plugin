---
status: accepted
date: 2026-10-04
---

# 以 HTTP API 直连替换 ws 通道，并按消息类型分派发送动作

用户决定将插件的平台通道从正向 WebSocket 直连全面替换为 SnowLuma 的 HTTP API
直连，同时按源消息类型分派发送动作：合并转发卡片走
`forward_group_single_msg`，普通文字走 `send_forward_msg`（单节点卡片），普通图片
走 `send_msg` 段直发。本决定取代 [ADR 0007](0007-direct-snowluma-websocket-client.md)
的 ws 通道；三参数 `forward_message`、一次性来源凭据、白名单、账本防重与结果口径
均不改变。

依据为 2026-10-04 对 arclinux 上运行实例（SnowLuma 1.14.17-node）的授权实测，全部
记录见 [`docs/snowluma-http-api-catalog.md`](../snowluma-http-api-catalog.md) 第 6 节。
本记录确认方向，**不表示已返工、不代表 U1–U8 全部确认、不授权部署或真实联验**。

## 决策内容

1. **通道**：单一 HTTP 端点（`POST /{action}`，`Authorization: Bearer`），不建立
   任何长连接；ws 的 echo 配对、事件广播隔离、连接代次与接收循环整体删除。
2. **动作分派**（由源消息类型决定，工具三参数不变）：
   - 合并转发卡片 → `forward_group_single_msg(message_id, group_id)`；卡片由平台
     按源消息标识重建，插件不接触资源 ID；
   - 普通文字 → `send_forward_msg` 单节点卡片；节点按**源发送者**身份署名
     （uin/name 只取自源消息），at 等内容段**原样保留**，不做提及文本化；
   - 普通图片（含图文混排）→ `send_msg` 段直发；at 段沿用 `send_material` 的
     文本化，不向目标群传播硬提醒；
   - 评论 → 源成功落账后固定 0.5 秒，以普通文本 `send_msg` 单独发送。
3. **配置**：`[snowluma]` 的 `websocket_url` 更名为 `http_url`（一次性切换，无
   兼容期），`plugin.config_version` 提升；运维口径见
   `docs/direct-snowluma-operations.md` 同步修订。
4. **术语**：CONTEXT.md「原卡片转发」更名为「卡片消息转发」，机制描述从“插件直引
   顶层资源”改为“平台按源消息标识重建、插件不提取不改写”。

## 实测依据（摘要）

- 四个固定只读动作与全部候选发送动作在 HTTP 复现，信封一致；Bearer 头认证被接受
  （失败 401）。**U1 在该实例确认**。
- `send_msg`（文本/图片/forward 段）、`send_forward_msg`（自定义节点）、
  `forward_group_single_msg` 均返回真实 `message_id`，`get_msg` 四项核验
  （ID/群/账号/正整数 `message_seq`）全部通过；`message_id` **可为负**，实现不得
  假设符号。
- `upload_forward_msg` 仅上传（`message_id=0`）；用 `send_forward_msg` 一步完成
  “上传+发送”，不引入“上传成功、发送未提交”中间态，故 `upload_forward_msg`
  退出主链路。
- 卡片节点可按源发送者 uin 署名；at 段在卡片节点内原样保留（是否触发客户端 @
  提醒未确认，归 U6）。
- HTTP 应答错误分类（1404/1400/401/`retcode=100`）与既有响应校验假设一致。

## 取舍

- **删除**：echo 关联、事件隔离、多客户端响应归属、连接重建与代次失效逻辑
  （ws 侧约一半复杂度的来源）；「断线不重放」语义转为「超时/断连不重试」。
- **新增**：
  - 客户端必须显式禁跟随重定向——实测 aiohttp 3.14.3 对 307/308 默认重发 POST、
    `Authorization` 与请求体到新 Location（令牌与评论正文双重外泄）；
  - 客户端自设 body 上限——服务端上限约 2–3MB，超限**直接断连、无结构化错误**；
  - HTTP 端点（默认 3000）与 ws（3001）可能各自持有 Token，操作者须按部署填写，
    不自动选端口。
- **沿用**：一次性来源凭据、白名单与归属预检、账本占位与防重、有界预算、
  `shared/result.py` 结果渲染、unknown/failed 保守分类。

## 未确认项（不因本决定改变）

- U3–U8 全部维持未确认：账号字段含义、同账号源记录、图片安全获取（U5）、卡片
  客户端效果（U6）、真实回执与登记顺序（U7/U8）。
- 图片安全边界收紧而非放宽：插件只传递来自可信 OneBot 源消息的材料，绝不把调用者
  任意 URL 交给服务端下载。
- 单实例、单账号、一个测试群的实测快照不等于全量联验；卡片内 at 的客户端提醒行为
  待真机确认。
