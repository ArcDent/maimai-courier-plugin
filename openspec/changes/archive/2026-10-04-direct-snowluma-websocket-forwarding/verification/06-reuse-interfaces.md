# 1.6 复用接口、输入输出与测试归属

日期：2026-10-02。以下为后续组的**公开继任契约**，不是已存在的新实现。
以现有导出及 D1 小接口为基线；协议假设见01–05/U清单。活跃代码不得导入 `service/` 或历史区。

## 传输：`snowluma_transport.py`

从 `service/onebot.py` 演进并保留名称：

- `OneBotConnection(endpoint, token_env_var)`：只保存合法固定地址与环境变量名，不含凭证。
- `OneBotUnresolved(reason, config_failure=False)`：安全配置诊断。
- `resolve_onebot_connection(config, environ)`：改接 `[snowluma]`，返回上述二者；
  复用现有地址/环境变量规则，无连接副作用，不解析旧 `[service]` 为新地址。
- `OneBotTransport` 与 `AiohttpOneBotTransport(connection, *, clock, environ, register_task)`：
  保留固定方法 `get_login_info()`、`get_msg(message_id)`、`get_group_list()`、
  `send_msg(*, target_group_id, message, stage="", expected_generation, before_submit)` 和 `close()`。
  前三者返回原始 OneBot 响应映射；只对后续独立只读调用允许建连。`stage` 不上线。
- 增加只读 `connection_generation`：每次建立/失效连接变化，旧值不能在新连接使用。
  查询可接受 `expected_generation` 约束；提供时固定连接失效即失败，不自动重连。
- `before_submit: Callable[[], bool]` 是**同步**许可回调，只由组合根/编排提供。
  发送排队完成且固定代次仍匹配后调用；返回 False 就不写帧，复验和开始写入之间
  不插入其它异步工作。异常也按未提交安全拒绝，不能回显异常原文。
- 演进 `OneBotUnavailable`，只增加内部 `submitted: bool`（默认 True，保守）、安全类别；
  False 必须有写入前证据，True 表示可能写出。开始 `send_json/send_str` 前置位。
  提交后取消要向上保留该证据；不能让裸取消被编排误当作 failed。
- 一个接收任务、一个当前响应槽、一个串行锁，运行实例/连接/动作关联；持续消费并丢弃
  无关事件。`register_task` 接现有本插件任务登记，不创建独立任务框架或事件缓存。
- 注入 `Clock`、会话创建工厂/测试端点和环境映射；超时/关闭共享绝对预算，返回或抛安全类别。
  `close()` 幂等、不发送 QQ 注销；未能退出的任务由调用方登记告警，不假装成功释放。

测试：`tests/test_snowluma_transport.py` 经上述四个固定动作和 `close` 观察本地端点；
复用 `tests/test_service_onebot_transport.py` 的本地设施思路，取消服务配置依赖。
不导出 `call_action(action, params)`，不通过 `_call`/接收循环私有方法验收。

## 本地来源预检：`forward_precheck.py`

保留 `precheck_forward_request(ctx, config, bindings, arguments) -> PreparedForward | Rejected`
及 `SourceSession`、三参数 `ForwardArguments`、白名单函数；不增加模型字段。
SDK 调用面已按 [02 的 2.8.0 固定源码与发布包证据](02-host-identity-mapping.md) 复核：
`ctx.chat.get_all_streams(platform="all_platforms")` 正常返回会话列表，
`ctx.message.get_by_id(message_id, chat_id=可信session_id, include_binary_data=True)`
正常返回消息字典或 `None`；两个公开签名都注解为 `Any`，不是强类型返回保证。
Runner 拆外层 `result`，SDK拆 `streams/message`；宿主业务失败字典原样返回，RPC错误
才抛出，继续复用既有形状/失败检查。会话账号与消息元数据不经SDK过滤或改写。
此证据针对本插件声明的SDK下限，不确认线上实际加载版本；不需要另建解包接口。
按02条件链确认宿主消息归属并在 `PreparedForward` **增加 `onebot_message_id`** 内存字段，
仅本地可信查询能赋值，无法映射则 `Rejected.not_sent`。原 `arguments.message_id` 不变，
原四字段账本键仍从它和 `SourceSession` 生成。宿主段预检继续走原公开入口。

测试：`tests/test_forward_precheck.py` 经宿主查询替身覆盖元数据冲突/错记录/未知链；
来源凭据、SDK导出与组件声明继续由既有公共插件测试守住。

## 小执行接口：`snowluma_client.py`

`SnowLumaClient(*, transport: OneBotTransport, clock: Clock, config_generation: int)`
继任 `SnowLumaGateway`；只公开准备、源发送、评论发送与关闭给编排，不保留服务会话/授权：

- `prepare_source(prepared: PreparedForward) -> PreparedSource | PrepareRejected`：只读。
  账号取 `prepared.source.account_id`，目标/源取可信预检；按账号→目标名册→源查询→
  归属与返回ID→材料执行。没有正文/资源参数，失败理由不泄露源内容。
- `PreparedSource` 复用 `service/materials.py` 的材料及源/目标字段，增加
  `account_id/connection_generation/config_generation`；不加服务尝试或挑战字段。
  `source_message_id` 是本次 OneBot 查询标识，防重仍由原 PreparedForward 决定。
- `SourceSendRequest` 收敛为 `prepared: PreparedSource`；`CommentSendRequest` 收敛为
  `prepared: PreparedSource, comment: str`，移除旧 `delivery_id/attempt_id`。
  `SourceSendStep/CommentSendStep` 保持请求→异步 `SourceSendResult` 的类型责任。
