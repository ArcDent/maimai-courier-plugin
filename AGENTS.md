# AGENTS.md

麦麦搬运喵（`maimai-courier-plugin`）是麦麦跨群分享消息的**独立** MaiBot 插件。
源群 Maisaka 可对已选类型（默认仅合并转发）按可编辑标准自主判断，也可响应群友明确
请求，以三参数 `forward_message` 转发本群源消息到一个白名单目标，另附可选评论。
当前发送路径为**插件内客户端 → 操作者配置的单一 SnowLuma HTTP API 端点**
（`POST /{action}`，`Authorization: Bearer`），不新增服务进程、不建立长连接。
本插件自己的持久账本是唯一投递权威；旧独立服务及客户端已隔离，不是备用通道。

## 阅读入口与当前边界

- 修改行为前读主规格 `openspec/specs/cross-group-forward/spec.md` 与
  `openspec/specs/snowluma-direct-client/spec.md`（HTTP 直连变更
  `2026-10-05-direct-snowluma-http-forwarding` 已归档并同步进主规格；其设计取舍
  见归档 `openspec/changes/archive/2026-10-05-direct-snowluma-http-forwarding/design.md`）。
  旧 ws 直连变更已归档，
  其设计取舍见归档 `openspec/changes/archive/2026-10-04-direct-snowluma-websocket-forwarding/design.md`
  D1–D11（历史）；当前架构决定见 `docs/adr/0008-http-direct-client-and-forward-actions.md`
  （取代 ADR 0007），实测依据见 `docs/snowluma-http-api-catalog.md` 第 6 节；
  原卡片边界见 ADR 0004。
- 核对外部契约时读归档 `verification/01`–`07` 与 `unverified-server-contracts.md`
  （位于 `openspec/changes/archive/2026-10-04-direct-snowluma-websocket-forwarding/`）。
  U1（仅 Bearer 头认证）已在 2026-10-04 授权实测实例确认，U3–U8 仍未确认，服务端字段
  仍按显式假设处理；U2 的长连接事件广播随 ws 通道删除，不再是本路径的风险面。离线
  通过不代表线上已支持。
  SDK 2.8.0 的公开接口已核对，但实际加载版本未确认；宿主锁文件 2.5.2 不证明运行版本，
  若实际为 2.5.2 则低于插件下限，不放行。
- 术语以 `CONTEXT.md` 为准，只收领域词汇、不塞实现细节；旧 ADR/归档保留历史事实。
  操作见 `docs/direct-snowluma-operations.md`，真实 QQ 检查见 `docs/real-qq-validation.md`。
  第8组离线映射与第9组实机返修见归档 `verification/08-offline-acceptance.md` 与
  `verification/09-deployment-revision.md`；不把实现者自测说成独立验收。
- `CLAUDE.md` 必须是 `AGENTS.md` 的相对软链接，不另建规则副本。代码、注释、文档均中文。

## 独立性与安全

- 不 import、不复制、不依赖复读插件 `repeat-plugin`，不读取其他插件配置、凭证、状态或
  账本。本插件状态只在宿主授予的 `ctx.paths.data_dir`，不访问真实服务数据库。
- `_manifest.json` 是插件专属依赖唯一入口，结构 `{type: "python_package", name, version_spec}`，
  当前 `aiohttp>=3.14.3`；不把历史服务项目环境复活为插件安装依赖，不全局安装依赖。
- 能力仅 `message.get_by_id`、`chat.get_all_streams`、`maisaka.proactive.trigger`，已核实；
  `send.*`、`chat.open_session`、`api.call` 及反向监听均不声明。Hook 名称不是能力。
- 真实发送仅经 `snowluma_client.py` / `snowluma_transport.py` 的固定七动作，按源类型
  分派：卡片 `get_forward_msg` 按源消息卡片标识展开查询后 `send_group_forward_msg`
  节点重组一次投递、文字 `send_forward_msg` 单节点卡片、图片 `send_msg` 段直发、评论
  `send_msg` 普通文本；源和评论各最多一次。禁止 `ctx.send.*`、适配器 API、旧 HTTP、
  历史服务、`send_msg` 直引旧 `res_id` 或更换动作 fallback。不注册同路由第二发送网关、
  不自动重试、不用发送探测结果；请求禁跟随重定向，凭证与请求体不得离开配置端点。
