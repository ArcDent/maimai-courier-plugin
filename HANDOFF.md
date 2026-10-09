# HANDOFF

本文件记录当前会话的交接信息；静态结论以 `AGENTS.md` 为准，术语以 `CONTEXT.md` 为准。

## 最近操作（2026-10-05 ~ 10-09）

6. **部署双形态目标 + 收敛版本到 arclinux（2026-10-09 09:20，用户授权）**：
   负载 49 项（11 根模块 + shared/ + _manifest.json + 文档；**不含** config.toml、
   账本、锁、tests、openspec、history、CLAUDE.md 软链），tar+scp 到
   `/home/arc/maimai/MaiBot/data/MaiMBot/plugins/arcdent.maimai-courier-plugin/`。
   部署前整目录备份 `~/maimai/backups/courier-pre-deploy-20261009-0920.tar.gz`
   （315K，含 67 条记录的四字段账本与全部 config_back 历史）。19 个生产文件 md5
   与本地逐一一致；容器内 compileall 通过；宿主 09:21:09–10 热重载新代码
   （pyc 重编译、已加载=7 失败=0）；09:21:24 自动入口在真实群 1104104631 成功
   唤起（「搬运回来」分组源群），证明分组判定与预搜索在真实配置下工作。
   **账本迁移尚未触发**：远端库仍为四字段 schema、67 条记录（10-08 22:55 最后
   写入），首次真实发送时才做五字段一次性迁移（既有行按 group 回填）；若迁移
   失败按 `ledger_unavailable` 关闭发送且原文件保留，备份可回退。远端
   `config.toml` 为三分组结构（test / 正式转发 / 搬运回来）、**均无 `private`
   键**：群转发保持可用，私聊默认关闭。容器日志中的 `gemini-3.7-flash-high
   unknown provider` 是宿主 LLM provider 配置问题，与插件无关。

4. **代码与测试收敛（2026-10-09，未提交）**：只做减法，不动行为契约。测试
   从 788 项收敛到 532 项（14664→11739 行，−20%）：按「只留核心逻辑」删除重复
   覆盖、极端注入故障、历史兼容与参数化变体（账本 −36 例、自动入口 −28 例、
   生命周期 −9 例、编排 −5 例、客户端 −11 例、传输 −4 例、验收 −5 例、组合根
   −4 例、配置 −6 例、预检 −1 例、材料 −1 例、共享类型 −3 例）；保留用例代码
   一字未改。删除 conftest.py 的两套顶层别名（生产模块与 `shared.*` 的
   `sys.modules` 登记，68→32 行）：全部测试 import 统一为 `maimai_courier_plugin.*`
   包路径，少一条「两处 import 指向不同模块对象」的分叉。生产注释收敛
   （6088→5799 行）：删除复述代码的长篇设计背景，保留安全契约、单点定义声明、
   ADR 编号引用与实测依据（如 `uin` 非 `sender`、6.10 私聊路由）。验证：默认
   530 passed+2 skipped / aiohttp 649 passed / pyright 0 errors。

5. **双形态目标与分组私聊开关变更完成（2026-10-09，未提交、未归档）**：
   `private-forward-target`（openspec 活跃变更，tasks 50 条全完，`--strict`
   校验 valid，待 sync/archive）。转发分组新增 `private` 开关（默认 false）；
   工具目标参数改二选一（`target_group_id`/`target_user_id`）；新增只读名册
   工具 `list_forward_friends`（`get_friend_list` 成为固定第八动作）；账本键
   四字段扩五字段（目标形态进键，ADR 0012，旧库一次性迁移）；回执与提交前
   复验按目标形态分治；结果面新增 `target_kind` 与目标标识。实测依据
   `docs/snowluma-http-api-catalog.md` 6.10（私聊路由、回执四项、名册裸数组）。
   验证：默认 786+2 skip / aiohttp 941 / pyright 0（收敛后复跑 530/649/0）。

