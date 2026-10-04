# 第8组完整离线验收与文档交付记录

日期：2026-10-04。变更：`direct-snowluma-websocket-forwarding`。
基准提交：`2d7f4a585222a3dc2f9d57e86baf7c9f925c3c78`。这是本组核实附件，不是新规划工件。

**55项离线任务及八组独立 Spec／Standards 验收均已完成，8.1–8.6全部勾选。**
2026-10-04，编排者执行 `openspec validate direct-snowluma-websocket-forwarding --strict`，
结果为 valid、退出码0；`git diff --check` 同样退出码0。
第8组独立 Spec 验收通过；Standards 发现一条不存在的历史基线链接，定点修正后，
独立 Spec 复验 PASS、Standards 复验 PASS（0 hard）。两轴分设执行，未以实现者自审替代。
主规格、Purpose、同步、归档和 Git 提交均未执行。
真实 QQ 联验未授权、未执行；U1–U8全部未确认，线上实际加载SDK版本未确认。

## 1. 范围与执行入口

- 已读任务8组完整标准、proposal、design D1–D10、三份delta、verification01–07/U清单、
  ISOLATION、活跃组合根/编排/客户端/传输/材料/账本接口及公开文档。
- 公共入口为宿主 `bind_forward_source` Hook 产出的参数 → `forward_message`，最后到
  本机 `aiohttp.web` 临时端点；使用默认生产 `AiohttpOneBotTransport`，没有替换网络会话。
  宿主SDK仍是既有替身，所有身份、素材、认证值、目录和回执均合成，不访问QQ/远端媒体。
- 新 `tests/test_direct_acceptance.py` 共41个参数化节点：18正常材料/评论组合、11故障、
  4评论撤权/卸载、1多客户端与不同目标、3历史闭锁、3源提交前撤权、1自动入口隔离。
  复用 `tests/direct_support` 的材料格式转换和存储故障代理、`tests/snowluma_transport_support`
  的本地端点与有限等待；未复制生产状态机。预期段/目标/ID/状态均独立字面量。
- 真实WS端点广播动作响应并注入匹配echo的伪装事件及无关echo；双客户端并发、关闭
  插件后另一客户端仍可查询，实际发送仅固定群动作。空冷目标仅宿主源会话存在，不唤起
  目标Planner；同消息另一目标独立账本。线路段、工具结果和真实SQLite公开读回相互核对。
- 旧HTTP/adapter API/host send：本地端点非WS请求记录为空、注入的 `ctx.api.call` 计数为0、
  `ctx.send.calls`为0；同时复用生产静态调用/导入检查和退役CLI检查，避免仅靠一个端点
  没收到HTTP就推断不存在其他通路。WS握手本身是HTTP Upgrade，不是旧HTTP转发服务。
- 内存平台替身的既有全链用例继续承担更细故障注入；不把 `MemorySocket/MemorySession`
  或 `DirectOneBot` 标为真实网络。下节同时列真实WS和既有公共测试，外部效果单列人工项。

## 2. 验收可靠性维护与实际失败记录

- `tests/test_forward_precheck.py` 的本模块公共装配登记pytest finalizer，即使提前断言失败
  也调用 `DirectFixture.close()`；收尾通过公开账本查询确认已关闭，并以独占文件锁证明
  使用权释放，不依赖GC。首次附加“重开整个目录”验证时遇到测试合法创建子目录而被历史
  检查拒绝；已改为检查实际文件锁释放，不放宽生产历史检查。
- `test_forward_send.py/test_forward_precheck.py` 中隐私断言改读实际记录的查询参数与发送段，
  不再用只投影路由字段的 `routing_observation()` 证明无正文。原资源ID确实必须在线路
  发送；只禁止上传内部节点/作者，不错误断言资源引用也不得发送。纯目标观察可继续复用旧助手。
- 新传输模块在无aiohttp时按约定导入期跳过。第一次全量默认测试使第7组精确收集守卫
  报2个失败（新模块尚未加入无库例外）；仅将此模块加入明确的无库集合，带库仍要求全部
  活跃文件收集，不全局忽略。`test_isolation.py`仅这处相关维护。
- 自动入口新用例初次误用替身记录字段 `session_id` 导致1个断言失败，按实际公开调用
  `stream_id` 修正测试，没有为错误预期修改SDK替身或生产代码。
