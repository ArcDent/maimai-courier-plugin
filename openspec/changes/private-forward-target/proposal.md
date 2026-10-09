## Why

转发目标只能是群：源群的料常需直接送给某个具体的人（固定搭子、客户、自收小号），现在
必须绕道「再建一个中转群」，既多一跳也把料暴露给第三方。同时这个口子不能白开——投递面
从「同组目标群」扩到「私聊」后，谁有权用、能发给谁、自动判断是否参与，都必须由配置显式
决定，否则模型一次误判就把群内容私聊给任意陌生人。

2026-10-08 已在同一实测实例（SnowLuma 1.14.17-node）完成私聊通道全端点预研实测
（`docs/snowluma-http-api-catalog.md` 6.10）：名册读取与字段形状、私聊文字/单节点卡片/
多节点卡片/https 图片段投递、私聊回执四项核验、负值目标消息 ID、非好友目标的平台拒绝
均有确定结论；其中「私聊回执是否带正整数平台消息序列」一项实测为**是**，因此不必为私聊
放宽可靠成功判据。本变更按实测结论立约，不再留「待实测后补」的悬空条目。

## What Changes

- **工具参数面**：`forward_message` 新增可选字符串参数 `target_user_id`，与既有
  `target_group_id` **二选一**（缺二者或同时给都拒绝），`comment` 语义不变。群目标与私聊
  目标共用同一套来源、账号、内容完整性与防重约束；拒绝措辞区分两种目标。
- **分组新增私聊开关**：每个转发分组新增布尔字段 `private`（默认 `false`）。只有来源群
  所属分组的 `private` 为真，才允许该源群调用私聊目标；`private` 不改变群目标的同组判定，
  不与目标群列表互相替代（「只开私聊、不放任何群目标」是合法配置）。
- **私聊目标必须为当前账号好友**：由固定只读动作 `get_friend_list` 取名册核对，非好友
  整条拒绝；名册字段（QQ 号、备注、昵称）只作数据展示，插件**不做**任何昵称/备注匹配。
- **新增只读群聊工具 `list_forward_friends`**：无参数、只读、与转发工具共用同一条一次性
  来源凭据链；仅当来源群所属分组 `private` 为真时返回名册（QQ 号＋备注＋昵称，设条目
  上限），未开启即拒绝。私聊目标的**选择权交给模型**：模型从名册里选 QQ 号，再以
  `target_user_id` 调用转发工具；插件不猜目标、不返回候选。
- **固定动作集 +1 个只读动作**：`get_login_info` / `get_group_list` / `get_msg` /
  `get_forward_msg` / `get_friend_list` / `send_msg` / `send_forward_msg` /
  `send_group_forward_msg`。私聊卡片与文字走 `send_forward_msg`
  （`message_type:"private"` + `user_id`），私聊图片与评论走 `send_msg` 同参数；实测可用
  的 `send_private_msg` / `send_private_forward_msg` **不引入**，不扩大发送动作面。
- **可靠成功判据不变**：私聊投递同样要求合法响应、同 ID 逐字比对、`message_type=private`
  且 `target_id` 等于请求的私聊目标、账号一致与正整数 `message_seq`；目标消息 ID 可为负
  值、按原值比对。不为私聊新增任何放宽的成功形态。
- **账本键扩展**：四字段键（账号/源群/源消息 ID/目标）新增 `target_kind`
  （`group` / `private`）并进入主键；既有库一次性迁移（既有行一律 `group`），迁移在既有
  独占锁与「先验证再写」约束内完成，失败即 `ledger_unavailable` 不发送。取舍见 ADR 0012。
- **自动入口不纳入私聊**：自动唤起的意图仍只列举同组目标群，不提供名册、不举荐私聊；
  模型若在自动唤起后自行带私聊目标调用工具，按与显式调用完全相同的权限口径处理
  （宿主载荷不含触发来源标记，无法也不做「本次调用是否源自自动唤起」的硬判定）。
- 配置：`plugin.config_version` 提升；`config.toml` 样例与模型默认逐项一致。

## Capabilities

### New Capabilities

无。私聊是既有「跨群转发」能力的目标形态扩展，不是新行为域。

### Modified Capabilities

- `cross-group-forward`: 「具名转发分组与同组投递判定」新增 `private` 开关及其判定口径；
  「三参数群聊工具与可信权限」改为「目标双形态（群/私聊）与好友核验」；「自动入口由源群
  决定」明确私聊不进入自动范围；「持久防重与不确定结果」的四字段键扩展为含
  `target_kind`；「结果与生命周期」补私聊目标的结果与定位信息。
- `snowluma-direct-client`: 「同账号查询与源材料可信性」新增好友名册只读查询与私聊目标
  好友核验；「真实回执与安全结果分类」把四项核验的目标一项适配为群或私聊两种形态；
  「材料最小化与隐私」明确名单册只按需提供给模型、不进日志与账本。

## Impact

- `config.py` / `config.toml`：`ForwardGroupSection` 新增 `private: bool = False`；
  `plugin.config_version` 提升；两处默认逐项一致；既有配置无需改写（缺省即关闭）。
- `source_binding.py`：公开参数声明从三参数改为「`message_id` + 二选一目标 + 可选
  `comment`」，并为 `list_forward_friends` 的工具调用签发同一类一次性凭据（仍只清洗
  指向本插件的调用）。
- `forward_precheck.py`：新增私聊分支（分组 `private` 开关、目标为好友的本地形态校验），
  分组判定族新增「来源群所属分组是否开启私聊」的单点判定；好友核验由直连准备阶段完成
  （本地预检仍不联网）。
- `shared/delivery.py`：`DeliveryKey` 增加 `target_kind`；`shared/send_steps.py` 的
  发送请求与 `shared/result.py` 的结果面增加目标形态与私聊目标标识（不出现名册字段）。
- `snowluma_transport.py`：动作集新增 `get_friend_list`；`send_msg` / `send_forward_msg`
  增加 `message_type` / `user_id` 私聊路由参数，其余安全边界（禁重定向、2MiB、超时、错误
  分类）不变。
- `snowluma_client.py`：准备阶段按目标形态核验好友名册归属；按 `kind` 分派时私聊卡片走
  `send_forward_msg`（私聊）而非群专用卡片动作；回执核验按目标形态比对群号或对端 QQ。
- `delivery_ledger.py`：表结构含 `target_kind`、主键五字段；开库流程在既有校验后完成
  一次性迁移，迁移失败按账本不可用处理；`_validate_database` 同时接受新旧两种形状。
- `plugin.py`：新增 `list_forward_friends` 工具（只读、受同一在途容量约束），
  `_send_allowed` 的成对复验按目标形态分治（群走同组判定，私聊走分组 `private` 开关）。
- `auto_entry.py`：不改目标列举口径（仍只列同组目标群），无需改动即可满足「私聊不进自动
  范围」；补用例固化该边界。
- `docs/snowluma-http-api-catalog.md` 6.10 已记录实测结论；`docs/adr/0012` 记录账本键
  迁移取舍；README 配置与用法说明、CHANGELOG 同步。
- 测试：`test_config`（`private` 字段与默认）、`test_forward_precheck`（私聊分支与拒绝）、
  `test_snowluma_client` / `test_snowluma_transport`（名册查询与私聊路由）、
  `test_delivery_ledger`（五字段键与既有库迁移）、`test_direct_acceptance`（真实回环端
  点到端）、`test_auto_entry`（私聊不进自动范围）、`test_lifecycle`（热更新时
  `private` 关闭的复验）。

喵呜~
