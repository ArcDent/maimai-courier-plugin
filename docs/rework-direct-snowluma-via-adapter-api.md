# 历史方向报告：经适配器 API 的方案已被取代

> **当前方向由 [ADR 0007](adr/0007-direct-snowluma-websocket-client.md) 确认：插件自己作为
> 正向 WebSocket 客户端直连已有 SnowLuma，不借适配器 API，也不新增独立服务。**
> 下文是 2026-10-02 的原始方向记录，不作为当前实现或部署依据，保留以追溯取舍。
> “零新增连接配置”“删除 aiohttp/新增 api.call”“零迁移适用所有安装”等结论不再成立；
> 活跃配置为默认空的 `snowluma.websocket_url/token_env_var`，网络依赖仍归插件清单，
> 服务切换证据仍闭锁。回执采用记录 ID、目标群、账号、正平台群序列四项核对。
> 图片 `file/base64` 的绝对排除及线上支持推断没有本轮服务端证据；U1–U8 仍未确认。
> 旧代码非删除式保存于 [历史区](../history/snowluma-forward-service/MANIFEST.md)，
> 不执行下文删除建议。当前使用见 [直连运维清单](direct-snowluma-operations.md)，
> 实现证据见归档的 [第8组附件](../openspec/changes/archive/2026-10-04-direct-snowluma-websocket-forwarding/verification/08-offline-acceptance.md)。
> 旧变更的归档状态与真实 QQ 联验未完成事实按原记录保留；本报告不授权同步或归档。

## 原文：返工方向报告：插件经适配器公开 API 直连 SnowLuma

日期：2026-10-02。状态：**方向报告，未改动任何代码、规格或服务器文件**，供新变更流程（阶段一/二）使用。

## 1. 为什么要返工

已实现的变更 `move-forwarding-to-snowluma-service` 把真实发送放进了**独立常驻服务** `service/`：
插件（MaiBot 内）→ 本机 HTTP → `service/` → SnowLuma OneBot。

用户预期的是「**插件 + SnowLuma 服务器**」两件套：SnowLuma 已经是服务器，不需要再引入第三个
运行件。独立服务带来的额外成本在部署时全部暴露：

- 需要为服务单独准备 Python 运行环境（宿主缺 `uv`/`pip`/`python3-venv`）；
- 需要额外的服务配置：账号、端点、监听端口、状态目录、HTTP 凭证、OneBot 凭证；
- 需要一个常驻与自启机制（systemd 或新容器）；
- 需要旧账本迁移（`delivery_ledger.sqlite3` → 服务端账本）与数据集身份绑定。

以上四项都与产品目标无关，只服务于「多一个进程」这个架构选择。

## 2. 目标方向

插件在 MaiBot 内直接调用**已部署适配器公开的 OneBot 动作 API**，借适配器现有的 SnowLuma
连接完成发送：

```
源群 Maisaka → forward_message（插件）
  → ctx.api.call("adapter.snowluma.action.call", action_name=…, params=…)
  → maim-bot-snowluma（现有连接，现有凭证）
  → QQ 目标群
```

收益：**零新增进程、零新增凭证、零新增端点配置、零账本迁移**。部署退化为「同步插件目录 +
重启 MaiBot」。

## 3. 已核实的支撑事实（部署版本 v1.0.3 源码，见 `docs/research/2026-09-30-forward-card-reuse.md`）

- 适配器把 `adapter.napcat.action.call` / `action.call_data` 声明为公开 API
  （`apis/support.py`，`public=True`），`plugin.py` 为每个 `adapter.napcat.*` 生成
  `adapter.snowluma.*` 同名别名；`call_action` 要求 OneBot 响应 `status == "ok"`，否则抛错。
- 适配器公开了专用动作 `adapter.napcat.message.forward_group_single_msg`（按源消息 ID 转发整条）。
- 原卡片直发只需一个 `forward` 段（`data.id` = 原转发资源 ID）：网关侧只要求非空 `resId`，
  不展开、不重新上传内部节点；空 `resId` 按 `MISSING_FIELD` 拒绝。
- 回执核实可用 `get_msg`：`group_id` + `user_id == get_login_info().user_id` +
  正 `message_seq` 三重核对；缺 ID、本地不透明 ID、身份不符一律按未知。
- 宿主 `api.call` 能力已在 `src/plugin_runtime/capabilities/registry.py` 核实；
  跨插件 API 调用走 `_cap_api_call`，目标提供方须为已注册插件。

## 4. 新变更需要核实的契约（只读；建议作为新变更第 1 组）

1. `api.call` 在插件清单中的声明方式与调用签名（能力名 `api.call`；参数 `api_name` /
   `version` / `args`），以及跨插件调用的错误面（提供方未加载、未公开、版本不符、超时）。
2. `action.call` 的返回结构：确认 `data.message_id` 可直接取得，以及失败/异常与
   「未发出 / 可能已发出」的区分能力（对照 gateway 侧的模糊失败语义）。
3. `send_group_msg` 对 `forward` 段的接受形态与目标群参数；是否需要在 params 中显式
   指定 `message_type`/`group_id`。
4. 普通文字/图片与评论经 `action.call` 发送的段形态（`text`；`image` 需 https `url`，
   不得传 `file`/本机路径/`base64://`）；确认图片可重放字段。