- 新正常链18节点首次即通过；后来37节点故障链通过，最终41节点随全量通过。现有行为
  覆盖新增不人为删除业务代码制造红灯。本组未改已通过业务/配置/清单/SDK替身。

## 3. 实际验证结果

环境：macOS、本地合成临时目录；Python **3.14.7**，pytest **9.1.1**，aiohttp **3.14.3**，
pyright **1.1.414**，uv **0.12.15**。验证依赖仅uv临时环境，无全局/真实SDK安装。
默认环境用 `importlib.util.find_spec("aiohttp")` 实查为False，不把已安装网络库的环境冒充无库。

修复上述测试维护问题后按顺序实际运行，均退出码0：

1. `uv run --no-project --with pytest pytest tests/ -q`：**777 passed, 6 skipped**。
2. `uv run --no-project --with pytest --with aiohttp pytest tests/ -q`：**856 passed，无skip**；
   同一依赖选择查询实际版本为3.14.3，符合清单下限。
3. `uv run --no-project --with pyright pyright`：**0 errors, 0 warnings, 0 informations**。
4. `uv run --no-project --with pyright pyright --outputjson`：**filesAnalyzed=22, errorCount=0**。
5. `uv run --no-project --with pytest --with aiohttp==3.14.3 pytest tests/ -q`：**856 passed**，固定下限复验。
6. `uv run --no-project --with pytest pytest -q`：根默认 **777 passed, 6 skipped**。
7. `uv run --no-project --with pytest pytest --collect-only -qq`：根默认13活跃测试文件，
   逐文件合计 **781节点**（含4个运行时跳过），不含历史。
8. `uv run --no-project --with pytest --with aiohttp==3.14.3 pytest --collect-only -qq`：
   根默认全部15活跃测试文件，合计 **856节点**，含新41和传输34节点，无历史。
9. `test_isolation.py::test_默认CLI仅收集活跃测试且直连与纯逻辑均保留` 两个参数分别
   实际执行根和显式 `tests/ --collect-only -q` 子命令，核对完整文件集合，均通过。

默认6 skip是两个真实网络模块导入期跳过（34+41节点）和4个运行期节点，不等于只缺6个
真实网络用例。带库所有856节点真正执行，未发生导入期skip。
类型范围：13根模块、8共享模块、`service/__init__.py`墓碑，22个活跃生产文件；目标
Python3.11，历史明确排除；缺真实SDK的导入诊断约定未改。

## 4. 新增/修改场景逐项映射

以下覆盖跨群转发delta的53个场景及直连客户端delta的22个场景，共75项；九项移除需求
单列第5节。按每份delta的Requirement及原Scenario标题列出。`tests/文件::函数` 是实际存在的
公共pytest节点；参数化函数表示包含其收集的全部分支，参数值见原测试。每项外部语义
仍受U清单和人工项约束；自动化通过仅说明冻结契约下的插件行为。
人工项编号指 `docs/real-qq-validation.md`，均未执行。

### cross-group-forward：独立插件与路径开关

- 单独安装 → `tests/test_isolation.py::test_无服务历史和第三方运行环境仍能公开创建并注册组件`；`tests/test_plugin.py::test_直连默认关闭或缺配置不构造连接且仍注册三组件`；人工P1–P4。
- 仅关闭自动入口 → `tests/test_lifecycle.py::test_仅关闭自动入口时待执行唤起被取消且工具仍可发送`；人工A1。
- 默认兼容 → `tests/test_config.py::test_旧配置缺少新字段时加载成功并补为默认`；`tests/test_config.py::test_旧服务配置退役且保留业务设置不产生直连`。

### cross-group-forward：自动入口由源群决定

- 自主分享 → `tests/test_auto_entry.py::test_白名单源群合并转发成为候选并唤起一次自动判断`；`tests/test_auto_entry.py::test_意图包含源消息ID合法目标与自主判断说明`；人工A1。
- 重复上报与自身消息 → `tests/test_auto_entry.py::test_同一候选重复上报只唤起一次`；`tests/test_auto_entry.py::test_机器人自身消息不触发自动入口`。
- 普通消息 → `tests/test_auto_entry.py::test_普通文字与图片不触发自动入口但仍可经工具发送`。
- 显式开启普通消息 → `tests/test_auto_entry.py::test_文字与图片的候选分类由配置选择`；`tests/test_auto_entry.py::test_意图说明实际候选类型而不把普通消息说成合并转发`。
- 消息未就绪 → `tests/test_auto_entry.py::test_候选消息始终不可读取时有限等待后放弃并记录原因`。
- 判断标准不能替代授权 → `tests/test_auto_entry.py::test_标准或正文要求越权时仍只提供合法目标并拒绝越权调用`；`tests/test_auto_entry.py::test_偏好排除当前内容时显式工具请求仍可发送`。
- Planner 选择沉默 → `tests/test_direct_acceptance.py::test_真实WS事件与宿主Hook并存只有源群判断且沉默不发送`；人工A1。
- 直连事件与宿主事件同时到达 → 同上真实WS节点；`tests/test_auto_entry.py::test_真实直连事件与宿主候选并发仅宿主唤起一次且沉默零新增发送`（后者是MemorySocket、不是网络）；人工U2/A1。

