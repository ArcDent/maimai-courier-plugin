## 1. 传输层动作表

- [x] 1.1 `snowluma_transport.py`：移除 `forward_group_single_msg`，新增只读 `get_forward_msg`（`id` 参数）与发送 `send_group_forward_msg`（`target_group_id`/`messages`/`before_submit`）
  - seam：`snowluma_transport.OneBotTransport` 协议导出与 `AiohttpOneBotTransport` 方法面
  - 验收：源码无 `forward_group_single_msg`；七动作逐一经内存替身调用成功，信封与 `{"action","params"}` 一致
- [x] 1.2 更新 transport 与 Action 表的 docstring/契约注释（六动作描述改为新动作集）
  - seam：模块 docstring 与 AGENTS.md 模块职责的一致性（grep 对照）
  - 验收：`snowluma_transport.py` 无旧动作名残留措辞

## 2. 客户端卡片分支

- [x] 2.1 `snowluma_client.py` `prepare_source` 的 forward 分支改为：取顶层唯一卡片段的 `forward.data.id` → `get_forward_msg` 展开 → 校验（messages 非空、sender 可取、content 为段数组）→ 材料=节点数组
  - seam：`SnowLumaClient.prepare_source`（导出方法）
  - 验收：内存替身：展开成功→`kind="forward"` 且材料为节点数组；展开业务失败/空节点/sender 缺失→可定位 `PrepareRejected`，零发送
- [x] 2.2 `_submit` 分派：卡片分支改发 `send_group_forward_msg`（`messages`=节点数组），只提交 group_id 与 messages，不设预览参数
  - seam：内存替身捕获的 `send_group_forward_msg` 请求体
  - 验收：断言 `messages` 为节点数组、`sender` 为标量、无 `summary/news/prompt/source` 键
- [x] 2.3 `_execute_send` 回执：`res_id`/`forward_id` 不入账不进结果（只取 message_id 走四项核验）
  - seam：`SourceSendResult` 字段面
  - 验收：替身返回含 `res_id` 的信封时结果体无资源 ID 字段；账本只写 target_message_id（既有行为，回归确认）

## 3. 卡片节点载荷构造

- [x] 3.1 `send_material.py` 新增卡片节点载荷构造与校验：从展开查询结果构建 `{"type":"node","data":{"sender","name","content"}}`（sender 压平限长、name 取 card 优先回退 nickname 限长、content 段原样深拷贝）
  - seam：`send_material` 新增导出（节点构造函数）+ 其拒绝路径
  - 验收：构造输出逐字段符合；sender/name 不可确认→拒绝；content 不被原地修改（输入冻结）
- [x] 3.2 嵌套 forward 段原样保留（不递归展开、不拒绝）
  - seam：同一构造函数的段级行为
  - 验收：content 内含 forward 段时原样保留；无递归调用（源码扫描 + 用例）

## 4. 预检与共享类型核对

- [x] 4.1 核对 `forward_precheck.py` 卡片路径（顶层唯一卡片判定、`kind="forward"`）与新准备链路口径一致，预检不访问平台
  - seam：`forward_precheck` 导出（pytest 可达）
  - 验收：既有预检用例全绿；契约外不做网络调用（替身零请求）
- [x] 4.2 核对 `shared/send_steps.py` 类型（`SourceKind`/`SourceMaterial.segments` 承载节点数组）无需新增
  - seam：`shared.send_steps.PreparedSource`（导出类型）
  - 验收：卡片材料经既有类型表达，无第二套类型/状态机
- [x] 4.3 复核 `config_version` 是否需要因本次行为变更提升：不新增配置项则维持 `1.5.0`，如提升则同步 `config.py` 与 `config.toml`
  - seam：`config.PluginSection.config_version` / `config.toml`
  - 验收：两处逐项一致；决定理由写入 tasks 回报（不新增配置键则维持）

## 5. 测试

