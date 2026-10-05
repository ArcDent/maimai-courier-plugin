本清单只细化当前变更的实现任务，依据本目录的 `proposal.md`、`design.md` 与 `specs/cross-group-forward/spec.md`；不扩大产品范围。下文 seam 指可直接驱动和观察结果的公共测试入口，不以私有辅助函数、任务容器或内部调用次数作为验收入口。

执行顺序：第 1 → 2 → 3 → 4 → 5 → 6 组，组内按编号推进；每组通过后才进入下一组。第 1 组为硬性前置：上次参考机 SSH 连接超时，相关契约仍未核实；证据不足时阻塞第 2–6 组，保持 checkbox 未勾选。核实结果可作为对应任务的验收记录追加在本文件，不另建报告文件。

共用测试约束：沿用 `tests/support.py` 的消息构造、可信来源绑定、可控时钟和配置热更新驱动。只在宿主能力边界注入结果或悬停，不替换生产预检、判断意图或投递编排来制造通过；新增行为先运行能复现问题的失败用例，再实现并复验。需要改测试替身时同时测试替身自身，不安装真实 SDK、不新增生产依赖。运行 `uv --with` 验证前告知临时依赖；各任务通过相关定向测试后才勾选，全量验证集中在第 6 组。

## 1. 宿主契约与兼容性前置检查

依赖：无。本组只读 `arcwsl:~/github/REFERENCE/`，不读取凭证、运行配置或线上消息记录，不修改远端、不部署、不发群消息。源码证据与合成示例必须脱敏；公开接口已定位但其具体字段尚未核实，不据此提前编写依赖猜测字段的测试。

- [x] 1.1 核实个人及全体提及的入站、存储回读和嵌套节点表示。
  - seam：宿主 `chat.receive.after_process` 公开载荷、SDK `ctx.message.get_by_id` 返回的消息与节点内容。
  - 验收：记录实际参考提交、源码路径与行号、个人／全体／缺少名称／缺少对象标识的合成结构；明确可用对象标识和真实显示字段，覆盖三种载荷来源，不能以之前合成的拒绝复现代替宿主字段证据。
- [x] 1.2 核实三条相关发送通路对普通文本的处理契约。
  - seam：SDK `ctx.send.text`、`ctx.send.hybrid`、`ctx.send.forward` 的文本输入及宿主向平台输出的消息段。
  - 验收：逐条记录含协议语法、命令样式或提及文字的文本是否被再次解析的源码证据；确认无须新增发送通路即可避免真实提醒。任何通路缺少安全文本保证则保持阻塞，不执行真实发送来试探、不猜转义规则。
- [x] 1.3 核实候选多选和多行标准所需的配置声明与界面承载。
  - seam：SDK 根导出的 `Field`／`PluginConfigBase` 配置声明，以及宿主插件配置界面的字段渲染和输入校验入口。
  - 验收：记录枚举列表、多行字符串、4000 字符上限及中文标签／提示的受支持声明方式、参考提交与消费位置；确认空列表合法、未知类型和超长输入如何报错。仅有模型字段不能代替界面支持证据，不发明 UI 元数据。
- [x] 1.4 核实旧配置补全、版本兼容与热更新顺序。
  - seam：SDK 插件配置注入、公开 `on_config_update(scope, config_data, version)` 生命周期与宿主配置加载入口。
  - 验收：明确新增字段缺省补全、是否需要调整 `plugin.config_version`、旧版本配置如何接受以及先注入配置再调用生命周期的顺序；用脱敏配置示例说明保留旧开关和白名单的结果，不读取或覆盖运行配置。若须改变既定方案，暂停并请求修订规划。

## 2. 自动入口配置增量

依赖：第 1 组全部通过。除 2.1 的替身自测与 2.5 的配置界面检查外，本组共用 seam 为 `CourierPlugin.build_default_config()`、`CourierPlugin.set_plugin_config()` 和公开 `plugin.config`；测试落在 `tests/test_config.py`，不通过私有字段查看配置。

- [x] 2.1 补齐配置测试替身实际缺失的字段声明与校验能力。
  - seam：`tests.fake_maibot_sdk` 导出的 `Field`、`PluginConfigBase` 构造及 `model_dump()`；自测落在 `tests/test_fake_maibot_sdk.py`。
  - 验收：按 1.3／1.4 证据最小补齐本变更消费的元数据、枚举列表和字符串长度行为；正反例均能区分有效与无效输入，嵌套配置和实例间默认值隔离不退化。不得在替身中硬编码本插件字段或错误的宽松别名，也不把替身通过声称为宿主界面已验证。
- [x] 2.2 增加两个自动入口字段并保持默认行为兼容。
  - 验收：`auto.message_types` 默认精确为 `["forward"]`，`auto.share_criteria` 与设计 D2 的默认原文一致；TOML 解析值与 `build_default_config()` 逐项相等。仅扩充现有 `[auto]`，保留默认关闭、自动入口开启、空白名单和宿主配置版本，不新增延迟、概率或冷却字段；更新旧测试的字段集合断言而非删除断言。
- [x] 2.3 验证候选枚举和标准文本的配置边界。
  - 验收：`text`／`image`／`forward` 的单选、多选和空列表均合法；未知枚举被明确拒绝，不解释为全选。标准的空串、全空白和合法多行内容可保存，非空内容及换行不被改写；4000 字符接受、4001 字符报错且不静默截断。有效标准的空白回退在 5.2 经自动入口另验。
- [x] 2.4 验证旧配置加载不扩大自动候选或覆盖既有设置。
  - 验收：只带旧字段且具有非默认开关和白名单的配置加载成功，新增字段补为默认；既有字段值原样保留。配置版本按 1.4 核实结果处理，测试覆盖兼容路径，不为方便测试取消宿主要求的版本字段。
- [x] 2.5 声明并核对配置界面的中文可编辑字段。
  - seam：宿主插件配置界面“自动入口”区域及 1.3 已核实的公开配置声明消费边界。
  - 验收：声明包含三个候选选项和多行标准编辑能力，标签明确区分“自动候选类型”和“自动分享标准”；提示覆盖默认仅合并转发、空列表停止自动机会、空白标准回退、4000 字符上限及普通消息可能增加调用量。逐项对照宿主消费规则核验，区分声明证据与实际界面验证，不为本任务部署或修改线上配置。

## 3. 提及转换与完整发送

