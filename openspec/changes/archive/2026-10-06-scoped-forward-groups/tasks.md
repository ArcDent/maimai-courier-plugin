# Tasks: 2026-10-06-scoped-forward-groups

## 1. 配置结构与配置期校验

- [x] 1.1 新增 `ForwardGroupSection` 配置模型（`name: str`、`source_groups: list[str]`、`target_groups: list[str]`，UI 元数据 label/hint/order 齐全，注释中文）
  - seam：`config.ForwardGroupSection` 经 `config.CourierConfig(scope={"groups": [dict]})` 构造
  - 验收：合法 dict 构造成功，`model_dump()` 产出且仅含 name/source_groups/target_groups 三键；默认 `groups=[]`
- [x] 1.2 `ScopeSection` 收敛为单个 `groups` 字段，删除平铺 `source_groups`/`target_groups`
  - seam：`config.CourierConfig()` 与 `get_components()` 组件声明
  - 验收：`CourierConfig().scope.model_dump()` 只含 `groups` 键；`get_components()` 仍正常返回一个 Tool 两个 Hook，无 schema 报错
- [x] 1.3 `ScopeSection.__init__` 实现三项配置期校验，抛中文 `ValueError`：源群跨组重复、同一分组内源群重复、`name` 去空白后为空或跨组重复
  - seam：`config.CourierConfig(scope={"groups": [...]})` 构造路径
  - 验收：四类非法输入各抛一条中文原因（跨组源重复点出群号与组名、组内源重复、name 空白、name 跨组重复）；合法多分组不抛；遵循 `SnowLumaSection.__init__` 先例不依赖 pydantic validator
- [x] 1.4 确认空目标分组与组内「源 = 目标」可保存，不在此层校验
  - seam：`config.CourierConfig(scope={"groups": [{"name": ..., "source_groups": ["1"], "target_groups": []}]})`
  - 验收：两类配置均构造成功；注释说明运行期由「目标 ≠ 来源群自身」与同组无人目标拒绝
- [x] 1.5 提升 `plugin.config_version` 1.5.0 → 1.6.0，`config.toml` 的 `[scope]` 改为 `groups = []` 并附注释形态 `[[scope.groups]]` 示例
  - seam：`CourierConfig().plugin.config_version` 与 `config.toml` 文本
  - 验收：`config.py` 与 `config.toml` 默认逐项一致均为 1.6.0；示例用标准 tomllib 解析不报错且不产生实际分组
- [x] 1.6 迁移 `tests/test_config.py` 断言并补齐结构校验用例
  - seam：`pytest tests/test_config.py`
  - 验收：原 `set(config["scope"])` 平铺断言改为 groups 断言；新增用例覆盖 1.3 四类拒绝 + 1.4 两类可保存 + 合法多分组；默认命令下该文件全绿

## 2. 分组判定单点定义与消费方切换

- [x] 2.1 新增 `forward_group_entries(config) -> tuple[ForwardGroup, ...]` 归一视图（`name` 去首尾空白；群号复用 `whitelist_entries` 口径；保持配置顺序；纯函数）
  - seam：`forward_precheck.forward_group_entries(config)`
  - 验收：name " a " → "a"、群号空白/None/字符串逐字符/非字符串化与既有 `whitelist_entries` 行为一致；组序保持配置序；输入不被原地修改
- [x] 2.2 新增成对判定 `shared_forward_group(config, source_group_id, target_group_id) -> str | None`：同组返回该组 `name`（去空白原值），否则 `None`
  - seam：`forward_precheck.shared_forward_group(config, source, target)`
  - 验收：同组命中返回组名；目标只在别组、不在任何分组、群号不在任何源组均返回 None；不抛异常、不做查询
- [x] 2.3 `source_group_is_allowed` 改为「源群在任一分组」语义；删除 `target_is_allowed`
  - seam：`forward_precheck.source_group_is_allowed(config, group_id)`
  - 验收：源群在任一分组返回 True、否则 False；生产代码与测试均无 `target_is_allowed` 引用
- [x] 2.4 `precheck_forward_request` 切换目标判定为 `shared_forward_group`，源群判定改分组措辞
  - seam：`forward_precheck.precheck_forward_request`（ctx 替身，不联网）
  - 验收：目标跨组拒绝措辞「目标群不在与来源群相同的转发分组内」；源群无分组措辞「调用来源群不在任何转发分组内」；「目标 = 来源群自身」仍在同组判定前被拒；任何失败均无发送副作用
- [x] 2.5 `plugin.py` 的 `_send_allowed`（源与评论复验共用）改调 `shared_forward_group`
  - seam：`plugin.create_plugin()` 公共工厂 → 工具调用链（`_send_allowed` 经源/评论提交前复验暴露）
  - 验收：分组收紧（目标被移出源组）后评论提交前复验失败，按明确未发送拒绝、不发送评论；源已成功的不回滚；预检与复验均从 `self.config` 现算，不共享缓存
- [x] 2.6 投递任务描述与 `_finish` 日志在可得时带压平后的 `分组=<name>` 前缀
  - seam：`plugin` 任务注册描述与 logger 输出（沿用 test_plugin/test_lifecycle 既有日志断言方式）
  - 验收：分组名含换行/控制字符时日志为单行且不含裸控制字符；`name` 不进入判定输入；无分组名可用时不加前缀