- [x] 5.1 `tests/test_snowluma_transport.py`：动作表改为七动作（移除旧卡片动作用例、新增 get_forward_msg/send_group_forward_msg 用例，含 sender 标量与 2MiB 边界回归）
  - seam：`tests/test_snowluma_transport.py`
  - 验收：aiohttp 档全绿，默认档 importorskip
- [x] 5.2 `tests/test_snowluma_client.py`：卡片分支重写（展开→校验→分派；拒绝矩阵：业务失败/空节点/sender 缺失/嵌套原样；res_id 不入账；展开查询超时按拒绝分类）
  - seam：`tests/test_snowluma_client.py`
  - 验收：默认档全绿；断言 kind=forward 只产生 `send_group_forward_msg` 请求（不产生 `send_msg`+forward 段）
- [x] 5.3 `tests/test_send_material.py`（或 client 测试内）：卡片节点构造单测（字段/限长/原样/嵌套/输入不修改）
  - seam：`tests/test_send_material.py`
  - 验收：默认档全绿
- [x] 5.4 `tests/test_direct_acceptance.py`：回环 HTTP 端点补卡片全链（Hook/Tool→预检→账本→展开→发送→回执→落账→评论）
  - seam：`tests/test_direct_acceptance.py`
  - 验收：aiohttp 档全绿；端点只绑 127.0.0.1；替身返回含 res_id 的信封
- [x] 5.5 既有用例改新契约：移除断言 `forward_group_single_msg` 的用例，`test_shared_types`/`test_forward_delivery` 等回归
  - seam：各 `tests/test_*.py`
  - 验收：两档全绿，无 skip 掩盖

## 6. 文档

- [x] 6.1 `AGENTS.md`：模块职责（client/transport 动作表与分派描述）、来源规则（卡片消息转发机制定义）、验证数字（实现后重跑更新）
  - seam：AGENTS.md 两节 + 验证小节
  - 验收：无 `forward_group_single_msg` 残留描述；卡片机制表述与 delta 一致
- [x] 6.2 `README.md`：支持类型表卡片行（展开重组描述 + 内层展示不保证）；结果状态表无需变
  - seam：README「支持与拒绝类型」节
  - 验收：无旧通道表述；无承诺字样
- [x] 6.3 `docs/direct-snowluma-operations.md`：排障提示补一条「卡片类失败先看平台 wording（如 HTTP download failed）」；`docs/real-qq-validation.md`：U4/U6 更新为「展开重组路径已实测可用、内层展示待联验」
  - seam：两文档对应小节（人工核对清单）
  - 验收：新增条目与实测一致，未验证项标注归属
- [x] 6.4 `CONTEXT.md`：「卡片消息转发」机制定义改为「按源消息展开的节点重组发送」
  - seam：CONTEXT.md 词条
  - 验收：词条与 delta 措辞一致
- [x] 6.5 `docs/snowluma-http-api-catalog.md`：第 6 节补引用本变更归档件（实现完成后由归档流程收口）
  - seam：catalog 6.5/6.6 交叉引用
  - 验收：引用路径存在

## 7. 验证与收尾

- [x] 7.1 `uv run --no-project --with pyright pyright` 生产代码零错误并记录 filesAnalyzed
  - seam：pyright 公共命令
  - 验收：0 errors；数字写入 README 验证小节
- [x] 7.2 `openspec validate card-forward-node-rebuild --strict`
  - seam：openspec CLI
  - 验收：输出 is valid
- [x] 7.3 两档 pytest 全量重跑并记录真实结果；`history/` 与归档目录零收集
  - seam：两档 pytest 公共命令
  - 验收：数字与文档一致
- [x] 7.4 红线自检：无 `ctx.send.*`、无任意动作代理、无 URL 凭证、日志不含正文/签名地址、无「失败换动作」降级链、卡片路径无 `send_msg` 直引源卡片段的请求构造
  - seam：`grep` 公共命令 + 人工审查 + 5.2 的通道断言
  - 验收：六项通过
