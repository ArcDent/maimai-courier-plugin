## Context

动机见 proposal.md - Why。当前实现状态：`forward_precheck.py` 用 `whitelist_entries` 归一 + `source_group_is_allowed` / `target_is_allowed` 两个成员判定覆盖四个消费点——工具预检、`plugin._send_allowed`（源与评论提交前复验）、`auto_entry.share_target_groups`（自动入口目标列举）、`auto_entry.screen_forward_candidate` / `auto_entry_invalid_reason`（源群侧筛选与热更新重验）。目标判定与来源无绑定是本次要消除的结构性缺口。

已核实的两个技术前提：
- 测试替身 `tests/fake_maibot_sdk.py` 的 `_validate_config_value` 对 `list[X]` 逐项校验，item 注解为嵌套 `PluginConfigBase` 时走 `annotation(**value)`，因此 `list[ForwardGroupSection]` 无需扩展替身即可在两处环境（真实 pydantic SDK 与替身）一致工作。
- 配置期预先拒绝的既有先例是 `config.SnowLumaSection.__init__`（不依赖 pydantic validator，替身环境同样执行）。

## Goals / Non-Goals

**Goals:**
- 分组判定只有一处定义：来源群号与目标群号成对进入，返回所属分组名（日志与拒绝原因定位）或空。
- 所有消费方（预检、源/评论复验、自动入口筛选与列举、热更新重验）切换到同一口径，无第二份实现。
- 配置期完成全部结构校验；运行期不修补结构错误。
- `name` 只以「压平后的定位文本」形态进入日志与结果，不改判定的任何输入。

**Non-Goals:**
- 不改直连协议、七动作、账本 schema 与四字段键；不动客户端/传输层。
- 不做配置自动迁移工具（手工改写，README 给示例）；不保留平铺键的兼容读取。
- 不改宿主 UI Schema 的任何生成逻辑；不新增配置项类型以外的新节。

## Decisions

### D1 判定函数族：成对判定 + 归一视图

`forward_precheck.py` 的公共面改为：

- `forward_group_entries(config) -> tuple[ForwardGroup, ...]`：一次归一全部分组（`name` 去首尾空白；源/目标群号沿用 `whitelist_entries` 口径），返回不可变视图；纯函数，随处可调。
- `source_group_is_allowed(config, group_id)`：语义不变——源群须出现在任一分组（自动入口筛选与复验都要单独判「是不是合法源」）。
- `shared_forward_group(config, source_group_id, target_group_id) -> str | None`：成对判定，命中返回分组名，否则 `None`。`plugin._send_allowed` 与工具预检的目标判定都走它，签名上没有「只判目标」的用法，从形式上杜绝漏绑来源。
- `share_target_groups(config, source_group_id)`（`auto_entry.py`）：改为列举该源所属分组的全部目标（顺序 = 配置顺序，排除源群自身，沿用现有去重）。
- `target_is_allowed` 删除：它的单目标语义在新模型下不存在；不要留一个「只判目标在不在任一分组」的宽松函数诱引误用（那正是要消除的越权面）。

备选：保留 `target_is_allowed(config, target, source_group_id=...)` 加关键字参数。否决——漏传即静默退化为全量判定，编译期无感知；成对签名让漏绑不可能。

### D2 结构校验落 `ScopeSection.__init__`

`config.py` 新增 `ForwardGroupSection`（`name: str`、`source_groups: list[str]`、`target_groups: list[str]`）与 `ScopeSection(groups: list[ForwardGroupSection])`；`ScopeSection.__init__` 在 SDK 字段校验前完成三项检查并抛中文 `ValueError`：源群跨组重复、同组内源群重复、`name`（去空白）为空或跨组重复。空目标组与组内源=目标不校验（规格允许保存，运行期拒绝）。

备选：pydantic `model_validator`。否决——真实 SDK（pydantic v2）与测试替身行为会分叉，替身不执行 validator；`__init__` 是两处环境都经过的路径，`SnowLumaSection` 已确立该先例。

### D3 `name` 的宽松校验与日志压平

校验仅两条：去首尾空白后非空、跨组唯一；不限长、不限字符集（阶段一明确选择）。日志与结果只使用压平空白与控制字符后的 name（`shared/identifiers.py` 已有压平能力则复用，不新增第二套），不改判定输入。含换行/控制字符的 name 不得破坏日志单行结构。

### D4 默认值、版本与仓库样例

- `ScopeSection.groups` 默认 `[]`；`config.toml` 的 `[scope]` 写 `groups = []` 加注释示例（注释形态的 `[[scope.groups]]` 块，不生效）。
- `plugin.config_version` 1.5.0 → 1.6.0；`config.py` 与 `config.toml` 默认逐项一致（既有规则）。
- 平铺键 `source_groups` / `target_groups` 直接移除，不做忽略式兼容；宿主按未知键处理时的行为不依赖本插件。旧配置升级后表现为「源群不在任何分组」的可定位拒绝（安全失效方向：不发送）。

### D5 拒绝措辞与日志前缀

目标跨组拒绝统一为「目标群不在与来源群相同的转发分组内」；源群不在任何分组沿用「调用来源群不在任何转发分组内」。投递任务描述与 `_finish` 日志在分组名可得时带 `分组=<name>` 前缀（压平后）。

### D6 热更新无新增逻辑

`on_config_update` 现有行为（只取消失效自动任务）不变：分组收紧后，等待中的候选经 `auto_entry_invalid_reason`（改调同组列举）失效取消；在途投递由 `_send_allowed`（改调成对判定）在源/评论提交前拦截。均复用既有复验机制，不加新的监听或撤销路径。

## Risks / Trade-offs

- [升级后旧键失效被误读为「白名单还在但发不出」] → 版本提升 + README/CHANGELOG 明示 Breaking；两类拒绝原因都带「转发分组」字样可直接定位。
- [成对判定漏接一个消费点造成越权投递] → 单点定义 + 每个消费方一条跨组拒绝用例；预检与复验不共享中间缓存，均从 `self.config` 现算。
- [`name` 无字符限制污染日志结构] → 仅压平后入日志/结果，判定不用 name；日志断言覆盖含换行 name。
- [测试夹具大范围迁移期间假绿] → 切换顺序为先建分组结构与判定（旧夹具先红）→ 逐消费方切换 → 夹具迁移；每步跑全量，禁止先改测试适配实现。
- [平铺键移除后可回退性差] → 回滚路径＝先把仓库配置示例与线上配置写回平铺键再降级代码；旧代码不认 `groups` 键，分组配置在旧代码下按空分组安全失效（无源无目标），不会误发。

## Migration Plan

1. 代码侧按顺序切换：`config.py` 结构 + 校验 → `forward_precheck.py` 判定族 → `plugin._send_allowed` 与任务描述 → `auto_entry.py` 列举与重验 → 测试夹具迁移 → 文档（README、CHANGELOG）。
2. 无数据迁移：账本、`data_dir` 状态、直连配置均不动；分组变化只影响路由判定。
3. 部署时操作者手工把 `[scope]` 改写成 `[[scope.groups]]` 块；空白名单用户无需操作（默认空分组等价）。

## Open Questions

无。阶段一拍板项（形态、重叠规则、校验层次、`name` 语义）已全部决议，无遗留可延后项。