### cross-group-forward：三参数群聊工具与可信权限

- 合法调用 → `tests/test_direct_acceptance.py::test_公共工具经真实WS完整材料评论与冷目标`；`tests/test_forward_precheck.py::test_工具声明仅含三个公开字符串参数`。
- 伪造上下文 → `tests/test_forward_precheck.py::test_模型伪造的会话平台与账号字段不能覆盖宿主来源`；`tests/test_forward_precheck.py::test_越权源消息被拒绝且不泄露正文`；`tests/test_forward_precheck.py::test_混合工具调用仅清洗转发调用并签发凭据`。
- 目标无权限 → `tests/test_forward_precheck.py::test_目标群白名单与源群自身被拒绝`；`tests/test_direct_acceptance.py::test_真实WS故障互证工具账本线路且重启不补发`（wrong_account）。
- 源消息标识无法对应 → `tests/test_forward_precheck.py::test_可信源映射不足或冲突在本地拒绝且不泄正文`；人工P6/U3。

### cross-group-forward：支持消息类型与完整性

- 文字和图片 → `tests/test_direct_acceptance.py::test_公共工具经真实WS完整材料评论与冷目标`（文字/单图/多图/图文）；人工V1–V4/U5。
- 合并转发卡片 → 同上（原卡片）；`tests/test_forward_precheck.py::test_顶层卡片混排仍在本地拒绝而未展开卡片按卡片处理`；人工V7/V8/U4/U6。
- 存在无法支持的段 → `tests/test_forward_precheck.py::test_缺失媒体与不支持段在任何发送副作用前整条拒绝`；`tests/test_snowluma_client.py::test_普通提及未知或不支持段不丢段降级`；人工V10/V11。
- 普通消息中的个人提及 → `tests/test_direct_acceptance.py::test_公共工具经真实WS完整材料评论与冷目标`（提及）；`tests/test_forward_send.py::test_含提及的图文混排经直连客户端混合通路保持段序与图片顺序`；人工V5/V6。
- 全体成员提及 → 同上真实WS提及分支；`tests/test_forward_precheck.py::test_全体成员提及转成无提醒的普通文本并保留前后文字`；人工V5。
- 提及缺少显示名称 → 同上真实WS提及分支；`tests/test_forward_precheck.py::test_个人提及显示名优先群名片其次昵称并回退对象标识`；人工V6。
- 嵌套节点中的提及 → `tests/test_snowluma_client.py::test_卡片只取原资源引用并忽略全部内部形状`；`tests/test_forward_send.py::test_含提及的完整卡片经直连客户端卡片通路保持节点归属`；人工V7/U6。
- 提及对象不可确认 → `tests/test_forward_send.py::test_无效提及与不支持段经工具入口拒绝且零发送`。
- 显示信息不能变成消息指令 → `tests/test_forward_send.py::test_提及显示信息只作普通文本不生成可执行段或改变路由`；真实WS正常链非空评论分支；人工C2/U4。
- 原资源不可用 → `tests/test_snowluma_client.py::test_卡片资源缺失或顶层混排整条拒绝`；`tests/test_snowluma_client.py::test_准备身份不符或材料来源无法确认拒绝且零发送`；人工V11/U6。
- 顶层卡片混排 → `tests/test_forward_precheck.py::test_顶层卡片混排仍在本地拒绝而未展开卡片按卡片处理`；`tests/test_snowluma_client.py::test_卡片资源缺失或顶层混排整条拒绝`；人工V9。

### cross-group-forward：真实发送与自然上下文

