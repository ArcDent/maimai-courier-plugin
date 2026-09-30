# AGENTS.md

麦麦搬运喵（`maimai-courier-plugin`）：麦麦在群聊间分享消息的**独立** MaiBot 插件。
源群出现**已选候选类型**的消息（默认仅**合并转发**，可配置加选普通文字与图片）后，
由源群 Maisaka 结合可编辑的自动分享标准自主决定是否分享，并可通过 `forward_message`
工具把本群指定**源消息**转发到白名单**目标群**，可选附带一条延迟 0.5 秒的评论。
个人与全体成员提及是原形态保真的明确例外：转成保留对象信息的普通文本，不向目标群
传播 @ 提醒。

术语见 CONTEXT.md，行为契约见主规格 `openspec/specs/cross-group-forward/spec.md`，
设计取舍见 `openspec/changes/archive/2026-09-29-introduce-maimai-courier/`。

## 独立性

- 本插件不 import、不复制、不依赖同级**复读插件** `repeat-plugin`：没有旧项目的
  import、配置或任务集合依赖（由 `tests/test_plugin.py::test_生产代码不依赖复读插件`
  守住）。
- 不读旧插件的运行配置、凭证或投递记录；本插件状态只落在宿主授予的
  `ctx.paths.data_dir`，不与任何其他插件共享。
- `_manifest.json` 的 capabilities 随插件整体声明、由第 3–7 组逐步消费；每一项都
  已在 Host `src/plugin_runtime/capabilities/registry.py` 核实。Hook 名称不是能力，
  不进该列表。

## 结构

- `plugin.py` — 插件主类：三生命周期（`on_load` / `on_unload` /
  `on_config_update(scope, config_data, version)`）、一个群聊 Tool
  `forward_message`、两个 Hook：`maisaka.planner.after_response`（blocking，
  在模型输出工具调用后签发一次性来源凭据）与 `chat.receive.after_process`
  （observe，观察已处理入站消息：筛选自动入口候选并把等待/唤起登记为本插件自己的
  后台任务）；`get_components()` 覆写把 Tool 的 `chat_scope` 提到组件声明**顶层**
  （承载性修正，见下）。它同时是组合根：在 `data_dir` 下打开账本（加载时一次检查、
  损坏即关闭发送）、把预检结果交给投递编排，并持有可注入的时钟、发送接缝与账本
  打开方式。生命周期语义：`_tasks` 只装本插件创建的后台任务（自动等待 `_auto_tasks`
  与在途投递），每项附带可定位描述（源消息／目标群／阶段）供超时告警逐条列出；
  `on_unload` 取消并**有界等待**它们退出（`TASK_EXIT_WAIT_SECONDS`
  = 5 秒，超出即按任务描述告警，不静默；宿主 Runner 的单次能力 RPC 自带 30 秒默认
  超时，取消在最近的 await 点生效）后释放账本；`forward_message` 在预检返回后、
  创建发送任务前复检卸载标记（预检期间卸载时直接按 `rejected` 返回，不打开账本、
  不发送）；`on_config_update` 立即取消已失效的待执行自动判断；
  投递在**源发送前与评论发送前**共用 `_send_allowed` 复验（卸载标记 + 总开关 +
  来源群/目标群白名单，读发送时最新配置）——复验失败时不发送、把占位按「明确未
  发送」释放并对外按 `rejected` 报告。
- `source_binding.py` — 工具参数面（公开参数声明、校验与统一拒绝措辞）与一次性
  来源凭据表：授权只由宿主 Hook 载荷签发、首次消费即作废；清洗与签发只作用于
  指向本插件 `forward_message` 的调用（同批其它工具调用原样保留，也不得获得
  凭据），工具调用里的会话/授权字段一概不读。
