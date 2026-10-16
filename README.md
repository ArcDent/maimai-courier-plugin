<div align="center">

# 麦麦搬运喵（maimai-courier-plugin）

麦麦在群聊间分享消息的独立 MaiBot 插件：源群出现候选消息后由 Maisaka 自己决定要不要分享，群友也可以用 `forward_message` 直接点名转发，并附一条 0.5 秒延迟的评论。

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

- **定位**：把麦麦的消息在群之间转出去。源群出现候选消息时，Maisaka 自己判断要不要分享（默认只看合并转发），拿不准就不吭声；群友也可以点名某条消息，让插件把它转到同一个转发分组里的其他群。
- **链路**：发消息的路很直接——插件自己发 HTTP 请求到你配置的那个 SnowLuma 地址，由它转给 QQ。不开长连接、不跑后台转发服务、不走 MaiBot 适配器的 API，也不用宿主的 `send.*`（ADR 0008 / 0010）。
- **边界**：跟复读插件没有任何代码关系，也不读它们的配置和凭证；具体行为契约在 openspec/specs/ 的主规格里。
- **状态**：目前只跑过测试和一次授权实测，真实 QQ 环境还没验证过。下文的「支持范围」是代码里的契约，不代表线上实测效果；想开总开关，先过 docs/real-qq-validation.md 的清单（docs/、openspec/ 是仓库本地私有文档，不随公开仓库分发）。

## Install

前提：MaiBot 在 `1.2.5–1.99.99` 版本区间，maibot-plugin-sdk `2.8.0+`；插件自己只多要一个 `aiohttp>=3.14.3`，宿主的依赖流水线会装上。

把整个文件夹放进 MaiBot 的 `plugins/` 下（文件夹名就是插件 id），部署时排除 `config.toml`，免得把服务器上带令牌的配置覆盖掉：

```bash
rsync -av --delete \
  --exclude .git --exclude .local --exclude __pycache__ \
  --exclude tests --exclude docs --exclude openspec --exclude history \
  --exclude conftest.py --exclude config.toml \
  ./ <宿主>:<宿主插件目录>/maimai-courier-plugin/
```

部署完总开关默认是关的，关着就不联网。启用前先过两份清单：真实联验清单和直连运维清单（都在 docs/）。旧服务的归档在 history/snowluma-forward-service/MANIFEST.md，只是留档，不会进 Python 路径。

## Usage

### Basic

群聊工具 `forward_message` 把本群一条消息转发到同一个转发分组的目标群：

```json
{"message_id": "1234567890", "target_group_id": "222222222", "comment": "这条挺有意思"}
```

参数就两个：`message_id` 必填，`comment` 可选的纯文本。走哪个平台、用哪个账号发，由 MaiBot 的 Planner Hook 用一次性凭据决定，模型塞别的字段想伪造也伪造不了。

自动入口挂在宿主的 `chat.receive.after_process` 上，给分组内的源群一次自己拿主意的机会：候选里只会列同组的目标群；合并转发候选先做两步只读查询，认出里面嵌着内层卡片就跳过，不给判断机会。

### Advanced

<details>
<summary>Click to expand: 私聊目标、类型支持与整条拒绝、八种 status</summary>

- **私聊目标**：分组开了 `private` 之后可以改用 `target_user_id`（和 `target_group_id` 二选一，同时填或都不填都不行），消息会转给你自己账号的 QQ 好友，发送前会先核对对方确实在你的好友名单里；`list_forward_friends` 没有参数，就是让模型把好友名单拉出来挑，名单不会写进日志、账本和发送结果。
- **类型**：支持文字、单图、多图、图文，还有顶层的合并转发卡片；单节点的卡片文字按原发送者的署名原样提交。卡片里还套着卡片的整条拒发，固定提示「源卡片包含嵌套聊天记录，无法完整转发，不建议继续尝试」（ADR 0011）；带文件、视频、语音、回复段的，图片缺附件或地址不安全的，以及引用（reply）消息，一律整条拒发。不拆条、不补摘要；普通图片里的 @ 只会变成文本，不会真的去提醒人。
- **发送完返回八种 `status`**：

| status | 含义 |
|:-------|:-----|
| `sent` | 发出去了，账也记了；评论没要求或也发成功了 |
| `partial` | 消息发出去了，评论没发成、被明确跳过或不确定 |
| `already_forwarded` | 这条之前已经发过，这次什么都不再发 |
| `sending` | 同一条消息正在路上，这次就不重复发了 |
| `rejected` | 这次没发出去：来源、内容、权限或连接检查没过 |
| `failed` | 能证明没发出去；想重试就再显式调用一次 |
| `unknown` | 情况说不清，先当没发处理，人工核对 |
| `ledger_unavailable` | 发之前账本就不可用，发送整个关掉，人工核对 |

`success=true` 只有 `sent` 和 `already_forwarded` 两种。整个流程是：预检 → 查账本 → 只读准备 → 复验并占位 → 发送 → 核对回执记账 → 0.5 秒后发评论 → 存结果。防重复发送靠 `delivery_ledger.sqlite3` 的五字段键，它是唯一的发送依据。

</details>

## Configuration

默认值都是收紧过的，跟 `config.py` / `config.toml` 一致：

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

- **总开关**：`plugin.enabled` 默认 `false`，关着的时候工具和自动入口都不发任何网络请求，加载本身也不联网；自动入口默认开，但只看合并转发。
- **分组**：一条消息想转发，目标群和来源群必须在同一个分组里，跨分组直接拒。一个源群只能挂在一个分组上，目标群倒是可以在多个分组里重复出现；`name` 必填、全局唯一，只是日志里的标签；`private` 是整个分组的私聊开关。分组留空等于旧的空白名单，不会有任何动作。
- **凭证**：令牌可以直接填 `token`，更推荐 `token_env_var` 填环境变量名（`token` 留空时才去读）。令牌只出现在 Bearer 请求头里，不落 URL、日志、账本和工具结果。
- **端点**：`http_url` 填你确认过的完整端点。明文 `http` 只允许指向回环、内网或链路本地地址，公网必须用证书有效的 `https`；请求不跟随重定向，包体上限 2MiB。
- **自动入口**：`auto.message_types` 只认 `text` / `image` / `forward`，清空就不会再给自动判断的机会；`share_criteria` 最长 4000 字。

所有字段和边界看 [config.py](config.py)。

## FAQ

**Q: 为什么总开关默认关闭？**
A: 这个版本只跑过本地测试和一次授权实测，真实 QQ 环境还没验证过，先别开。等你确认了运行版本和端点配置、账本没问题，并且拿到真实联验授权，再打开（清单见 docs/real-qq-validation.md）。

**Q: 卡片套卡片（嵌套卡片）能转发吗？**
A: 不能。实测发到目标群只剩一个空壳卡片，所以发送前直接拒绝，并提示改用被引卡片自己的消息 ID；自动分享也会跳过这类候选（ADR 0011）。

**Q: 明文 http 端点可以用吗？**
A: 目标是回环、内网或链路本地地址时可以；公网必须用证书有效的 https。带账号信息或查询参数的 URL 不接受，请求也不跟随重定向。

## Maintainers

[@ArcDent](https://github.com/ArcDent)

## Contributing

欢迎 PR，小的优先。行为契约和规则在 AGENTS.md（本地私有文档）。

```bash
uv run --no-project --with pytest pytest tests/ -q                  # 默认：530 passed, 2 skipped
uv run --no-project --with pytest --with aiohttp pytest tests/ -q   # 传输与跨进程用例：649 passed
uv run --no-project --with pyright pyright                          # 生产代码零错误
```

## License

MIT © ArcDent