- [x] 2.7 `auto_entry.share_target_groups` 改列举来源群所属分组的全部目标；`auto_entry_invalid_reason` 的「没有合法目标群」改同组口径
  - seam：`auto_entry.share_target_groups(config, source_group_id)` / `auto_entry.auto_entry_invalid_reason(config, source_group_id, message_type)`
  - 验收：列举只含同组目标，保持配置顺序、排除源群自身、沿用去重；源群无同组目标时 invalid_reason 非空且措辞可定位；源群不在任何分组返回空列举
- [x] 2.8 `tests/support.py` 的 `enabled_config` 增加分组构造入口，既有调用点参数保持可用
  - seam：`tests.support.enabled_config`（默认命令收集的全部夹具调用方）
  - 验收：默认单分组含 `SOURCE_GROUP_ID` 与 `TARGET_GROUP_ID`；既有 `source_groups=`/`target_groups=` 关键字仍可用且落入同一默认分组；新增 `groups=` 显式构造入口

## 3. 测试用例与全量校验

- [x] 3.1 `tests/test_forward_precheck.py` 补齐判定族与预选用例
  - seam：`forward_precheck` 判定函数 + `precheck_forward_request`
  - 验收：覆盖同组通过、跨组拒绝（目标只在别组/不在任何分组）、目标群跨组重复可投、源群兼作别组目标、「目标 = 源群自身」照旧拒绝、`forward_group_entries` 归一边界（空白/字符串逐字符/None）
- [x] 3.2 `tests/test_auto_entry.py` 补齐列举、筛选与热更新用例
  - seam：`auto_entry.screen_forward_candidate` / `share_target_groups` / `auto_entry_invalid_reason`
  - 验收：同组目标列举正确；同组无合法目标不提供判断机会；等待中目标被移出组触发失效取消（沿用假时钟推进）；源群不在任何分组不产候选
- [x] 3.3 `tests/test_lifecycle.py` 与 `tests/test_plugin.py` 补齐复验与日志用例
  - seam：`create_plugin()` 工厂 + 工具调用链 + `_send_allowed` 行为
  - 验收：在途投递评论复验遇分组收紧按 rejected 报告且无发送；`on_config_update` 只取消失效自动任务的行为回归不破坏；任务描述含压平分组名
- [x] 3.4 `tests/test_direct_acceptance.py` 端到端用例（真实 aiohttp 回环端点）
  - seam：插件公共 Hook/Tool 入口 + 回环 HTTP 传输 + 账本互证
  - 验收：同组端到端成功并落账；跨组在发送前拒绝且传输层零请求（回环端点无调用记录）；需 `--with aiohttp` 才覆盖
- [x] 3.5 迁移其余引用 `enabled_config` 的夹具调用点，实跑默认全量命令
  - seam：`uv run --no-project --with pytest pytest tests/ -q`
  - 验收：全绿并报告实际数字（AGENTS.md 验证节写入由 4.4 负责）
- [x] 3.6 实跑 aiohttp 全量命令
  - seam：`uv run --no-project --with pytest --with aiohttp pytest tests/ -q`
  - 验收：全绿；记录实际数字；确认 3.4 用例在该命令下真正执行而非跳过
- [x] 3.7 实跑静态检查
  - seam：`uv run --no-project --with pyright pyright`
  - 验收：生产代码零错误；记录 filesAnalyzed 实际值；`forward_group_entries` 等新公共函数有类型标注

## 4. 文档与收尾

- [x] 4.1 README 配置节改写为转发分组说明
  - seam：README.md 配置章节与功能描述
  - 验收：含 `[[scope.groups]]` 结构示例、「同组才能互投」规则、Breaking 升级提示（平铺键移除、需手工改写）、默认空分组等价空白名单
- [x] 4.2 CHANGELOG 新增条目
  - seam：CHANGELOG.md
  - 验收：条目含日期 2026-10-06、BREAKING 标记、行为变化（同组判定、name 语义、配置期校验）
- [x] 4.3 同步 `docs/direct-snowluma-operations.md` 与 `docs/real-qq-validation.md` 的白名单描述
  - seam：两份文档的白名单相关段落
  - 验收：措辞统一为转发分组口径；联验清单按需补充分组判定联验项（未授权前不得宣称已联验）
- [x] 4.4 收敛 AGENTS.md
  - seam：AGENTS.md 的 config.py/config.toml、forward_precheck、auto_entry 相关行与验证节
  - 验收：判定函数名（`shared_forward_group`/`forward_group_entries`）、`[scope]` 描述、自动入口合法目标范围均为分组口径；验证数字为 3.5/3.6/3.7 实跑值
- [x] 4.5 核对 delta 与主 spec 的一致性，并向用户提示 sync/archive 决定
  - seam：`openspec/specs/cross-group-forward/spec.md` 与 `openspec validate`
  - 验收：主 spec 中「空白名单/两级白名单」等本变更未直接覆盖的旧措辞逐条核对并显式记录差异；`openspec validate "<change>" --strict` 通过；由用户决定 `/openspec-sync-specs` 或 `/openspec-archive-change`