- `send_material.py` — 内容完整性预检与发送材料构建：图片必须带可恢复二进制、
  合并转发逐节点递归校验（含宿主空节点占位文本），任一缺失即整条拒绝；导出
  `is_forward_card` 作为**唯一**的「是否合并转发卡片」形状判据（含宿主未展开的
  `dict` 包装形态），自动入口与预检共用，避免序列化形态变化时一处放行一处拒绝。
  **个人与全体成员提及是原形态保真的唯一例外**：在同一递归预检里把已核实形态的
  `at` 段转成不触发提醒的普通文本（有名称 `［提及：显示名称（对象标识）］`、无名称
  `［提及：对象标识］`、全体 `［提及：全体成员］`，名称与标识压平控制字符、不截断
  （截断会让提及指向不存在的对象；群名另有压平、限长与尖括号转写），
  保留顺序与节点归属、输入不被原地修改；对象不可确认（既非 `all` 又无标识）仍整条
  拒绝。含提及的相邻文本片段按原换行归并，转换不新增换行。
- `forward_precheck.py` — 工具入口的权限与内容预检编排：凭据换可信会话、
  来源/目标白名单与目标路由核对，全部通过才产出 `PreparedForward`（不发送）；
  导出 `whitelist_entries` / `source_group_is_allowed` / `target_is_allowed`，让
  预检、发送前复验与自动入口共用同一份白名单口径。
- `delivery_ledger.py` — `ctx.paths.data_dir` 内的 SQLite 投递账本：唯一键为
  账号 + 源群 + 源消息 ID + 目标群（评论不参与键），状态由单一 `LedgerStatus`
  （`sending` / `sent` / `failed` / `unknown`）表达并附带评论结果；SQL 全参数绑定、
  每次操作独立短事务（BEGIN／事务体／COMMIT 失败都显式回滚，网络等待期间不持有
  事务）；插件打开账本后调用一次 `recover_interrupted()` 把上一次进程中断遗留的
  `sending` 归并为待核实，恢复失败至少告警一次（遗留记录继续阻止重发）；读、写与
  恢复失败一律抛 `LedgerUnavailableError`，`None` 只表示记录不存在、`False` 只表示
  没有可更新的在途占位；库损坏或不可读时返回 `LedgerUnavailable` 供调用方关闭发送，
  不覆盖也不删除既有状态数据。**单实例假设**：启动恢复不区分占位归属的进程，多进程
  共享同一 `data_dir` 时会把活进程的 `sending` 一并归并为 `unknown`（只影响可用性，
  不破坏防重安全），因此插件按单实例（单进程）运行。
- `forward_delivery.py` — 投递编排：预检通过 → 账本占位（`sending`）→ **源发送前
  权限复验**（`SendPermission`，失败即按「明确未发送」释放占位并返回 `rejected`）→
  源发送步骤 → 回执落账（`sent` / `failed` / `unknown`）→ 非空评论固定等待 0.5 秒 →
  重验总开关与目标权限 → 发送评论 → 保存评论结果。`SourceSendStep` 与
  `CommentSendStep` 是插件仅有的两个发送接缝（`SourceSendResult` 带状态、真实目标
  消息 ID 与原因），编排自身不调用任何宿主发送能力；发送步骤返回契约外状态字面量
  （空串、大小写不符、超时/取消等）一律按 `unknown` 兜底，不得降级为可重试失败；
  源发送明确失败但终态登记失败时对外同样按待核实报告。统一结果面由
  `render_forward_result` 产出：`status` 七态（`sent` / `partial` /
  `already_forwarded` / `sending` / `rejected` / `failed` / `unknown`，另有
  `ledger_unavailable`），`success` 只表示完整成功（`sent` / `already_forwarded`），
  `sent`/`delivered` 表示源消息已确认投递（含「已发送但状态未落盘」），评论另由
  `comment_status` 表达，错误阶段由 `stage` 表达。
- `host_send.py` — 两个发送接缝的真实实现：纯文字 `send.text`、单张图片
  `send.image`、多图或图文混排 `send.hybrid`、合并转发卡片 `send.forward`、评论
  `send.text`（普通文本，不解析成命令或消息段）；一律显式开启 `storage_message` 与
  `sync_to_maisaka_history` 并请求详细回执。成功证据只能是非空真实目标消息 ID
  （`sent=True` 但无回执 ID 按待核实）；宿主把平台失败与发送后的异常都归为未成功
  回执，因此「未成功回执」按待核实保守登记，绝不换通路重发；只有调用宿主前就能
  确定「没有发出任何消息」（如空文本材料）才返回 `failed`。
