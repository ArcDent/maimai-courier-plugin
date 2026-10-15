<div align="center">

# 麦麦搬运喵（maimai-courier-plugin）

麦麦在群聊间分享消息的独立 MaiBot 插件：源群出现候选消息后由 Maisaka 自主决定分享或沉默，群友也可通过 `forward_message` 显式请求转发，可附一条 0.5 秒延迟的评论。

![License](https://img.shields.io/badge/License-MIT-yellow.svg)
![Version](https://img.shields.io/badge/Version-0.1.0-blue.svg)

</div>

## Contents

- [Background](#background)
- [Install](#install)
- [Usage](#usage)
  - [Basic](#basic)
  - [Advanced](#advanced)
- [Configuration](#configuration)
- [FAQ](#faq)
- [Maintainers](#maintainers)
- [Contributing](#contributing)
- [License](#license)

## Background

本插件承担麦麦的跨群消息分享能力：源群出现已选候选类型（默认仅合并转发）的消息后，源群 Maisaka 依据可编辑标准自主决定分享或沉默；群友也可明确请求，把本群指定源消息转发到同属一个转发分组的目标群。

当前路径为**插件进程内客户端 → 操作者配置的单一 SnowLuma HTTP API 端点 → QQ**：源消息、评论和平台查询都经本插件自己的端点按请求完成，不建立长连接、不启动独立转发服务、不借用适配器 API、不调用宿主 `send.*`（架构决定见 ADR 0008，转发分组见 ADR 0010）。

本插件不 import、不复制、不依赖复读插件，不读取其他插件的配置、凭证或投递记录。行为以主规格「跨群转发」与「直连客户端」（openspec/specs/）为准，领域术语见 CONTEXT.md。

> **当前状态**：仅通过离线合成验收与一次授权实测快照（ADR 0008），真实 QQ 联验未授权、未执行。以下支持范围是实现契约，不是线上效果保证。实际启用前须满足真实联验清单（docs/real-qq-validation.md）。
>
> docs/、openspec/ 与根目录其余 Markdown 为仓库本地私有文档（含运维与本机信息），未随公开仓库分发。

## Install

前提：MaiBot `1.2.5–1.99.99`、maibot-plugin-sdk `2.8.0+`（线上实际加载版本部署前另行确认）；插件专属依赖仅 `_manifest.json` 声明的 `aiohttp>=3.14.3`，由宿主依赖流水线装载，无需额外常驻进程。

目录名即插件 id（`maimai-courier-plugin`），整个放进 MaiBot 的 `plugins/` 目录；`config.toml` 排除，避免覆盖服务器上的配置（含令牌）：

```bash
rsync -av --delete \
  --exclude .git --exclude .local --exclude __pycache__ \
  --exclude tests --exclude docs --exclude openspec --exclude history \
  --exclude conftest.py --exclude config.toml \
  ./ <宿主>:<宿主插件目录>/maimai-courier-plugin/
```

部署后总开关默认关闭、不联网；启用前先核对真实联验清单（docs/real-qq-validation.md）与直连运维清单（docs/direct-snowluma-operations.md）。旧服务与历史归档见 history/snowluma-forward-service/MANIFEST.md，仅供追溯，不加入 Python 路径。

## Usage

### Basic

群聊工具 `forward_message` 把本群源消息转发到**同属一个转发分组**的目标群：

```json
{"message_id": "1234567890", "target_group_id": "222222222", "comment": "这条挺有意思"}
```

公开参数：`message_id`（必填，本群源消息 ID）、`comment`（可选，默认空，普通文本）。源群、平台和账号来自宿主 Planner Hook 签发的一次性来源凭据，模型不能用额外字段伪造。

自动入口观察宿主 `chat.receive.after_process`，给转发分组内的源群一次判断机会：只列同组目标群，合并转发候选经两步只读查询预识别嵌套卡片，命中不提供判断机会。

### Advanced

<details>
<summary>Click to expand: 私聊目标、类型支持与整条拒绝、八种 status</summary>

**私聊目标**：来源群所属分组开启 `private` 后，可用 `target_user_id`（二选一目标参数，与 `target_group_id` 同缺或同在都拒绝）把消息转发给操作者账号的 QQ 好友，发送前经好友名册核验对端为同账号好友：

```json
{"message_id": "1234567890", "target_user_id": "3000100001", "comment": "这条挺有意思"}
```

群聊工具 `list_forward_friends`（无公开参数）列出操作者账号的 QQ 好友名册，供模型在提交私聊目标前选取对端；名册数据不进日志、账本与投递结果。

**支持与整条拒绝**：支持普通文字、单图、多图、图文，以及唯一顶层合并转发卡片（单节点卡片文字按源消息发送者署名原样提交）。展开结果含嵌套内层卡片（卡片套卡片）时发送前整条拒绝，固定措辞「源卡片包含嵌套聊天记录，无法完整转发，不建议继续尝试」（ADR 0011），显式工具调用同样被拒。普通图片路径的提及转为文本形态（不产生顶层 at 提醒）；夹带文件、视频、语音、回复段，图片缺媒体或含不安全地址，引用（reply）段消息都整条拒绝。不丢段、拆段、生成摘要。

**八种工具 `status`**：

| status | 含义 |
|:-------|:-----|
| `sent` | 源成功且已落账，评论未要求或也成功 |
| `partial` | 源已成功落账，评论未完成、明确未发或待核实 |
| `already_forwarded` | 已有源成功事实，本次零发送，也不补评论 |
| `sending` | 同键源在途，本次不重复发送 |
| `rejected` | 本次源未提交，来源、内容、权限、连接准备等拒绝 |
| `failed` | 源可证明未发送，只有后续显式新调用可再尝试 |
| `unknown` | 证据不足或终态未落账，保持防重、人工核对 |
| `ledger_unavailable` | 源发送前账本不可用，关闭发送并人工核对 |

`success=true` 对应 `sent` 或 `already_forwarded`。顺序：可信本地预检 → 查账本 → 直连只读准备（账号核对、名册核验）→ 复验并持久源占位 → 最后复验 → 按类型一次发送 → 回执核实并落账 → 非空评论等 0.5 秒后一次提交 → 保存结果。唯一投递权威是插件目录内的 `delivery_ledger.sqlite3`，五字段键防重发。

</details>

## Configuration

安全默认如下，与 `config.py` / `config.toml` 一致：

```toml
[plugin]
enabled = false
config_version = "1.7.0"

[auto]
enabled = true
message_types = ["forward"]
share_criteria = "结合消息内容与目标群，自行判断是否值得分享；不值得或无法判断时保持沉默，不必回复，不要编造消息内容。"

[scope]
groups = []

# [[scope.groups]]
# name = "研发群"
# source_groups = ["111111"]
# target_groups = ["222222", "333333"]
# private = false

[snowluma]
http_url = ""
token = ""
token_env_var = ""
```

要点：

- **总开关默认关闭**：关闭时工具和观察 Hook 都不发出任何平台请求，加载本身不联网。自动入口默认开启、仅选合并转发。
- **具名转发分组**：一次转发只要求「目标群与来源群属于同一个分组」，跨分组目标在发送前整条拒绝；同一源群只能属于一个分组（配置期直接拒绝重复），目标群可跨组重复；`name` 仅作日志标识，必填且跨组唯一；`private` 是分组级私聊开关（默认关闭）。默认空分组等价于旧的空白名单，不触发任何判断。
- **凭证**：`snowluma.token` 直接填令牌，或 `token_env_var` 填环境变量名（`token` 留空时读取）。凭证只随 `Authorization: Bearer` 头发送，不进 URL、日志、账本或工具结果。
- **端点安全**：`http_url` 必须是操作者确认的完整 HTTP API 端点；明文 `http` 只允许回环、私有或链路本地目标，公网必须使用可验证证书的 `https`；请求禁止跟随重定向；请求体上限 2MiB。
- `auto.message_types` 只接受 `text` / `image` / `forward`，空列表停止所有自动机会；分享标准最多 4000 字符。

配置模型全量字段与边界见 [config.py](config.py)；默认总开关关闭的原因见 FAQ。

## FAQ

**Q: 为什么总开关默认关闭？**
A: 本轮仅通过离线合成验收与一次授权实测快照，服务端契约未完全确认。先保持关闭，确认运行版本、端点与环境注入、账本完整性及转发分组，获得真实测试授权后再启用（见 docs/real-qq-validation.md）。

**Q: 卡片套卡片（嵌套卡片）能转发吗？**
A: 不能。实测该形态在目标群客户端只能显示为降级空壳，发送前整条拒绝并提示改用被引用卡片自身的消息 ID，自动入口对这类候选也不提供判断机会（ADR 0011）。

**Q: 明文 http 端点可以用吗？**
A: 仅当目标解析为回环、私有或链路本地地址时允许；公网目标必须使用可验证证书的 `https`。不接受监听地址、URL 用户信息、查询串、片段或其他协议，请求禁止跟随重定向。

## Maintainers

[@ArcDent](https://github.com/ArcDent)

## Contributing

PRs welcome. Small PRs first. 行为契约与规则真源见 AGENTS.md（软链 CLAUDE.md，本地私有文档）。

```bash
uv run --no-project --with pytest pytest tests/ -q                  # 默认：530 passed, 2 skipped
uv run --no-project --with pytest --with aiohttp pytest tests/ -q   # 传输与跨进程用例：649 passed
uv run --no-project --with pyright pyright                          # 生产代码零错误
```

## License

MIT © ArcDent
