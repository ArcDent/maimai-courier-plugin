# 第7组可逆隔离记录

核对日期：2026-10-04。范围仅为直连变更任务7.1–7.4，不代表第8组交付或真实QQ联验。

## 基线与恢复依据

原 [MANIFEST.md](MANIFEST.md) 的109项（108个普通文件、1个相对软链接）全部保留，
总计1,633,141字节。逐项核对路径、用途、SHA-256、字节数和完整内容；
`CLAUDE.md → AGENTS.md` 按链接目标文本核对。MANIFEST自身未改写，其SHA-256为
`7f8f26383872fab09163b99d13833765723d776e9db3eec533b1041ab844e582`。
参考提交仍为 `2d7f4a585222a3dc2f9d57e86baf7c9f925c3c78`，不假定未跟踪文件已被Git保存。

下列65项、985,739字节在退出原路径前，均已逐字节确认与本目录同相对路径的完整内容
相同，并再次确认其摘要与MANIFEST一致。因此直接使用已存在的完整历史副本作为可逆
搬移目标；没有用摘要替代内容，也没有用当前文件反向覆盖基线。本次待隔离项没有
后续增量，故不创建虚假的增量副本。活跃业务此前改写的文件继续留在原路径，历史原版不变。

恢复单项时可从本目录同相对路径复制完整原内容，核对MANIFEST后恢复；墓碑同样可被
原版替换。恢复源码不等于获准部署、启用旧发送或回滚账本。本目录不是第二套可部署产品。
只核对源码、公开样例与说明，没有访问真实状态、凭证、运行配置或远端；未发现真实凭证
标记，旧测试里的假凭证属于合成夹具。没有Git写操作，没有不可逆删除。

## 退出原路径的逐项清单

每项原用途及原SHA-256均见MANIFEST同名项；以下去向是最终隔离决定，不修改原清单的
第1组历史事实。`remote_client.py` 与 `service/__init__.py` 原内容保留于此，原路径改为
无导入、无发送能力的退役墓碑；其余列出的原路径撤出，仅在历史保留完整源码。

- `pyproject.toml`：专属服务运行环境，随历史隔离，插件依赖仍仅由清单声明。
- `remote_client.py`：旧HTTP客户端，原位置只留墓碑。
- `service/__init__.py`：旧服务包入口，原位置只留墓碑。
- `service/__main__.py`：旧模块CLI入口。
- `service/auth.py`：旧服务认证。
- `service/backends.py`：旧服务后端边界。
- `service/cli.py`：旧启动、迁移与导出命令。
- `service/config.py`：旧服务受控配置。
- `service/coordinator.py`：旧服务协调器。
- `service/gateway.py`：旧服务发送网关。
- `service/http_api.py`：旧HTTP路由。
- `service/ledger_migration.py`：旧服务账本迁移。
- `service/ledger_store.py`：旧服务账本仓储。
- `service/materials.py`：旧服务材料转换。
- `service/onebot.py`：旧服务网络传输。
- `service/server.py`：旧HTTP服务器。
- `service/sessions.py`：旧会话和阶段授权。
- `shared/ledger.py`：仅旧服务与专属测试使用的仓储协议。
- `shared/protocol.py`：仅旧服务与专属测试使用的控制协议。

以下测试逐项以AST导入及实际公共测试入口确认属于旧产品，而非仅凭文件名前缀选择：