- 成功发送可读 → `tests/test_direct_acceptance.py::test_公共工具经真实WS完整材料评论与冷目标`（非空评论、双ID/落账）；QQ可见性人工V1–V8/C2；宿主历史不作保证。
- 目标冷会话 → 同上真实WS节点（只有源宿主会话，目标Planner零调用）；人工A2。
- 服务不可用 → `tests/test_direct_acceptance.py::test_真实WS故障互证工具账本线路且重启不补发`（before_send/after_send/no_response）；`tests/test_forward_delivery.py::test_直连不可达时关闭发送`；人工F1/F2。
- 自发事件回流 → `tests/test_direct_acceptance.py::test_真实WS事件与宿主Hook并存只有源群判断且沉默不发送`；人工U2/A1/F8。

### cross-group-forward：可选评论固定延迟

- 非空评论 → `tests/test_direct_acceptance.py::test_公共工具经真实WS完整材料评论与冷目标`（0.49秒零评论、0.5秒评论sending占位）；人工C2。
- 空评论 → `tests/test_direct_acceptance.py::test_公共工具经真实WS完整材料评论与冷目标`（空/空白）；`tests/test_direct_acceptance.py::test_真实WS多客户端并存关闭隔离且另一目标独立`（缺省）；`tests/test_forward_send.py::test_空评论或全空白评论只发送源消息且不等待`。
- 转发失败 → `tests/test_direct_acceptance.py::test_真实WS故障互证工具账本线路且重启不补发`（failed/unknown，评论零提交）。
- 等待期间客户端退出 → `tests/test_direct_acceptance.py::test_真实WS评论等待撤权保留本次结果与持久字段边界`（卸载）；`tests/test_lifecycle.py::test_异步等待后按最新权限和连接条件阻止提交`（断线等）；人工F5/F7。
- 等待期间切换连接配置 → 同上真实WS节点（切换引用）；`tests/test_forward_delivery.py::test_端点热更新只允许新调用使用新连接且旧评论不补发`。

### cross-group-forward：持久防重与不确定结果

- 跨重启重复调用 → `tests/test_direct_acceptance.py::test_真实WS评论等待撤权保留本次结果与持久字段边界`；`tests/test_forward_delivery.py::test_成功回执立即落账并跨重启防重`；人工F3。
- 评论失败 → `tests/test_direct_acceptance.py::test_真实WS评论等待撤权保留本次结果与持久字段边界`；`tests/test_forward_send.py::test_评论未完成时返回部分成功并保留源消息回执`。
- 结果未知 → `tests/test_direct_acceptance.py::test_真实WS故障互证工具账本线路且重启不补发`；`tests/test_delivery_ledger.py::test_评论在途重开仅评论转未知并保留源成功`；人工F2/F6。
- 同消息不同目标 → `tests/test_direct_acceptance.py::test_真实WS多客户端并存关闭隔离且另一目标独立`；人工F3。
- 旧账本迁移 → `tests/test_delivery_ledger.py::test_完整原库字段逐项兼容且不会重建`；`tests/test_delivery_ledger.py::test_重开自动恢复源在途但保留成功未知且不按时间容量清理`；真实原库核对人工P5/F9，无自动服务回迁。
- 服务历史尚未核对 → `tests/test_direct_acceptance.py::test_真实端点可用但服务历史痕迹使公共工具闭锁`；`tests/test_delivery_ledger.py::test_本目录历史证据不读内容不联网也不修改`；人工P5/F9。
- 发送前后账本失败 → `tests/test_direct_acceptance.py::test_真实WS故障互证工具账本线路且重启不补发`（reserve/mark_sent/reserve_comment/record_comment）；人工F4/F6。
- 同一数据目录重复加载 → `tests/test_delivery_ledger.py::test_独占失败不恢复活实例且不同目录隔离并可安全重开`；人工F9。

### cross-group-forward：结果与生命周期

