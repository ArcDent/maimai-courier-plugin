## 1. 类型检查模块（单点判定）

- [x] 1.1 新建根模块 `type_check.py`：公开导出卡片内容类型检查（输入 `get_forward_msg` 展开结果的节点序列，输出拒绝原因或空串）与固定对外措辞、reason 常量；当前只判「节点 content 含其它合并转发卡片」一种
  - seam：`type_check` 模块公开导出（纯函数，无 IO、无传输/客户端依赖）
  - 验收：`tests/test_type_check.py` 覆盖：嵌套命中返回固定原因与措辞、非嵌套返回空串、空展开返回空串；`pytest tests/test_type_check.py` 全过
- [x] 1.2 畸形输入稳定性：段不是字典、`data` 不是映射、标识字段缺失或类型错误时稳定放行或拒绝，不抛异常、不改写输入
  - seam：同上
  - 验收：参数化畸形用例全过；输入对象零变更（深拷贝比对）；模块不 import 任何生产模块（当前仅标准库）

## 2. 工具路径发送前拒绝

- [x] 2.1 `snowluma_client.prepare_source` 卡片分支：展开查询成功后先做类型检查，命中即返回 `PrepareRejected(reason, 固定措辞)`，不进入 `build_card_nodes`
  - seam：`SnowLumaClient.prepare_source` 公开方法（`tests/test_snowluma_client.py` 既有假 transport 夹具）
  - 验收：假 transport 展开结果含嵌套 forward 段 → `PrepareRejected.reason` 与 `type_check` 常量一致、detail 为固定措辞；该用例内发送动作计数为 0
- [x] 2.2 既有分支行为不回退：非嵌套卡片照常构造节点材料；展开失败、结构校验不通过、顶层混排的既有拒绝措辞与分类不变
  - seam：`prepare_source` 同 2.1；`tests/test_forward_precheck.py` 内既有 prepare 用例
  - 验收：既有相关用例零改断言全过；新增「非嵌套不误拒」用例（节点含 reply/at/image 均放行）

## 3. 自动入口只读预识别

- [x] 3.1 `snowluma_client.py` 新增只读方法 `card_content_rejection(message_id) -> str | None`：`get_msg` 校验并取平台卡片标识 → `get_forward_msg` 展开 → 交 `type_check` 判定；命中返回原因，未命中或查询失败返回 None
  - seam：`SnowLumaClient` 公开方法（`tests/test_snowluma_client.py` 假 transport）
  - 验收：命中返回固定原因；展开无嵌套返回 None；`get_msg`/`get_forward_msg` 失败、信封非法、超时均返回 None 且不抛；方法体内零发送动作调用
- [x] 3.2 `tests/direct_support.py` 增补内存平台夹具：展开结果含嵌套 forward 段的卡片、可触发展开失败的内存传输开关
  - seam：`tests/direct_support.py` 公开导出（测试公共装配）
  - 验收：既有 direct_support 消费者（test_auto_entry / test_plugin / test_direct_acceptance 等）零改动通过；新夹具可表达「嵌套」与「展开失败」两种结果
- [x] 3.3 `auto_entry.run_auto_entry` 新增可选参数 `presearch: Callable[[ForwardCandidate], Awaitable[str | None]] | None`：执行时机为可读性确认与配置初检之后、目标名称查询之前；仅对合并转发候选调用；命中返回 `WakeOutcome(triggered=False, 可定位原因)`；调用异常按失败放行处理
  - seam：`auto_entry.run_auto_entry` 公开函数 + 插件观察 Hook 组件入口（`tests/test_auto_entry.py` 既有 `wake_once`/`drive_observe_hook` 驱动方式）
  - 验收：注入命中 → `proactive.trigger` 零调用且目标名称零查询；注入返回 None → 照常唤起一次；text/image 候选 presearch 零调用；不传 presearch（默认 None）时既有全部用例零改断言通过
- [x] 3.4 `plugin.py` 组合根构造并注入 presearch：闭包内对 forward 候选调用 3.1 方法；客户端按当前配置惰性创建（端点不可解析或创建失败即跳过预搜索），创建后与工具路径复用同一客户端
  - seam：插件公共工厂 `create_plugin` 与观察 Hook 入口（`tests/test_plugin.py` / `tests/test_auto_entry.py` 的 `make_direct_plugin`）
  - 验收：内存平台返回嵌套展开 → 观察 Hook 后 `proactive.trigger` 零调用且有可定位日志；内存平台展开失败 → 照常唤起一次；无端点配置时的插件 → 照常唤起、不报错、无 transport 创建

## 4. 生命周期与预算回归

- [x] 4.1 卸载五秒预算：预搜索只读查询在途时触发卸载，查询被取消、任务有界退出、不留悬挂会话
  - seam：`tests/test_lifecycle.py` 既有卸载驱动方式
  - 验收：慢响应预搜索场景卸载仍在 5 秒内收尾并逐项告警定位；卸载后无新增常驻任务
- [x] 4.2 热更新竞态：预搜索进行中候选类型被移除、开关关闭或分组收紧，按既有口径取消或不唤起；仅改标准不取消仍合法候选
  - seam：`tests/test_lifecycle.py` 既有热更新驱动
  - 验收：失效候选零唤起；仅改标准的候选仍按最新标准唤起（既有用例不回退）