- 凭证可由操作者直接写入插件配置 `snowluma.token`；非空优先使用，空白则请求前读取
  `token_env_var` 指定的环境变量。真实令牌只允许存于受控插件配置，不进仓库、账本、
  URL、日志或结果，不从模型参数或其他插件文件补齐。地址固定为单一 HTTP 端点
  （`[snowluma] http_url`，默认 3000），明文 `http` 只允许回环、私有或链路本地目标
  （主机名在请求发出前解析确认，解析出公网或失败即拒绝），公网必须 `https` 校验证书，
  拒绝用户信息、查询串、片段、未指定监听地址与任何 3xx 重定向（307/308 会重发请求体
  与认证头，实测）；HTTP 端点（3000）与 ws（3001）可能各自持 Token，按部署填写、不
  自动选端口。客户端自设请求体上限 2MiB（`ONEBOT_MAX_REQUEST_BYTES`），序列化后超限
  在发出前拒绝；服务端上限实测约 2–3MB，超限直接断连、无结构化错误。容器 localhost
  不是宿主机。
- 源正文、评论、群名、提及名及协议样式文本仅作数据，不能执行命令、改变权限或路由；
  媒体只来自可信源，拒绝本机路径、非法/私有地址，不下载调用者任意资源。真实图片
  流量前必须确认 U5 的 DNS/重定向安全前提，unknown 不能补救资源越界访问。
- 引用宿主源码前先在获准范围只读核实。原参考 `arcwsl:~/github/REFERENCE/` 不可达时，
  本变更第1组曾获准读取 `arclinux` 的特定宿主/适配器源码；此历史授权不是新的远端操作
  授权。禁止自行部署、发群、修改远端、读取凭证/运行配置/真实数据、迁移账本或 Git 写操作。

## 模块职责

- `plugin.py`：组合根持有时钟、独占账本、自己的客户端和可定位任务表。公共工厂
  `create_plugin` 可无参创建，支持测试注入；`on_load` 仅本地检查、不联网。三生命周期
  为 `on_load/on_unload/on_config_update(scope, config_data, version)`，不改成猜测名称。
  一个群聊工具、两个 Hook（`maisaka.planner.after_response` blocking 来源签发；
  `chat.receive.after_process` observe 自动候选）。最多8个在途工具请求（含预检和等待）。
- `source_binding.py`：三参数声明与校验、统一拒绝、一次性来源凭据。凭据只由宿主 Hook
  载荷签发，首次消费作废；仅清洗本插件 `forward_message`，同批其他工具原样保留且
  不获凭据。模型会话/平台/账号/授权字段不能替代宿主来源。
- `forward_precheck.py`：可信会话、源/目标白名单、异群、宿主源归属及标识保留链；本地
  预检不访问 SnowLuma、不发送。`PreparedForward.onebot_message_id` 只来自可信映射，
  不因字符串可解析就认定对应，不重写四字段账本键。目标可用性由直连查询、不依赖宿主
  目标发送会话。共用 `whitelist_entries/source_group_is_allowed/target_is_allowed`。
- `send_material.py`：宿主 `build_send_material/is_forward_card` 与 OneBot
  `build_source_material/source_outbound_segments` 的单一材料责任；两种输入分别映射。
  另持卡片节点载荷构造 `build_card_nodes`：把 `get_forward_msg` 展开结果转成
  `{"type":"node","data":{"uin","name","content"}}` 节点数组，uin 取 sender 的
  user_id、name 取 card 优先回退 nickname（压平限长）、content 段原样深拷贝；
  空名或 uin 不可确认整条拒绝，嵌套 forward 段原样保留。字段名是 `uin` 而非
  `sender`：平台只解析 `user_id`/`uin` 与 `nickname`/`name`，`sender` 被静默
  丢弃、uin 回退 bot 自身（2026-10-06 实测，见 API 目录 6.8）；文字路径
  `snowluma_client._text_card_node` 同口径用 `uin`/`name`。署名取值
  （`card_node_sender`/`card_node_name`）与卡片标识取值（`forward_card_id`）只在
  本模块单点定义，客户端直接复用。
  OneBot 源材料经 `verbatim_mentions` 分路径：文字路径 at 原样，图片路径 at 文本化。
  `is_forward_card` 为宿主卡片唯一形状判据（含未展开 dict 包装）。
  宿主图片须有可恢复二进制，不能替代 OneBot 安全重放核验；卡片仅判唯一顶层形状，
  不本地递归拒绝节点；卡片内媒体由平台按目标群上下文重建，插件不提取资源 ID。
