# 历史资料：独立转发服务运维清单

> **已被插件直连方案取代，不是当前部署指引。** 当前路径是插件内客户端直接连接已有
> SnowLuma 正向 WebSocket 服务端，见 [直连运维清单](direct-snowluma-operations.md)。
> 下文完整保留旧独立服务阶段的说明与演练记录，其命令不适用于活跃插件，不应执行。
> [隔离历史中的完整原件](../history/snowluma-forward-service/docs/service-operations.md)
> 与 [MANIFEST](../history/snowluma-forward-service/MANIFEST.md) 可复核原内容；本抬头未覆盖原件。
> 历史离线演练不是部署或真实 QQ 联验，旧联验未执行状态不变。

## 原文：转发服务运维清单：启动、凭证、备份、迁移与回滚

> 适用范围：本仓库的**独立 SnowLuma 转发服务**（`service/`）与插件侧客户端
> （`remote_client.py`）。本文只描述本项目自己的服务与账本；**不读取其他插件的
> 状态，不自动部署，不删除任何数据**（任何删除都需要单独、明确的确认）。
> 命令在仓库根执行；`uv sync` 后 `uv run python -m service …` 与部署环境里
> `python -m service …` 等价（依赖按 `pyproject.toml` 安装，Python >= 3.11）。
> 注意 `uv run --no-project` 不会安装 `aiohttp`：账本类命令仍可用，但 `start` 会在
> 连接网关时失败（退出码 4，提示无法查询网关登录账号）。
> 本文不记录任何真实群号、路径或凭证值。

## 0. 前置与边界

- 服务按**单实例**运行：一个状态目录同一时间只允许一个服务进程打开；不要把它放在
  多个实例共享的目录里。
- 服务端与客户端各自管理自己的状态：服务端状态目录归服务进程；插件状态仍只在
  `ctx.paths.data_dir`（旧账本归档与本地迁移收据）。
- `start` 的启动顺序固定为：配置 → HTTP 凭证 → 账本独占打开与数据集绑定校验 →
  启动恢复 → OneBot 连接与账号核对 → HTTP 服务端就绪。任一步失败都会明确报错并以
  非零退出码结束，且**不建立发送任务**；监听就绪只表示接口可用，不代表已有发送。
- 退出码约定：`0` 成功；`2` 命令行用法错误；`3` 配置缺失/不可读/校验不过；
  `4` 尚未可用（账本未初始化、传输未接通等）；`5` 账本故障（损坏、schema 不符、
  身份不符、被独占或恢复失败）。

## 1. 凭证环境变量准备

凭证值**只**放在环境变量里，配置文件中只写变量名，不写凭证；凭证不会进入账本、
日志或导出文件。

| 变量（默认名） | 用途 | 读取方 |
| --- | --- | --- |
| `MAIMAI_COURIER_SERVICE_TOKEN` | HTTP v1 接口的 Bearer 凭证 | 服务端（配置 `token_env_var`） |
| `COURIER_SERVICE_TOKEN` | 同一份 HTTP 凭证的客户端侧变量名 | 插件客户端（配置 `service.token_env_var`） |
| `MAIMAI_COURIER_ONEBOT_TOKEN` | 连接 SnowLuma OneBot 的凭证 | 服务端（配置 `onebot_token_env_var`） |

要点：服务端与客户端各自的**变量名**由各自配置指定、可以不同，但指向的凭证**取值
必须一致**；服务端的 OneBot 凭证只配置在服务端环境。启动前先确认这三个变量在对应
进程环境里已设置；缺失时 `start` 会在建立任何网络连接前拒绝（退出码 3）。

## 2. 首次初始化：显式选择 fresh 或 migrate

命令面（以下 `--help` 输出为公开契约，不要凭记忆改写）：

```bash
python -m service ledger fresh --state-dir <状态目录> [--confirm-ledger-id <ID>]
python -m service ledger migrate --state-dir <状态目录> --snapshot <旧账本快照> [--confirm-ledger-id <ID>]
python -m service ledger export --state-dir <状态目录> --output <导出路径> [--confirm-ledger-id <ID>]
python -m service ledger status --state-dir <状态目录>
python -m service start --config <服务配置路径>
```

- **新装**：`ledger fresh --state-dir <DIR>` 生成稳定账本身份（32 位十六进制），
  把该身份逐字填入服务配置 `ledger_id` 与客户端 `expected_ledger_id`；客户端再填写
  非空的 `migration_confirmation`（自定标识，用于识别本次数据集）。
