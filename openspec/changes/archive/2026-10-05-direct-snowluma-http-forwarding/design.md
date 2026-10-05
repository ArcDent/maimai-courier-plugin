## Context

现状：`snowluma_transport.py` 维持一条 aiohttp 正向 ws 连接，含 echo 配对
（`实例:代次:序号`）、单待决槽、接收循环、事件判别与连接代次失效；
`snowluma_client.py` 在单一 `send_msg` 群分支上执行全部源与评论发送，卡片以
`forward` 段引用源资源 ID。实测依据（2026-10-04，arclinux，1.14.17-node）见
`docs/snowluma-http-api-catalog.md` 第 6 节与 ADR 0008：HTTP 与 ws 共用动作表与
信封；三类发送路径均返回可核验 `message_id`（可为负）；`message_id=0` 只出现在
仅上传的 `upload_forward_msg`；服务端 body 上限约 2–3MB、超限直接断连；
aiohttp 3.14.3 对 307/308 默认重发请求体与认证头。动机见 proposal.md - Why，行为
契约见本变更两份 delta spec。

## Goals / Non-Goals

**Goals:**
- 用无连接 HTTP 客户端替换 ws 传输层，删除 echo/事件/代次相关全部逻辑。
- 发送按源消息类型分派：卡片 → `forward_group_single_msg`；文字 → `send_forward_msg`
  单节点卡片；图片 → `send_msg` 段直发；评论 → `send_msg` 普通文本。
- 保持全部安全边界不变：一次性来源凭据、白名单/归属预检、账本占位与防重、
  有界预算、unknown/failed 保守分类、`shared/result.py` 统一渲染。

**Non-Goals:**
- 不新增任意动作代理、不改工具三参数、不改自动候选分类口径。
- 不实现「上传成功、发送未提交」中间态（`upload_forward_msg` 退出主链路）。
- 不承诺卡片内 at 的客户端提醒行为、外层预览差异（U6 未确认项）。
- 不处理多账号多 HTTP 端点的批量部署形态（操作者逐账号配置）。

## Decisions

### D1：传输层形态——无连接、按请求发送

`OneBotConnection` 保留（地址、`token_env_var`、可选配置令牌，repr 隐藏），新增
`http_url` 语义。客户端持有一个惰性创建的 aiohttp `ClientSession`：
`trust_env=False`（不经环境代理）、`allow_redirects=False`（D2）、按调用 close。
不使用任何后台任务：没有接收循环、心跳、重连队列与代次。
备选：每请求新建 session——连接池收益归零且 close 路径更碎；长连接 ws——正是
被替换的对象。

### D2：安全四闸——端点判定、禁重定向、body 上限、凭证仅经头

1. 端点判定扩展 `onebot_endpoint_rejection` 至 `http/https`：明文仅回环/私有/
   链路本地（建连前解析确认），公网必须 https；拒绝用户信息、查询串、片段、
   未指定监听地址。现有 ws 判定的每条规则原样映射。
2. `allow_redirects=False` + 显式处理 3xx：307/308 会重发请求体与认证头
   （实测），302 也会改方法；一律按 `configuration`/`insecure_endpoint` 类别
   拒绝，不跟随、不换主机重试。
3. 新常量 `ONEBOT_MAX_REQUEST_BYTES = 2 * 1024 * 1024`：低于实测服务端上限
   （2–3MB）留余量；请求体超限在序列化后、发出前拒绝（`failed`，未提交）。
4. 令牌只经 `Authorization: Bearer`；`token` 非空优先，空白仅请求前读
   `token_env_var`；不读其他插件配置。

### D3：固定动作表与类型分派

动作表：`get_login_info`、`get_group_list`、`get_msg`（只读）+ 发送
`send_msg`（群分支，图片与评论）、`forward_group_single_msg`（卡片）、
`send_forward_msg`（文字卡片）。没有任意动作代理；契约外动作名不可构造。
分派落点在 `snowluma_client` 的 `_execute_send` 前：按 `PreparedSource.kind`
（`forward` / `text` / `image`）选择发送实现，三者的回执核验完全共用
`_verify_target`。备选：把分派放进 `forward_delivery`——编排层会重新感知平台
差异，违背「统一结果面」；放入 transport——传输层不该知道消息类型语义。

### D4：准备阶段与材料边界

`prepare_source` 仍执行 账号（`get_login_info`）→ 目标名册
（`get_group_list`）→ 源归属（`get_msg`：ID/群/消息类型）三步只读核验，随后按
源消息内容判定 kind 并构造材料：
- `forward`：材料 = 源消息标识 + 目标群（卡片资源由平台重建，插件不持有 res_id）；
- `image`：材料 = OneBot 段数组（`send_material` 现状：顺序保留、at 文本化、
  不支持段/缺媒体整条拒绝）；
- `text`：材料 = 单节点卡片载荷 `{"type":"node","data":{"uin","name","content"}}`，
  `uin`/`name` 只取自源消息 sender（`card` 优先、回退 `nickname`，控制字符压平、
  限长），`content` 为源消息段原样拷贝（含 at）。
判定顺序：顶层唯一卡片 → forward；无卡片且含图片段 → image；否则 → text。
普通消息的 reply 段在材料构造前整条拒绝（两类普通路径共用，沿用现口径）。