- `snowluma_client.py`：小接口 `prepare_source/send_source/send_comment/close`。
  准备按账号→目标名册→同账号源查询→ID/群归属→`kind`（卡片/文字/图片）与材料，不发送；
  源/评论在取得执行机会后再次查账号，实际写入前同步许可。按 `kind` 分派三类发送：
  卡片取顶层唯一卡片段 `forward.data.id`→`get_forward_msg` 展开→`build_card_nodes`
  构造节点，`send_group_forward_msg` 只提交目标群与节点数组一步投递；文字节点署源
  发送者 `uin/name`（`card` 优先、回退 `nickname`，压平限长）、内容段原样含 at；图片
  沿用 `send_material` 段直发与 at 文本化。
  统一四项回执核验与未知分类；目标消息 ID 可为负、按原值逐字比对，
  `send_forward_msg`/`send_group_forward_msg` 额外返回的 `res_id`/`forward_id` 不入账。
- `snowluma_transport.py`：`OneBotConnection` 持有地址、环境变量名和可选配置令牌（repr
  隐藏）；`OneBotTransport` 固定七动作 `get_login_info/get_group_list/get_msg/
  get_forward_msg/send_msg/send_forward_msg/send_group_forward_msg` 与 `close`，没有
  任意动作代理、不建立长连接：
  惰性 `ClientSession`（`trust_env=False`、`allow_redirects=False`）按调用
  `POST /{action}`，凭证仅经 `Authorization: Bearer` 头；3xx 显式拒绝并归类
  `configuration`/`insecure_endpoint`；序列化后请求体超 2MiB 在发出前拒绝；超时→`timeout`、
  401/403→`authentication`、断连→`transport_unavailable`，`retcode` 1404/1400/100 交上层
  结构化处理。单实例锁串行、不重放动作；`close()` 幂等且受卸载 5 秒预算约束。HTTP
  请求-响应一一对应，不存在 ws 的 echo 配对、事件广播与跨连接代次归属问题。
- `delivery_ledger.py`：唯一活跃账本 `delivery_ledger.sqlite3`，先独占、查历史、验证库，
  再兼容扩展与恢复；短事务不持有网络等待。保留原 `open_ledger/reserve/get/mark_*`，
  `reserve_comment` 在评论发送前原子占位，`record_comment` 保存终态，关闭释放独占。
- `forward_delivery.py`：账本先查→只读准备→复验/源占位→最终复验/源发送→落账→非空评论
  等待0.5秒→复验/评论占位→最终复验/评论发送→落账；结果统一由 `shared/result.py`
  的 `render_forward_result` 渲染，不分散状态机，不调用旧通道。
- `shared/`：保留活跃纯类型、四字段键与状态、准备/发送请求、结果、提及、标识符、配置
  规则与时钟；各子模块单点定义，包顶层不重导出符号（0 兼容层）。生产一律显式走
  `shared.clock` / `shared.delivery` 等子模块导入，禁止复制第二套类型/状态机，
  不用 conftest 别名掩盖历史依赖，不留根模块重导出。
- `config.py/config.toml`：业务三节加 `[snowluma]`；该节仅 `http_url/token/token_env_var`，
  默认空（ADR 0008：旧 WS 端点键无兼容期）。`plugin.config_version="1.5.0"` 是宿主强制项；模型/样例默认逐项一致。
  `MessageType` 是候选配置与分类的唯一词汇，旧 `[service]` 不转换为可发送配置。
- `auto_entry.py`：只从宿主已处理入站筛选候选、有限等可读、唤起源群一次；不调用直连
  执行、不选目标、不复制聊天链。规则见下节。
- 退役模块已删除（`host_send.py` / `remote_client.py` / `service/` 原墓碑不导出代码）：
  旧服务、专属测试与环境完整留在 `history/snowluma-forward-service/`，MANIFEST 的109项
  1,633,141字节不得覆盖；65项退出原路径的证据见 ISOLATION。历史不导入、不收集，
  不是第二套产品。复活入口由 `tests/test_repo_isolation.py` 的生产扫描与隔离加载
  守卫守住；不设占位模块、不留重导出。
- `tests/`：仅活跃用例，SDK 替身不安装真 SDK。`direct_support` 是内存平台装配，
  `test_direct_acceptance` 经真实 aiohttp 回环端点测公共 Hook/Tool、账本和线路互证；
  二者不能互相冒充。参数与期待独立，测试公共接口，不通过私有实现函数证明行为。

## 来源、材料与自动判断规则

