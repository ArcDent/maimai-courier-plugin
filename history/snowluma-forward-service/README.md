# 麦麦搬运喵（maimai-courier-plugin）

麦麦在群聊间分享消息的**独立** MaiBot 插件：源群出现候选类型消息（默认**合并转发**，
可在配置中加选普通文字与图片）后，由源群 Maisaka 结合可编辑的自动分享标准自主决定
是否分享；也可以响应群友请求，通过 `forward_message` 工具把**本群**指定**源消息**
转发到白名单**目标群**，可选附带一条延迟 0.5 秒的评论。

从本版本起，**全部源消息（普通文字、图片、合并转发卡片）与评论的发送都迁移到同一
仓库的独立 SnowLuma 转发服务**（`service/`）执行：插件侧客户端只做本地权限预检、
阶段授权与结果汇报，不再调用宿主 `send.*` 发送能力，也不存在旧发送通道回退。插件仍
可独立安装；服务与共享代码部署在同一仓库、由使用者自行启动。

三个必须知道的行为要点：

- 普通文字/图片中的**个人与全体成员提及仍转成保留对象信息的普通文本**，不向目标群
  传播 @ 提醒；**原卡片内部的提及与回复随整张卡片原样保留、不再逐段改写**；
- 目标群 QQ 客户端能看到消息，但**不保证目标群宿主消息记录与 Maisaka 历史同步**；
- 新投递的防重事实只落在**服务端账本**（服务端状态目录）；插件 `data_dir` 只保留旧
  账本归档与一份不含凭证的本地迁移收据。

- 术语定义（源群、目标群、原卡片转发、服务端账本等）见 [CONTEXT.md](CONTEXT.md)；
- 行为契约见主规格 `openspec/specs/cross-group-forward/spec.md`；本变更的规格增量
  （跨群契约修订与服务端规格）在获准同步/归档前暂存于
  `openspec/changes/move-forwarding-to-snowluma-service/specs/`；
- 设计取舍见 [docs/adr/0004](docs/adr/0004-preserve-original-forward-card.md)（原卡片
  保留内部内容）与 [docs/adr/0005](docs/adr/0005-separate-snowluma-forward-service.md)
  （独立服务与客户端、不保证目标群历史）。

## 组件、版本与依赖

| 项目 | 值 | 说明 |
| --- | --- | --- |
| 插件版本 | `0.1.0` | `_manifest.json` 的 `version`（manifest v2） |
| 宿主 MaiBot | `>= 1.2.5` | 下限取部署机自报版本；其宿主代码与参照 Host `a995cc4` 的能力名/Hook 行号逐一对齐 |
| MaiBot 插件 SDK | `>= 2.8.0` | 生命周期与工具/Hook 契约在 SDK `4d8e577`（2.8.0）核实 |
| 插件专属依赖 | `aiohttp>=3.14.3` | 声明在 `_manifest.json` 的 `dependencies`，由宿主依赖流水线装载 |
| 服务端运行环境 | Python `>= 3.11`、`aiohttp>=3.14.3` | 仓库根 `pyproject.toml`；`uv sync` 或等价方式安装，服务进程不依赖 `maibot_sdk` |

插件声明的能力（`_manifest.json` 的 `capabilities`，逐项在宿主
`src/plugin_runtime/capabilities/registry.py` 核实）：

| 能力 | 用途 |
| --- | --- |
| `message.get_by_id` | 按 ID 取源消息（本地预检的来源归属与可读性） |
| `chat.get_all_streams` | 解析来源会话（取账号、平台与群号） |
| `maisaka.proactive.trigger` | 给源群一次自主判断机会（自动入口只唤起，不发送消息） |

发送能力（`send.text` / `send.image` / `send.hybrid` / `send.forward`）与
`chat.open_session` 已从声明中移除：真实发送由服务端通过自己的 OneBot 连接执行。
Hook 名称不是能力，不进该清单。

## 安装与启用

1. 把本插件目录整体放进 MaiBot 的 `plugins/`（目录名任意，插件 ID 取自
   `_manifest.json` 的 `arcdent.maimai-courier-plugin`）。