依赖：第 2 组通过，提及材料必须来自 1.1 的已核实形态。3.1–3.4 共用 seam 为模块导出的 `send_material.build_send_material()`；可在 `tests/test_forward_precheck.py` 增加材料用例，不调用其私有递归函数。3.5–3.7 共用 seam 为注册的 `maisaka.planner.after_response` 来源绑定 Hook → `CourierPlugin.forward_message()`，使用 `run_bound_forward` 驱动，观察真实 `host_send` 实现交给替身 `ctx.send` 的载荷、工具结果及 `DeliveryLedger.get()`；不以替身源发送步骤替代这条链。

- [x] 3.1 实现个人提及的普通文本呈现和缺少名称回退。
  - 验收：有名称时输出 `［提及：显示名称（对象标识）］`，无名称时输出 `［提及：对象标识］`；文字前、中、后及连续提及均保留顺序。输入消息不被原地修改，转换后的结构指纹与材料一致；有效个人提及用例先复现旧拒绝再通过，纯文字和图片的既有材料测试仍通过。
- [x] 3.2 实现全体成员提及的无提醒文本呈现。
  - 验收：已核实的全体提及输出 `［提及：全体成员］`，转换材料中没有实际 `at` 段；前后文字保留。只有对象不可确认的异常数据不得被当成全体提及，个人与全体标记按 1.1 契约区分。
- [x] 3.3 保持无效提及与其他不可发送内容的整条拒绝。
  - 验收：缺少可确认对象、非法段结构、混入仍不支持的文件／语音／视频／回复／表情段、图片媒体缺失或空节点时返回 `UnsupportedContent`；原因定位段或节点且不泄露正文。把这些情况放在有效内容之后仍整条拒绝，不输出删除异常段后的部分材料。
- [x] 3.4 将提及转换应用到合并转发与嵌套节点。
  - 验收：个人和全体提及在原节点内转成普通文本，节点作者、消息标识、节点顺序、嵌套关系及其余文字／图片不变；节点输入不被原地改写。不可恢复节点、宿主空节点占位、未展开卡片和超深嵌套仍按原契约拒绝。
- [x] 3.5 接通提及消息的真实发送链并保留排版和消息数量。
  - 验收：含提及的纯文字、图文、完整卡片分别经现有 `send.text`／`send.hybrid`／`send.forward` 通路发送；无评论时恰好一次源发送，不额外拆消息。原文字换行、图片顺序和节点归属不变，不因转换新增换行；不含提及的多个文本段维持既有测试行为。固定存储、历史同步和详细回执选项仍全部开启，取得真实目标消息 ID 后才能报告成功。
- [x] 3.6 验证显示信息不会成为提醒、命令或权限指令。
  - 验收：名称中的协议语法、命令样式、控制字符和提示指令只进入设计允许的普通文本呈现；目标和账号不变，不调用额外成员查询，不生成可执行消息段。无效提及或其他不支持段经工具入口返回 `rejected` 且 `ctx.send.calls` 为空；普通文本不会被宿主重解析的证据另引用 1.2，不能只凭替身载荷作平台保证。
- [x] 3.7 回归含提及消息的回执、防重与评论顺序。
  - 验收：确认源发送后立即可查到真实目标 ID；同一源消息与目标的第二次调用不重发、不补评论。非空评论仅在源成功落账后、注入时钟推进 0.5 秒且权限仍合法时发送；空白评论不等待。源回执未知不发评论、不允许盲目重试，评论失败保留源投递事实并返回部分成功。

## 4. 可配置候选分类

依赖：第 3 组通过。本组共用 seam 为 `auto_entry.screen_forward_candidate()` 和经 `CourierPlugin.get_components()` 取得的 `chat.receive.after_process` 注册 Hook；使用 `received_message`／`drive_observe_hook` 驱动，观察 `ctx.message`、`ctx.maisaka.proactive` 和 `ctx.send`。不直接测试新建私有分类辅助，不通过内部候选表断言行为。

- [x] 4.1 实现文字与图片的单一候选分类及配置选择。
  - 验收：非空文字归 `text`，单图、多图和图文归 `image`；文字或图片附带提及时类别不变，返回候选携带已确认类别。只选文字不触发图文，只选图片不触发纯文字；同时选中两类时一条图文仍只产生一个候选与一次源群唤起，自动入口自身不发送。
- [x] 4.2 复用卡片判据并保持合并转发分类优先。
  - 验收：展开卡片、未展开包装和卡片混排均归 `forward`，不另写一份卡片形状规则；`forward` 未选中时不能因同时含文字或图片进入候选。已选卡片仍可获得判断机会，但未展开或不支持混排经工具发送预检继续拒绝，不把分类成功当成可发送保证。
- [x] 4.3 排除无内容和不支持的普通混排候选。
  - 验收：仅空白、仅提及、非法结构，以及无卡片但夹带不支持段的普通消息均放弃；不只凭含有文字或图片就入选。观察 Hook 对这些输入不查询源消息、不唤起、不发送；图片候选不在入站分类阶段下载或恢复媒体。
- [x] 4.4 保留默认、空选择和可信来源限制。
  - 验收：未配置新字段时只有合并转发可入选；空候选列表、总开关或自动入口关闭时三类均不唤起。非 QQ、非白名单源群、机器人自身、通知、账号或发送者不可确认、安全标识非法、无合法异群目标仍全部被排除；不会因新增类型而绕过这些检查。
- [x] 4.5 保持各候选类型的有限可读等待。
  - 验收：文字、图片和卡片只有按可信会话及源消息 ID 确认可读后才各唤起一次源群；一直不可读时按注入时钟在既有 10 秒上限结束，轮询间隔仍为 0.5 秒，记录可定位原因且不发送、不生成虚假摘要。测试不使用真实睡眠，不唤起目标群。
- [x] 4.6 保持跨类型选择和配置更新的一次判断语义。
  - 验收：对同一账号、源群、源消息的重复或并发上报，多选和窗口内配置更新均不增加已登记候选的唤起次数；既有 10 分钟／4096 项去重边界测试仍通过。改变候选选择不扫描或重放历史消息，自动去重与持久投递账本仍分别工作。

## 5. 可编辑标准与热更新边界

依赖：第 4 组通过。5.1–5.2 共用 seam 为模块导出的 `auto_entry.build_share_intent()` 与注册观察 Hook 到 `ctx.maisaka.proactive.trigger` 的完整链；5.4–5.7 共用 seam 为注册观察 Hook、`CourierPlugin.on_config_update()`／`on_unload()`，必要时串接 `forward_message()`。热更新统一由 `apply_config_update` 按宿主顺序驱动；只在 `ctx.message.get_by_id`、`ctx.chat.get_all_streams`、`ctx.maisaka.proactive.trigger` 或发送能力替身上控制异步悬停，不替换生产协作者。