0. **转发分组变更四阶段完成并归档（2026-10-06，未提交）**：`[scope]` 从平铺
   `source_groups`/`target_groups` 改为具名 `groups` 结构数组（`name`+源群+目标群），
   投递判定收紧为「目标与来源同组」。决策记录 `docs/adr/0010-scoped-forward-groups.md`，
   术语入 CONTEXT.md「转发分组」。实现：`ForwardGroupSection`+`ScopeSection.__init__`
   三项配置期校验（源群跨组/组内重复、name 空白/跨组重复，中文 ValueError）；
   `forward_precheck` 判定族 `group_id_entries`/`forward_group_entries`/
   `forward_group_of`/`shared_forward_group`（成对判定单点，`target_is_allowed` 已删），
   预检、`plugin._send_allowed` 源/评论复验、`auto_entry` 列举全部收敛同源；投递任务
   与失败日志带压平 `分组=<name>` 前缀。测试 620+2 skip / aiohttp 715 / pyright 0
   （18 文件）。四阶段走 spec-driven-workflow（grill 5 问→propose/specs/design/tasks
   →4 组 subagent-run 双轴验收→verify→sync+archive `2026-10-06-scoped-forward-groups`），
   主 spec 同步 1 ADDED+6 MODIFIED（11 处措辞，Purpose 分组口径，共 12 需求）。
   收敛（`whitelist_entries`→`group_id_entries`、6 处 docstring 精简）与部署随本条
   完成。**arclinux 实机部署已完成**（见下）：55 文件运行负载（无符号链接、排除
   `config.toml`/账本/锁）经 tar+scp 到
   `data/MaiMBot/plugins/arcdent.maimai-courier-plugin/`，备份
   `~/maimai/backups/courier-pre-deploy-20261006-1103.tar.gz`，代码/shared 18 文件
   md5 与本地一致；容器内冒烟 7 项通过（见关键发现）；宿主两次热重载均 7 插件 0 失败。
   部署联验顺带修复两个问题：`ForwardGroupSection.name` 给空串默认（宿主要求嵌套
   模型字段全有默认，否则配置界面 Schema 生成失败）、`ScopeSection.__init__` 覆盖
   dict 与实例两条输入路径的校验。**远端 `config.toml` 仍是旧平铺配置（未读取、
   未改写）：新代码按空分组 fail-closed，需操作者在远端手工把 `[scope]` 改写为
   `[[scope.groups]]` 后才能恢复发送。**

1. **部署到 arclinux 实机（2026-10-06 01:32，用户授权）**：把当前未提交工作区
   （uin 修订＋收敛）打成运行负载，tar+scp 到
   `/home/arc/maimai/MaiBot/data/MaiMBot/plugins/arcdent.maimai-courier-plugin/`
   （arclinux rsync 3.4.1 与 mac 新版 exclude 协议不兼容，仍走 tar+scp）。负载只含
   10 个根模块＋`shared/` 8 子模块＋`_manifest.json`＋README/CONTEXT/AGENTS/
   CHANGELOG/HANDOFF/docs；**不含** `config.toml`、账本与锁文件——远端真实
   `config.toml`（10-05 17:59）与 `delivery_ledger.sqlite3`（10-06 01:17）原样保留、
   未读取内容。部署前备份 `~/maimai/backups/courier-pre-deploy-20261006-0132.tar.gz`；
   远端退役件 `clock.py`/`host_send.py`/`remote_client.py`/`service/` 与 macOS
   `._*` 附属文件已清除。15 个代码/shared 文件 md5 与本地逐一一致；容器内真实 SDK
   冒烟通过（compileall、三组件、群聊 chat_scope、七动作＋close、旧动作不在公开面、
   `[snowluma]` 三键）。宿主文件监视自动热重载（无需重启容器）。
   **部署事故与修复**：负载里的 `CLAUDE.md` 是符号链接，被账本历史证据规则判为
   「链接文件」，导致发送前整体 `ledger_unavailable`（01:35:57 一次真实转发被拒，
   未发送、账本未动，属契约内 fail-closed）。已删除该符号链接并 `touch plugin.py`
   触发重载（01:38:58，pid=5743，7 插件 0 失败），副本验证开账本恢复。**教训：部署
   负载不得含符号链接等非常规文件；`CLAUDE.md` 软链只属开发机，不入运行负载。**
   实机联验由用户执行中；Git 仍未提交。

