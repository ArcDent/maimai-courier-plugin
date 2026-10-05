# HANDOFF

本文件记录当前会话的交接信息；静态结论以 `AGENTS.md` 为准，术语以 `CONTEXT.md` 为准。

## 最近操作（2026-10-05 ~ 10-06）

0. **部署到 arclinux 实机（2026-10-06 01:32，用户授权）**：把当前未提交工作区
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

1. **代码与注释收敛（2026-10-06，未提交）**：删除 ws 兼容残留与全部旧引用。
   `shared/config_rules.py` 的 `ENDPOINT_SCHEMES` 收敛为 `{"http","https"}`，删除
   `HTTP_ENDPOINT_SCHEMES` 与 `SnowLumaSection.__init__` 的冗余协议预检；生产与
   测试注释清除归档变更的 `design D#`、分组编号、「附件0#」、U5 与宿主源码
   `文件:行号` 定位，改为自陈述；卡片标识与署名取值单点化（`forward_card_id` /
   `card_node_sender` / `card_node_name` 由 `send_material` 单点定义），客户端删除
   `_forward_card_id`、`_source_sender_uin`、`_source_sender_name`、重复的
   `SENDER_NAME_MAX_LENGTH` 与死导入 `Final`/`SourceKind`；删除 3 个源码措辞扫描
   用例、合并重复旧键守卫；`build_source_material` 文字路径双重解析保留为待办。
   验证：默认 596+2 skip / aiohttp 689 / pyright 0（18 文件）。

1. **卡片节点发送者字段修订（2026-10-06，未提交）**：实测证明平台 OneBot 层
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
- 真实发送只经 `snowluma_client.py` / `snowluma_transport.py` 的固定七动作 HTTP
  直连（`get_forward_msg` + `send_group_forward_msg` 为卡片新增对）；
  旧服务与环境完整隔离在 `history/snowluma-forward-service/`（MANIFEST 109 项
  不得覆盖），复活入口由 `tests/test_repo_isolation.py` 守卫。
- openspec 活跃变更区为空；全部 6 个变更已归档，两份主规格已同步为新口径。

## 进行中

- 两个变更与两轮修订（uin 字段、收敛）的全部改动在工作区未提交（用户自行 commit）。
- 收敛债剩余 1 条 judgement call：`snowluma_client._prepare` 文字路径对同一源消息
  调用两次 `build_source_material`（先默认口径判 kind，再 `verbatim_mentions=True`
  取 at 原样段）；已清偿：client 死导入、卡片判据重叠、sender helper 与
  `SENDER_NAME_MAX_LENGTH` 双定义、design 引用锚点。

## 下一步

1. 用户检查后提交；提交前重跑三条约验证命令（596+2 skip / 689 / 18）。
2. 真实 QQ 联验未授权、未执行（见 `docs/real-qq-validation.md`）：U3–U8、内层
   卡片展示、卡片内 at 客户端提醒、图片 URL 时效、body 精确阈值。
3. arclinux 部署件尚未同步 uin 修订（部署件上卡片署名仍是 bot 自身）。
4. 测试令牌曾出现在对话记录中，建议轮换。

## 关键发现

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