- `send_source(request, *, before_submit) -> SourceSendResult` 与
  `send_comment(request, *, before_submit) -> SourceSendResult`：只发固定群动作一次。
  源与评论相同许可回调语义，空白评论在任何平台查询前结束；没有补发。
- 执行入口用同一有界串行执行锁包住准备/发送所需查询和动作；**取得发送执行机会之后**
  再查账号，携带固定代次，随后交传输同步 `before_submit`。传输仍复验代次和回调，
  不能把客户端较早的检查当最后许可。实例只由组合根持有，生产不另有调用者穿插动作。
  获取锁也计入期限；已绑定连接断开不在本阶段重连，连接/配置代次变化停止旧评论。
- 回执有限查询与分类统一复用 `response_payload/response_data/canonical_message_id`
  和原网关四项判据（不是方向报告的“三重”）；补齐类型校验并去掉超预算最小等待。
  只有04的证据才 `sent`；未提交 `failed`，可能提交/未知取值 `unknown`。
- `close()` 关闭本客户端连接，不触碰别的客户端；上层生命周期共享5秒预算。

这里的串行执行锁只是已有串行化责任延伸，不新增通用连接池/动作调度框架。
注入点：固定传输协议、Clock、同步许可回调；测试用公开方法的替身控制账号/断线/回执。
测试：`tests/test_snowluma_client.py`，复用旧 `test_service_source_attribution`、材料、
回执及分类用例的合成数据；不调用 `_identity_verdict` 或 `_send_allowed`。

## 材料：`send_material.py` / `shared/mentions.py`

- 保留宿主 `build_send_material(raw_message) -> SendMaterial | UnsupportedContent`、
  唯一宿主卡片判据 `is_forward_card`；宿主二进制预检不等价于远端可重放。
- 从 `service/materials.py` 收敛 `build_source_material(message)`、
  `source_outbound_segments(material)`、`PreparedSource/PrepareRejected` 的活跃定义。
  前者为 `SourceMaterial | PrepareRejected`，后者为 OneBot 段序列或拒绝；纯转换、不IO。
  两种输入使用不同入口，不把 OneBot 段误套宿主包装形状。
- `SourceMaterial` 继续唯一定义于 `shared/send_steps.py`；`shared/mentions.py` 仍是
  提及格式唯一实现，`shared/identifiers.py` 仍是标识校验唯一规则。媒体安全前提见03，
  不能照抄旧“任意外部域名即安全”的注释。

测试：既有 `tests/test_forward_precheck.py` 做宿主材料回归；OneBot材料经公开
`build_source_material` 或 `SnowLumaClient.prepare_source` 测试，成功与整条拒绝都覆盖。

## 唯一账本与评论占位：`delivery_ledger.py`

- 保留 `open_ledger(path, *, clock=None) -> DeliveryLedger | LedgerUnavailable`，先独占、
  查历史、验证schema，再兼容扩展与恢复；复用 `service/ledger_store.py` 的 flock
  思路和 `ledger_migration.py` 的完整性检查，不复制服务表/尝试/会话。
- 保留 `reserve(key) -> Reservation`、`get(key) -> DeliveryRecord | None`、
  `mark_sent/mark_failed/mark_unknown/record_comment -> bool`、`recover_interrupted -> int`、`close()`。
  None仅无记录，False仅没资格/没命中，存储错误仍抛 `LedgerUnavailableError`。
- 最小新增 `reserve_comment(key) -> Reservation`：仅源 `sent` 且评论空态获得一次
  原子 `sending` 占位，存储错误不发送；终态写入只允许该评论在途位。
  新调用即便源已sent也不会调用此方法，重复调用不补发。
- 评论 `sending` 重启转 `unknown`，源保持 `sent`；原字段和四字段键不变，正文不入库。
  锁/SQLite连接可注入故障，结果经公开入口验收，事务内不等网络。

测试：`tests/test_delivery_ledger.py`；静态状态文件规则见07。测试用临时目录，不打开真实库。

## 编排与结果：保留现有责任

`deliver_forward` 的关键输入改为 `PreparedForward`、本地 `DeliveryLedger`、上述客户端、
Clock和同步 `send_allowed`；输出仍 `DeliveryResult`。顺序是账本先查→只读准备→复验与
源占位→最后复验发送→源落账→固定0.5秒→复验与评论占位→最后复验发送→评论落账。
取消/未知/落账失败按04，不恢复远程授权、HTTP或宿主fallback。

`shared/result.py` 的 `SourceSendResult/DeliveryResult/render_forward_result` 仍是唯一结果面，
`shared/delivery.py` 仍定义键与状态，`clock.py` 重导出 `shared.clock`，不制造第二套类型。
服务专属 `shared/protocol.py/ledger.py` 等只在确认活跃引用清零后历史隔离。

测试：`tests/test_forward_delivery.py/test_forward_send.py/test_lifecycle.py/test_plugin.py`，
从 `create_plugin`、公开 Hook/Tool、热更新入口观察本地WS发送、账本与结果；
`tests/test_auto_entry.py` 保持宿主自动入口唯一性。历史专属用例完整保存，默认收集隔离归第7组。

本附件已明确落点/输入输出/失败与注入；后续如需改变这些责任或媒体安全前提，返回规划，
不以私有测试入口或通用动作代理绕过 D1。