- **从旧版本迁移**：先停旧发送路径 → 为旧账本制作一致性快照 → `ledger migrate`
  （见第 3 节）。
- 已有状态的目录**不会**被 `fresh` 隐式覆盖：身份已存在时需 `--confirm-ledger-id`
  一致才幂等成功；有记录但无身份、或检测到旧的 `delivery_ledger.sqlite3`，都会拒绝
  并要求显式迁移。
- `ledger status` 是只读入口（不取锁、不建表），打印账本身份、schema 版本、各状态
  计数与导入摘要；未初始化的目录返回退出码 4，损坏/身份不符返回 5。

服务配置示例（字段名与 `service/config.py` 的模型字段一致；凭证只写变量名）：

```toml
[service]
account_id = "10001"                     # 绑定账号（网关登录身份必须与它一致）
state_dir = "/var/lib/maimai-courier"    # 服务独占状态目录（必填）
client_identity = "maibot-plugin"        # 认证主体名；客户端 service.client_identity 与它一致
ledger_id = "由 fresh/migrate 输出的账本身份"
listen_host = "127.0.0.1"                # 默认回环；跨主机监听必须配置 TLS 证书与私钥
listen_port = 8790
token_env_var = "MAIMAI_COURIER_SERVICE_TOKEN"
allowed_source_groups = ["111111111"]    # 服务端许可上限，与客户端白名单取交集
allowed_target_groups = ["222222222"]
onebot_endpoint = "ws://127.0.0.1:3001/onebot"   # 非回环主机必须 wss://
onebot_token_env_var = "MAIMAI_COURIER_ONEBOT_TOKEN"
# tls_cert_file = "…"                    # 跨主机监听时与私钥成对配置
# tls_key_file  = "…"
```

## 3. 旧账本迁移（先停旧发送）

1. **停止旧发送路径**：关闭旧版本插件的总开关（或卸载），确认没有在途投递，再停止
   旧进程写入账本。迁移工具本身只读快照，但快照必须取自静止的旧库。
2. **制作一致性快照**：复制旧插件 `data_dir` 下的 `delivery_ledger.sqlite3` 到独立
   备份路径（原库保留、不改动）。若现场存在 `-wal` / `-shm` 伴随文件，先让旧进程
   完全退出后再复制相应文件。
3. **显式迁移**（目标状态目录必须尚未初始化；已迁移过的账本拒绝 `fresh`）：

   ```bash
   python -m service ledger migrate --state-dir <服务状态目录> \
       --snapshot <旧账本快照路径> [--confirm-ledger-id <账本身份>]
   ```

   映射规则：旧 `sent` 保留目标消息 ID 与评论结果；旧 `unknown` 保留阻断；旧
   `sending` 归并为**待核实**（不自动续发）；旧明确 `failed` 保留为可被新的显式
   尝试使用。工具会核对快照完整性、schema、四字段键集合与数量，导入摘要写入账本
   元数据；**原库与原快照都不被改写**。同一快照重复导入是幂等的（不新增记录）。
4. **核对**：`ledger status --state-dir <服务状态目录>` 应显示账本身份、状态计数与
   导入摘要；把身份填入服务配置与客户端预期账本 ID。
5. **失败即关闭**：缺库、损坏、未知 schema、冲突终态或模拟中断都会拒绝导入且不启用
   发送；重复提交确定性失败。此时保持关闭，修好快照或人工核对后再重试，**不要**
   退回 `fresh` 当作空历史。

## 4. 备份与恢复

- **停机备份**：停止服务进程（SIGTERM，有界退出）后，整目录复制服务状态目录
  （`service_ledger.sqlite3` 与 `service.lock`）；服务运行中不要复制
  （独占锁与写入中状态会让副本不一致）。
- 备份包含数据集身份、schema 版本与导入摘要等元数据（都在账本文件内）；备份中
  不含凭证。
- **恢复**：把备份目录放回同一位置（或配置指向该副本）→ `ledger status` 核对身份
  与计数 → `start` 时配置的 `ledger_id` 必须与之一致。恢复后旧会话与未消费准备
  全部失效；启动恢复会把遗留的在途占位归并为待核实（不自动续发源或评论）。
- 临时材料不构成“可自动发送的队列”：恢复不会让任何未发送内容自动发出。
- 成功与待核实记录不按时间或容量清理；不要手工删改账本记录。