- `tests/test_acceptance_channels.py` → `tests.client_support`、`tests.service_support`、旧验收报告。
- `tests/test_acceptance_faults.py` → 旧客户端/服务替身及 `shared.protocol`。
- `tests/test_acceptance_isolation.py` → `service.coordinator/http_api`、旧服务与跨进程替身。
- `tests/test_acceptance_processes.py` → 旧跨进程替身及 `shared.protocol`。
- `tests/test_acceptance_rollback.py` → 旧服务CLI与服务账本替身。
- `tests/test_remote_client.py` → `remote_client`、`service.config`、旧客户端替身。
- `tests/test_remote_client_transport.py` → 旧HTTP客户端及 `service.server`。
- `tests/test_service_auth.py` → `service.auth/backends/cli/config/http_api`。
- `tests/test_service_binding.py` → `remote_client` 服务绑定与迁移收据。
- `tests/test_service_cancel.py` → 旧服务替身及 `shared.protocol`。
- `tests/test_service_capacity.py` → `service.coordinator/http_api` 及旧服务替身。
- `tests/test_service_card_send.py` → `service.gateway/materials`。
- `tests/test_service_cli.py` → `service.cli`。
- `tests/test_service_comment_send.py` → `service.gateway`。
- `tests/test_service_comment_stage.py` → 旧服务阶段授权替身。
- `tests/test_service_facilities.py` → 旧服务替身及 `shared.ledger`。
- `tests/test_service_gateway_connection.py` → 旧网关、配置、传输、账本迁移与CLI。
- `tests/test_service_http.py` → `service.backends/http_api` 及旧控制协议。
- `tests/test_service_http_server.py` → `service.cli/config/server`。
- `tests/test_service_image_send.py` → `service.gateway/materials`。
- `tests/test_service_ledger_attempts.py` → 旧账本仓储、控制协议与服务替身。
- `tests/test_service_ledger_comments.py` → 旧账本仓储、控制协议与服务替身。
- `tests/test_service_ledger_datasets.py` → 旧账本初始化、仓储与控制协议。
- `tests/test_service_ledger_export.py` → 旧服务CLI、迁移、仓储与控制协议。
- `tests/test_service_ledger_migration.py` → 旧迁移、仓储与控制协议。
- `tests/test_service_ledger_migration_failures.py` → 旧迁移、仓储与控制协议。
- `tests/test_service_ledger_recovery.py` → 旧迁移、仓储与控制协议。
- `tests/test_service_ledger_source.py` → 旧仓储与控制协议。
- `tests/test_service_ledger_store.py` → 旧迁移、仓储与服务仓储协议。
- `tests/test_service_onebot_transport.py` → `service.config/onebot`。
- `tests/test_service_plain_text.py` → `service.gateway/materials`。
- `tests/test_service_policy.py` → 旧服务策略与会话替身。
- `tests/test_service_prepare.py` → 旧服务准备与控制协议。
- `tests/test_service_query.py` → 旧服务查询与控制协议。
- `tests/test_service_receipts.py` → `service.gateway`。
- `tests/test_service_send_classification.py` → `service.gateway/materials`。
- `tests/test_service_sessions.py` → `service.sessions`。
- `tests/test_service_shutdown.py` → `service.cli/coordinator`、旧控制协议与服务替身。
- `tests/test_service_source_attribution.py` → `service.gateway/materials/onebot`。
- `tests/test_service_source_stage.py` → 旧阶段授权协议与服务替身。
- `tests/test_shared_protocol.py` → 旧会话/挑战/阶段授权、仓储协议及旧发送请求字段。

5个专属支持模块一并隔离：

- `tests/acceptance_support.py` 仅被上述3个旧验收模块使用，依赖旧服务阶段与查询。
- `tests/client_support.py` 仅被上述旧客户端和验收模块使用，装配旧HTTP服务。
- `tests/service_support.py` 消费旧协调器、网关、仓储、会话与HTTP路由；其消费者均在上述清单内。
- `tests/cross_process_support.py` 仅被旧验收、旧服务支持与子进程驱动使用；含旧服务CLI启动。
- `tests/client_process.py` 由前一模块按文件路径启动；其动态仓库加载及旧HTTP作业入口已核对，
  不是仅看AST顶层import就推断无人使用。

## 保留的活跃依赖

`tests/test_shared_types.py` 保留，其导出身份、四字段键和结果测试与新插件仍直接相关。
`tests/support.py`、`tests/direct_support.py`、`tests/snowluma_client_support.py`、
`tests/snowluma_transport_support.py` 及SDK替身均保留；没有活跃消费者引用上列旧支持模块。

共享纯逻辑保持原模块单点定义：

- `shared/clock.py` → 根 `clock.py` 重导出，直连传输/客户端复用。
- `shared/delivery.py` → 活跃账本、编排及共享聚合导出同一个 `DeliveryKey`/状态类型。
- `shared/result.py` → 编排、客户端、插件及聚合导出同一结果类型与渲染函数。
- `shared/mentions.py`、`shared/identifiers.py` → 材料转换、预检、自动候选/客户端复用。
- `shared/config_rules.py` → 活跃配置与直连传输复用。
- `shared/send_steps.py` → 直连客户端、材料与编排使用第4组已有请求与准备类型。