2. **代码与注释收敛（2026-10-06，未提交）**：删除 ws 兼容残留与全部旧引用。
   `shared/config_rules.py` 的 `ENDPOINT_SCHEMES` 收敛为 `{"http","https"}`，删除
   `HTTP_ENDPOINT_SCHEMES` 与 `SnowLumaSection.__init__` 的冗余协议预检；生产与
   测试注释清除归档变更的 `design D#`、分组编号、「附件0#」、U5 与宿主源码
   `文件:行号` 定位，改为自陈述；卡片标识与署名取值单点化（`forward_card_id` /
   `card_node_sender` / `card_node_name` 由 `send_material` 单点定义），客户端删除
   `_forward_card_id`、`_source_sender_uin`、`_source_sender_name`、重复的
   `SENDER_NAME_MAX_LENGTH` 与死导入 `Final`/`SourceKind`；删除 3 个源码措辞扫描
   用例、合并重复旧键守卫；`build_source_material` 文字路径双重解析保留为待办。
   验证：默认 596+2 skip / aiohttp 689 / pyright 0（18 文件）。

3. **卡片节点发送者字段修订（2026-10-06，未提交）**：实测证明平台 OneBot 层
   `parseForwardNodes` 只解析 `user_id`/`uin` 与 `nickname`/`name`，卡片路径原
   `build_card_nodes` 使用的 `sender` 字段仅被标量校验放行、解析时静默丢弃，uin
   回退 bot 自身——部署态投递的整张卡片在目标群显示 bot 署名。已改为 `uin`（与
   文字路径 `_text_card_node` 口径一致），源码根因、字段矩阵与真实内容复验记录在
   `docs/snowluma-http-api-catalog.md` 6.8。普通图片 `send_msg` 段直发无法署源身份
   （协议层面即 bot），改单节点卡片可保留但属产品语义变更，未本次处理。
   验证：默认 597+2 skip / aiohttp 690 / pyright 0（低于旧记录数字系工作区既有
   四个测试文件删除所致）。**arclinux 部署件尚未同步本次修订。**

两个 openspec 变更完成、归档、同步进主规格并部署，全部未提交：

1. **HTTP 直连变更**（archive `2026-10-05-direct-snowluma-http-forwarding`）：
   ws 传输层整体替换为 SnowLuma HTTP API——固定七动作、Bearer-only、显式禁 3xx
   重定向、请求体 2MiB 前置拒绝、错误分类与 `retcode` 分层；三类分派（卡片 /
   文字单节点卡片 / 图片段直发）＋评论普通文本 0.5 秒；配置键 `websocket_url`→
   `http_url`、`config_version` 1.5.0。走四阶段流程（grill→propose→spec-to-todo→
   subagent-run 7 组双轴）实施。
2. **卡片展开重组变更**（archive `2026-10-05-card-forward-node-rebuild`，决策另立
   `docs/adr/0009`）：动机是真实部署排障——跨群含图卡片在原路径全部 400。四条
   通道实测后唯一可用通道为 `get_forward_msg` 展开 + `send_group_forward_msg`
   节点重组；`send_msg` 直引旧 res_id 的假成功陷阱写入契约禁止；嵌套一层展开
   原样提交（Q2 实测平台接受）。
3. **部署**：arclinux `arcdent.maimai-courier-plugin` 同步新运行件（tar+scp；
   arclinux rsync 3.4.1 与 mac 新版 exclude 协议不兼容已绕开）；真实
   `config.toml` 与 `delivery_ledger.sqlite3` 原样保留；容器内冒烟通过。备份在
   `~/maimai/backups/`（含 `…-pre-card-rebuild.tar.gz`）。

## 项目静态结构

- 生产代码 18 个模块：根 10 个（`plugin` / `config` / `source_binding` /
  `send_material` / `forward_precheck` / `forward_delivery` / `delivery_ledger` /
  `auto_entry` / `snowluma_client` / `snowluma_transport`）＋ `shared/` 8 个子
  模块；pyright include 18 项。`CLAUDE.md` 是指向 `AGENTS.md` 的相对软链接。
- 测试经 `conftest.py` 注册合成包 `maimai_courier_plugin` 后一律走包路径导入；
  conftest 不再登记任何顶层别名（0 兼容层）。默认档 530+2 skip（aiohttp 档 649）。
- 两个群聊工具（`forward_message` 二选一目标 + `list_forward_friends` 只读名册）
  与两个 Hook；`[scope]` 为具名转发分组（新增 `private` 私聊开关），配置期校验走
  `ScopeSection.__init__`（dict 与实例两条路径都覆盖，勿改回 validator）。
- 真实发送只经 `snowluma_client.py` / `snowluma_transport.py` 的固定八动作 HTTP
  直连（`get_friend_list` 为私聊名册新增对）；账本五字段键（ADR 0012）。
  旧服务与环境完整隔离在 `history/snowluma-forward-service/`（MANIFEST 109 项
  不得覆盖），复活入口由 `tests/test_repo_isolation.py` 守卫。
