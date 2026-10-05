# 第9组实机部署返修与复验记录

日期：2026-10-04。变更：`direct-snowluma-websocket-forwarding`。
基准提交：`2d7f4a585222a3dc2f9d57e86baf7c9f925c3c78`。本文件是本组核实附件与返修记录，
不是新规划工件；三份 delta 正文、design D11 与主规格均未改动。

**9.1–9.3 已完成：两档测试全绿、活跃生产文件类型检查零错误。
9.4（在 `arclinux` 重新部署并复验真实布局）未执行，由操作者按部署手册另行执行。**
两轴评审返修（归档证据补齐、旧文案同步与 judgement 项）已在第二轮完成，见第 7 节。
本组全部证据来自本机合成临时目录与本地回环端点；未连接真实 SnowLuma、未发消息、
未读取部署机凭证/运行配置/真实账本。

## 1. 范围与公共入口

- 改动只落在 `delivery_ledger.py`、`shared/config_rules.py`、`snowluma_transport.py`
  与对应活跃测试；未改 `plugin.py`／`forward_delivery.py`／`forward_precheck.py`／
  `send_material.py`／`snowluma_client.py`、配置模型默认值、SDK 替身、历史 109 项、
  其他 spec/design 正文与其他组 checkbox。第二轮按评审允许清单另同步 `config.py`、
  `config.toml`、`README.md`、`AGENTS.md`、两份 `docs/`、`C/tasks.md` 2.2 与
  `C/design.md` D2 指向，见第 7 节；仍未触碰历史 109 项与其他组勾选。
- 9.1 的公共 seam 是 `open_ledger`（`delivery_ledger` 公开导出）+ 合成状态目录；
  私有判定函数不经测试直接调用，行为断言全部经 `open_ledger` 与 `DeliveryLedger`
  公开方法。
- 9.2 的公共 seam 是 `onebot_endpoint_rejection`／`resolve_onebot_connection`
  （配置期）与 `AiohttpOneBotTransport` 的公开四动作（建连前）；解析器经构造参数
  注入，测试使用本地 `aiohttp.web` 回环端点与既有内存线路替身。
- 真实部署现场形态（状态目录等于插件目录；同网络只能明文 `ws://maim-bot-snowluma:3001`）
  见私有手册 `.local/runbooks/direct-snowluma-deployment.md` 的 B1/B2，本文件不复制
  真实群号、凭证或私有路径细节。

## 2. 修订一：账本历史核对只阻断状态类证据（9.1）

`open_ledger` 的目录判定由「只允许主库/锁/伴随文件的固定名单」改为「只阻断状态类
证据、放行插件自身部署文件」（design D11 第 1 条）：

- **放行**：主库 `delivery_ledger.sqlite3`、本插件锁 `delivery_ledger.lock`、主库存在
  时的 `-wal/-shm/-journal`；以及源码、文档、配置、缓存等常规文件与目录
  （`.py/.md/.toml/.txt/.rst/.yaml/.yml/.ini/.cfg/.pyc/.pyo`、`__pycache__/`、
  `config_back/` 等）。子目录只做一层名字检查，不读内容、不递归多层。
- **阻断**（`LedgerUnavailable`，不改动任何文件）：符号链接、硬链接（nlink≠1）与
  非常规文件；`service_binding.json` 前缀（忽略大小写，含 `.tmp` 等）；主库以外的
  数据库文件（`.sqlite3/.sqlite/.db` 及其 `-wal/-shm/-journal` 形态，如
  `service_ledger.sqlite3`、`delivery_ledger.sqlite3.bak`、`delivery_ledger-20261001.sqlite3`）；
  主库缺失时的孤立伴随文件；其他进程的 `.lock` 文件；名字含状态标记
  （`ledger/delivery/courier/snapshot/receipt`，忽略大小写）且不是部署类扩展名的条目；
  子目录自身或其直接子项名字命中上述规则的条目。第二轮补齐 `.tmp-<8位十六进制>` 导出
  临时名、归档类扩展名与中文「快照/归档/备份」标记三条规则，见第 7.1 节。