2. 按 [服务运维清单](docs/service-operations.md) 初始化并启动转发服务（显式选择
   `fresh` 新装或 `migrate` 迁移旧账本，再配置 OneBot 地址与凭证环境变量）。
3. 配置插件（见下）并设置客户端凭证环境变量；重启 MaiBot 或热加载插件。
4. 本插件不 import、不复制、不依赖其他插件，可单独安装与卸载。

首次安装默认**总开关关闭**、**自动入口开启**（只给源群 Maisaka 一次判断机会，
不直接发送）、**自动候选仅合并转发**、**白名单为空**；服务侧未完成「账本身份 +
迁移确认 + 凭证」绑定时，工具调用一律拒绝，不会回退旧发送通道。

## 配置

插件配置四个节（`config.toml` 与 `config.py` 默认值逐项一致）：

| 配置项 | 默认值 | 含义 |
| --- | --- | --- |
| `plugin.enabled` | `false` | 总开关。关闭时不自动判断、也不执行工具转发 |
| `plugin.config_version` | `"1.2.0"` | 宿主强制项，新增字段时提升 |
| `auto.enabled` | `true` | 自动入口开关：只控制自动判断机会，不禁用工具转发 |
| `auto.message_types` | `["forward"]` | 自动候选类型：`"text"` / `"image"` / `"forward"`；空列表 = 停止自动判断机会，未知取值拒绝 |
| `auto.share_criteria` | 见下 | 传给源群 Maisaka 的自动分享标准（最多 4000 字符，空/全空白回退默认） |
| `scope.source_groups` | `[]` | 源群白名单 |
| `scope.target_groups` | `[]` | 目标群白名单 |
| `service.endpoint` | `"http://127.0.0.1:8787"` | 转发服务地址；本机回环可用 `http`，跨主机必须 `https`（或受控加密隧道） |
| `service.client_identity` | `"maibot-plugin"` | 向服务声明的认证主体名（服务端据此绑定源/目标许可上限） |
| `service.token_env_var` | `"COURIER_SERVICE_TOKEN"` | 保存服务凭证的**环境变量名**；配置里不填凭证值 |
| `service.expected_ledger_id` | `""` | 预期服务端账本身份；留空或与实际不符时工具调用一律拒绝 |
| `service.migration_confirmation` | `""` | 本次数据集（新装或迁移）的确认标识；留空禁止发送 |

`auto.share_criteria` 默认原文为「结合消息内容与目标群，自行判断是否值得分享；
不值得或无法判断时保持沉默，不必回复，不要编造消息内容。」。它只是判断依据：模型
仍可能不分享或不完全遵循，也不改变白名单、来源校验与持久防重，更不会成为群友
**明确请求**工具转发时的内容偏好门禁。开启普通文字或图片候选会增加模型调用量。

服务端默认监听端口是 `8790`（`service/config.py` 的 `listen_port`），与客户端默认
`service.endpoint` 的端口 `8787` 不同：部署时以服务启动日志「HTTP 服务端已就绪并
监听 …」报告的实际地址为准，把客户端 `endpoint` 指向它（或显式把服务
`listen_port` 配成客户端使用的端口）。

配置示例（展示的是**配置完成后**的形态，**不要直接复制启用**；凭证值只放在环境变量
里，配置里只出现变量名）：

```toml
[plugin]
enabled = true
config_version = "1.2.0"

[auto]
enabled = true
message_types = ["forward"]
share_criteria = "结合消息内容与目标群，自行判断是否值得分享；不值得或无法判断时保持沉默，不必回复，不要编造消息内容。"

[scope]
source_groups = ["111111111"]
target_groups = ["222222222"]

[service]
endpoint = "http://127.0.0.1:8787"
client_identity = "maibot-plugin"
token_env_var = "COURIER_SERVICE_TOKEN"
expected_ledger_id = "由服务端 fresh/migrate 输出，逐字填入"
migration_confirmation = "本次数据集确认标识，自定但需非空且保持不变"
```