- 评论等待期间关闭 → `tests/test_direct_acceptance.py::test_真实WS评论等待撤权保留本次结果与持久字段边界`；人工F5。
- 等待真实结果 → `tests/test_forward_send.py::test_发送与评论未完成时不提前返回全部成功`；真实WS正常链等待和故障链no_response；`tests/test_forward_send.py::test_工具结果可区分八种调用状态且口径一致`。
- 等待期间移除候选类型 → `tests/test_lifecycle.py::test_等待期间移除一类候选只取消该类判断`；`tests/test_lifecycle.py::test_名称查询期间失效时零提交`。
- 唤起前修改标准与目标 → `tests/test_lifecycle.py::test_名称查询期间修改标准后按最新标准与目标提交`；`tests/test_lifecycle.py::test_名称查询期间增删目标只提交最新合法目标`。
- 已提交的判断不追溯重写 → `tests/test_lifecycle.py::test_已提交判断不因配置更新被改写或补发`。
- 远端撤销竞态 → `tests/test_direct_acceptance.py::test_真实WS源账号查询等待后复验权限`；`tests/test_lifecycle.py::test_异步等待后按最新权限和连接条件阻止提交`；`tests/test_forward_delivery.py::test_提交后取消保留未知且阻断重发`；名称沿用delta，不存在旧HTTP撤销协议；人工F7。
- 卸载与其他客户端并存 → `tests/test_direct_acceptance.py::test_真实WS多客户端并存关闭隔离且另一目标独立`；`tests/test_lifecycle.py::test_默认工厂孤立动作卸载告警保留源目标阶段`；`tests/test_lifecycle.py::test_阻塞账本关闭纳入卸载剩余预算且保留真实使用权`；人工F8。

### cross-group-forward：退役实现与活跃路径隔离

- 新插件正常加载 → `tests/test_isolation.py::test_无服务历史和第三方运行环境仍能公开创建并注册组件`；人工P2/P3。
- 直连故障 → `tests/test_plugin.py::test_全部支持类型与故障路径都不调用宿主旧发送通道`；`tests/test_plugin.py::test_生产代码不含旧发送或宿主旁路调用`；真实WS故障链零旧通道计数。
- 默认验证与历史保存 → `tests/test_isolation.py::test_默认CLI仅收集活跃测试且直连与纯逻辑均保留`；`tests/test_isolation.py::test_历史109项基线完整且清单自身未被改写`；README/历史抬头人工文档检查。

### snowluma-direct-client：正向连接与受控认证

- 与现有适配器并存 → `tests/test_direct_acceptance.py::test_真实WS多客户端并存关闭隔离且另一目标独立`；`tests/test_snowluma_transport.py::test_3_4_双客户端广播和过期响应隔离且关闭互不影响`；真实适配器人工U2/F8。
- 地址或凭证未完成 → `tests/test_config.py::test_不可信直连地址被安全拒绝且不替换合法配置`；`tests/test_config.py::test_未配置地址返回明确诊断而不是连接描述`；`tests/test_snowluma_transport.py::test_3_2_握手失败安全分类且重定向零请求`；人工P4/U1。
- 认证重定向 → `tests/test_snowluma_transport.py::test_3_2_握手失败安全分类且重定向零请求`（真实本地重定向目标零请求/凭证）；人工U1。
- 默认关闭 → `tests/test_plugin.py::test_直连默认关闭或缺配置不构造连接且仍注册三组件`；`tests/test_plugin.py::test_默认总开关关闭时消息输入不发送`。

### snowluma-direct-client：同账号查询与源材料可信性

- 错误账号 → `tests/test_direct_acceptance.py::test_真实WS故障互证工具账本线路且重启不补发`（wrong_account）；人工P6/U3。
- 查询返回另一群 → `tests/test_snowluma_client.py::test_准备身份不符或材料来源无法确认拒绝且零发送`（wrong_group）；人工U3/F1。
- 标识对应不足 → `tests/test_forward_precheck.py::test_可信源映射不足或冲突在本地拒绝且不泄正文`；人工P6/U3。
- 核验后连接重建 → `tests/test_snowluma_client.py::test_每阶段取得执行机会后核对账号并固定配置连接`；`tests/test_snowluma_transport.py::test_3_7_排队后的许可复验拒绝且固定连接不重连`；人工F7。

### snowluma-direct-client：动作关联与事件隔离

- 事件先于动作响应 → 每个真实WS完整链发送前注入事件；`tests/test_snowluma_transport.py::test_3_3_伪装事件和非法响应不能完成动作且空闲消费心跳`；人工U2。
- 其他客户端和迟到响应 → `tests/test_snowluma_transport.py::test_3_4_双客户端广播和过期响应隔离且关闭互不影响`；`tests/test_direct_acceptance.py::test_真实WS多客户端并存关闭隔离且另一目标独立`。
- 伪装成响应的事件 → `tests/test_snowluma_transport.py::test_3_3_伪装事件和非法响应不能完成动作且空闲消费心跳`；真实WS正常链匹配echo的伪装事件不污染结果。