- **新装判定**：只有「本次新建锁」（`lock.created`）且不存在任何状态类证据，主库缺失
  才按新装建库；已有上次运行留下的锁或存在状态类证据，继续关闭发送并提示人工核对，
  不静默把删除过的账本当新装。
- **诊断**：只含安全条目名与类别（例如「服务切换收据」「主库以外的数据库或归档」
  「其他进程的锁文件」「带状态标记的未核对条目」），不回显条目正文；主库、锁与
  证据文件一律不删除、不移动、不改写（测试逐字节核对）。

真实部署目录形态的离线复核（本机合成，非部署机）：把当前仓库运行时载荷
（根 `.py`、`_manifest.json`、`config.toml`、`shared/*.py`、`service/__init__.py`）
复制到临时目录，再加 `config_back/`、`__pycache__/delivery_ledger.cpython-313.pyc`
与合成「10 条 `sent`」旧库：`open_ledger` 正常打开，抽样键状态 `sent`，
`reserve()` 返回 `granted=False`，即既有成功记录仍阻断重发；目录另出现
`delivery_ledger.lock`，无其他新增文件。此复核只证明离线形态可打开，不替代 9.4
在真实 `ctx.paths.data_dir` 上的只读复验。

### 与旧断言的冲突处理（按 D11 新语义更新，安全用例只增不减）

- 旧「本目录历史证据」用例中 `service.lock`、`人工任意快照`、`export.tmp-1234abcd`、
  「未知目录」四个合成名字在名字规则下不再命中。处理：`service.lock` 保持阻断
  （见下）；`人工任意快照` 改为命名的归档 `旧账本快照.sqlite3`；`export.tmp-1234abcd`
  改为旧导出的真实形态 `service_ledger.sqlite3.tmp-1234abcd`；「未知目录」改为子目录内
  一层命中规则（`历史快照目录/delivery_ledger.sqlite3.bak`）。用例数不减，证据覆盖改为
  可被名字识别的形态。第二轮补齐 `.tmp-8hex` 规则后，该用例已恢复为原始任意命名
  `export.tmp-1234abcd` 并确认阻断（见 §7.1）。
- 旧断言 `assert evidence not in result.reason` 与 D11 新增「诊断可含安全条目名」冲突，
  改为只断言**正文**（`damaged-or-secret`、`假敏感正文`）不进入诊断；条目名仍不当作
  敏感信息，但条目正文不读取也不回显。
- 「无法确认来源的路径」用例新增「命名管道」参数，覆盖非常规文件阻断。

### 偏离与保守新增（相对 D11 正文的显式说明）

- `service.lock` 这类**其他进程的 `.lock` 文件**不属 D11 逐字枚举，但它是另一权威对
  同一目录的独占状态痕迹（第 7 组状态清单中的服务状态文件），任务枚举的标记列表
  也不含 `lock`。本组按「状态类证据」保守处理：除本插件锁以外，顶层与子目录一层
  的 `.lock` 名字一律阻断。此为收紧而非放宽，真实部署载荷不含其他 `.lock` 文件，
  不造成误阻断。
- `service_binding.json` 前缀改为忽略大小写，仍是收紧。
- 未放宽任何一条既有安全判定：链接、硬链接、非常规文件、孤立伴随文件、不兼容库、
  独占失败均照旧关闭发送。

## 3. 修订二：明文端点仅限回环或私有目标（9.2）

- `shared/config_rules.py` 新增 `is_private_host(host)`：只做字面量判定（不做 DNS），
  覆盖回环、RFC 1918 私有、链路本地、ULA 与 IPv4 映射地址，容忍 `[]` 包装、尾点与
  区域后缀（`fe80::1%en0`）；名字与公网地址不是可信目标。
- `onebot_endpoint_rejection`：`ws` 且主机是**公网 IP 字面量**时仍在配置期拒绝
  （原因改为「非回环或私有的 SnowLuma 连接必须使用 wss」）；`ws` 回环/私有字面量放行；
  主机名不再因「非回环」在配置期拒绝，改由建连前判定；`wss` 行为不变。用户信息、
  查询串、片段、未指定地址、非 ws 协议与主机形态等既有拒绝项逐项保持。