`shared/__init__.py` 退出服务协议聚合；`conftest.py` 清理旧客户端、旧仓储/协议及无需
加载的宿主墓碑顶层别名，只对活跃测试的既有导入建立单实例别名。独立公共加载测试不使用
conftest，不注入历史路径；`host_send.py` 既有退役墓碑原样保留。

`config.py` 仅撤出过渡 `ServiceSection`，完整类原文已在基线保存；直连节、默认值、校验和
旧配置诊断均不变。`pytest.ini` 与 `pyrightconfig.json` 显式排除历史，活跃测试不按名称
批量忽略；类型检查仍覆盖宿主墓碑，同时纳入两个直连生产模块。

## 旧归档任务保护

隔离前记录、隔离后复核以下任务文件完整字节不变：

- `2026-10-02-move-forwarding-to-snowluma-service/tasks.md`：
  `d9ffd24bb963b6f7736c2c32ecc09cbc3f04757ec1467807a0a4d450cb028869`。
- `2026-09-30-support-mentions-and-configurable-auto-sharing/tasks.md`：
  `01bdd8dc98c6b0c3bd2a023d6ee2874a37a41186f6e2c603d09935a43177259e`。
- `2026-09-29-introduce-maimai-courier/tasks.md`：
  `a0eb6a7f88a014809f55e753634139a69147c3ac876e60717e9ea18b712720d8`。

以上路径相对 `openspec/changes/archive/`。旧真实联验状态不改写，源码保留与离线测试
均不是部署或QQ联验证据。第8组的运营文档、规则说明、完整场景交付另行处理。

## 本组红绿记录与最终验证

新增 `tests/test_isolation.py` 的7个测试节点，入口为公共工厂、公开导出、真实pytest收集
CLI、旧CLI退役及基线文件内容；不执行历史业务。

- 首个公共加载测试红灯：`create_plugin` 的导入经共享聚合进入旧 `shared.ledger`；
  移除旧协议聚合后，该测试及既有共享类型测试为8 passed。
- 收集测试红灯：根默认收集进入历史，出现48个模块路径冲突；显式 `tests/` 仍包含
  已退役产品的测试。可逆隔离并设置默认收集范围后，公共加载/收集/共享/插件为21 passed。
- 配置公开面红灯：`ServiceSection` 仍可导出；仅撤出该过渡定义后，配置/隔离/共享
  测试为63 passed。
- 补充完整性、导入图与CLI证据后，隔离/共享/插件为25 passed。独立解释器中进一步
  核对时钟、投递键、结果、提及、标识符与准备类型的对象身份，不依赖pytest别名。
  补充身份断言时曾误用预检未导出的 `safe_identifier`，已改为其实际导出的
  `verbatim_identifier`，没有为测试增加生产接口。

以下均于2026-10-04实际运行，全部退出码0；依赖仅由uv临时提供：

- `uv run --no-project --with pytest pytest tests/ -q`：777 passed，5 skipped。
- `uv run --no-project --with pytest --with aiohttp==3.14.3 pytest tests/ -q`：815 passed。
- `uv run --no-project --with pytest pytest -q`：777 passed，5 skipped。
- `uv run --no-project --with pytest pytest tests/ --collect-only -q`：781 tests collected。
- `uv run --no-project --with pytest --with aiohttp==3.14.3 pytest --collect-only -q`：815 tests collected。
- `uv run --no-project --with pyright pyright`：0 errors，0 warnings，0 informations。
- 同一命令加 `--outputjson`：filesAnalyzed=22，errorCount=0。

活跃区共14个测试文件；没有网络库时，34个真实传输测试所在模块按既有约定在导入期
跳过，另4个依赖真实传输的节点运行时跳过，因此测试摘要显示5 skipped；纯配置、
客户端逻辑、共享类型、账本与新隔离检查均收集。带网络库后815个节点全部执行，
收集检查确认没有历史服务节点、没有遗漏任何活跃测试文件。上述数值以本次输出为准，
不套用先前分组筛选命令的统计。

类型检查覆盖13个根模块、8个共享模块和服务包退役墓碑，共22个生产文件；其中
`host_send.py`、`remote_client.py`、`service/__init__.py` 仅为说明性墓碑。
历史区域明确排除，未从历史导入补全类型，未通过忽略活跃文件消除错误。