5. 回执核实：`get_msg` 的可用性与三重核对字段；查询次数上界。
6. 适配器 API 的**稳定性边界**：它并非为跨插件调用设计的稳定契约，升级可能改名或改参数；
   需在规格中把「依赖适配器公开 API 的哪个版本区间」写清，并明确不可用时的行为（安全拒绝）。

## 5. 保留 / 删除清单

**保留（与当前实现相同语义）**

- 三参数工具 `forward_message`、宿主来源凭据（`maisaka.planner.after_response`）、
  两级白名单、目标≠源群。
- 自动入口只唤起源群 Planner、候选分类、去重、有限等待、最新配置复验。
- 普通消息提及文本化（回到 `send_material.py` 单点）；原卡片内部保留不改写（ADR 0004）。
- 评论文本与固定 0.5 秒延迟、源失败/未知不进评论、不自动补发。
- 结果面 `render_forward_result` 的八态与 `success`/`sent`/`delivered`/`comment_status`/`stage`。
- **插件自己的持久账本重新成为唯一权威**（`delivery_ledger.py` 恢复职责），单实例假设、
  跨重启防重、`unknown` 不降级为可重试失败、成功记录不清除等铁律继续适用。
- 「目标群宿主历史不保证」的公开口径。

**删除 / 退役**

- `service/`（整个独立服务：CLI、HTTP 路由、会话与阶段授权、服务端账本、迁移/导出、OneBot 网关）。
- `remote_client.py`、根 `pyproject.toml`、`[service]` 配置段、`node_modules` 无关项。
- `_manifest.json` 的 `aiohttp` 依赖（不再需要 HTTP 客户端）；**新增 `api.call` 能力**。
- 「准备/源授权/评论授权」两阶段协议、会话心跳、远端撤销、数据集身份与迁移收据
  （`service_binding.json`）——这些只服务于跨进程信任边界。
- 跨进程测试设施（`tests/cross_process_support.py`、`tests/client_process.py`、
  `tests/test_acceptance_processes.py` 等）与 aiohttp 相关用例。

**收缩**

- `shared/`：若只剩插件侧使用，退回仓库根模块，避免为单一消费者保留共享层抽象；
  仅保留确实双向使用的纯逻辑（如结果面、协议常量）。

## 6. 与规划工件的关系（供阶段二处理）

- 新变更（建议名 `forward-via-snowluma-adapter-api`）的 delta 需要：
  - `cross-group-forward`：把「真实发送与自然上下文」「持久防重与不确定结果」「结果与生命周期」
    等需求改回**插件直连适配器 API** 的措辞（发送位置、回执核实、防重位置、生命周期）。
  - `snowluma-forward-service`：**整能力移除**（含 Purpose），并说明迁移路径。
- 旧变更 `move-forwarding-to-snowluma-service`：1–7 组已实现并通过双轴复核、8.1–8.3 已完成、
  **8.4（真实联验）未执行**。它不应作为已交付归档；建议作为被取代（superseded）的历史保留，
  由新变更说明取代关系。ADR 0005 需标注被取代；ADR 0004（原卡片内部保留）继续有效。
- 现有实现（`service/` 等）在返工完成前不要部署：缺少新变更驱动会留下需要再次回退的运行件。

## 7. 风险与取舍

| 风险 | 说明 | 缓解 |
| --- | --- | --- |
| 依赖适配器公开 API 稳定性 | API 非跨插件稳定契约，升级可能变更或下线 | 规格写明依赖的适配器版本区间；不可用时安全拒绝并如实报告，不回退旧通道 |
| 适配器未加载/未公开 API | 目标提供方缺失时调用失败 | 启动与发送前检查可用性，明确拒绝原因 |
| 回执证据强度 | 依赖 `get_msg` 三重核对；缺 ID/不透明 ID 仍须按未知 | 复用现有证据规则，不把受理当成功 |
| 防重回插件 | 需要重新启用插件账本与单实例假设 | 复用 `delivery_ledger.py` 既有语义与测试 |
| 目标历史不保证 | 与现状一致，直连适配器不经宿主发送链 | 文档显式公开，不做补偿 |

## 8. 部署影响（返工完成后）

- 只需把插件目录同步到 MaiBot 插件挂载目录（`maim-bot-core` 以 `/MaiMBot/plugins` 挂载该目录），
  然后重启 `maim-bot-core`；`_manifest.json` 去掉 `aiohttp`、加入 `api.call`。
- 无需新进程、新端口、新凭证；旧账本继续作为防重来源，**不需要迁移**。
- 上线顺序仍建议：先只填白名单并保持总开关关闭 → 确认插件加载与依赖流水线通过 →
  再按用户确认开启总开关 → 按 `docs/real-qq-validation.md` 做真实联验。

## 9. 本轮已做与未做

- **已做**：只读核对部署主机环境（Python 3.13、无 uv/pip/venv 包、Docker 与 compose 结构、
  SnowLuma 监听 3000–3002、插件目录与旧账本现状、旧插件总开关当前为开启）。
- **未做**：未同步任何代码到服务器、未修改服务器任何文件、未重启任何容器、未执行账本迁移、
  未改动本仓库的代码/规格/配置、未提交任何 Git 改动。

## 10. 待用户在新流程中决定

1. 是否确认「插件 → 适配器公开 API → SnowLuma」为本轮唯一方向（替代独立服务）。
2. 是否保留 `service/` 相关代码作为历史（不部署）还是一并删除；ADR/spec 的取代方式。
3. 旧变更 `move-forwarding-to-snowluma-service` 的处置（保留为 superseded / 直接关闭）。
