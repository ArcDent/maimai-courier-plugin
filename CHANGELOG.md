# CHANGELOG

本文件按时间倒序记录项目变更；只收用户可见的结构、契约与验证面变化。

## [未发布] - 2026-10-06（转发分组）

### Changed（BREAKING）

- `[scope]` 节由两份平铺白名单（`source_groups` / `target_groups`）改为 `groups`
  具名转发分组结构数组，每项含必填 `name`、`source_groups`、`target_groups`；两份
  平铺键直接移除，不设兼容期，**存量配置需手工改写**（OpenSpec 变更
  `2026-10-06-scoped-forward-groups`，尚未归档；决策见 `docs/adr/0010`）。
  `plugin.config_version` 1.5.0 → 1.6.0；默认空分组集合等价于旧的空白名单（没有
  源群也没有目标群），未改写的旧配置表现为「源群不在任何转发分组内」的可定位拒绝。
- 投递判定口径由「目标在目标白名单」收紧为**目标必须与来源同属一个转发分组**：
  工具预检、源与评论提交前复验、自动入口的候选筛选与目标列举共用同一个成对判定，
  不再各自复制。跨组目标（只在别的分组，或不在任何分组内）在发送前整条拒绝，措辞为
  「目标群不在与来源群相同的转发分组内」。
- 自动入口的合法目标从「全部目标白名单」收紧为「来源群所属分组的目标」；同组没有
  合法且不同于源群的目标时不提供判断机会，显式调用同样因同组判定被拒。
- `name` 只作用户标识与日志前缀（投递任务描述与 `_finish` 日志带压平后的
  `分组=<name>` 前缀），不参与任何路由、权限或判断；不限长与字符集，但去空白后非空
  且跨组唯一。
- 目标群可跨组重复（多个源群喂同一个吃瓜群），源群也可兼作别组的目标群，实际投递
  仍受「目标 ≠ 来源群自身」兜底。

### Added

- 分组判定族单点定义：`forward_group_entries`（归一视图，纯函数）、`forward_group_of`
  （来源群所属分组）、`shared_forward_group`（成对判定，命中返回分组名）与
  `source_group_is_allowed`（源群出现在任一分组）；`target_is_allowed` 随之删除。

### Changed（配置期校验）

- `config.ForwardGroupSection` 与 `ScopeSection.groups`：源群跨组重复、同一分组内源群
  重复、`name` 去空白后为空或跨组重复一律抛中文 `ValueError`，不静默归并、不放行部分
  配置（沿用 `SnowLumaSection.__init__` 先例，两处 SDK 环境一致）；空目标分组与组内
  「源 = 目标」允许保存，由运行期既有规则拒绝。

### Fixed（部署联验补齐）

- arclinux 实机部署暴露：宿主生成配置 Schema 时要求嵌套模型所有字段有默认值，
  `ForwardGroupSection.name` 无默认导致「构造插件配置 Schema 失败」（配置界面不可用，
  config.toml 手写不受影响）。`name` 改为空串默认；实际生效仍以配置期校验为准
  （空白 name 一律拒绝），并新增 `ForwardGroupSection()` 无参构造用例守住该契约。
- 同一链路暴露：分组结构校验原先只覆盖 dict 输入，宿主默认视图以实例路径落回配置
  会绕过三项校验。`ScopeSection.__init__` 现对 dict 与 `ForwardGroupSection` 实例两条
  路径都执行校验，实例路径的空白 name / 重复同样拒绝。

### 验证

- 默认档 `620 passed, 2 skipped`；aiohttp 档 `715 passed`；pyright `0 errors`
  （filesAnalyzed=18）。四字段账本键与直连协议、七动作、账本 schema 均不变。
- arclinux 实机部署（`maim-bot-core`，SDK 2.9.0 / Python 3.13.16）：55 文件运行负载
  md5 18/18 与本地一致；容器内冒烟 7 项通过（编译导入、1 Tool + 2 Hook 且
  chat_scope=group、七动作+close 公开面、配置面、判定族、宿主 Schema 无参构造、
  实例路径校验）；宿主热重载 7 插件 0 失败，Schema 失败日志消除。远端真实
  `config.toml`、账本与锁原样保留；**分组配置需操作者在远端手工改写**[scope] 后才能
  恢复发送（旧平铺键不被识别，fail-closed 不误发）。

## [未发布] - 2026-10-06

### Added