- `config.py` / `config.toml` — 最小配置：总开关、自动入口开关与两个判断输入
  （`auto.message_types` 默认 `["forward"]`、`auto.share_criteria` 默认 design D2
  原文且上限 4000 字符），以及源／目标白名单，外加宿主强制的
  `plugin.config_version`（SDK `extract_plugin_config_version` 缺它即抛
  `PluginConfigVersionError`）；`MessageType`（`text` / `image` / `forward`）是
  配置枚举与自动分类共用的唯一词汇来源；两处默认值必须逐项一致。
- `auto_entry.py` — 源群自动判断入口（只唤起判断，不发送消息）：按宿主
  `chat.receive.after_process` 载荷做候选分类与筛选（白名单源群、非机器人自身、
  非通知的 QQ 消息；无法确认机器人账号或发送者时放弃候选）。分类复用
  `is_forward_card`：命中卡片一律归 `forward`（含未展开卡片）；无卡片时只允许已
  识别的文字、图片与提及附属段参与，图文混排归 `image`、非空文字归 `text`，夹带
  不支持段或非法结构即放弃；候选类型再按 `auto.message_types` 门控（默认仅
  `forward`，空列表不提供自动机会），分类成功不承诺可发送。用源消息 ID 经注入
  时钟**有限**确认消息可读取（`AUTO_READINESS_TIMEOUT_SECONDS` = 10 秒、轮询
  `AUTO_READINESS_POLL_INTERVAL_SECONDS` = 0.5 秒，皆是代码常量），再对源群调用
  一次 `maisaka.proactive.trigger`。候选的会话 ID 与源消息 ID 先过 `safe_identifier`
  （限长 128、无空白/控制字符/尖括号引号反引号）才允许进入意图与 metadata：形态
  异常直接放弃候选，绝不净化成对不上真实消息的另一个字符串。意图携带源消息 ID、
  **实际候选类型**、合法目标群号与可用名称、用户配置的分享标准原文与固定边界
  说明（「自行判断、可以沉默」）；群名按不可信显示文本处理（尖括号转全角、压平
  空白、限长），永不作为路由依据；标准空或全空白回退设计 D2 原文，只按文字处理。
  等待结束后按**最新**配置重验总开关/自动入口、来源群、目标白名单与候选类型，
  目标名称查询结束后重读最新配置并用最新标准提交（`auto_entry_invalid_reason`
  判定含候选类型，只改标准不取消仍合法的候选）；已提交给宿主的判断不追溯改写；
  去重是内存有界状态（`AUTO_DEDUP_WINDOW_SECONDS` = 10 分钟、
  `AUTO_DEDUP_MAX_ENTRIES` = 4096，逐出最早登记项），与持久投递账本完全分离，
  不掷骰、不签目标群决策票、不唤起目标群、不追加上下文。
- `clock.py` — 时钟边界（`Clock` 协议 + `SystemClock`）：评论延迟与有界等待一律走
  注入时钟，测试注入可控替身即可确定性推进超时。
- `README.md` / `docs/` — 公开文档：README 面向插件使用者（独立安装、默认关闭、
  白名单、两个自动入口配置键与分享标准说明、三参数用法、支持与拒绝类型（含提及
  转换）、结果状态与人工核对、账本备份、版本与能力声明），`docs/handover-cross-group-forward-switch.md` 是部署切换与回滚清单
  （对照旧项目 `remove-cross-group-forward`），`docs/adr/` 记录设计取舍；
  私有操作手册在 `.local/runbooks/`（`.gitignore` 已排除，不入库）。