- `AiohttpOneBotTransport`：建连前若端点为 `ws` 且主机不是回环字面量，先解析主机
  （默认 `loop.getaddrinfo`，可经构造参数 `resolver` 注入），要求**全部**地址为
  回环/私有/链路本地；公网地址、混合结果、空结果与解析失败一律抛
  `OneBotUnavailable("insecure_endpoint", submitted=False)`，不创建网络会话、
  不读取凭证、不写出任何帧，也不因容器名单标签形态放行公网目标。字面量地址不查询
  解析器，`wss` 不做该判定。异常不携带解析细节（`__cause__` 为空）。
- 配置文本 `SnowLumaSection.websocket_url` 的 `description`/`hint` 在第一轮仍写着
  「仅回环可用 ws」；第二轮已按 D11 口径同步（见 §7.3），不再是遗留问题。

## 4. TDD 红绿记录（实际命令输出）

每个行为先经公共入口写失败测试确认红，再最小实现变绿；未预写未来功能，未调用私有
判定函数替代行为验收。

1. 9.1 正例红（部署文件形态应可打开）：`pytest tests/test_delivery_ledger.py -q -k "9_1 or 子目录 or 状态类证据 or 孤立伴随 or 部署文件"`
   → `2 failed, 16 passed, 300 deselected`，失败原因均为
   `LedgerUnavailable(reason='本插件目录存在未核对的历史条目，须人工核对')`。
2. 9.1 实现后，旧「历史证据」用例中与新语义冲突的 15 个断言失败
   （`evidence not in result.reason` 与三个不再命中的合成名字、未知目录），按上节
   更新后整文件 `320 passed`。
3. 9.2 配置红：`pytest tests/test_config.py -q` → `29 failed, 56 passed`（`is_private_host`
   未定义、私有字面量与容器名被配置期拒绝）。
4. 9.2 传输红：`pytest tests/test_snowluma_transport.py -q -k 9_2`（带 aiohttp）→
   `15 failed`，全部为 `TypeError: ... unexpected keyword argument 'resolver'`。
5. 实现后：`pytest tests/test_config.py -q` → `85 passed`；
   `pytest tests/test_snowluma_transport.py -q` → `49 passed`。

新增用例：账本 19 个 9.1 节点 + 命名管道 1 个（文件 300→320）；传输 15 个 9.2 节点
（文件 34→49）；配置新增 28 个独立用例，合法地址清单 +6（私有字面量、ULA、链路本地、
容器名、IPv4 映射回环），原被拒的 `ws://snowluma.example` 移出改由建连前判定。
预期值均为独立字面量，不测私有函数、不用实现自身推导预期。

## 5. 实际验证结果（2026-10-04，均退出码 0）

环境：macOS、本机合成临时目录；Python **3.14.7**、pytest **9.1.1**、aiohttp **3.14.3**、
pyright **1.1.414**；验证依赖只用 `uv` 临时环境，无全局安装、无真实 SDK、不连真实端点。

1. `uv run --no-project --with pytest pytest tests/ -q`
   ```
   ..........................................                               [100%]
   830 passed, 6 skipped in 3.84s
   ```
2. `uv run --no-project --with pytest --with aiohttp==3.14.3 pytest tests/ -q`
   ```
   ............................................................             [100%]
   924 passed in 4.55s
   ```
   带库档真实执行 `tests/test_snowluma_transport.py` 的 49 个本地回环 WS 用例
   （含新增 15 个 9.2 节点），无跳过。
3. `uv run --no-project --with pyright pyright`
   ```
   0 errors, 0 warnings, 0 informations
   ```
   检查范围仍为活跃生产代码（13 根模块 + 8 共享模块 + `service` 包墓碑），历史区不检查。

对照：本组开始前默认档为 777 passed/6 skipped、带库档 856 passed（第 8 组记录），
两档现均只增不减；`pytest tests/ -q` 与仓库根默认收集同范围，历史用例不进入。

## 6. 遗留问题与 9.4 前置条件

- **9.4 未执行**：需在 `arclinux` 同步修订后的生产文件（第二轮另含 `config.py`
  文案与 `shared/` 导出；`tests/` 只在离线使用），以真实插件目录只读验证
  `open_ledger` 可打开、既有 10 条成功记录仍阻断重发，插件保持未激活；端点与凭证
  引用由操作者提供后再验。
