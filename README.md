# 麦麦搬运喵（maimai-courier-plugin）

麦麦在群聊间分享消息的**独立** MaiBot 插件：源群出现**合并转发**后，由源群 Maisaka
自主决定是否分享；也可以响应群友请求，通过 `forward_message` 工具把**本群**指定
**源消息**转发到白名单**目标群**，可选附带一条延迟 0.5 秒的评论。

- 术语定义（源群、目标群、合并转发、源消息、目标消息、已投递记录）见 [CONTEXT.md](CONTEXT.md)；
- 行为契约见主规格 `openspec/specs/cross-group-forward/spec.md`；
- 设计取舍（为什么独立、为什么评论延迟固定 0.5 秒、为什么只落一份账本）见
  `openspec/changes/archive/2026-09-29-introduce-maimai-courier/design.md` 与
  [docs/adr/0001](docs/adr/0001-standalone-source-decided-forward.md)。

## 版本与能力声明

| 项目 | 值 | 说明 |
| --- | --- | --- |
| 插件版本 | `0.1.0` | `_manifest.json` 的 `version`（manifest v2） |
| 宿主 MaiBot | `>= 1.2.5` | 下限取线上部署机自报 1.2.5；其宿主代码与参照 Host `a995cc4` 的能力名/Hook 行号逐一对齐，能力名与 Hook 已只读核实 |
| MaiBot 插件 SDK | `>= 2.8.0` | 生命周期与发送回执形态在 SDK `4d8e577`（2.8.0）核实 |
| 额外依赖 | 无 | 只用 Python 标准库与 SDK |

声明的能力（`_manifest.json` 的 `capabilities`，逐项在宿主
`src/plugin_runtime/capabilities/registry.py` 核实）：

| 能力 | 用途 |
| --- | --- |
| `message.get_by_id` | 按 ID 取源消息原始材料、自动转发的就绪确认 |
| `chat.get_all_streams` | 解析来源会话（取账号与作用域） |
| `chat.open_session` | 解析或创建目标群发送路由 |
| `send.text` | 纯文字源消息、评论 |
| `send.image` | 单张图片 |
| `send.hybrid` | 多张图片、图文混排 |
| `send.forward` | 合并转发卡片 |
| `maisaka.proactive.trigger` | 给源群一次自主判断机会（自动转发只唤起，不发送消息） |

Hook 名称不是能力，不进该清单。

## 安装（独立安装）

1. 把本目录整体放进 MaiBot 的 `plugins/`（目录名任意，插件 ID 取自 `_manifest.json`
   的 `arcdent.maimai-courier-plugin`）。
2. 重启 MaiBot 或热加载插件。
3. 本插件不 import、不复制、不依赖其他插件，可单独安装与卸载。

首次安装默认**总开关关闭**、**自动转发开启**（自动入口开关 `auto.enabled` 默认
开启）、**白名单为空**：在填好白名单并确认之前，既不会自动转发，也不会执行工具
转发。

## 配置

配置只有三个节（`config.toml` 与 `config.py` 默认值逐项一致，WebUI 可改）：

| 配置项 | 默认值 | 含义 |
| --- | --- | --- |
| `plugin.enabled` | `false` | 总开关。关闭时不自动转发、也不执行工具转发 |
| `auto.enabled` | `true` | 自动入口开关：源群收到合并转发后是否给源群 Maisaka 一次判断机会（只影响自动转发，不禁用工具转发） |
| `scope.source_groups` | `[]` | 源群白名单：允许麦麦从中转发消息的群号 |
| `scope.target_groups` | `[]` | 目标群白名单：允许接收转发消息的群号 |

推荐顺序：先填好白名单，再打开总开关；部署切换时建议先只填**测试群**白名单，联验
通过后再补齐其余群（见 [交接清单](docs/handover-cross-group-forward-switch.md)）。评论
延迟（0.5 秒）、自动去重窗口与等待上限都是代码常量，不出现在配置里，也不接受模型
参数覆盖。

下列示例展示的是**配置完成后**的状态（总开关已开、两组白名单已填），只用于核对
字段形态；**不要直接复制启用**：首次安装请保持 `plugin.enabled = false`，先配好
白名单再开启。

```toml
[plugin]
enabled = true
config_version = "1.0.0"

[auto]
enabled = true

[scope]
source_groups = ["111111111"]
target_groups = ["222222222"]
```

## 工具 `forward_message`

群聊 Maisaka 可用的工具，公开参数只有三个字符串：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `message_id` | 是 | 本群源消息的消息 ID（字符串，避免长 ID 精度损失） |
| `target_group_id` | 是 | 目标群号，须在目标群白名单内且不同于源群 |
| `comment` | 否（默认 `""`） | 可选评论，按普通文本发送；空/全空白表示不附带评论 |

调用参数示例（由模型生成，只有这三个参数，没有会话、平台或授权字段）：

```json
{
  "message_id": "1234567890",
  "target_group_id": "222222222",
  "comment": "这条挺有意思，分享给大家"
}
```