### snowluma-direct-client：有界动作与不重放

- 断线前后 → `tests/test_direct_acceptance.py::test_真实WS故障互证工具账本线路且重启不补发`（before_send/after_send/no_response）；`tests/test_snowluma_transport.py::test_3_7_提交后故障保留证据且后续只读不重放`；人工F2。
- 排队期间撤销权限 → `tests/test_forward_delivery.py::test_真实传输排队结束后复验最新权限且零发送帧`（网络库会话替身）；`tests/test_snowluma_transport.py::test_3_7_排队后的许可复验拒绝且固定连接不重连`（真实WS）；`tests/test_direct_acceptance.py::test_真实WS源账号查询等待后复验权限`。
- 事件持续到达 → `tests/test_snowluma_transport.py::test_3_5_握手阻塞及持续事件均不能延长期限`；`tests/test_snowluma_transport.py::test_3_6_缓冲事件洪泛公平让出且不扩张后台任务`；`tests/test_snowluma_transport.py::test_3_6_完整消息超过四MiB关闭且提交事实不丢失`。
- 并发容量耗尽 → `tests/test_forward_delivery.py::test_八个在途投递时第九个繁忙且退出后释放容量`；`tests/test_snowluma_transport.py::test_3_6_并发调用仅有一个待决动作`。

### snowluma-direct-client：真实回执与安全结果分类

- 成功响应但标识不足 → `tests/test_direct_acceptance.py::test_真实WS故障互证工具账本线路且重启不补发`（no_id）；`tests/test_snowluma_client.py::test_本地不透明回执只有候选ID不能确认发送`；人工U7。
- 回执不属于本次投递 → 同上真实WS错record/target；`tests/test_snowluma_client.py::test_回执错记录错群错账号或非正整数序列均未知`；人工U7。
- 发送后查询失败 → `tests/test_snowluma_client.py::test_回执查询三秒总预算且仅查本次ID不叠加十秒`；`tests/test_snowluma_client.py::test_回执末次等待不超过三秒剩余预算`；人工U7/U8/F2。
- 明确未提交 → `tests/test_direct_acceptance.py::test_真实WS源账号查询等待后复验权限`；真实WS故障before_send；`tests/test_forward_delivery.py::test_明确未发送的失败可重试`。

### snowluma-direct-client：材料最小化与隐私

- 恶意媒体和正文 → `tests/test_snowluma_client.py::test_媒体不可重放在准备阶段拒绝不读文件或内网`；`tests/test_forward_precheck.py::test_正文与节点中的伪造指令不能改变源群与目标选择`；`tests/test_forward_send.py::test_提及显示信息只作普通文本不生成可执行段或改变路由`；远端DNS/重定向安全人工U5。
- 工作完成或失效 → `tests/test_snowluma_client.py::test_工作结束释放临时正文而结果仍可定位`；`tests/test_delivery_ledger.py::test_调用方原始原因不能将正文凭证资源写入账本或日志`。
- 库异常带有敏感载荷 → `tests/test_snowluma_transport.py::test_3_2_握手失败安全分类且重定向零请求`；`tests/test_snowluma_client.py::test_异常不回显载荷并保留可定位身份`；真实WS故障链检查日志/结果不含合成敏感值。

## 5. 九项 REMOVED 的替代与隔离证据

通用隔离证据：`history/snowluma-forward-service/ISOLATION.md` 的65项原路径退出清单，
`tests/test_isolation.py::test_生产导入图不经过退役模块且原入口没有发送实现`、
`tests/test_isolation.py::test_旧独立服务CLI不再提供启动入口`、
`tests/test_isolation.py::test_公开配置和共享导出不再提供服务控制协议`。

1. 独立服务与专用调用边界 → 受控正向连接/合法工具；真实WS完整链与公共孤立加载。
   `service/__main__.py/server.py/http_api.py`等只留完整历史，原包入口墓碑无启动。
2. 认证与最小账号权限 → 环境变量引用、可信宿主账号与直连账号、两级白名单；配置/握手/
   wrong_account节点。旧HTTP认证及服务许可上限在历史 `service/auth.py/config.py`。
3. 源材料来自可信源消息 → 本地预检+同账号`prepare_source`，实际线路仅查询源ID和发送
   已核实材料；源归属/原卡片测试。旧 `service/materials.py/gateway.py`完整历史保留。
