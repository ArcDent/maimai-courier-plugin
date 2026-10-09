# Tasks — private-forward-target

> 依据：proposal.md（为什么/改什么）、specs/ 两份 delta（行为契约）、design.md
> D1–D12（怎么做）。实测结论固定于 `docs/snowluma-http-api-catalog.md` 6.10，本清单
> 不含「待实测后补」的悬空项。每条完成后跑实际验证命令并记录真实结果，不沿用旧数字。
> seam 一律取公共接口（模块导出 / 工具入口 / 测试公共装配），私有实现函数不计数。

## 0. 实测基线（已完成）

- [x] 0.1 在实测实例上跑 `.local/probes/private-route-probe.py`，覆盖名册读取、好友核验、私聊文字/单节点卡片/多节点卡片/https 图片段、回执四项、负值 ID、非好友拒绝
  - seam：`.local/probes/private-route-probe.py`（私有探针，`--config` 读容器内受控配置）
  - 验收：原始记录 `.local/report/private-route-probe-20261008-*.json` 中 9 类用例均有确定结论，无「未测/不可判定」格
- [x] 0.2 把实测结论归档为公开证据并据此消解计划分岔
  - seam：`docs/snowluma-http-api-catalog.md` 6.10
  - 验收：私聊回执带正整数 `message_seq` 已确认，spec 中不为私聊放宽成功判据；固定动作集只增 `get_friend_list`

## 1. 共享类型与配置面

- [x] 1.1 给 `DeliveryKey` 增加 `target_kind`（取值 `group` / `private`）与取值字面量，既有构造点全部显式传值
  - seam：`shared.delivery.DeliveryKey`（公共数据类）
  - 验收：`tests/test_shared_types.py` 覆盖两种形态的键构造与相等性；同账号同源消息下群与私聊为不同键
- [x] 1.2 给 `SourceSendRequest` / `CommentSendRequest` 增加目标形态与私聊对端字段，群目标路径字段保持兼容语义
  - seam：`shared.send_steps.SourceSendRequest` / `CommentSendRequest`
  - 验收：`tests/test_forward_delivery.py` 中两种形态的请求都能构造并进入同一编排；群路径字段无回归
- [x] 1.3 结果渲染增加目标形态与目标标识（群号或私聊 QQ 号），不夹带名册其它条目
  - seam：`shared.result.render_forward_result`（公共渲染入口）
  - 验收：`tests/test_forward_delivery.py` 断言私聊结果含形态与对端标识、群结果保持原字段；结果中不出现备注/昵称
- [x] 1.4 配置模型 `ForwardGroupSection` 新增 `private: bool = False` 与 UI 元数据（label/hint/order）
  - seam：`config.ForwardGroupSection` / `config.build_default_config()`
  - 验收：`tests/test_config.py` 断言默认视图 `private=False`；宿主 Schema 默认视图可无参构造
- [x] 1.5 配置期校验补 `private` 非布尔拒绝，并提升 `plugin.config_version`
  - seam：`config.CourierConfig` / `config.ScopeSection.__init__`
  - 验收：`tests/test_config.py` 覆盖 `private` 传非布尔得到明确校验错误；`config_version` 高于 1.6.0 且模型与样例一致
- [x] 1.6 `config.toml` 样例补 `private` 键，与模型默认逐项一致
  - seam：`tests/test_config.py` 的默认一致性比对（样例 vs 模型默认）
  - 验收：比对通过；样例中不含任何真实凭证值

## 2. 账本键扩展与既有库迁移（design D5 / ADR 0012）

- [x] 2.1 建表语句改五字段主键，`_validate_database` 同时接受四字段旧库与五字段新库
  - seam：`delivery_ledger.open_ledger`（公共打开入口）
  - 验收：`tests/test_delivery_ledger.py` 覆盖两种形状的库都能打开；未知结构/多余列仍被拒绝
- [x] 2.2 开库流程加一次性迁移：独占 → 只读验证 → 事务内建新表回填（既有行 `group`）→ 记录数与状态校验 → 替换 → 提交
  - seam：`delivery_ledger.open_ledger`
  - 验收：旧四字段库迁移后记录数、状态与 `target_message_id` 逐条一致，`reserve()` 对既有键仍返回 `granted=False`
- [x] 2.3 迁移任一步失败时保留原文件并按账本不可用拒绝发送
  - seam：`delivery_ledger.open_ledger` 返回的 `LedgerUnavailable`
  - 验收：注入校验失败/形状不可识别/写盘失败，均得到可定位原因，原文件字节不变，不新建空库
