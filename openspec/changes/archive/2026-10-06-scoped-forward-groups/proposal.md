## Why

`[scope]` 节是两份平铺白名单（`source_groups` / `target_groups`）：任意源群可发往任意目标群，唯一约束是「目标 ≠ 来源群自身」。实际投递诉求是分区的——一个吃瓜群常需同时接收多个源群的料，而某个源群的料只给固定对象；平铺结构表达不了「同组才能互投」，越权面还随目标数量线性放大（每加一个目标，所有源群都自动获得投给它）。

## What Changes

- **BREAKING**：`[scope]` 节改为 `groups` 具名转发分组结构数组，每项含必填 `name`、`source_groups`、`target_groups`；两份平铺白名单直接移除，不设兼容期（风格同 ADR 0008），存量配置需手工改写，`plugin.config_version` 随之提升，默认空分组等价于旧的空白名单（没有源也没有目标）。
- 判定口径从「目标在目标白名单」改为「**目标与来源同属一个转发分组**」；工具预检、源/评论提交前复验、自动入口的候选筛选与目标列举共用同一个分组判定函数，不复制第二套。
- 配置期结构校验：源群跨组重复、同一分组内源群重复、`name` 跨组重复或为空白，一律拒绝写入；空目标分组与组内「源 = 目标」允许保存，由运行期既有的「目标 ≠ 来源群自身」规则拒绝。
- `name` 不参与任何路由、权限或判断，只作用户标识与日志前缀；不限长、不限字符集，但非空且跨组唯一。
- 目标群可跨组重复；源群也可兼作别组的目标群，实际投递仍受「目标 ≠ 来源群自身」兜底。
- 自动入口的合法目标从「全部目标白名单」收紧为「来源群所在分组的目标」；「没有合法目标群即不唤起」的既有行为保留，只是范围收紧。
- 投递账本四字段键（账号/源群/源消息 ID/目标群）不变；热更新复验改按同组口径，未发送的不发送、已成功的不回滚。

## Capabilities

### New Capabilities

无。不新建 capability：分组是路由约束的口径变化，不是新的行为域。

### Modified Capabilities

- `cross-group-forward`: 新增「具名转发分组与同组投递判定」需求（分组结构、配置期校验、同组判定口径与默认空分组）；修订三参数工具权限、自动入口、评论复验、默认空白名单与热更新复验中对白名单的表述，统一改为转发分组口径；自动分享标准的「越权」场景同步改述为分组外投递。

## Impact

- `config.py` / `config.toml`：`ScopeSection` 改为 `groups` 结构数组（含配置期结构校验），`plugin.config_version` 提升；两处默认值逐项一致。
- `forward_precheck.py`：`whitelist_entries` / `source_group_is_allowed` / `target_is_allowed` 收敛为分组判定单点定义，消费者（预检、`plugin._send_allowed` 源与评论复验、`auto_entry`）共用。
- `auto_entry.py`：`share_target_groups` 改为列举同组目标；`auto_entry_invalid_reason` 的「没有合法目标群」判定同口径。
- 直连链路不变：`snowluma_client` / `snowluma_transport` 只接收目标群号，同账号名册确认与发送动作不受影响。
- 测试夹具与用例：`test_config`、`test_forward_precheck`、`test_auto_entry`、`test_lifecycle`、`test_plugin`、`test_direct_acceptance` 等从平铺白名单迁移到分组结构；新增分组校验与跨组拒绝用例。
- 文档：README 配置说明、`docs/direct-snowluma-operations.md`（如涉及白名单描述）、CHANGELOG；术语依据 `CONTEXT.md` 的「转发分组」与 `docs/adr/0010-scoped-forward-groups.md`。
