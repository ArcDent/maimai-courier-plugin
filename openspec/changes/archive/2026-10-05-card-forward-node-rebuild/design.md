## Context

现状卡片路径 = `prepare_source` 的三步只读核验后直接 `forward_group_single_msg`
（源消息标识）。2026-10-05 实测（私有报告 `.local/report/card-forward-rebuild-report.md`，
公开证据 `docs/snowluma-http-api-catalog.md` 6.5/6.6）证明四条通道中只有
`send_group_forward_msg` 投递 `get_forward_msg` 展开节点能穿过「跨群含图卡片」；
动机与契约变化见 proposal.md。本设计只回答「怎么改」。

## Goals / Non-Goals

**Goals:**
- 卡片路径改为「展开查询 → 节点校验 → 单次卡片发送」，一次投递动作数不增加
  （展开是只读，发送仍一个）。
- 固定动作表最小变更：移除 `forward_group_single_msg`、新增只读
  `get_forward_msg`。
- 材料边界收紧：节点与其内媒体只来自展开查询；禁绝 `send_msg` 直引旧 res_id。

**Non-Goals:**
- 不做「失败后换动作」降级链（红线禁止）；本变更就是替换，不是双轨。
- 不递归展开嵌套、不重建内层卡片（内层展示归 U6）。
- 不改普通文字/图片路径、评论段、账本、白名单与结果口径。
- 不做真实群联验（U3–U8 维持未确认）。

## Decisions

### D1：单选 `send_group_forward_msg`（一步），不用 `upload_forward_msg` 两步

实测：`upload_forward_msg` 单独一步返回 `retcode=100 "private image target uid is
missing"`（缺图片目标上下文），而 `send_group_forward_msg` 在同一载荷下成功（上传+
发送一体，平台自行补足目标上下文）。两步法会引入「上传成功、发送未提交」中间态，
与「一次源最多一个发送动作」冲突；一步法无此问题。备选（两步 upload + send_msg 引
新 res_id）被否。

### D2：动作表与载荷形状

固定动作表变为：只读 `get_login_info` / `get_group_list` / `get_msg` /
`get_forward_msg`；发送 `send_msg`（图片段+评论）、`send_forward_msg`（文字单节点）、
`send_group_forward_msg`（卡片展开重组）；`close`。`forward_group_single_msg` 移除。
`send_group_forward_msg` 参数：`group_id` + `messages`（节点数组）；节点形状
`{"type":"node","data":{"sender":<标量 uin>,"name":<显示名>,"content":<源段数组>}}`
——`sender` 必须是标量（实测对象形态报 1400 "sender must be a scalar value"），
`name` 取 card 优先、回退 nickname（压平、限长，与文字路径 `_source_sender_name`
同口径），`content` 为展开节点 `message` 段数组原样。不设置 `summary`/`news`/
`prompt`/`source`。

### D3：准备阶段链路（forward 分支）

`get_msg(message_id)` 取源记录（归属核验不变）→ 从顶层唯一卡片段取
`forward.data.id` → `get_forward_msg(id)` 展开 → 校验：messages 非空、每项
sender 可取（缺失/不可确认整条拒绝）、content 为段数组 → 材料 = 节点数组。
展开失败（业务 100/结构不符/空节点）→ `PrepareRejected` 明确拒绝，不进发送。
一次投递只读次数：3（准备）+ 1（展开）+ 回执 ≤8，均在既有 10 秒动作预算内，
不新增常量；`get_forward_msg` 与 `get_msg` 共用同一 `_bounded` 预算。

### D4：嵌套处理（实测结论 Q2）

展开只取一层：节点 content 内的 forward 段原样保留提交，平台接受（实测外层投递
ok）。插件不递归展开、不重建内层、不因嵌套拒绝。内层是旧 res_id 引用，其在目标群
的展示不保证——契约写入「嵌套内层展示不作保证」，联验清单 U6 跟踪。

### D5：回执与结果

`send_group_forward_msg` 返回 `message_id` + `res_id` + `forward_id`；只取
`message_id` 走既有四项核验（同 ID 记录/目标群/账号/正整数 message_seq，原值逐字、
负数照比）。`res_id`/`forward_id` 不入账、不进结果（新契约条款）。

### D6：拒绝语义对齐既有口径

展开查询业务失败（`retcode=100`）→ 明确业务失败分类（`PrepareRejected` 或发送前
`failed`），与 `business_failure` 现有口径一致；传输异常（超时/断连/3xx/超限）→
既有类别不变。不得因「旧路径失败过」对新路径做自动重试。

### D7：预算与容量

不新增时间常量：展开查询计入 10 秒准备预算；一次投递动作数 = 4 只读（login/
名册/源/展开）+ 1 发送 + ≤8 回执只读 + 评论（1 发送 + ≤8 回执），与现行预算框架
兼容。在途工具 8 个上限不变。

### D8：契约与文档同步面

delta 改 `cross-group-forward`（卡片条款）与 `snowluma-direct-client`（材料/回执/
隐私三需求）；CONTEXT.md 术语机制定义；AGENTS.md 模块职责与关键规则段；README
支持类型表（卡片路径描述）；运维手册（无操作变化，补一条排障提示：卡片失败时
优先看平台 wording）；联验清单 U4/U6 更新；catalog 6.6 已被本变更引用。

## Risks / Trade-offs

- **[展开节点体量超 2MiB body 上限]** → 客户端前置拒绝并报告可定位原因；大卡片
  的实际上限联验期测定（F10）。不做分块或压缩。
- **[`get_forward_msg` 大卡片耗时]** → 计入同一 10 秒预算，超时按待核实/明确未
  发送分类，不重试。
- **[客户端观感变化]** → 重组卡片是「结构性搬运」而非原资源直引；外层预览、内层
  展示均以联验为准（U6），README 不承诺。
- **[动作表变更的兼容面]** → 旧部署若缓存旧代码行为无影响（插件整体替换、无
  配置兼容问题）；`forward_group_single_msg` 移除后任何残留引用由测试扫描拦截。

## Migration Plan

- 配置：无变化（`[snowluma]` 三键不动，`config_version` 复核后不提升）。
- 账本：四字段键不变；`target_message_id` 允许负值既有口径不变；无迁移。
- 部署：随插件整体替换（用户已按此模式同步过 arclinux）；回滚 = 回退代码版本。
- 放行：保持总开关由操作者控制；本变更不宣称联验完成。

## Open Questions

- 大卡片（节点媒体多、content 长）的实际展开耗时与 body 上限：实现时以本地合成
  大卡片压测记录，联验期用真实卡片复核。
- 内层卡片在目标群客户端的真实展示（含是否可展开、是否降级）：归 U6，授权后实测。