- [x] 2.4 `_fetch` / `reserve` / `mark_*` / `reserve_comment` / `record_comment` 换用五字段键，`SELECT` 显式列不与 `LEDGER_COLUMNS` 混用
  - seam：`delivery_ledger.DeliveryLedger` 的 `reserve` / `get` / `mark_sent` / `mark_failed` / `mark_unknown` / `reserve_comment` / `record_comment`
  - 验收：同号群与私聊的占位/落账互不覆盖；评论占位仍只为已存在的源记录创建
- [x] 2.5 组合根账本打开点与键构造同步更新，任务表描述按形态展示目标
  - seam：`plugin.create_plugin`（公共工厂）与 `tests/test_lifecycle.py` 的任务表断言
  - 验收：任务描述可区分「目标群=…」与「私聊=…」；卸载告警逐条列出时不出现名册字段

## 3. 工具参数面与凭据链（design D1/D2/D10）

- [x] 3.1 转发工具参数声明改为「`message_id` 必填 + `target_group_id`/`target_user_id` 二选一 + 可选 `comment`」
  - seam：`source_binding.parse_forward_arguments` 与 `plugin.get_components()` 的参数声明
  - 验收：`tests/test_forward_precheck.py` 覆盖二选一互斥（同缺/同在均拒绝）、非法 ID 拒绝；组件声明的公开参数与实现一致
- [x] 3.2 凭据表为本插件的两个工具都签发一次性凭据，同批其它工具调用仍原样保留且不获凭据
  - seam：`source_binding.SourceBindingTable.sanitize_and_bind_output_items` / `consume`
  - 验收：`tests/test_plugin.py`（或绑定专属用例）断言混批调用中只有本插件两个工具被改写、凭据首次消费作废、错配 `message_id` 拒绝
- [x] 3.3 注册只读群聊工具 `list_forward_friends`（无公开参数，受 8 在途容量约束，零发送）
  - seam：`plugin.get_components()` 组件声明 + 工具入口
  - 验收：组件声明含两个群聊工具且顶层 `chat_scope="group"` 保持；关闭总开关时调用不产生任何平台请求
- [x] 3.4 名册工具门禁：凭据换可信来源 → 来源群所属分组 `private` 为真才查名册
  - seam：名册工具入口（公共）
  - 验收：未开启/不在任何分组/无凭据三种情况都得到可定位拒绝，且不发出任何平台请求
- [x] 3.5 名册返回只含 QQ 号、备注与昵称，条目设代码常量上限，超出即说明已截断
  - seam：名册工具入口 + `snowluma_transport.OneBotTransport.get_friend_list`
  - 验收：`tests/test_snowluma_transport.py` 与工具用例断言返回字段集合固定、上限处明示截断，不做匹配排序
- [x] 3.6 名册数据最小暴露面：不进日志、账本、任务表与投递结果
  - seam：名册工具入口的日志与结果面（`tests/test_plugin.py` 捕获日志断言）
  - 验收：日志只记查询类别与条目数；账本与 `render_forward_result` 输出中不含备注/昵称

## 4. 本地预检与分组判定族（design D7）

- [x] 4.1 分组归一视图 `ForwardGroup` 透出 `private`，`forward_group_entries` 纯函数口径不变
  - seam：`forward_precheck.forward_group_entries`
  - 验收：`tests/test_forward_precheck.py` 断言归一视图含 `private`，仍不改写输入、不查询
- [x] 4.2 新增「来源群所属分组是否开启私聊」单点判定，预检与提交前复验两处共用（名册工具门禁随 3.4 接线，是同一函数第三处消费）
  - seam：`forward_precheck` 新导出的私聊许可判定（公共函数）
  - 验收：已接线的调用方共用同一函数、无第二份复制；`tests/test_forward_precheck.py` 与 `tests/test_lifecycle.py` 给出一致结论；`auto_entry.py`/`plugin.py`/`source_binding.py` 中无复制的私聊判定
- [x] 4.3 `precheck_forward_request` 增加私聊分支：目标互斥 → 分组开关 → 源归属与材料校验；好友核验留给直连准备阶段
  - seam：`forward_precheck.precheck_forward_request`
  - 验收：本地预检仍零网络请求；私聊分支在好友核验前只做本地可判定的校验，非好友由准备阶段拒绝（`tests/test_snowluma_client.py` 覆盖）
- [x] 4.4 拒绝措辞区分两种目标形态并统一经既有定尾，不泄露越权正文
  - seam：`forward_precheck.precheck_forward_request` 返回的 `Rejected`
  - 验收：两类拒绝原因可定位且均含「本次未发送任何消息」定尾；不回显源正文或名册条目