- `tests/` — pytest 与 `tests/fake_maibot_sdk.py` 替身（本机不安装真实 SDK）；
  组件声明经插件公共入口 `get_components()` 断言；组 4 起另有 `tests/support.py`
  的替身发送步骤（经替身 `ctx.send` 记录「进入源发送」）与账本替身（按方法注入
  落盘失败）；组 5 起 `tests/fake_maibot_sdk.py` 的 `ctx.send` 覆盖
  `text`/`image`/`hybrid`/`forward` 四通路与按调用顺序注入的回执队列，并可让单次
  发送悬停在途，`tests/test_forward_send.py` 以真实发送链驱动 5.1–5.6 的验收；
  组 6 起替身 `ctx.maisaka.proactive` 覆盖主动唤起（回执队列与异常注入），
  `tests/support.py` 的 `received_message()` 按宿主真实载荷形状构造入站消息
  （`merged_forward_message()` 复用同一构造点）、`drive_observe_hook()` 以后台任务
  方式分发观察 Hook 并推进注入时钟、`apply_config_update()` 按宿主顺序驱动配置
  热更新（先 `set_plugin_config` 再 `on_config_update`），
  `tests/test_auto_entry.py` 驱动 6.1–6.5、`tests/test_lifecycle.py` 驱动 7.1–7.4
  的验收；后续变更的提及转换用例落在 `tests/test_forward_precheck.py`（材料层）与
  `tests/test_forward_send.py`（真实发送链），候选分类与可编辑标准用例落在
  `tests/test_auto_entry.py`，热更新竞态落在 `tests/test_lifecycle.py`。

## 关键规则

- **默认安全**：默认总开关关闭、自动入口开启、自动候选仅合并转发、白名单为空；
  总开关关闭时工具与观察 Hook 都不得产生任何发送。
- **不把延迟与概率做成配置**：0.5 秒评论延迟、自动去重与等待上限是代码常量
  （change design D1），不要改造成配置项或随机时长。
- **投递状态只落在账本**：发送前先持久登记 `sending`，成功回执后立即登记 `sent`
  并保存真实目标消息 ID；只有「明确未发送」的失败可登记 `failed` 并允许后续显式
  重试；超时、取消、回执丢失与重启遗留的 `sending` 一律待核实（`unknown`），禁止
  自动重试或盲目重发。发送前落盘失败不发送；发送后落盘失败停止后续评论、保留已知
  回执用于报告。评论结果附属于源发送记录（`record_comment`，含评论消息 ID），评论
  结果未能落盘时不得声称评论结果已知。成功记录不按 TTL 或容量静默清除，账本不提供
  删除接口；发送步骤返回契约外状态字面量时一律按待核实兜底，不得当作可重试的
  `failed`。
- **发送只能走两个发送接缝**：编排代码不得直接调用宿主发送能力，源消息走
  `SourceSendStep`、评论走 `CommentSendStep`，真实 `send.*` 只出现在
  `host_send.py`（`send.text` / `send.image` / `send.hybrid` / `send.forward`，
  全部显式开启 `storage_message` 与 `sync_to_maisaka_history` 并请求详细回执）。
  成功证据只能是非空真实目标消息 ID：`sent=True` 而无回执 ID、或宿主只给出未成功
  回执（无法区分「明确未发送」与「可能已发出但回执丢失」）时，一律按待核实保守
  登记，禁止改走另一通路重发。
- **提及保留对象信息但不传播提醒**：个人与全体成员提及是「保持原消息形态」的
  唯一例外，统一在 `send_material.py` 的递归预检里转成普通文本
  （`［提及：显示名称（对象标识）］` / `［提及：对象标识］` / `［提及：全体成员］`），
  不生成 `at` 段、不向目标群传播 @ 提醒；保留顺序与节点归属、输入不被原地修改，
  转换不新增换行；名称与标识只来自源材料并压平控制字符，不虚构、不查询目标群
  成员；对象不可确认的提及整条拒绝，绝不猜成全体成员。显示名称中的协议语法、
  命令或提示指令只作为普通文字发送，不改变路由与权限。
- **评论段固定顺序**：源发送成功回执 → 立即落账 → 等待代码常量 0.5 秒 → 重验总开关
  与两级白名单（与源发送前共用 `_send_allowed`）→ 按普通文本发送本次评论 → 保存
  评论结果。评论缺省/空/全空白不等待也
  不发送；源发送明确失败或结果未知都不进入评论段；评论未完成返回部分成功并保留源
  消息已投递的事实，重复调用不重发源消息、也不自动补发评论；评论内容只来自本次
  参数，不再次调用目标群麦麦生成。