- [x] 5.1 将实际候选类型和可编辑标准接入判断意图。
  - 验收：合法多行标准的内容和换行保留，位于明确标注的用户配置区域；普通文字／图片不被称为合并转发。意图仅携带源消息 ID、实际类型、合法目标、标准与固定边界说明，不摘抄正文或节点；固定部分不夹带技术主题、广告等不可编辑偏好，仍允许模型沉默，群名只作不可信显示文本。
- [x] 5.2 验证标准的缺省与空白回退在自动链路实际生效。
  - 验收：旧配置缺少字段、显式空串及全空白时，最终提交给宿主的标准均等于设计 D2 默认原文；不解释成无条件转发，不报缺失字段错误。非空多行内容原样传递，具有花括号、协议语法或命令样式的标准仍只按文字处理，不进行模板展开或代码执行。
- [x] 5.3 保持显式工具请求与自动偏好分离。
  - seam：可信来源绑定 Hook → `CourierPlugin.forward_message()`，结合观察 Hook 提交的目标列表；用真实发送链与工具结果验收。
  - 验收：自动入口关闭、候选列表为空或偏好排除当前内容时，合法显式工具请求仍可发送且无须自动评估资格。反之，标准或正文要求越权时，非法来源、非白名单目标、自群目标及不可完整发送内容仍被拒绝；意图只提供合法目标，不新增授权票据或发送后门。
- [x] 5.4 在消息等待期间取消失效候选并保留仍合法的工作。
  - 验收：并存的不同类型候选等待可读时，移除其中一类只使失效候选不再提交，其他合法候选仍能继续；空选择、关闭自动入口或撤销来源／目标权限也会阻止相应判断。只改标准不丢弃合法候选，后续提交使用新标准；不整体取消在途投递。
- [x] 5.5 在目标名称查询结束后按最新配置提交判断。
  - 验收：让 `ctx.chat.get_all_streams` 悬停后分别修改标准、撤销类型、增删目标、关闭开关或卸载；恢复查询后，合法候选只带最新标准和最新目标，新增目标无名称时仅列群号，已撤销或已卸载的候选零提交。旧名称结果不重新带回过期目标，不为补齐名称再次引入提交前的异步查询窗口。
- [x] 5.6 验证已提交的判断不会被追溯重写或重复触发。
  - 验收：在宿主主动判断能力已收到请求后更新类型或标准，已提交意图不被当成已改写或已撤回，同一候选不再为新配置补发一次请求；若本地等待被取消，如实区分取消等待与撤销宿主工作，不对外宣称后者成功。
- [x] 5.7 回归热更新与卸载期间的发送权限边界。
  - 验收：从已提交判断继续发起工具调用，源发送前撤销权限时零发送、按 `rejected` 返回并释放明确未发出的占位；评论等待期间撤销权限则保留源投递、不发评论。在途发送取消仍按可能已发出处理，卸载只结束本插件工作且保持既有有界等待及超时告警，不留下继续发送的孤立任务。

## 6. 回归与文档交接

依赖：第 5 组通过。本组仅汇总实际验收结果；规划工件存在或 CLI 校验成功不等于实现完成，也不等于真实群消息验证通过。

- [x] 6.1 运行完整测试与生产代码类型检查。
  - seam：项目 CLI `uv run --no-project --with pytest pytest tests/ -q`、`uv run --no-project --with pyright pyright`。
  - 验收：预先告知临时依赖，记录命令、退出状态与实际结果；全量测试通过、生产代码零类型错误。保留 SDK 根导出、组件顶层 `chat_scope`、独立性、账本故障保守处理和固定评论延迟等既有保护，不删除失败断言换取通过。
- [x] 6.2 同步用户文档、项目规则与规则软链接约束。
  - seam：README 的用户配置说明、`AGENTS.md`／`CLAUDE.md` 规则入口，以及 `tests/test_project_rules.py` 的公开项目约束检查。
  - 验收：文档准确覆盖两个配置键、三类候选、图文分类、提及例外、空值语义、4000 字符限制、热更新边界、调用成本及偏好不保证模型必然遵循；不再声称普通消息永不自动触发。`CLAUDE.md` 仍软链接至 `AGENTS.md`，相关测试通过；主规格同步与目的描述更新保留为待流程授权事项，不提前当成已完成。
- [x] 6.3 汇总宿主证据与部署回滚限制。
  - seam：第 1 组已核实的宿主／SDK 公共契约、配置界面入口和用户可见的工具结果；验收记录落在本任务下。
  - 验收：逐项区分源码契约、测试替身、实际界面和群消息验证的证据等级，未做的明确标注未验证；说明升级不扩大默认候选、不覆盖旧配置、不删除或迁移投递账本，回滚旧版会再次拒绝提及。只给人工操作说明，不执行部署、未授权发送或 Git 写操作。
- [x] 6.4 核对增量规格场景并严格校验变更。
  - seam：本变更全部 `#### Scenario` 与对应公共测试／源码核实结果；CLI `openspec validate "support-mentions-and-configurable-auto-sharing" --strict`。
  - 验收：每个增量场景均有可定位用例或注明限制的核实证据，严格校验通过；未满足的实现验收继续未勾选并列出阻塞，不把未授权的线上验证写成通过。输出真实改动与验证总结，提醒用户自行提交，并按流程进入 `/openspec-verify-change`。

## 第 1 组核实记录（宿主契约证据）

核实日期 2026-09-30，只读走查 `arcwsl:~/github/REFERENCE/`。实际参考提交：MaiBot `a995cc4553c174fe00d2d44f8ce3d11746f920d4`、maibot-plugin-sdk `4d8e577d409fc7886e4a9f19dd94bea829081e4f`、snowluma `7726a3899a3cae2b48119c255a7f368bd6ee2bc7`（与规划假设一致）。核实用例中的 ID、昵称、群号全部为脱敏合成值；未读取任何运行配置、凭证或线上消息，未执行发送、部署或远端写操作。行号均对上述提交。

### 1.1 个人与全体提及的载荷形态（结论：已核实，checkbox 已勾选）

**三段载荷共用同一 at 形状。** 宿主插件运行时的对外序列化只有一个出口：`src/plugin_runtime/host/message_utils.py:134-142` 把 `AtComponent` 写成
`{"type": "at", "data": {"target_user_id": <str>, "target_user_nickname": <str|null>, "target_user_cardname": <str|null>}}`。
三段来源分别是：