- openspec 活跃变更区有 1 个：`private-forward-target`（tasks 全完、校验 valid，
  未 sync/archive）；其余 7 个变更已归档，两份主规格已同步（cross-group-forward
  12 需求 + snowluma-direct-client 私聊/名册需求待随该变更归档同步）。

## 进行中

- 双形态目标变更 + 本轮收敛 + 更早的分组/嵌套修订，全部改动在工作区未提交
  （用户自行 commit）。
- openspec 活跃变更 `private-forward-target`：tasks 50 条全完、`openspec validate
  --strict` 通过，尚未 sync 主规格、未 archive；归档前主规格仍是旧口径。
- arclinux 部署件已同步到本次收敛后的 10-09 版本（09:20 部署，见「最近操作」
  第 6 条）：八动作、名册工具、五字段账本键均已就位；**账本四字段→五字段迁移
  尚未触发**（首次真实发送时进行，备份在
  `~/maimai/backups/courier-pre-deploy-20261009-0920.tar.gz`）；远端三分组均无
  `private` 键，私聊默认关闭、群转发可用。

## 下一步

1. 用户检查后提交；提交前重跑三条验证命令（当前实跑：默认 530 passed+2 skipped /
   aiohttp 649 passed / pyright 0 errors）。
2. `private-forward-target` 走 sync-specs + archive（主规格同步双形态目标与
   私聊/名册需求）。
3. 远端观察首次真实发送的五字段迁移结果：成功则 67 条既有记录按 group 回填、
   防重语义保持；若 `ledger_unavailable` 且原因指向迁移，用上述备份回退并人工
   核对。如需私聊功能，由操作者在远端 `config.toml` 的目标分组里显式加
   `private = true`（默认关闭是安全形态，不主动代改）。
4. 部署 arclinux 的固定红线：tar+scp（rsync 协议不兼容）、**负载不得含符号链接**
   （`CLAUDE.md` 软链入负载会触发 `ledger_unavailable`，见 01:32 部署教训）、不带
   `config.toml`/账本/锁、部署前备份 `~/maimai/backups/`。
5. 真实 QQ 联验未授权、未执行（见 `docs/real-qq-validation.md`）：U3-U8、内层
   卡片展示、卡片内 at 客户端提醒、图片 URL 时效、body 精确阈值；新增私聊路径
   联验条目 P1/P2（对端展示目视确认）。
6. `.local/sync-specs.py` 是上一轮归档用的一次性合并脚本（含旧 Purpose 文案），
   已完成使命，待用户处置；`.local/` 为仓库外私有区。
7. 测试令牌曾出现在对话记录中，建议轮换。

## 关键发现

- arclinux 实机容器（`maim-bot-core`）实测：maibot_sdk **2.9.0**、Python 3.13.16；
  宿主插件运行时把 `plugins/` 与 `data/plugins/` 挂为同一目录（device:inode 相同），
  部署到 `data/MaiMBot/plugins/` 即运行时路径；`temp/plugins/` 是空壳。
- 宿主配置 Schema 生成器对嵌套配置模型要求**所有字段有默认值**（无参构造取默认
  视图）；且宿主可能以 dict 或已构造实例两条路径落配置——配置期校验必须同时覆盖
  两条路径，否则默认视图（空 name）会绕过校验。
- 卡片通道四路实测分化：`forward_group_single_msg` 跨群含图必 400；`send_msg`
  直引旧 res_id 是假成功（信封 ok＋四项回执过、群内空壳）；`upload_forward_msg`
  单独一步缺图片目标上下文；`send_group_forward_msg` 节点重传唯一内容完整。
- `get_forward_msg` 展开只取一层；平台接受嵌套 forward 段原样提交（Q2 实测）。
- 服务端请求体实际上限约 2–3MB：3MB 起直接断连、无结构化错误码。
- aiohttp 3.14.3 对 307/308 默认重发 POST＋Authorization＋请求体（本地实测，
  令牌与正文双重外泄）→ 显式 `allow_redirects=False` 是硬约束。
- `send_group_forward_msg` 返回 `message_id`＋`res_id`＋`forward_id`；平台目标
  消息 ID 可为负值。
- HTTP 与 ws 端点（3000/3001）可能各自持有 Token，部署时按端填写。