### D5：回执核验与 ID 语义

复用现有 `_verify_target`：`get_msg` 四项证据（message_id / message_type=group /
group_id / user_id / 正整数 message_seq），最多 8 次、总 3 秒、间隔 0.5 秒。
新事实纳入：目标 ID 可为负——比对按原值逐字（`canonical_message_id` 已支持
int/str 原样）；`retcode=100` 类业务失败明确归类为业务结果，不冒充传输异常；
`send_forward_msg` 额外返回的 `res_id`/`forward_id` 一律不入账、不进结果
（只取 `message_id`）。

### D6：预算与并发

沿用注入时钟常量：单动作 10 秒总预算（排队/查询/发送/回执核验各自有界）、
评论 0.5 秒、卸载 5 秒、在途工具 8 个。一次投递的动作数上界：准备 3 次只读 +
源 1 次发送 + 回执 ≤8 次只读 + 评论 1 次发送 + 评论回执 ≤8 次只读；不自动重试，
查询类失败按既有口径明确拒绝。ws 特有的「事件不重置期限」随接收循环删除。

### D7：模块边界——单工具、执行层两文件

对外仍是一个 `forward_message` 工具。内部执行层保持两文件而非强行合并：
`snowluma_transport.py`（端点安全判定、固定动作调用、body/重定向/预算闸门）与
`snowluma_client.py`（类型分派、准备编排、回执核验、结果分类）。理由：前者是
可隔离替换的传输实现（测试可用内存替身），后者持有契约语义；合成单文件会让
pyright 与测试接缝同时失去边界，且与项目「小接口 + 可注入」现状冲突。
删除清单：ws 接收循环、echo/pending 槽、事件判别、连接代次、心跳相关。

### D8：配置键切换

`websocket_url` → `http_url`，`config_version` 提升（`1.5.0`）；`token` /
`token_env_var` 不动。`config.toml` 样例、`README.md`、
`docs/direct-snowluma-operations.md` 同步；旧键不再解析（无兼容期，ADR 0008）。
`SnowLumaSection.__init__` 的预校验改判 `http_url`。

### D9：术语与文档口径

CONTEXT.md「卡片消息转发」已由阶段一拍板；README 的支持类型表、结果状态表、
`docs/real-qq-validation.md` 的联验清单按 delta spec 口径更新；ADR 0008 记录
通道与动作决策，本 design 记录技术取舍。U1–U8 归档结论不改写，仅以实测快照
补充新事实（`docs/snowluma-http-api-catalog.md` 第 6 节）。

## Risks / Trade-offs

- **[body 超限被静默断连]** → 客户端 2MB 前置拒绝 + 结构化 reason；实测 3MB 起
  断连无错误码，不自测猜阈值，联验期记录真实阈值再决定是否调整常量。
- **[重定向外泄凭据与正文]** → `allow_redirects=False` + 3xx 显式拒绝；本地
  已复现 307 外泄路径，测试固化。
- **[负数 message_id 破坏既有假设]** → 比对照旧逐字；账本既有记录（正数）不受
  影响，查询按原值；测试覆盖负数与字符串两种形态。
- **[文字卡片化改变目标群观感]** → README 明示形态差异（文字=单节点卡片、
  图片=普通消息、卡片=平台重建卡片）；卡片内 at 的提醒行为列为不保证（U6）。
- **[图片 URL 由服务端下载]** → 材料只来自可信 `get_msg` 结果，绝不传递调用者
  任意 URL；签名 CDN 地址不进日志与结果（沿用现有隐私口径）。
- **[HTTP 与 ws 端点 Token 互不通用]** → 运维文档明确 3000/3001 可能各自持
  令牌；配置缺失即停发，不探测另一端口。
- **[平台动作字段漂移]** → 响应校验只认信封 + 必需字段；契约外状态一律
  unknown；联验通过前不放行真实发送。

## Migration Plan

1. 代码：transport 重写 → client 分派 → precheck/material 收缩 → config 键 →
   组合根装配；每步保持测试可运行。
2. 配置：发布说明给出 `websocket_url` → `http_url` 手工迁移一句式；无自动迁移
   （插件默认关闭、无存量启用安装，ADR 0008 已记录判断）。
3. 账本：四字段键不变、`target_message_id` 允许负值不改变语义；无迁移动作。
4. 回滚：回退到变动前代码版本即可；账本与既有记录向前兼容，无数据迁移。
5. 放行：沿用 `docs/direct-snowluma-operations.md` 的停机/开启顺序；U3–U8 未
   联验前保持总开关关闭。

## Open Questions

- 卡片内 at 在 QQ 客户端是否触发提醒、外层预览真实形态：待真实联验（U6），
  不影响本任务的实现取舍。
- 服务端 body 上限的精确值与超长文本的业务拒绝阈值：实现按 2MB 常量 + 业务
  失败分类兜底，联验期记录实测值后如需调整只改常量。
- 文字卡片节点在目标群的展开观感（单节点卡片的可用性）：联验确认；若不可用，
  回退路径是把 text 也改回 `send_msg` 段直发（分派点已隔离，改动限于 D4 材料
  构造与一份 spec 措辞）。