1. **入站 Hook 载荷**：`src/plugin_runtime/host/message_utils.py:412-452`（`_session_message_to_dict`，`raw_message` 在 `:431-434`）← `src/plugin_runtime/hook_payloads.py:19-28` ← `src/chat/message_receive/bot.py:234-255`；触发点在 `bot.py:806-816`（先 `message.process()`，再 `_invoke_message_hook("chat.receive.after_process", …)`），Hook 规格见 `bot.py:79-96`。Runner 以原始字典调用插件处理器（`src/plugin_runtime/runner/runner_main.py:2164-2205`，`_invoke_plugin_callable(handler_method, **invoke.args)`），与现有 `auto_entry` 直接读 `payload["raw_message"]` 的口径一致。
2. **存储回读**（`ctx.message.get_by_id`）：`src/plugin_runtime/capabilities/data.py:416-438` → `src/services/message_service.py:98-113` → `data.py:384-399`，最终仍走同一个 `_session_message_to_dict`，形状与入站一致（唯一差异是 `include_binary_data` 默认 `False`，只影响媒体段，不影响 at）。落库形状由 `src/common/data_models/message_component_data_model.py:463-471`（`_item_2_dict`，同样三个字段）经 `src/common/utils/utils_message.py:61-64` msgpack 写入，回读走 `:56-59` 与 `message_component_data_model.py:519-524`（`item["data"]["target_user_id"]` 为必填键）。
3. **嵌套节点**：`message_utils.py:201-222`（`_forward_component_to_dict`）对节点 `content` 复用同一个 `_component_to_dict`，因此节点内 at 与顶层完全同形；反向 `_component_from_dict` 在 `message_utils.py:238`，其中 `at` 分支在 `:266-274`。

**字段来源与真实性。** 入站解析 `src/common/utils/utils_message.py:132-158`（`_parse_at_segment_data`）：对象标识按 `target_user_id` → `qq` → `user_id` → `id` 取第一个非空字符串，昵称按 `target_user_nickname` → `nickname` → `name`，群名片按 `target_user_cardname` → `card`，缺省为 `None`（`:161-170` 的 `_first_non_empty_string` 全空返回 `""`）。即宿主显式兼容 OneBot 的 `qq` 键（`:138`）。显示名会在 `message.process()` 期间被 `src/chat/message_receive/message.py:360-392`（`process_at_component`）**原地补写**：群名片优先 → 昵称 → 机器人自身昵称（`is_bot_self`，`:369-374`）→ `PersonInfo` 库查询（`src/common/utils/utils_person.py:39-55`，查不到即返回 `None`）→ 仍无则回退 `@<ID>`（`:390-391`）。因此名称字段**可能已被填上也可能为 `None`**，取决于成员信息是否已在库；插件不得假定有名称。另注：`AtComponent.to_seg()` 只输出对象标识（`message_component_data_model.py:253-254`），宿主自身也不把"名"当作权威事实。

**全体成员。** 宿主对消息段没有"全体成员／at-all"概念（无相应特判，`target_user_id` 原样透传；`src/maisaka/reply_necessity.py:56` 的 `@all` 纯文本前缀清理与消息段无关）。全网关（snowluma）把全体映射为字面量 `all`：`packages/onebot/src/event-converter/element-codecs.ts:158-163`（`uid === 'all' || targetUin === 0` → `{"type":"at","data":{"qq":"all"}}`）、`packages/protocol/src/events.ts:31-36`（注释 `0 denotes @all`）、`packages/protocol/src/format.ts:101`（`data.qq === 'all' ? '@全体成员'`）、`packages/protocol/src/element-builder.ts:103-124`（发侧 `mentionAll` 同样判定）。**结论：本部署中全体提及的对象标识就是 `"all"`**（宿主不校验、不改写）。

**脱敏合成结构（三种来源形状相同，仅名称字段随补名而异）。**

- 个人、已有名称（入站 Hook／节点内）：
  `{"type":"at","data":{"target_user_id":"20002","target_user_nickname":"某群友","target_user_cardname":"群名片"}}`
- 个人、缺少名称（适配器只送对象标识，或库中无此人；入站与回读都可能出现）：
  `{"type":"at","data":{"target_user_id":"20002","target_user_nickname":null,"target_user_cardname":null}}`
- 全体成员（名称字段同样为 `null`）：
  `{"type":"at","data":{"target_user_id":"all","target_user_nickname":null,"target_user_cardname":null}}`
- 缺少对象标识（解析器不报错、标识为空串）：
  `{"type":"at","data":{"target_user_id":"","target_user_nickname":null,"target_user_cardname":null}}`

**限制与冲突（不改变既定方案，供第 3 组实现时遵守）：** ① 全体判据只能取"对象标识恰为 `all`"，该字面量属网关契约而非宿主契约，换适配器／网关需重新核实；② snowluma 把 `targetUin === 0`（个人 at 解析不出 uin 时）也归为 `all`（`element-codecs.ts:160`），插件无法区分"真·全体"与"解析不出的个人"，两者都只会被转成普通文本、都不产生提醒，故只影响显示保真度、不影响安全契约（但 3.2 的"不得把缺失标识猜成全体"在插件侧仍须按空标识拒绝执行）；③ MaiBot 侧适配器（OneBot 事件 → maim_message）源码不在 REFERENCE，无法核实它是否随事件附带昵称，插件必须按"名称为 `None`"实现回退。

### 1.2 三条发送通路对普通文本的处理（结论：已核实，checkbox 已勾选）

**SDK 面只是透传。** `maibot_sdk/capabilities/send.py:27-51`（`text` / `forward: 102-126` / `hybrid: 127-151`）全部是把参数原样交给 `ctx.call_capability`，SDK 侧不解析、不转义。

**宿主接收与装配。** `send.text` → `src/plugin_runtime/capabilities/core.py:306-340` → `src/services/send_service.py:1144-1163`（`text_to_stream_with_message`）→ `MessageSequence([TextComponent(text)])`；`send.hybrid` → `core.py:439-468`（段归一化 `_normalize_plugin_segment:411-425` 对 text 只做 `str(...)`）→ `_message_sequence_from_dict`；`send.forward` → `core.py:470-522`（节点内容逐段归一化后装进 `[{"type":"forward","data":[…]}]`）。出站装配 `send_service.py:516-590`（`_build_outbound_session_message`）与 `:373-397`（`_detect_outbound_message_flags`）只依据**段类型**推断标记：`is_command` 仅在 `DictComponent.data["type"] == "command"` 时为真，`TextComponent` 永远是纯文本；全流程没有任何代码读取文本内容去重新解析 at／CQ／命令段。

**发往平台。** 两条驱动都送结构化段：插件网关驱动 `src/platform_io/drivers/plugin_driver.py:71-137`（`:112` 传 `_session_message_to_dict`），旧链 `src/chat/message_receive/uni_message_sender.py:248-258`（`send_prepared_message_to_platform`）→ 同文件 `:223` 的 `message.to_maim_message()` → `src/common/data_models/mai_message_data_model.py:160-212` → `src/common/utils/utils_message.py:82-92` → 逐段 `to_seg()`，其中 `TextComponent.to_seg()` = `Seg(type="text", data=<原文>)`（`message_component_data_model.py:46-59`），节点内文本同样是 text 段（`ForwardComponent.to_seg`，`:361-364`）。