- 真实部署目录若含顶层符号链接（例如规则文件软链）或非常规文件，仍会按 D11
  关闭发送并要求人工核对；第 8 组部署载荷为普通文件，但 9.4 部署前应确认不复制
  `CLAUDE.md` 等链接。
- **名字判定的固有边界（残留风险）**：纯任意名（无扩展名、无状态标记、非
  `.tmp-<8位十六进制>` 形态，例如 `x`、`旧数据`）无法按名字识别为状态证据，会被
  放行；判定不读内容，无法代替人工核对。该边界由
  `tests/test_delivery_ledger.py::test_纯任意名文件无法按名字判定为证据` 固定，9.4
  现场对无法解释的此类文件应人工判断，不把它当作已确认安全。补齐的三条规则只覆盖
  可命名形态（`.tmp-8hex`、归档扩展名、中文快照/归档/备份标记），不承诺穷尽。
- U1–U8 全部保持未确认；本组未新增任何服务端事实断言，`ws://maim-bot-snowluma:3001`
  的解析与可达性只能在 9.4 的部署网络内确认。

## 7. 第二轮返修：归档证据补齐、旧文案同步与评审项（2026-10-04）

两轴评审后返修本组全部未通过项；改动只在评审允许清单内，未触碰历史 109 项、
未变更其他组勾选、未做任何真实机器操作（9.4 仍由父流程执行）。

### 7.1 账本状态类证据补齐三类规则（A）

`_state_evidence_category` 保持同一纯名字判定函数（不读内容、不改文件），新增：

1. 名字以 `.tmp-<8 位十六进制>` 结尾（正则 `\.tmp-[0-9a-f]{8}\s*\Z`，忽略大小写）→
   阻断，识别历史导出/半成品形态。`$` 只在末尾或结尾换行前匹配、`\Z` 只认真正的
   末尾；单用 `\Z` 会漏掉以换行结尾的异常名字，故用 `\s*\Z` 容忍结尾空白，使
   `x.tmp-1234abcd\n` 这类名字同样命中（用例含该参数）。
2. 归档类扩展名 `.tar/.tgz/.gz/.zip/.7z/.rar/.bak` → 阻断。
3. 名字含中文状态标记 `快照`/`归档`/`备份` 且不以部署类扩展名结尾 → 阻断。
   ASCII `backup`/`archive` 刻意不进标记集：真实部署有 `config_back/`，按 ASCII
   子串判定会误伤。

部署形态继续放行：`config.toml.backup.20260929233435`（扩展名是数字时间戳）、
`config_back/`、`__pycache__/*.pyc`、源码/文档/配置不改动、不阻断。

TDD 红绿（公共入口 `open_ledger`，实际命令与输出）：

1. 红（实现前，新增用例）：
   `uv run --no-project --with pytest pytest tests/test_delivery_ledger.py -q -k "状态类证据 or 部署文件 or 数字时间戳 or 纯任意名 or 本目录历史证据 or 子目录"`
   ```
   6 failed, 34 passed, 287 deselected in 0.38s
   ```
   失败均为新形态未命中证据规则、错误地继续按新装打开：`export.tmp-1234abcd`
   （5.3 与 9.1 两处入口）、`备份.tar.gz`、`人工快照.tar`、`导出归档`。
2. 绿（实现后整文件）：
   `uv run --no-project --with pytest pytest tests/test_delivery_ledger.py -q`
   ```
   327 passed in 1.63s
   ```
   用例数 320 → 327，只增不减。

新增/恢复用例：9.1 阻断参数补 `export.tmp-1234abcd`、`备份.tar.gz`、
`人工快照.tar` 与单独隔离中文标记规则的 `导出归档`；5.3 旧用例恢复为原始任意命名
`export.tmp-1234abcd`；新增反例 `config.toml.backup.20260929233435` 与
`__pycache__/x.pyc` 仍放行；子目录一层判定用例的父目录改为中性名「插件子目录」，
保证仍由子项命中规则触发（否则父目录名先命中，子项判定失去覆盖）。阻断用例
逐字节核对证据文件零改动，反例同样核对。

### 7.2 固有边界（残留风险）