## 5. 直连传输与客户端（design D3/D4/D6/D11/D12）

- [x] 5.1 固定动作集新增只读动作 `get_friend_list`，安全边界与其它只读一致
  - seam：`snowluma_transport.OneBotTransport.get_friend_list`
  - 验收：`tests/test_snowluma_transport.py` 覆盖禁重定向、2MiB 上限、超时/认证分类与 envelope 校验；动作集清单同步更新
- [x] 5.2 `send_msg` / `send_forward_msg` 增加 `message_type` / `user_id` 私聊路由参数，不引入具名私聊发送动作
  - seam：`snowluma_transport.OneBotTransport.send_msg` / `send_forward_msg`
  - 验收：请求体含私聊路由参数；断言动作名仍限于固定集合（无 `send_private_msg` / `send_private_forward_msg`）
- [x] 5.3 名册条目取值（QQ 号/备注/昵称）单点定义并导出，客户端与名册工具共用
  - seam：`send_material`（或同层模块）新导出的名册条目取值函数
  - 验收：`tests/test_send_material.py`（或对应模块用例）覆盖 `user_id` 为整数/字符串两种形态与形状不可识别的拒绝；无第二处解析
- [x] 5.4 准备阶段在账号核对后查名册并确认私聊目标为同账号好友
  - seam：`snowluma_client.SnowLumaClient.prepare_source`
  - 验收：`tests/test_snowluma_client.py` 覆盖非好友、名册形状异常、查询失败三种发送前拒绝；群目标路径不查名册
- [x] 5.5 按 `kind` 分派：私聊卡片与文字走 `send_forward_msg` 私聊路由，私聊图片与评论走 `send_msg`
  - seam：`snowluma_client.SnowLumaClient.send_source` / `send_comment`
  - 验收：三类源在私聊下的动作与参数分派正确；群路径动作与分派不变（回归用例通过）
- [x] 5.6 回执核验按目标形态分治并单点定义：群核验群号，私聊核验 `message_type=private` + `target_id` 对端 + `user_id` 机器人自身
  - seam：`snowluma_client.SnowLumaClient.send_source` 的结果分类（公共出口）
  - 验收：`tests/test_snowluma_client.py` 覆盖私聊正/负 ID 回查、回执不属于本次投递、缺证据归 unknown；`message_seq` 仍要求正整数
- [x] 5.7 图片路径到私聊沿用同一材料形态（`url` https 段），不新增图片规则
  - seam：`send_material.source_outbound_segments` → `snowluma_client` 发送路径
  - 验收：私聊图片用例与群路径共用同一段构造；本地 `file` 引用仍按既有媒体规则拒绝

## 6. 投递编排与生命周期复验

- [x] 6.1 `deliver_forward` 的账本先查、占位与落账全部改用五字段键，阶段顺序不变
  - seam：`forward_delivery.deliver_forward`
  - 验收：`tests/test_forward_delivery.py` 中群与私聊两条链路都跑通「先查→准备→占位→发送→落账→评论」，防重语义不变
- [x] 6.2 提交前 `_send_allowed` 复验按目标形态分治：群验同组判定，私聊验分组 `private` 开关
  - seam：`plugin.forward_message` 工具入口（经 `tests/test_lifecycle.py` 观察复验行为）
  - 验收：`private` 热更新关闭后，未提交的私聊源与评论都按明确未发送报告；已成功的不回滚；评论臂的公开入口用例随 6.1（交付编排换用五字段键）落地后在同一 seam 补充
- [x] 6.3 连接替换与配置变化路径把目标形态纳入失效判定，旧未提交阶段照常失效
  - seam：`plugin.forward_message` 工具入口
  - 验收：`tests/test_lifecycle.py` 覆盖端点/令牌变化时私聊在途阶段失效，不跨代次续发

## 7. 自动入口边界（design D9）

- [x] 7.1 保持自动入口只列举同组目标群，意图不含名册与私聊举荐
  - seam：`auto_entry.share_target_groups` / `auto_entry.build_share_intent`
  - 验收：`tests/test_auto_entry.py` 断言意图文本只含同组目标群与群名，不含好友名册或私聊目标
- [x] 7.2 固化「主动唤起后模型自行提交私聊目标按显式口径」的边界，不做触发来源硬判定
  - seam：`plugin.forward_message` 工具入口 + `auto_entry` 入口行为
  - 验收：`tests/test_auto_entry.py` 与 `tests/test_lifecycle.py` 各一条用例：唤起后私聊调用仅受分组开关与好友核验约束，无额外放行/拒绝