**网关侧（snowluma）不会把文本变成提及。** 只有**字符串**消息才会走 CQ 解析：`packages/onebot/src/message-parser.ts:440-448`（`typeof message === 'string'` 且 `autoEscape=false` 时才 `parseFromCQString`）；**数组**消息逐段查 codec，text 段经 `element-codecs.ts:137-145` 得到 `{type:'text', text}`，最终 `element-builder.ts:46-50`（`makeTextElem` = `{text:{str}}`）直接写字符串。真正提及只能由 `at` 段构造（`element-builder.ts:103-124 makeMentionElem`），而本插件三条通路都不产出 at 段（提及一律在预检阶段转普通文本）。

**结论：** `ctx.send.text` / `ctx.send.hybrid` / `ctx.send.forward` 三条通路都保证文本以结构化 text 段送达，**无须新增发送通路**，含协议语法（如 `[CQ:at,qq=…]`）、命令样式或提及文字的文本都不会被宿主或网关重新解析成提及／命令。

**残余依赖（记录，不作为本组阻塞）：** 网关的 CQ 解析分支要求上游把文本**误转成 CQ 字符串**；maim_message 协议交给适配器的是结构化 Seg（见上），但适配器源码不在 REFERENCE，这一跳无法核实。因此"平台层不产生真实提醒"的最终确认必须留到获授权后的真实群消息验证；3.6 不得只凭替身载荷声称平台已保证。

### 1.3 配置声明与界面承载（结论：已核实，checkbox 已勾选）

**声明面。** SDK 根导出 `Field`／`PluginConfigBase`（`maibot_sdk/__init__.py:25`；`config.py:19` 的 `Field` 直接来自 pydantic），基类行为见 `config.py:44-61`（`validate_assignment=True, extra="ignore"`）。Schema 生成器 `config.py:181-262` + `_build_field_schema:302-366` 读取的元数据键是**封闭集合**：`label` / `hint` / `placeholder` / `x-widget` / `x-icon` / `order` / `rows` / `max_length` / `min_items` / `max_items` / `step` / `pattern` / `hidden` / `disabled` / `i18n` 等（`:324-361`），未列出的键不会进入 Schema。

- **多选枚举**：`_extract_select_choices:446-461` 识别 `list[Literal[...]]` → `_map_field_type:515-541` 给 `type: "select"` → `:363-364` 追加 `"multiple": true`。前端据 `field.multiple` 渲染 MultiSelect（`dashboard/src/routes/plugin-config.tsx:234-283`），组件允许逐项移除、无最少项限制（`dashboard/src/components/ui/multi-select.tsx`）。
- **多行字符串**：`x-widget` 决定 `ui_type`（`config.py:324`），`rows` 决定行数（`:353`）；`ui_type: "textarea"` 由 `plugin-config.tsx:285-298` 渲染成多行输入（`rows={field.rows ?? 3}`）。`x-widget: "textarea"` 是宿主自有配置在用的既有取值（`src/config/official_configs.py:203`），不是新发明。
- **长度上限**：Schema 的 `max_length` 只来自 `json_schema_extra["max_length"]`（`config.py:343`）。**实测确认**：`Field(max_length=4000)` 单独写时 Schema 里 `max_length` 为 `null`，必须同时写 `json_schema_extra={"max_length": 4000}` 才会出现在 Schema／提示里；前端仅在单行 `text` 控件上把它当输入上限（`plugin-config.tsx:355`），textarea 不使用它，因此 4000 的强制来自后端校验而非前端截断。

**校验入口（超限与未知类型如何报错）。** 配置页保存走 `PUT /plugins/config/{plugin_id}`（`src/webui/routers/plugin/config_routes.py:593-660`）→ `_validate_plugin_config_via_runtime:319-337` → `src/plugin_runtime/host/supervisor.py:871-905` → Runner `plugin.validate_config`（`runner_main.py:2330-2374`，`suppress_errors=False, enforce_version=True`）→ 插件模型 `validate_plugin_config`（SDK `config.py:167-178`）。校验失败经 `E_BAD_PAYLOAD` 转成 `ValueError` → HTTP 400，前端以 toast 展示服务端原文（`dashboard/src/routes/plugin-config/hooks/usePluginConfigEditor.ts:135-151`）；可视化模式没有前端预校验，唯一把关点是后端。

**实测（本地，合成脱敏模型；把参考机 SDK `config.py` 源码经 stdin 载入后按本变更设计声明字段，仅用 `uv run --no-project --with pydantic` 临时依赖，不写远端、不落文件）：**

- `list[Literal["text","image","forward"]]` + `default_factory=lambda: ["forward"]` → `{"type":"select","choices":["text","image","forward"],"multiple":true,"default":["forward"],"label":"自动候选类型"}`。
- `x-widget:"textarea"` + `rows:4` + `json_schema_extra.max_length:4000` → `{"ui_type":"textarea","rows":4,"max_length":4000,"label":"自动分享标准","hint":"上限 4000 字符"}`（中文标签／提示原样输出）。
- 空列表 `[]`：通过；`["video"]`：`literal_error`（`Input should be 'text', 'image' or 'forward'`）；`"x"*4001`：`string_too_long`（`String should have at most 4000 characters`）；`4000` 字符：通过；含空白与换行的标准文本**不被改写**（`'  a\n b  '` 原样保留）。
- SDK 自带测试同样断言多选 schema 与空列表默认值（`tests/test_sdk.py:488-500`）。

**证据等级说明：** Schema 生成与后端校验为源码 + 实测；真实浏览器渲染未验证（本组不部署、不开 WebUI），前端证据来自 dashboard 源码与其单测。实现时只使用上列已核实键（`label`/`hint`/`x-widget`/`rows`/`max_length`/`order`），不得发明新的 UI 元数据。

### 1.4 旧配置补全、版本兼容与热更新顺序（结论：已核实，checkbox 已勾选）

**加载与注入链路。** Runner 激活插件：`runner_main.py:1545-1559`（`_apply_plugin_config` 失败即 `PluginActivationStatus.FAILED`）→ `:874-904`（`_apply_plugin_config`：读盘 `_load_plugin_config:1122-1136` → `_normalize_plugin_config` → 需要时写回 → `set_plugin_config(plugin_config)` 注入）→ `:906-985`（`_normalize_plugin_config`：`enforce_version=True`，先 `_prepare_plugin_config_for_version_update`，再调用插件自己的 `normalize_plugin_config`）。