4. 准备与执行分离 → 只读准备、持久占位、所有异步等待后的最后许可；源/评论时序节点。
   `shared/protocol.py`、旧会话/挑战/心跳/阶段授权退出活跃导入，未恢复远程授权协议。
5. 发送结果与只读核实 → 四项回执、有限同ID查询、本插件账本查询；回执故障与重启测试。
   旧HTTP查询及`remote_client.py`实现归历史，原位置只墓碑，不再次发包探测。
6. 服务端持久投递事实 → `delivery_ledger.py`唯一权威、四字段键/双阶段占位；完整原库、
   占位、终态失败/重启节点。`service/ledger_store.py`及`shared/ledger.py`退出活跃路径。
7. 撤销与有限生命周期 → 同步提交前复验、本插件5秒共享关闭预算；真实WS撤权/卸载与
   不合作清理测试。旧 `service/sessions.py`和远程注销/撤销接口只属历史。
8. 旧账本迁移与回滚防重 → 完整原库兼容、服务历史闭锁，不自动回迁；历史证据及重开
   测试。旧迁移/导出CLI及专属回滚测试完整保留，不自动运行或删除真实收据。
9. 最小数据与部署隔离 → 自己数据目录独占、临时材料释放、安全错误、历史不收集；
   隐私/独占/根收集节点。根服务`pyproject.toml`、服务状态/跨进程设施归历史，清单仍
   是插件依赖唯一入口，不维护两套产品。

## 6. 文档与不可混淆的限制

- README、AGENTS和新 `docs/direct-snowluma-operations.md` 描述当前唯一客户端/本地账本路径；
  两直连字段默认空、证书/回环边界、容器localhost、凭证不落盘、候选/标准、完整材料、
  未知/部分成功/账本不可用、目标历史不保证均明确。
- `docs/real-qq-validation.md` 全部人工检查保持空框，U1–U8未确认，公开SDK2.8.0与宿主
  锁2.5.2不作为运行证据。旧服务手册、adapter API方向报告及旧切换对照仅加纠偏抬头，
  原历史内容保留；运维和QQ清单链接完整历史原件。
- `CONTEXT.md` 当前投递账本术语已符合唯一权威，无必要更正，因此未改；未动旧ADR、
  归档任务和主规格。`CLAUDE.md → AGENTS.md`仍由公共规则测试验证，未另造副本。
- 评论等待中撤权/卸载：本次partial/failed证明没提交，持久评论字段仍空；后续
  already_forwarded不补评论。评论已提交且结果persist失败：partial/unknown/
  comment_persist，保留已知真实评论ID，重开评论unknown，不冒称可靠落账。
- 109基线项（1,633,141字节）及MANIFEST未覆盖；完整性测试全量通过。ISOLATION记录
  原65项退出仍不变，既有归档任务的真实联验未完成事实不变。本组只动8.x复选框。

## 7. 最终验收结果与后续边界

- 8.1/8.2：已完成，覆盖与新增41节点见上。
- 8.3：已完成，两档/固定版本/根收集/类型实跑通过；独立 Spec 重跑同样得到默认
  777 passed/6 skipped、两种带库命令均856 passed、22生产文件零类型错误。
- 8.4/8.5：已完成，文档/规则对齐，单一规则软链保持。
- **8.6为[x]**：75场景与9移除需求映射、交接、严格校验和两轴独立验收已完成。
  编排者执行 `openspec validate direct-snowluma-websocket-forwarding --strict` 返回 valid，
  `git diff --check` 通过；实现者未执行流程级命令。
- 独立 Spec：PASS。独立 Standards 初审发现 handover 抬头引用了不在109项基线中的
  不存在文件；只删除不实链接和保存声明、保留历史正文，未补造或修改冻结快照。
  文档修复后的独立 Spec 复验 PASS，Standards 复验 PASS（0 hard）。后两次是文档
  定向复验，不冒称再次重跑856项；此前独立全量结果见第3节与本节。
- 前组遗留非阻塞规范建议：传输清理布尔变量命名、标识符类型适配的小重复、账本回滚
  辅助函数旧名。未为命名建议扩大生产重构，可留后续收敛；第6组测试清理和隐私观测
  建议已在本组修复。所有组均无未关闭 hard violation 或 Spec 阻断项。
- 真实部署/凭证注入/账本处置/QQ联验/回滚、主规格/Purpose同步、归档与Git提交留用户
  另行决定。U1–U8及实际SDK版本仍未确认；离线验收通过不是上线放行。