## 5. 回滚到旧实现（先停新路径，再验证兼容快照）

1. **停止新路径**：关闭插件总开关并停止转发服务，确认没有新的发送。
2. **兼容导出**（服务必须在停止状态；运行中会被独占锁拒绝）：

   ```bash
   python -m service ledger export --state-dir <服务状态目录> \
       --output <兼容快照输出路径> [--confirm-ledger-id <账本身份>]
   ```

   导出是旧 schema 的兼容快照，包含迁移后产生的**新结果**；映射保守：在途状态导出
   为待核实、评论不自动补齐；成功后工具会回读校验键集合与数量，且不改写服务账本。
   输出路径已存在时拒绝覆盖。
3. **验证阻断作用**：用旧公共账本入口（`open_ledger` / `reserve`）打开导出文件，
   确认全部成功/待核实键都不能再占位，源已发但评论不确定的记录仍阻止源重发。
4. **切换**：只有验证通过后，才把导出快照交给旧实现使用。**不允许**同时运行新旧
   两条发送路径。
5. **无法安全导出时保持关闭**：不完整、冲突或无法验证的导出不得用于恢复发送；
   直接启用只含迁移前历史的旧库会丢失新投递的防重作用。

## 6. 误连空库/其他数据集的保护

- 服务配置的 `ledger_id` 与实际身份不一致、或留空时，`start` 在打开后立即拒绝
  （退出码 5），不建立发送任务。
- 客户端要求 `expected_ledger_id` 与 `migration_confirmation` 均非空，且本地收据
  （`service_binding.json`）与当前配置一致；换库或改标识会拒绝发送。
- 不要用“新建一个空状态目录”来绕过以上检查；需要换数据集时，走第 2 节的显式
  `fresh`/`migrate` 与两端的身份配置更新。

## 7. 本文档的演练记录（2026-10-02，临时目录，合成数据）

以下步骤在 `/tmp/mc-drill-8.2` 的合成环境实际执行（旧库由旧公共接口写入合成记录，
未读取任何真实凭证、运行配置、线上消息或真实账本；模拟 OneBot 为本地回环替身）：

| 步骤 | 命令（简写） | 观察结果 |
| --- | --- | --- |
| 合成旧库并复制快照 | 旧接口 `open_ledger` + `reserve`/`mark_sent`/`mark_unknown`/`mark_failed`/`record_comment` | 4 条记录（sent/unknown/sending/failed 各 1）；原库与快照 SHA-256 相同 |
| 迁移 | `ledger migrate --state-dir … --snapshot …` | 退出码 0；账本身份 `0ce3a002…`；导入 4 条，`sending`→`unknown`；原库/快照 SHA-256 不变 |
| 只读状态 | `ledger status --state-dir …` | 退出码 0；身份一致；`failed=1, sent=1, unknown=2`；导入摘要 1 条 |
| 停机备份/恢复 | 整目录复制后 `ledger status --state-dir …` | 退出码 0；副本身份与计数与源一致 |
| 兼容导出 | `ledger export --state-dir … --output …` | 退出码 0；旧入口复验：sent/unknown/在途归并后的 unknown 均不可再次占位，明确 failed 可重试；源账本未改写 |
| 正常启动 | 回环模拟 OneBot + `start --config …` | 依次打印账本就绪/恢复、账号就绪、`HTTP 服务端已就绪并监听 127.0.0.1:<端口>`；SIGTERM 后退出码 0 |
| 缺配置拒绝 | `start --config <不存在>`；未设置 HTTP 凭证变量 | 退出码 3，明确原因；未建立网络连接 |
| 运行中导出保护 | 服务运行中 `ledger export` 指向其状态目录 | 退出码 5：“状态目录已被另一个服务进程独占”，未写出文件 |
| 误连保护 | `start` 指向另一个已初始化账本且 `ledger_id` 不符；`ledger_id` 留空 | 退出码 5，身份不符/缺少预期身份两种原因；未进入 OneBot 连接 |
| 未初始化保护 | `ledger status --state-dir <未初始化目录>` | 退出码 4，提示先 `fresh`/`migrate`，不隐式创建新账本 |
| 旧库复核 | 旧公共入口打开导出快照 | 成功记录保留目标 ID `t-sent-1` 与评论结果 `t-comment-1`；已确认阻断行为 |

演练全程没有执行任何部署、发送或删除操作；临时目录仅用于本次核对。