- 卡片消息转发新路径：`get_forward_msg` 按源消息卡片标识展开查询 →
  `send_material.build_card_nodes` 结构校验与发送者署名（uin 取 sender 的
  user_id、name 取 card 优先回退 nickname 并限长）→ `send_group_forward_msg`
  一次提交目标群与节点数组；平台按目标群上下文重建卡片并重新上传卡片内媒体
  （归档变更 `2026-10-05-card-forward-node-rebuild`，决策见 `docs/adr/0009`）。
- 传输层新增只读动作 `get_forward_msg` 与发送动作 `send_group_forward_msg`，固定
  七动作表（`get_login_info`/`get_group_list`/`get_msg`/`get_forward_msg` +
  `send_msg`/`send_forward_msg`/`send_group_forward_msg` + `close`），仍无任意动作代理。
- `build_card_nodes` 导出：嵌套 forward 段原样保留、不递归展开；content 段深拷贝、
  输入冻结；空名或 uin 不可确认整条拒绝。

### Changed（收敛，同日第二轮）

- 端点判定移除 ws/wss 兼容面：`shared/config_rules.py` 的 `ENDPOINT_SCHEMES` 收敛为
  `{"http", "https"}`，删除 `HTTP_ENDPOINT_SCHEMES` 与 `SnowLumaSection.__init__`
  的冗余协议预检；非 http/https 地址仍由同一函数拒绝并给出安全原因。
- 卡片标识与节点署名取值单点化：`send_material` 导出 `forward_card_id`（替代
  `snowluma_client._forward_card_id`）与 `card_node_sender` / `card_node_name`
  （替代客户端的 `_source_sender_uin` / `_source_sender_name` 与重复的
  `SENDER_NAME_MAX_LENGTH`），两条发送路径共用同一口径。
- `snowluma_client.py` 删除死导入 `Final` / `SourceKind`；`conftest.py` 与测试断言
  同步去掉已删的根 `clock` 别名引用。

### Removed

- 生产与测试代码中全部旧引用：归档变更的 `design D#` 编号、变更分组编号
  （「第 N 组」）、`附件0#`、未确认契约编号 U5、宿主源码 `文件:行号` 定位；注释与
  docstring 改为自陈述（附实测依据的引用只保留 `docs/snowluma-http-api-catalog.md`）。
- 三个源码措辞扫描型测试（断言源码不含 `websocket`/`ws_connect`/旧动作名等）与
  重复的旧键守卫用例；ws 协议拒绝、旧动作不在公开面等行为守卫保留。

### Changed

- 直连通道由正向 WebSocket 全面替换为 SnowLuma HTTP API：`POST /{action}`、
  `Authorization: Bearer` 唯一凭证通道、显式禁跟随重定向（含 307/308）、请求体
  2MiB 序列化后前置拒绝、错误分类 timeout/authentication/transport_unavailable、
  `retcode` 1404/1400/100 交上层结构化处理（归档变更
  `2026-10-05-direct-snowluma-http-forwarding`，决策见 `docs/adr/0008`）。
- 按源消息类型三类分派：卡片走展开重组、普通文字走 `send_forward_msg` 单节点卡片
  （署源发送者身份、内容段含 at 原样）、普通图片走 `send_msg` 段直发（at 文本化）；
  评论固定源成功落账 0.5 秒后以普通文本单独发送，空白评论不等待不占位。
- `[snowluma]` 配置键 `websocket_url` 更名为 `http_url`（无兼容期），
  `plugin.config_version` 1.4.0 → 1.5.0（无新增配置键）。
- 提及按路径分流：普通图片路径转 `［提及：…］` 文本、普通文字卡片路径原样提交；
  卡片内 at 是否触发客户端提醒不作保证。

### Removed

- ws 传输层：echo 配对、事件广播隔离、连接代次与接收循环整体删除（HTTP 请求-响应
  一一对应，此类风险不再存在）。
- `forward_group_single_msg`：实测对跨群含图卡片必然失败（平台重建时下载卡片内旧
  签名 URL 得 `retcode=100 "HTTP download failed: 400"`）。
- `send_msg` 直引旧卡片 res_id 通道：实测信封成功且四项回执全过，但目标群展示为
  降级空壳（回执层无法识别内容降级——假成功陷阱），契约层禁止。

### Fixed

- **卡片转发保留原发送者身份**：`build_card_nodes` 节点发送者字段由 `sender` 改为
  `uin`。平台 OneBot 层自定义节点分支只解析 `user_id`/`uin` 与
  `nickname`/`name`，`sender` 仅被元数据标量校验放行、解析时静默丢弃，uin 回退
  登录账号自身——旧实现投递的整张卡片在目标群显示为 bot 本人发送。2026-10-06 对
  运行实例对照实测：`sender` 标量 → bot；`uin`/`user_id` → 原发送者（回查节点
  sender 逐字段证实），真实内容（video+text 两节点）随 `uin` 投递完整（依据见
  `docs/snowluma-http-api-catalog.md` 6.8）。