## 5. 文档与实测证据

- [x] 5.1 `AGENTS.md` 同步：卡片契约改「嵌套发送前拒绝 + 固定措辞」、`auto_entry` 增加预搜索职责（只读、fail-open、时机、仅 forward 候选）、新增 `type_check` 模块职责（判定族单点、当前仅一种、扩展经同一点）
  - seam：文档一致性自查（全文检索旧措辞）
  - 验收：AGENTS.md 无「嵌套…原样提交」「展示不作保证」残留；`CLAUDE.md` 仍为 `AGENTS.md` 相对软链接
- [x] 5.2 `README.md`：用户可见变化（含嵌套聊天记录的卡片被明确拒绝及固定措辞；自动入口对该类候选不唤起；其它已实测有问题的类型本轮不阻断、留待以后）
  - 验收：支持/拒绝类型段落与新契约一致；不含真实群号、账号与凭证
- [x] 5.3 `CHANGELOG.md`：条目记录行为契约变化（标注 BREAKING）与回滚方式
  - 验收：条目含日期、变更点、影响面与回滚说明
- [x] 5.4 `docs/snowluma-http-api-catalog.md` 第 6 节新增实测记录：2026-10-06 三通道对比（旧引用空壳 / 新引用空壳且丢 reply / 拍平结构丢失）与 16 类型 × 3 路径矩阵结论（`xml`/`share`/`contact` 节点内容 seq=0、`poke`/`market_face` 拒收、share/location/music/contact 平台转 json、image 走 url 可用）
  - 验收：结论可溯源到 `.local/report/nested-forward-probe-*.json` 与 `.local/report/segment-type-matrix-*.json`；不含正文、凭证与完整资源标识
- [x] 5.5 `docs/real-qq-validation.md`：U6 记录「嵌套内层客户端降级空壳、与资源新旧无关」结论与自动入口预识别行为；V7 场景描述改拒绝口径；未执行项维持未确认标注
  - 验收：U6/V7 与新 delta 一致；不把单实例快照写成全量联验完成

## 6. 验证与收尾

- [x] 6.1 默认档全量回归并记录实际结果：`uv run --no-project --with pytest pytest tests/ -q`
  - 验收：零失败；实跑 `672 passed, 2 skipped`（2026-10-07，已同步 AGENTS.md / README.md / CHANGELOG.md）
- [x] 6.2 传输与跨进程档：`uv run --no-project --with pytest --with aiohttp pytest tests/ -q`
  - 验收：零失败；实跑 `769 passed`（无 skipped，已同步三份文档）
- [x] 6.3 `uv run --no-project --with pyright pyright` 生产代码零错误
  - 验收：`0 errors, 0 warnings, 0 informations`；filesAnalyzed=19（基线 18 + 1：`type_check.py` 已加入 `pyrightconfig.json` include）
- [x] 6.4 `openspec validate nested-card-type-check --strict` 通过；spec ↔ design ↔ tasks ↔ 实现逐项自查
  - 验收：valid（`Change 'nested-card-type-check' is valid`）；四个修改需求的新增场景均有测试/实现落点：支持消息类型与完整性「嵌套卡片发送前拒绝」→ `type_check.unsupported_card_content` + `snowluma_client._prepare` 卡片分支插入（`tests/test_type_check.py`、`tests/test_snowluma_client.py:209,226`、`tests/test_direct_acceptance.py:817`）；自动入口「嵌套卡片候选不唤起」/「预搜索不可用不阻断」→ `auto_entry._card_presearch_rejection` + `plugin._presearch_card_content` + `snowluma_client.card_content_rejection`（`tests/test_auto_entry.py:1938,1955,2229,2252,2269`、`tests/test_direct_acceptance.py:848`）；snowluma-direct-client「嵌套卡片不提交」→ 同拒绝链路（`tests/test_snowluma_client.py:209`、`tests/test_direct_acceptance.py:817`）；「自动入口预搜索有界只读」→ `card_content_rejection` 走 `_bounded` 同一预算、零发送动作、取消照常传播（`tests/test_lifecycle.py:751,807,832`、`tests/test_auto_entry.py:1955`）
- [x] 6.5 未触碰范围自查：固定七动作、账本四字段键与防重状态机、普通文字/图片路径、`send_material` 材料构造无行为变化（该文件仅两处 docstring 同步新契约措辞，无行为变更）
  - 验收：`git diff af72eb65 --stat` 仅含本变更预期文件（9 个存量修改 + 6 个测试 + `pyrightconfig.json`，另新增 `type_check.py`、`tests/test_type_check.py`、`docs/adr/0011`、openspec 变更目录四个未跟踪项）；`snowluma_transport.py`（七动作）、`delivery_ledger.py` 与 `shared/`（四字段键与防重）、`send_material.py`（材料构造）、`forward_precheck.py`、`forward_delivery.py`、`config.py`、`source_binding.py` 无 diff；`snowluma_client.py` 改动限于卡片链路类型检查插入与新增只读 `card_content_rejection`（仅用既有 `get_msg`/`get_forward_msg` 两动作），`auto_entry.py`/`plugin.py` 限于预搜索缝隙与组合根注入，发送分派逻辑零改动