**缺省补全。** 缺失字段由插件模型的归一化补齐：SDK `normalize_plugin_config`（`maibot_sdk/plugin.py:156-182`）→ `merge_plugin_config_data`（`config.py:126-143`）→ `_fill_missing_fields`（`:614-645`，递归补默认值），再 `validate_plugin_config` 生成强类型实例。Runner 在 `:969-976` 调用它，因此**即使不提升版本号，新字段也会以默认值进入内存配置与 WebUI bundle**，只是不写回用户 `config.toml`。

**版本语义。** `_prepare_plugin_config_for_version_update:988-1011` + `extract_plugin_config_version:276-300` + `compare_versions`（`src/config/config_utils.py:162-166`，按 `int` 点分段比较，故 `plugin.config_version` 必须是数字点分）：

- `latest > current` → `rebuild_plugin_config_data(default, raw)`：以新默认配置为骨架，用 `_overlay_plugin_config_fields:251-273` 回填**仍然存在且类型兼容**的旧值、跳过 `config_version`（`:262`），返回 `should_persist=True` → 写回 `config.toml`（尽量保留注释，`_merge_plugin_config_document`）。
- 版本相等或降级 → `rebuild(raw, {})`（原样副本），不写回。
- 缺 `[plugin].config_version`：`PluginConfigVersionError`（`extract_plugin_config_version:276-300`）；激活路径不吞异常（`_apply_plugin_config` 用 `suppress_errors=False`），插件加载失败。既有 `plugin.config_version` 必须保留。

**热更新顺序。** `runner_main.py:2260-2295`（`_handle_config_updated`）：`scope == "self"` 时**先** `_apply_plugin_config(meta, config_data=payload.config_data)`（内部 `set_plugin_config` 注入并校验最新值），**再** `await instance.on_config_update(scope, config_data, version)`。与既有实现（处理器内直接读 `self.config`）一致，不需要缓存快照。

**脱敏合成示例（旧配置 → 归一化结果）。** 旧配置（无新字段）：
`{"plugin": {"enabled": true, "config_version": "1.0.0"}, "auto": {"enabled": false}, "scope": {"source_groups": ["1"], "target_groups": ["2"]}}`
→ `merge_plugin_config_data(默认配置, 旧配置)` 得 `changed=True`，归一化后为
`{"plugin": {"config_version": "1.0.0"}, "auto": {"enabled": false, "message_types": ["forward"], "share_criteria": <设计默认原文>}, "scope": {"source_groups": ["1"], "target_groups": ["2"]}}`（实测值：`auto.enabled=False` 保留、`message_types=["forward"]` 补齐）。旧开关、白名单、账本均不受影响；不符类型的垃圾值会被回填阶段跳过（`_is_plugin_config_value_type_compatible:233-248`）。手动写入未知枚举时插件加载失败（明确拒绝，不扩大触发范围），与规格"未知类型必须被明确拒绝"一致。

**是否需要提升 `plugin.config_version`：功能上非必需，推荐提升。** 不提升：旧 `config.toml` 原样保留新字段缺省，运行期与界面用默认值；提升（如 `1.0.0` → `1.1.0`，需同步 `config.py` 默认值与 `config.toml`）：宿主走 rebuild + 写回，用户的配置文件里直接出现新字段与提示注释，旧值仍逐项保留。二者都不覆盖旧开关／白名单、不删账本；具体版本号与两处默认值同步由第 2 组落实。

**冲突情况：无。** 本组四项均取得源码级证据，未发现与 `design.md` 冲突的宿主事实，第 2–6 组可以继续；仅第 1.1／1.2 的下列限制需在后续任务中如实表述：全体标识取网关字面量 `all`；平台级"无真实提醒"的最终确认属于未做的线上验证；界面渲染未在真实 WebUI 打开验证。

## 第 6 组执行记录（回归与文档交接）

执行日期 2026-09-30。本组只做验证与文档，未修改任何生产代码；未部署、未发群消息、未执行 Git 写操作；未执行 `openspec archive`／`sync`／`update`。改动文件：`README.md`、`AGENTS.md`、`docs/handover-cross-group-forward-switch.md`、本文件（第 6 组 checkbox 与本记录）。

### 6.1 门禁命令与结果

临时依赖：仅经 `uv run --no-project --with` 现场提供 `pytest`／`pyright`，不落文件、不安装项目依赖、不动全局环境。

```text
$ uv run --no-project --with pytest pytest tests/ -q
........................................................................ [ 30%]
........................................................................ [ 61%]
........................................................................ [ 91%]
....................                                                     [100%]
236 passed in 1.64s
PYTEST_EXIT=0

$ uv run --no-project --with pyright pyright
0 errors, 0 warnings, 0 informations
PYRIGHT_EXIT=0
```

文档改动前后各跑一次全量测试（均 236 passed）；`tests/test_project_rules.py` 定向 2 passed。保留 SDK 根导出、组件顶层 `chat_scope`、独立性、账本故障保守处理与固定评论延迟等既有断言，未删除或弱化任何断言换取通过。

### 6.2 文档与规则同步记录

- `README.md`：配置表新增 `auto.message_types`／`auto.share_criteria`；TOML 示例 `config_version` 更新为 `1.1.0` 并补两个字段；新增《自动入口（候选类型与分享标准）》（候选分类、硬边界、意图内容、热更新边界、调用成本）；支持列表补充个人／全体提及的普通文本转换与「原形态保真唯一例外」，拒绝列表补充「对象不可确认的提及」；开头概述与安装章节不再声称普通文字和图片永不自动触发；写明标准只作自然语言判断依据、不保证模型必然遵循、不为显式请求增加偏好门禁。
- `AGENTS.md`：开头概述、「结构」章节的 `config.py`／`config.toml`、`send_material.py`、`auto_entry.py`、README／docs 与 tests 描述全部同步；「关键规则」新增「提及保留对象信息但不传播提醒」与「候选类型与自动分享标准是配置，只影响自动评估」两条规则，更新「默认安全」「自动入口只唤起、不发送」「卸载与热更新只取消自己的工作」的措辞。
- `docs/handover-cross-group-forward-switch.md`：第 3 节补充「升级不扩大自动范围／不覆盖旧配置／不迁移删除账本」与「回滚本插件旧版会再次拒绝提及」；第 2 节候选类型表述与第 5 节回滚说明同步。
- `CLAUDE.md` 仍为指向 `AGENTS.md` 的软链接（`lrwxr-xr-x CLAUDE.md -> AGENTS.md`），`test_CLAUDE_是指向AGENTS的软链接` 通过；`tests/test_project_rules.py` 未修改（措辞变化未触及既有断言的精确匹配）。
- 主规格 `openspec/specs/cross-group-forward/spec.md` 的同步与目的描述更新仍保留为待流程授权事项，本次未改。