推荐顺序：先按运维清单初始化服务（拿到账本身份）→ 填好服务配置与白名单 → 再打开
总开关。评论延迟（0.5 秒）、阶段授权有效期、客户端请求超时都是代码常量，不出现在
配置里，也不接受模型参数覆盖。

## 工具 `forward_message`

群聊 Maisaka 可用的工具，公开参数只有三个字符串：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `message_id` | 是 | 本群源消息的消息 ID（字符串，避免长 ID 精度损失） |
| `target_group_id` | 是 | 目标群号，须在目标群白名单内且不同于源群 |
| `comment` | 否（默认 `""`） | 可选评论，按普通文本另发一条消息；空/全空白表示不附带评论 |

调用参数示例（由模型生成，只有这三个参数，没有会话、平台或授权字段）：

```json
{
  "message_id": "1234567890",
  "target_group_id": "222222222",
  "comment": "这条挺有意思，分享给大家"
}
```

调用所在的源群、平台与机器人账号都取自宿主真实会话（由宿主 Hook 签发的一次性
凭据绑定），模型无法用参数伪造；服务端还会用自己绑定的账号重新查询源消息并核对
群归属，客户端提交不了源正文、资源 ID 或媒体地址。

## 支持与拒绝的内容

支持（可完整取得时原样发送；全部由服务端执行）：

- 纯文字：多个文本段按原换行拼成普通文本消息；
- 单张图片、多张图片、文字与图片混排（保持段序与段内容）；
- 合并转发卡片：以源消息中**整张原卡片**的资源引用发送，卡片内部回复、提及、
  媒体等内容随资源原样保留，**不逐节点清洗、不重建节点**；接受的源消息形态是
  「该卡片单独存在」；
- 普通消息中的个人与全体成员提及：这是「保持原消息形态」的**唯一例外**——为不向
  目标群传播 @ 提醒，提及转成保留对象信息的普通文本：有显示名称时为
  `［提及：显示名称（对象标识）］`，没有可用名称时为 `［提及：对象标识］`，全体
  成员为 `［提及：全体成员］`；转换保留顺序、不新增换行、不生成 `at` 段。

明确拒绝（整条拒绝，不丢段、不降级、不以描述冒充媒体）：

- 普通消息中的语音、文件、视频、回复、表情等其它段，以及未知段类型；
- 普通消息中对象不可确认的提及（不会猜成全体成员，也不会丢段后声称完整转发）；
- 普通消息中图片缺少可恢复媒体材料；
- 合并转发卡片与其它顶层内容混排（例如卡片 + 文字）、没有资源引用的卡片；
- 空内容消息、缺失或无法定位的源消息。

原卡片内部即使包含插件不支持的类型也不构成拒绝理由（它们属于原资源的一部分）；
卡片内部提及保留为原样，平台如何呈现其提醒行为**尚未验证、不作保证**。

## 发送路径与结果

顺序固定：本地权限与内容预检 → 服务端准备（只校验、查源、登记，不发送）→ 读最新
配置复验 → 源阶段授权 → 服务端持久占位并发送源消息 → 落账（真实目标消息 ID）→
评论非空时等待**固定 0.5 秒** → 复验并提交评论阶段授权 → 发送评论 → 保存评论结果。

工具结果键：`success` / `status` / `sent` / `delivered` / `already_forwarded` /
`target_message_id` / `comment_status` / `comment_message_id` / `source_message_id` /
`target_group_id` / `stage` / `reason`（预检通过后另有 `send_kind` /
`material_shape`）。

| `status` | 含义 | 需要人工核对 |
| --- | --- | --- |
| `sent` | 完整成功（未要求评论，或评论也成功） | 否 |
| `partial` | 源消息已投递并保存，评论明确未发送或结果待核实 | 评论结果待核实时需要 |
| `already_forwarded` | 同键此前已成功投递，本次没有新发送 | 否 |
| `sending` | 同键投递正在发送中（并发调用只会发送一次） | 否 |
| `rejected` | 预检、总开关或服务绑定拒绝，本次未发送任何消息 | 否 |
| `failed` | 源消息明确未发送，可后续显式重试 | 否 |
| `unknown` | 结果待核实（超时、回执丢失、状态未落盘、重启遗留） | **是：核实前不会重发** |
| `ledger_unavailable` | 服务端账本不可用，已关闭发送，本次未发送任何消息 | 是（检查服务状态目录与文件权限） |