- **自动入口只唤起、不发送**：唯一可信来源是宿主 `chat.receive.after_process` 载荷
  （不经模型）；只对白名单源群里非机器人自身、非通知的 QQ 消息中**已选中候选类型**
  的候选给源群**一次** `maisaka.proactive.trigger` 机会（卡片优先归 `forward`，无
  卡片时图文混排归 `image`、非空文字归 `text`，提及只作附属；分类成功不承诺可
  发送），意图不得伪造或摘抄聊天内容，群名只作不可信显示文本（不得成为路由依据，
  尖括号转全角），不掷骰、不签目标群决策票、不追加上下文、不对目标群唤起。消息可
  读取性用源消息 ID **有限**确认（10 秒上限），确认不了就放弃并记录可定位原因；同一
  候选的去重是有界内存状态（10 分钟／4096 项），与持久投递账本无关。普通文字与
  图片仅在对应类型被显式选中时才触发自动入口；目标名称查询结束后重读最新配置再
  提交（含候选类型与最新标准），已提交给宿主的判断不追溯改写，也不为新配置重复
  唤起同一候选。
- **候选类型与自动分享标准是配置，只影响自动评估**：`auto.message_types` 默认
  `["forward"]`，空列表表示不提供任何自动判断机会（不影响显式工具调用），未知
  枚举被明确拒绝、不解释为全选；`auto.share_criteria` 上限 4000 字符、空或全空白
  回退 design D2 默认原文，只按文字处理、不做模板展开或命令执行。标准是交给源群
  Maisaka 的自然语言判断依据，**不保证模型必然遵循**，也不得成为群友明确请求的
  额外内容偏好门禁：显式 `forward_message` 调用只受总开关、白名单、内容完整性与
  防重约束，不要求自动评估资格；标准不得改变路由、工具权限或支持的发送类型。
- **生命周期与 Hook 名称以核实契约为准**（Host `a995cc4` / SDK `4d8e577`）：
  `on_load` / `on_unload` / `on_config_update`，观察 Hook 为
  `chat.receive.after_process`。不要凭记忆改写成 `on_start` 之类的名字。
- **卸载与热更新只取消自己的工作**：插件持有并等待**本插件创建的**后台任务
  （自动等待 + 在途投递，登记在 `_tasks`），`on_unload` 取消后**有界等待**退出
  （上限 `TASK_EXIT_WAIT_SECONDS`，超出告警而非静默），不触碰其他插件的任务；
  `on_config_update` 只取消失效的待执行自动判断（如候选类型被移除、开关或白名单
  变化），只改标准不取消仍合法的候选、等待结束后用最新标准提交，投递不整体取消
  ——在途发送可能已产生副作用，正确做法是**源发送前与评论发送前**按最新配置复验
  （`_send_allowed`），未发送的一律不发送、已成功的不回滚。热更新入口按宿主顺序
  （先注入配置再调用）读 `self.config`，不得缓存旧快照；复验失败的占位按「明确未
  发送」释放（可显式重试），绝不降级为待核实，也不得跳过复验直接发送。
- **只导入 SDK 根 `__all__` 内的名字**：`HookMode` / `HookOrder` / `ErrorPolicy`
  只存在于 `maibot_sdk.types`，不在根导出面，hook 模式用字符串字面量
  `"observe"` / `"blocking"`（由 `tests/fake_maibot_sdk.py` 的
  `SDK_ROOT_EXPORTS` 与 `test_生产代码只导入SDK顶层导出的名字` 守住）。
- **`get_components()` 顶层 `chat_scope` 修正不得删除**：Runner 只读组件声明顶层，
  `@Tool(chat_scope=…)` 经 SDK 只落进 metadata，删掉会让群聊专用工具在私聊可见。
- 引用宿主源码前先在 `arcwsl:~/github/REFERENCE/` 只读核实；禁止部署、发群消息、
  修改远端仓库、读取凭证与运行配置。
- 代码、注释与文档一律中文；不使用全局依赖。

## 验证

```bash
uv run --no-project --with pytest pytest tests/ -q      # 全部通过
uv run --no-project --with pyright pyright              # 生产代码零错误
```

`pyrightconfig.json` 只检查生产代码（根目录生产模块 `config.py` / `plugin.py` /
`clock.py` / `source_binding.py` / `send_material.py` / `forward_precheck.py` /
`delivery_ledger.py` / `forward_delivery.py` / `host_send.py` / `auto_entry.py`，
不含 `tests/`），并关掉 `reportMissingImports`：真实 `maibot_sdk` 只存在于
MaiBot 容器内，本机永远解析不到。
