## Why

ws 直连（ADR 0007）要求插件自建 echo 配对、事件广播隔离、连接代次与接收循环，复杂度集中在传输层。2026-10-04 对运行实例（SnowLuma 1.14.17-node，arclinux）的授权实测证明：HTTP API 与 ws 共用同一动作表与应答信封、Bearer 头认证被接受、全部所需发送动作都返回可核验的真实 `message_id`（四项回执证据齐全）；同时平台提供 `forward_group_single_msg` 与 `send_forward_msg`，可按消息类型分派，使发送链路比「统一 `send_msg` 段直发 + 插件自建材料」更短。依据实测与用户逐项确认的决策，本次把通道与发送动作一并替换。

## What Changes

- **BREAKING** 通道全面替换：正向 ws 直连 → SnowLuma HTTP API 直连（`POST /{action}`，`Authorization: Bearer`）；删除 echo 配对、事件广播隔离、连接代次与接收循环；「断线不重放」语义转为「超时/断连不重试」。
- 发送动作按源消息类型分派（工具三参数不变）：
  - 合并转发卡片 → `forward_group_single_msg(message_id, group_id)`，卡片由平台按源消息标识重建，插件不接触资源 ID；
  - 普通文字 → `send_forward_msg` 单节点卡片，节点按**源发送者**身份署名（uin/name 只取自源消息），at 等内容段**原样保留**，不做提及文本化；
  - 普通图片（含图文混排）→ `send_msg` 段直发，at 段沿用 `send_material` 文本化，不向目标群传播硬提醒；
  - 评论 → 源成功落账后固定 0.5 秒，以普通文本 `send_msg` 单独发送。
- `upload_forward_msg` 退出主链路：`send_forward_msg` 一步完成「上传+发送」，不存在「上传成功、发送未提交」中间态。
- **BREAKING** 配置：`[snowluma]` 的 `websocket_url` 更名为 `http_url`（一次性切换，无兼容期），`plugin.config_version` 提升。
- 术语：CONTEXT.md「原卡片转发」更名为「卡片消息转发」，机制描述从「插件直引顶层资源」改为「平台按源消息标识重建、插件不提取不改写」。
- 实现红线（均有实测依据）：显式禁跟随重定向（aiohttp 3.14.3 对 307/308 默认重发 POST+Authorization+请求体）；客户端自设 body 上限（服务端上限约 2–3MB，超限直接断连无结构化错误）；`message_id` 可为负、按原值照比；卡片外层预览参数（`summary`/`news`/`prompt`/`source`）一律不设置。

## Capabilities

### New Capabilities

（无。新行为全部落在既有两个能力的既有需求上。）

### Modified Capabilities

- `snowluma-direct-client`：「正向连接与受控认证」从 ws 握手/重定向/证书口径改为 HTTP 端点、禁重定向与 body 上限口径；「动作关联与事件隔离」随通道消失（HTTP 天然一一对应）；「同账号查询与源材料可信性」中卡片材料收缩为只取源消息标识；「真实回执与安全结果分类」明示 `message_id` 可为负、按原值核验。
- `cross-group-forward`：「支持消息类型与完整性」改写——卡片消息转发定义、普通文字卡片化（at 原样、不做文本化）、普通图片段直发（at 文本化保留）、卡片内 at 的客户端提醒行为列为不保证项。

## Impact

- **代码**：`snowluma_transport.py` 重写为 HTTP 直连客户端（固定动作集、安全端点判定、有界预算、令牌仅经认证头）；`snowluma_client.py` 增加按源消息类型的发送分派；`forward_precheck.py` 卡片材料收缩（只取标识与归属）；`send_material.py` 收缩为只服务图片直发路径；`config.py` 键改名与 `config_version` 提升；`plugin.py` 组合根装配调整；`shared/` 结果与类型按需微调。
- **配置与文档**：`config.toml` 样例、`README.md`、`docs/direct-snowluma-operations.md`、ADR 0008 之后的使用口径。
- **测试**：传输、客户端、预检与真实回环端点的验收用例按 HTTP 重写；仍只用本地合成端点、不安装真实 SDK。
- **依赖**：不变（`aiohttp>=3.14.3`，`_manifest.json` 唯一入口）。
- **未确认项（不因本变更改变）**：U3–U8 全部维持未确认；卡片内 at 是否触发客户端 @ 提醒待真机确认（归 U6）；图片只传递来自可信 OneBot 源消息的材料，绝不把调用者任意 URL 交给服务端下载。