**纯任意名（无扩展名、无状态标记、非 `.tmp-<8位十六进制>` 形态）无法按名字识别为
状态证据，会被放行；这是名字判定法的固有边界，不是本组可消除的缺陷。**例如状态
目录里出现 `x` 或 `旧数据` 这类来源不明文件，名字规则不阻断、也不读内容判断，只能
靠人工核对发现。该边界由
`tests/test_delivery_ledger.py::test_纯任意名文件无法按名字判定为证据` 固定，并在
§6 列为 9.4 的核对残留风险；不以「用例只增不减」带过。

### 7.3 旧文案同步（B）

- `config.py`：`SnowLumaSection.websocket_url` 的 `description` 与 `hint` 改为 D11
  口径（回环/私有/链路本地可用 `ws`；公网必须 `wss`；主机名建连前解析确认，
  解析出公网或失败即拒绝）。
- `config.toml`：该字段注释同步。
- `README.md`：地址段、跨容器段与账本停止条件三处同步，含「插件自身源码、文档、
  配置与缓存不构成证据」。
- `AGENTS.md`：地址段改为 D11；「未知条目即 `ledger_unavailable`」改为状态类证据
  名字规则口径并注明部署文件不构成证据。`CLAUDE.md` 软链无需另改。
- `docs/direct-snowluma-operations.md`：地址段与跨容器段同步；账本停止条件改为
  状态类证据口径，补「插件自身源码/文档/配置/缓存不构成证据」与纯任意名边界说明。
- `docs/real-qq-validation.md`：P4 改为 D11 口径。
- `C/tasks.md` 2.2 验收措辞更新为 D11（保持已勾选）；`C/design.md` D2 补
  「（地址口径见 D11 修订）」指向，不改写历史结论。
- 既有核实附件加顶部修订提示：`verification/05-aiohttp-capabilities.md`（地址口径）
  与 `verification/07-history-baseline.md`（账本证据口径），原证据正文未改。

### 7.4 评审 judgement（C）

- `shared/__init__.py` 聚合导出补 `is_private_host`（与 `is_loopback_host` 对称）；
  `tests/test_shared_types.py` 的无 SDK 导出清单同步补两个名字。
- `shared/config_rules.py` 模块头改为当前消费者 `config.py` 与 `snowluma_transport.py`，
  不再提已退役的 `remote_client.py`/历史 `service/config.py`。
- 9.2 拒绝路径补 `tests/test_config.py::test_地址解析不通过时不读取凭证环境值`：
  用既有 `ReadForbiddenEnvironment` 证明端点被拒时解析入口零环境取值。
- `is_unique_local` 的 getattr 兼容用法补注释：标准库 `IPv6Address` 公开属性集不含
  该名（目标下限 3.11 与实测 3.14 均无），ULA 已由 `is_private` 覆盖，getattr 只作
  出现该属性时的兼容层，不承担主判定。

### 7.5 第二轮实际验证结果（2026-10-04，均退出码 0）

环境同 §5（Python 3.14.7、pytest 9.1.1、aiohttp 3.14.3、pyright 1.1.414，`uv` 临时
环境，无全局安装、不连真实端点）。

编排者随后定点修正了 `.tmp-<8位十六进制>` 规则对结尾空白的漏检（改用
`\.tmp-[0-9a-f]{8}\s*\Z`，并补 `export.tmp-1234abcd\n` 与 `人工任意快照` 两个参数），
下表为修正后复跑结果；第二轮修复时的中间数字为 838/6 与 932。

1. `uv run --no-project --with pytest pytest tests/ -q`
   ```
   839 passed, 6 skipped
   ```
2. `uv run --no-project --with pytest --with aiohttp==3.14.3 pytest tests/ -q`
   ```
   933 passed
   ```
3. `uv run --no-project --with pyright pyright`
   ```
   0 errors, 0 warnings, 0 informations
   ```

对照第一轮 830 passed/6 skipped、924 passed、0 错误，三项只增不减；默认档 6 个跳过
仍是两个真实传输模块导入期跳过与四个运行期跳过，带库档无跳过。**9.4 仍未执行**，
不得据本轮离线结果宣称已部署或已联验。