### 6.3 核实记录（宿主证据与部署回滚限制）

**证据等级 A：源码契约（第 1 组 1.1–1.4；参考提交 Host `a995cc4` / SDK `4d8e577` / snowluma `7726a38`）**

1. 提及载荷：入站 Hook、`ctx.message.get_by_id` 回读与嵌套节点共用同一序列化出口；对象标识与名称字段来源、补名时机、全体 = 网关字面量 `all`（1.1）。
2. 三条发送通路（`send.text`／`send.hybrid`／`send.forward`）文本均以结构化 text 段送达，宿主与网关不重新解析（1.2）。
3. 配置声明：多选枚举、textarea 多行、4000 上限（须 `json_schema_extra` 双写）、空列表合法、未知枚举与超长由后端校验拒绝（1.3）。
4. 旧配置缺省补全、`plugin.config_version` 语义与「先注入配置、再调用生命周期」的热更新顺序（1.4）。

**证据等级 B：测试替身（本机用例）** — 提及转换与真实发送链：`tests/test_forward_precheck.py`、`tests/test_forward_send.py`；候选分类与可编辑标准：`tests/test_auto_entry.py`；配置声明、旧配置兼容与长度边界：`tests/test_config.py`；热更新与卸载竞态：`tests/test_lifecycle.py`；替身自测：`tests/test_fake_maibot_sdk.py`。替身证据不代替宿主界面与平台行为。

**证据等级 C：实际界面——未验证。** 未部署、未打开真实 WebUI；控件承载证据来自宿主 dashboard 源码与其单测（第 1 组 1.3），真实浏览器渲染待用户部署后确认。

**证据等级 D：真实群消息——未验证。** 未部署、未发送任何群消息；平台层「提及不产生真实提醒」的适配器一跳无法在 REFERENCE 核实（第 1 组 1.2 残余限制），联验清单位于 `docs/handover-cross-group-forward-switch.md` 第 4 节（基础转发联验按其清单记录为既有授权、尚未执行）；本次新增的「提及不产生真实提醒」属平台行为，执行前需另行确认。

**升级与回滚的人工操作说明（只给说明，均未执行）**

- 升级：旧配置缺少新字段由默认补齐（自动候选仍仅 `forward`），既有开关、白名单与投递账本不被覆盖、不迁移、不删除；`plugin.config_version` 由 `1.0.0` 提升为 `1.1.0` 时宿主以新默认配置为骨架回填仍兼容的旧值并写回 `config.toml`，配置文件里直接出现新字段与提示注释。
- 回滚：先关闭 `plugin.enabled`／自动入口并确认无在途发送，再恢复旧版本；恢复不支持提及转换的旧版后，含个人／全体提及的源消息会**再次被整条拒绝**；账本保留、不迁移也不删除；同一时间只能有一套转发路径（对照旧项目 `remove-cross-group-forward` 的口径）。
- 未做的操作：未部署、未发群消息、未执行任何 Git 写操作。

### 6.4 增量场景映射

本变更 `specs/cross-group-forward/spec.md` 共 34 个 `#### Scenario`（MODIFIED 23 + ADDED 11），逐条映射如下。`openspec validate "support-mentions-and-configurable-auto-sharing" --strict` 输出 `Change 'support-mentions-and-configurable-auto-sharing' is valid`，退出码 0。

**MODIFIED Requirements（23）**

1. 独立插件与路径开关（3）
   - 单独安装 → `test_plugin.py::test_工厂可创建插件并注册群聊工具与观察Hook`、`test_生产代码不依赖复读插件`、`test_manifest_声明独立插件与已核实能力`
   - 仅关闭自动入口 → `test_auto_entry.py::test_自动入口关闭时收到合并转发也不唤起`、`test_总开关或自动入口关闭时三类均不唤起`；`test_lifecycle.py::test_仅关闭自动入口时待执行唤起被取消且工具仍可发送`；`test_auto_entry.py::test_自动入口关闭或候选为空时显式工具请求仍可发送`
   - 默认兼容 → `test_config.py::test_旧配置缺少新字段时加载成功并补为默认`、`test_自动入口新增字段的默认值与设计一致`；`test_auto_entry.py::test_未配置新字段时只有合并转发入选`（宿主侧补全依据另见 1.4）
2. 自动入口由源群决定（6）
   - 自主分享 → `test_auto_entry.py::test_白名单源群合并转发成为候选并唤起一次自动判断`、`test_意图包含源消息ID合法目标与自主判断说明`、`test_判断意图标注用户配置区域并保留多行标准原文`
   - 重复上报与自身消息 → `test_auto_entry.py::test_同一候选重复上报只唤起一次`、`test_并发上报同一候选只唤起一次`、`test_多选下重复与并发上报同一候选只唤起一次`、`test_机器人自身消息不触发自动入口`
   - 普通消息 → `test_auto_entry.py::test_普通文字与图片消息不触发自动入口`、`test_普通文字与图片不触发自动入口但仍可经工具发送`、`test_只选中一类候选时其它类型不提供判断机会`
   - 显式开启普通消息 → `test_auto_entry.py::test_文字与图片的候选分类由配置选择`、`test_只选中一类候选时其它类型不提供判断机会`、`test_同时选中文字与图片时图文混排只唤起一次`（断言零发送与只唤起源群）、`test_各候选类型就绪后各唤起一次且等待时序一致`、`test_意图说明实际候选类型而不把普通消息说成合并转发`
   - 消息未就绪 → `test_auto_entry.py::test_候选消息始终不可读取时有限等待后放弃并记录原因`、`test_各候选类型始终不可读时有限等待后放弃`
   - 判断标准不能替代授权 → `test_auto_entry.py::test_标准或正文要求越权时仍只提供合法目标并拒绝越权调用`、`test_forward_precheck.py::test_正文与节点中的伪造指令不能改变源群与目标选择`、`test_auto_entry.py::test_自动判断与显式请求共用投递防重`、`test_auto_entry.py::test_自动判断与显式请求并发时最多一次源发送`