`success=true` 只表示**完整成功**；`sent` / `delivered=true` 表示源消息已确认投递
（含「已发送但状态未落盘」），不等于评论也成功。`unknown` 记录会持续阻止该键重发，
插件与服务都不提供删除接口；人工核对目标群实际消息后按运维判断处理。

## 防重、迁移与备份

- 新投递的防重事实只落在**服务端账本**（服务端状态目录中的
  `service_ledger.sqlite3`）；唯一键是「账号 + 源群 + 源消息 ID + 目标群」，评论、
  请求 ID、客户端或进程变化都不参与键；
- 只有「明确未发送」的失败才登记 `failed` 并允许后续显式重试；超时、取消、回执
  丢失与进程中断遗留一律待核实，**不自动重试、不盲目重发**；成功记录不按时间或
  容量自动清除；
- **旧插件账本不自动导入**：旧的 `delivery_ledger.sqlite3` 保留在原 `data_dir`
  （不读、不改、不删）。启用新路径前必须用 `ledger migrate` 显式迁移，否则旧的
  成功/待核实记录会失去防重作用，可能重复转发一次；
- 插件 `data_dir` 只保留旧账本归档与本地迁移收据 `service_binding.json`（只含账本
  身份与迁移确认标识，不含凭证）；
- 误连另一个数据集（预期账本身份不符、缺少确认标识、收据不一致）时一律关闭发送，
  不按地址自动信任新数据集；
- 客户端与服务端各自按单实例运行；一个数据目录不要被多个实例共享。服务端状态目录
  的备份、迁移与回滚步骤见 [服务运维清单](docs/service-operations.md)。

## 目标群 Maisaka 历史不保证

本工具只保证：QQ 目标群收到消息，源群 Maisaka 获得真实投递结果。目标群宿主的
消息记录与 Maisaka 上下文**不属于保证范围**：目标群没有活动麦麦时不会为它创建或
唤起 Planner，也不写宿主数据库、不伪造入站消息、不追加摘要。**“目标群麦麦没看到
这条分享”不是本工具的失败**，不要据此重发或改用其它发送路径。

## 服务端命令行

```bash
python -m service start --config <服务配置路径>
python -m service ledger fresh --state-dir <状态目录> [--confirm-ledger-id <ID>]
python -m service ledger migrate --state-dir <状态目录> --snapshot <旧账本快照路径> [--confirm-ledger-id <ID>]
python -m service ledger export --state-dir <状态目录> --output <导出路径> [--confirm-ledger-id <ID>]
python -m service ledger status --state-dir <状态目录>
```

命令的完整参数、退出码约定、凭据环境变量准备、备份/恢复、迁移与回滚演练步骤见
[docs/service-operations.md](docs/service-operations.md)。

## 真实 QQ 联验

真实部署与发群联验**需要单独授权，尚未执行**；输入、观察点与判定标准见
[docs/real-qq-validation.md](docs/real-qq-validation.md)。在完成该清单前，本文档中
的任何“支持/拒绝”表述都只代表自动化与合成测试的结论，不代表线上投递已验证。

## 开发与验证

```bash
uv run --no-project --with pytest pytest tests/ -q            # 默认：单元、故障与授权边界（525 passed, 4 skipped）
uv run --no-project --with pytest --with aiohttp pytest tests/ -q   # 额外覆盖真实 HTTP/WebSocket 传输与跨进程用例（536 passed）
uv run --no-project --with pyright pyright                    # 生产代码类型检查（0 errors）
```

默认命令不会安装网络库：需要 `aiohttp` 的传输与跨进程模块在导入期跳过（4 skipped），
**只有带 `--with aiohttp` 的命令才覆盖传输与跨进程场景**。CI/发布前应两条都跑。