## 8. 测试补齐

- [x] 8.1 `tests/test_config.py`：`private` 默认 False、非布尔拒绝、版本提升、样例与模型默认一致
  - seam：`config.build_default_config()` / `config.CourierConfig`
  - 验收：新增用例全绿，既有默认一致性比对不回退
- [x] 8.2 `tests/test_forward_precheck.py`：二选一互斥、私聊开关关闭、只开私聊无群目标合法、群路径无回归
  - seam：`forward_precheck.precheck_forward_request`
  - 验收：四种边界各有独立用例；本地预检零网络断言保持
- [x] 8.3 `tests/test_snowluma_transport.py`：`get_friend_list` 固定动作与错误分类、私聊路由参数、动作集未扩张
  - seam：`snowluma_transport.OneBotTransport`
  - 验收：断言请求动作名集合等于固定集合；私聊路由参数逐字段比对
- [x] 8.4 `tests/test_snowluma_client.py`：准备阶段好友核验、三类私聊分派、私聊回执四项（含负 ID）与 unknown 分类
  - seam：`snowluma_client.SnowLumaClient.prepare_source` / `send_source` / `send_comment`
  - 验收：非好友/形状异常/回执不匹配/缺证据四类各有用例；`res_id`/`forward_id` 仍不入账
- [x] 8.5 `tests/test_delivery_ledger.py`：五字段键、既有四字段库迁移保留阻断、迁移失败关闭发送、同号群与私聊互不阻断
  - seam：`delivery_ledger.open_ledger` / `DeliveryLedger`
  - 验收：迁移前后记录一致；注入失败的用例中原文件未被改写
- [x] 8.6 `tests/test_direct_acceptance.py`：真实 aiohttp 回环端点端到端覆盖私聊文字/卡片/图片与评论，账本与线路互证
  - seam：公共 Hook/Tool 入口 + 回环端点装配（`tests/direct_support.py`）
  - 验收：私聊三类源与评论都跑通且账本落五字段键；与内存装配用例不互相冒充
- [x] 8.7 `tests/test_auto_entry.py`：自动意图不含私聊、唤起后私聊调用按显式口径
  - seam：`auto_entry.screen_forward_candidate` / `build_share_intent`
  - 验收：意图断言不含名册；唤起后私聊调用用例仅验权限口径
- [x] 8.8 `tests/test_lifecycle.py`：`private` 热更新关闭的提交前复验、卸载不补发
  - seam：`plugin.create_plugin` 生命周期入口
  - 验收：`private` 关闭与端点变化两条路径都按明确未发送报告，不回滚已成功事实
- [x] 8.9 名册工具用例：开启可查、未开启拒绝、条目上限截断、名册不进日志与结果
  - seam：名册工具入口（经 `plugin.get_components()` 声明）
  - 验收：四种行为各一条用例；日志捕获断言不含备注/昵称

## 9. 文档与验证命令

- [x] 9.1 README 增补双形态目标用法、`private` 开关与名册工具说明、结果形态展示
  - seam：README 的配置与用法章节（公开文档）
  - 验收：读者可按文档完成「开开关 → 取名册 → 带 QQ 号转发」全流程；默认关闭表述保持
- [x] 9.2 `docs/direct-snowluma-operations.md` 更新固定动作集（八动作）与名册查询的运维说明
  - seam：运维清单（公开文档）
  - 验收：动作清单与 `snowluma_transport` 实际集合一致；名册查询的失败分类可定位
- [x] 9.3 `docs/real-qq-validation.md` 补私聊路径联验条目
  - seam：联验清单（公开文档）
  - 验收：新增条目含对端展示（头像/卡片渲染）目视确认；未授权前不宣称已联验
- [x] 9.4 AGENTS.md 同步模块职责、「来源、材料与自动判断规则」与「持久事实、提交与生命周期」三处口径
  - seam：AGENTS.md 规则文件
  - 验收：动作集、参数面、五字段键、`private` 开关四处表述与新实现一致；`CLAUDE.md` 仍为相对软链接
- [x] 9.5 CHANGELOG 记录本次变更与 ADR 0012
  - seam：CHANGELOG.md
  - 验收：条目含双形态目标、分组开关、账本迁移与实测依据编号
- [x] 9.6 实跑三条验证命令并更新 AGENTS.md 实跑数字
  - seam：`uv run --no-project --with pytest pytest tests/ -q`、`--with aiohttp` 档、`uv run --no-project --with pyright pyright`
  - 验收：记录当次真实数字（不沿用旧值）；pyright 生产代码零错误；任一失败先修复再收尾