- 公开工具只有必填字符串 `message_id/target_group_id` 和可选 `comment`（默认空）。
  源群、源消息、可信账号必须对应；目标在白名单且不同源群，还须同账号群名册确认。
  缺失/非法/私聊/越权/源不可定位在发送前拒绝，不泄露越权正文。
- 只导入 SDK 根 `__all__` 名字：`HookMode/HookOrder/ErrorPolicy` 不在根导出，模式用
  `"observe"/"blocking"`。保留 `get_components()` 的顶层 `chat_scope="group"` 修正，
  SDK 装饰器只落 metadata、Runner 读顶层，删除会使私聊可见。
- 默认关闭、空白名单；直连缺配置/认证失败/账号不符/账本不可用一律不发送。
- 普通消息不支持段、缺媒体、未知提及对象整条拒绝；文字/图片顺序保留。提及按路径分流：
  普通图片（含图文混排）路径转为 `［提及：显示名称（对象标识）］` / `［提及：对象标识］` /
  `［提及：全体成员］`，普通文字路径内容段随单节点卡片原样提交、不文本化；两路都不查询
  目标成员。名称和标识仅来自源、压平控制字符、不截断、不猜造。相邻文本按原换行归并，
  转换不新增换行、输入不原地修改，不生成顶层 `at` 提醒。引用（reply）
  段只作消息装饰，随消息整条拒绝并指引改用被引用卡片自身的消息 ID，不读被引用内容。
- 卡片消息转发按源消息卡片标识展开查询→节点重组发送：取顶层唯一卡片段的
  `forward.data.id`，`get_forward_msg` 展开后经 `build_card_nodes` 结构校验构造节点
  数组，`send_group_forward_msg` 一次提交目标群与节点；平台按目标群上下文重建卡片并
  重新上传卡片内媒体。内部回复、提及、媒体与未知段随节点内容原样保留，嵌套内层
  forward 段原样提交、不递归展开，其目标群展示不保证；插件不提取、不改写、不清洗
  节点，不提交调用方资源 ID，不设置外层预览/摘要参数。禁 `send_msg` 直引旧 `res_id`
  （实测信封成功而群内展示降级空壳、四项回执无法识别）。顶层多卡片/混排、源消息不存在
  或缓存缺失、展开失败/空节点/sender 不可确认仍整条拒绝，不摘要/拆段/以源ID猜资源。
  外层预览由平台默认生成，卡片内 at 是否触发客户端提醒不作未经真实验证的保证。
- 自动入口仅白名单源群、非机器人自身、非通知的 QQ 消息；身份/发送者不明就放弃。
  卡片优先 `forward`（含未展开），无卡片时图文为 `image`、非空文字为 `text`，提及只
  附属；非法结构/不支持段放弃。分类成功不承诺可发送，未选类型不触发。
- 会话ID和源ID先过 `safe_identifier`（限128、无空白/控制字符/尖括号引号反引号），
  异常直接放弃、不净化成另一ID。意图含真实源ID、实际候选类型、合法目标与可用名称、
  标准原文和“自行判断、可以沉默”边界；不摘抄/伪造正文，不掷骰、不签目标决策票。
- 群名仅显示：压平空白、尖括号全角、限长，不作路由。标准最多4000字符，空白回退
  设计默认，只作文字、不模板展开或执行；模型不保证遵循，显式工具不受自动偏好门禁。
  候选默认 `["forward"]`，空列表不自动判断、未知枚举拒绝，普通类型须显式选中。
- 可读等待最多10秒、每0.5秒轮询；失败记定位原因后放弃。去重为内存10分钟/4096项，
  逐出最早登记，与持久账本分离。等待后和目标名称查询后用最新开关/白名单/候选/标准；
  只改标准不取消合法候选，移除类型等只取消失效等待。已提交判断不追溯、不重复唤起。
- 插件不经长连接接收任何推送，不把事件当回执或第二事件源，不伪造宿主入站；只唤起源群，
  不向目标追加上下文、不强制评论。Planner 沉默零发送，关闭自动入口不阻止合法显式工具。

## 持久事实、提交与生命周期

- 四字段键固定为账号/源群/源消息ID/目标群；评论、请求、连接或进程变化不改键。
  源/评论发送前各持久占位，同键并发只一次。`sent/sending/unknown` 阻断重发和补评论，
  只有明确未提交 `failed` 允许后续显式新尝试。成功/未知无 TTL/容量清理、无删除接口。