3. 支持消息类型与完整性（9）
   - 文字和图片 → `test_forward_precheck.py::test_完整文字图片与混合消息预检通过且保持原形态`；`test_forward_send.py::test_纯文字源消息经send_text通路发送并保存真实回执`、`test_图片源消息经send_image通路发送可恢复媒体`、`test_图文混排经send_hybrid保持段序`
   - 合并转发卡片 → `test_forward_precheck.py::test_完整合并转发准备完整聊天记录卡片`；`test_forward_send.py::test_合并转发卡片经send_forward保持节点与作者`
   - 存在无法支持的段 → `test_forward_precheck.py::test_缺失媒体与不支持段在任何发送副作用前整条拒绝`、`test_合并转发节点含不可用媒体或不支持段时整条拒绝`；`test_forward_send.py::test_多图或混排缺少可恢复媒体时在调用宿主前整条拒绝`
   - 普通消息中的个人提及 → `test_forward_precheck.py::test_个人提及按对象标识与显示名称转成普通文本并保持顺序`、`test_个人提及显示名优先群名片其次昵称并回退对象标识`、`test_提及与图片混排时指纹与材料一致且图片顺序不变`；`test_forward_send.py::test_含个人提及的纯文字经send_text通路发送且不新增换行`
   - 全体成员提及 → `test_forward_precheck.py::test_全体成员提及转成无提醒的普通文本并保留前后文字`、`test_全体成员提及按名称缺失的个人提及区分`
   - 提及缺少显示名称 → `test_forward_precheck.py::test_个人提及显示名优先群名片其次昵称并回退对象标识`、`test_提及对象标识与显示名同口径压平控制字符`
   - 嵌套节点中的提及 → `test_forward_precheck.py::test_合并转发节点内的提及在原节点转成普通文本并保持节点结构`、`test_嵌套节点内的提及同样转换且层级不变`、`test_节点内提及归并同样止于紧邻文本段`；`test_forward_send.py::test_含提及的完整卡片经send_forward保持节点归属`
   - 提及对象不可确认 → `test_forward_precheck.py::test_对象不可确认的提及整条拒绝并定位到段`；`test_forward_send.py::test_无效提及与不支持段经工具入口拒绝且零发送`
   - 显示信息不能变成消息指令 → `test_forward_send.py::test_提及显示信息只作普通文本不生成可执行段或改变路由`、`test_含恶意提及的混排只生成文本与图片段`、`test_提及转换不查询额外成员且消息查询只限源消息`；平台层不重解析的源码依据见 1.2，**真实平台验证未做（限制）**
4. 结果与生命周期（5）
   - 评论等待期间关闭 → `test_lifecycle.py::test_评论等待中卸载时不发送评论并保留源成功记录`、`test_评论等待中撤销目标权限时不发送评论并保留源成功记录`；`test_forward_send.py::test_评论发送前权限撤销时明确未发送`
   - 等待真实结果 → `test_forward_send.py::test_发送与评论未完成时不提前返回全部成功`；`test_lifecycle.py::test_卸载取消在途投递并等待任务退出`、`test_卸载等待超时告警可定位且不再发起新的发送`
   - 等待期间移除候选类型 → `test_lifecycle.py::test_等待期间移除一类候选只取消该类判断`、`test_等待期间撤销开关白名单或清空候选时不再提交`、`test_热更新不整体取消在途投递`、`test_名称查询期间失效时零提交[撤销候选类型-revoke_type]`
   - 唤起前修改标准与目标 → `test_lifecycle.py::test_名称查询期间修改标准后按最新标准与目标提交`、`test_名称查询期间增删目标只提交最新合法目标`、`test_只改标准不取消合法候选且后续提交使用新标准`
   - 已提交的判断不追溯重写 → `test_lifecycle.py::test_已提交判断不因配置更新被改写或补发`；`test_auto_entry.py::test_窗口内配置更新不增加已登记候选的唤起次数`

**ADDED Requirements（11）**

5. 可配置的自动候选类型（6）
   - 配置可见与可编辑 → `test_config.py::test_自动候选类型声明三个选项且默认仅合并转发`、`test_自动分享标准声明多行编辑与双写长度上限`、`test_自动入口声明的界面元数据只用已核实键集合`、`test_fake_maibot_sdk.py::test_配置字段替身保留声明元数据与默认值`；**真实浏览器界面未验证（限制，见 6.3-C）**
   - 图文混排的选择边界 → `test_auto_entry.py::test_文字与图片的候选分类由配置选择`、`test_只选中一类候选时其它类型不提供判断机会`、`test_同时选中文字与图片时图文混排只唤起一次`
   - 提及附属于普通消息 → `test_auto_entry.py::test_文字与图片的候选分类由配置选择`（含 `at` 的用例）；提及对象与内容完整性的发送侧检查见第 3 组对应用例
   - 未展开卡片仍按卡片分类 → `test_auto_entry.py::test_卡片判据与内容预检共用同一形状口径`、`test_未选中卡片类型时卡片消息不因混有文字或图片进入候选`、`test_未展开的合并转发卡片也能成为候选`、`test_未展开卡片同样进入判断且不伪造内容`、`test_卡片分类成功不等于可发送`
   - 不支持的普通混排 → `test_auto_entry.py::test_无内容非法结构与不支持混排不进入候选`
   - 空选择与未知类型 → `test_config.py::test_候选类型单选多选与空列表均可保存`、`test_未知候选类型被明确拒绝且不扩大触发范围`；`test_auto_entry.py::test_空候选列表时三类均不唤起且显式调用不受影响`
6. 可编辑的自动分享标准（5）
   - 修改内容偏好 → `test_auto_entry.py::test_判断意图标注用户配置区域并保留多行标准原文`、`test_固定提示不夹带另一套不可编辑偏好`；`test_lifecycle.py::test_只改标准不取消合法候选且后续提交使用新标准`
   - 默认与空白标准 → `test_auto_entry.py::test_标准缺省空串与全空白时回退设计默认原文`；`test_config.py::test_分享标准空串全空白与多行内容均可保存且不改写`、`test_旧配置缺少新字段时加载成功并补为默认`
   - 多行原意与长度边界 → `test_config.py::test_分享标准长度上限4000且超限不静默截断`、`test_自动分享标准声明多行编辑与双写长度上限`；`test_auto_entry.py::test_非空标准原样传递且花括号与命令样式只按文字处理`
   - 显式请求不受偏好门禁 → `test_auto_entry.py::test_偏好排除当前内容时显式工具请求仍可发送`、`test_自动入口关闭或候选为空时显式工具请求仍可发送`
   - 标准中的越权要求 → `test_auto_entry.py::test_标准或正文要求越权时仍只提供合法目标并拒绝越权调用`

**结论：** 34／34 场景均有可定位用例或源码核实记录，0 项未映射；注明限制 2 项（真实 WebUI 渲染未验证；真实平台／群消息未验证，两者均不写成已通过）。未发现实现与规格不符的缺口，无阻塞第 6 组勾选的项。主规格同步与目的描述更新保留为待流程授权事项；下一步按流程进入 `/openspec-verify-change`，提交由用户自行执行。