- 普通图片消息经 `send_msg` 段直发时发送者必然是 bot（QQ 协议层面无法署源身份）；
  实测改为单节点卡片（`uin`/`name` + 源段原样，走 `send_forward_msg`）可保留原
  发送者且图片段被平台重新上传接受，但普通消息展示形态由「消息」变「单节点
  聊天记录」属产品语义变更，未在本次修正范围内（catalog 6.8）。
- 目标消息 ID 可为负值：按返回原值逐字核验，不假设符号。
- 卡片发送动作额外返回的 `res_id`/`forward_id` 不入账、不进工具结果、不作成功证据。
- 运维排障提示补充：卡片类失败先看平台 `wording`（如 `HTTP download failed: 400`），
  替代「只读固定措辞、无法定位」的旧状。

### 验证

- 默认档 `596 passed, 2 skipped`；aiohttp 档 `689 passed`；pyright `0 errors`
  （filesAnalyzed=18）。（用例数低于本文件早前记录的 600/695：工作区既有四个测试
  文件删除与收敛轮删除源码措辞扫描用例、合并重复守卫所致，与功能面无关。）
- 全部 6 个 openspec 变更已归档，活跃变更区为空；两份主规格
  （`cross-group-forward` / `snowluma-direct-client`）已同步为新口径，
  `openspec validate --specs` 2/2 通过。
- 部署：arclinux `arcdent.maimai-courier-plugin` 已同步新运行件，真实 `config.toml`
  与 `delivery_ledger.sqlite3` 原样保留；容器内冒烟通过（import / `create_plugin` /
  三组件 / 七动作 / 旧动作已移除）。

## [未发布] - 2026-10-05

### Removed

- 删除 4 个残余模块：根 `clock.py`（`shared/clock.py` 的重导出）与三个退役墓碑
  `host_send.py`、`remote_client.py`、`service/__init__.py`；历史证据留在
  `history/snowluma-forward-service/`，复活入口由 `tests/test_repo_isolation.py` 守卫。
- 删除 `shared/send_steps.py` 的 3 个生产零引用符号：`SourceSendStep`、
  `CommentSendStep`、`normalize_send_status`（服务端网关时代的发送步骤协议与状态收窄）。
- `shared/__init__.py` 改为纯包说明：删除 53 个符号的重导出与 `__all__`，包顶层不再
  聚合任何名字。
- `delivery_ledger.py` 删除 `SendStatus` 重导出与兼容注释；`auto_entry.py` 删除 2 个
  全仓无引用的导入（`MAX_IDENTIFIER_LENGTH`、`IDENTIFIER_FORBIDDEN_CHARACTERS`）。
- 删除 129 个复述性测试 docstring；按规则类别收敛测试参数化矩阵（BINARY 大小写/位置、
  列比较与混合比较库注入、事务阶段×公开操作、端点合法/非法/不可信、WAL 校验形态、
  结构不兼容与状态类证据名），每条规则保留代表用例。

### Changed

- 时钟导入统一为 `shared.clock` 单一路径（原 4 个生产模块走根 `clock.py`、2 个走
  `shared.clock` 的双路径收敛为一条）。
- 运行原则确立为 0 兼容层：不留重导出、双路径或只为兼容存在的别名；测试的身份断言
  改为子模块单路径，并新增「包顶层不聚合符号」「账本不再重导出」反向守卫。
- 修正 23 处描述已删机制的失实注释（`[service]` 节、ws 信封、服务端/网关组件、自认
  兼容面）；`shared/config_rules.py` 模块 docstring 与 `shared/__init__.py` 缩减。
- `conftest.py` 生产模块名单去掉根 `clock` 别名；`pyrightconfig.json` include 收缩为
  18 项；`AGENTS.md` 模块职责、墓碑状态与验证数字同步。

### 验证

- 默认档 `550 passed, 2 skipped`；aiohttp 档 `642 passed`；pyright `0 errors`
  （filesAnalyzed=18）。
- 默认档较结构收敛前（768）少 218 个用例：其中 8 个随 `normalize_send_status` 删除，
  其余 210 个来自参数化矩阵按规则类别收敛；每条拒绝规则、事务阶段、排序规则与 WAL
  校验形态均保留代表用例，无覆盖规则整体消失；129 个复述性测试 docstring 一并删除。
- 测试驱动链路新增共享 helper `wake_once`（`tests/direct_support.py`）：把
  Fixture 装配、源消息装载与观察 Hook 驱动收敛为一次调用。