- 完整原库保留，确认新装才建库；有服务收据（含临时文件）、主库以外的数据库/归档
  副本、主库缺失伴随文件、链接文件、损坏/不兼容库，或命中状态类证据名字规则（导出
  临时名、归档扩展名、中文「快照/归档/备份」标记等）的条目即 `ledger_unavailable`；
  插件自身源码、文档、配置与缓存不构成证据。不自动回迁、不删证据/新建空历史。
  独占失败不恢复另一实例，源sending重启归unknown、评论sending只恢复评论unknown。
- 可靠成功要求合法匹配响应及同ID记录、目标群、账号、正整数平台群序列；目标消息 ID
  可为负值，按返回原值逐字比对、不假设符号；非空ID、写帧成功、受理、自发事件不够。
  回执只查该候选ID，最多8次/总3秒/间隔0.5秒受剩余预算
  约束，不模糊搜索或重发探测。契约外状态、提交后断线/取消/模糊失败一律保守unknown。
- 源确认但终态未落盘：保留已知真实ID，整体unknown/source_persist，不评论、不称完整成功。
  评论结果未落盘：partial/unknown/comment_persist，可保留真实评论ID，不称可靠保存。
  评论等待中撤权/取消：本次partial/comment_status=failed，账本评论字段仍空；后续
  already_forwarded不补评论，不声称所有未提交评论结果均已持久化。
- 评论仅本次参数、普通文本单独发；源成功落账后固定至少0.5秒，再复验、占位、提交。
  缺省/空/全空白不等待、不占位；源失败/未知不进入评论，不再次调用目标模型生成。
- 在所有异步准备、排队、账号查询之后、实际写入之前同步 `_send_allowed` 检查最新
  卸载/总开关/两级白名单，加端点配置与账号条件；检查与开始提交间无异步等待。
  请求超时或断连不重试、不切换动作或通道；地址/配置令牌/凭证环境变量名变化使旧未提交
  阶段失效（HTTP 无长连接，不存在跨连接代次续发）。
  尚未提交源的权限拒绝为rejected、评论为partial；已提交按可靠结果或unknown，不假装撤回。
- `on_config_update` 遵守宿主先注入配置再回调，只取消失效自动任务、不整体取消投递。
  `forward_message` 预检后复查卸载、创建投递前复验；变更标准不充当发送许可变更。
- `on_unload` 立即停止新提交，取消/等待自己的任务、结束自己的在途请求、关自己的账本，
  全部共享5秒总预算。任务表含源ID/目标/阶段，超时逐项告警，残留写入风险时保持账本
  独占、不让新实例接管，不假称全部退出；不动别的插件/客户端、不注销QQ、不撤回消息。
- 时钟统一注入：动作排队/请求发送/回执等待各10秒内，最多8个在途工具，评论0.5秒，
  回执3秒，卸载5秒；自动可读/去重上限同上。均代码常量，不改成随机、模型参数或配置；
  客户端请求体上限 2MiB（`ONEBOT_MAX_REQUEST_BYTES`）同为代码常量。
- 日志只保留安全定位，不记录凭证/原文/评论/资源/签名媒体地址；临时材料随有限工作结束
  释放，账本只存必要投递事实，不保存发送队列。
- 目标群宿主消息记录与 Maisaka 历史同步不保证；冷目标不创建/唤起 Planner，不写宿主
  数据库、不伪造入站、不追加摘要或重发补齐。真实 QQ 可见性与展示另行联验。

## 验证

```bash
uv run --no-project --with pytest pytest tests/ -q                  # 默认：596 passed, 2 skipped
uv run --no-project --with pytest --with aiohttp pytest tests/ -q   # 传输与跨进程用例：689 passed
uv run --no-project --with pyright pyright                          # 生产代码零错误（filesAnalyzed=18）
```

覆盖边界详见 [README 的离线开发验证](README.md#离线开发验证)。以上为 2026-10-06
代码与注释收敛后的实跑结果；每次修改重跑实际结果，不沿用旧数字；测试仅使用临时合成数据与
本地端点，不安装真实 SDK、SSH、读取真实状态、部署或发 QQ。根默认收集与 `tests/`
同范围，历史不收集。
`pyrightconfig.json` 明确排除历史，目标 Python 3.11；真实 SDK 未安装，因此关闭
`reportMissingImports`。实机返修证据见归档 `verification/09-deployment-revision.md`，
离线通过不能代替实际加载版本确认、独立验收或真实 QQ 联验。