调用所在的源群、平台与机器人账号都取自宿主真实会话（由宿主 Hook 签发的一次性
凭据绑定），模型无法用参数伪造；伪造这些字段不会改变实际查询与发送目标。

## 支持与拒绝的内容

支持（材料可完整恢复时原样发送）：

- 纯文字：多个文本段按换行拼成一条普通文本消息（`send.text`）；
- 单张图片（`send.image`）与多张图片（`send.hybrid`）；
- 文字与图片混排（`send.hybrid`，段序与段内容原样）；
- 合并转发卡片（`send.forward`，节点作者与段内容原样；嵌套不超过 3 层）。

> 说明：真实入站转发的节点级原文能否完整恢复，已在 8.5 真实实例联验中由用户确认
> 通过（本代理未独立复核卡片级发送）；证据与强度说明见
> `openspec/changes/archive/2026-09-29-introduce-maimai-courier/tasks.md` 任务 1.2 与 8.5。

明确拒绝（整条拒绝，不丢段、不降级、不以描述冒充媒体）：

- 语音、文件、视频等其它段类型，以及未知段类型；
- 图片缺少可恢复二进制（只剩描述文本，`binary_data_base64` 为空）；
- 未展开的合并转发（宿主未恢复出节点材料）；
- 空内容节点（宿主会替换成占位文本 `[empty forward node]`，原文不可恢复）；
- 没有节点的卡片、合并转发与其它段混排、嵌套超过 3 层、没有内容的消息。

## 转发过程与结果

顺序固定：权限与内容预检 → 账本登记「发送中」→ 源发送前按**最新配置**复验 →
发送源消息 → 立即保存真实目标消息 ID → 评论非空时等待**固定 0.5 秒** → 再次复验 →
发送评论 → 保存评论结果。

工具结果键：`success` / `status` / `sent` / `delivered` / `already_forwarded` /
`target_message_id` / `comment_status` / `comment_message_id` / `source_message_id` /
`target_group_id` / `stage` / `reason`（预检通过后另有 `send_kind` / `material_shape`）。

| `status` | 含义 | 需要人工核对 |
| --- | --- | --- |
| `sent` | 完整成功（未要求评论，或评论也成功） | 否 |
| `partial` | 源消息已投递并保存，评论明确未发送或结果待核实 | 评论结果待核实时需要 |
| `already_forwarded` | 同键此前已成功投递，本次没有新发送 | 否 |
| `sending` | 同键投递正在发送中（并发调用只会发送一次） | 否 |
| `rejected` | 预检或总开关拒绝，本次未发送任何消息 | 否 |
| `failed` | 源消息明确未发送，可后续显式重试 | 否 |
| `unknown` | 结果待核实（超时、回执丢失、状态未落盘、重启遗留） | **是：核实前不会重发** |
| `ledger_unavailable` | 投递账本不可用，已关闭发送，本次未发送任何消息 | 是（检查数据目录与文件权限） |

`success=true` 只表示**完整成功**；`sent` / `delivered=true` 表示源消息已确认投递，
不等于评论也成功。`unknown` 记录会持续阻止该键重发、插件也不提供修改或删除账本
记录的接口；人工核对目标群实际消息后，按运维判断处理（例如保留现状或停用插件）。

## 账本与备份

投递状态只落在宿主授予的数据目录（`ctx.paths.data_dir`，通常为
`data/plugins/arcdent.maimai-courier-plugin/`）里的 SQLite 账本
`delivery_ledger.sqlite3`：

- 唯一键是「账号 + 源群 + 源消息 ID + 目标群」，评论不参与键：同键成功记录跨重启
  阻止重复投递；同一源消息发给不同目标群互不影响；
- 只有「明确未发送」的失败才登记 `failed` 并允许后续显式重试；超时、取消、回执
  丢失与进程中断遗留一律登记待核实，**不自动重试、不盲目重发**；
- 成功记录不按时间或容量自动清除，账本没有删除接口。

备份：停止插件后复制 `delivery_ledger.sqlite3`；SQLite 可能在同一目录生成同前缀的
伴随文件 `delivery_ledger.sqlite3-wal` / `delivery_ledger.sqlite3-shm`，如存在也一并
复制（插件自身未启用 WAL，正常情况下不会出现）。不要手工删改记录：删掉成功记录会
失去防重保护。

账本损坏或不可读时，插件会关闭发送并报出明确原因，不会覆盖、删除或当空库继续；
此时先备份现场再排查。插件按**单实例**（单进程）运行：一个数据目录不要被多个
MaiBot 实例共享，否则启动恢复会把另一个进程的「发送中」记录一并归并为待核实。

## 开发与验证

```bash
uv run --no-project --with pytest pytest tests/ -q      # 单元测试（含故障与并发边界）
uv run --no-project --with pyright pyright              # 生产代码类型检查
```

真实群联验（真实发送、评论与自然上下文、重启防重）**已获用户授权，待部署环境就绪
后执行**；执行清单见
[docs/handover-cross-group-forward-switch.md](docs/handover-cross-group-forward-switch.md)。
