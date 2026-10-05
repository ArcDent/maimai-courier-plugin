## 1. 配置与端点安全

- [x] 1.1 扩展 `shared/config_rules.py` 的 `onebot_endpoint_rejection`，覆盖 `http`/`https` 端点
  - seam：`shared.config_rules.onebot_endpoint_rejection`（模块导出，纯函数）
  - 验收：`http://公网主机` 拒绝；`http://127.0.0.1:3000` 放行；`https://公网` 放行；含用户信息/查询串/片段/未指定监听地址的 URL 拒绝；既有 `ws`/`wss` 判定结果不变
- [x] 1.2 改造 `config.py`：`SnowLumaSection.websocket_url` 更名为 `http_url`（描述与 hint 改 HTTP 口径），`__init__` 预校验改判新键
  - seam：`config.SnowLumaSection` / `config.CourierConfig`（公开配置模型）
  - 验收：`http_url` 填 `ws://x` 被 `ValueError` 拒绝；合法 `http://127.0.0.1:3000` 通过；`token` 非空时不再要求 `token_env_var`
- [x] 1.3 提升 `PluginSection.config_version` 至 `1.5.0`，并同步 `config.toml` 样例与 `plugin` 节说明
  - seam：`config.PluginSection` / `config.toml`（二者默认逐项一致）
  - 验收：模型默认值与 `config.toml` 逐项相同；样例 `[snowluma]` 只含 `http_url`/`token`/`token_env_var` 三键且无凭证值
- [x] 1.4 补配置层测试：非法地址矩阵、令牌优先与环境变量回退、config_version 值
  - seam：`tests/test_config.py`（pytest 公共命令可达）
  - 验收：`uv run --no-project --with pytest pytest tests/test_config.py -q` 全绿

## 2. HTTP 传输层

- [x] 2.1 重写 `snowluma_transport.py`：删除 ws 连接、接收循环、echo/pending 槽、事件判别与连接代次；`OneBotConnection` 改持 `http_url` 语义
  - seam：`snowluma_transport.OneBotTransport`（Protocol 导出）与 `OneBotUnresolved`/`OneBotUnavailable`（公开类型）
  - 验收：模块源码无 `ws_connect`/`send_json`/`receive` 调用；`resolve_onebot_connection` 对空/非法/缺凭证配置返回 `OneBotUnresolved`
- [x] 2.2 实现无连接发送：惰性 `ClientSession`（`trust_env=False`、`allow_redirects=False`），固定动作表 `get_login_info`/`get_group_list`/`get_msg`/`send_msg`/`forward_group_single_msg`/`send_forward_msg` 的 `POST /{action}`，令牌仅经 `Authorization: Bearer`
  - seam：`snowluma_transport.AiohttpOneBotTransport`（导出类，六个动作方法 + `close`）
  - 验收：对本地回环 HTTP 替身逐动作调用成功；请求体为 `{"action","params"}` 信封；URL 与日志不含令牌
- [x] 2.3 实现安全闸门：3xx 显式拒绝并归类 `insecure_endpoint`/`configuration`；新增常量 `ONEBOT_MAX_REQUEST_BYTES = 2*1024*1024`，序列化后超限在发出前拒绝；明文 `http` 目标建连前解析判定（回环/私有/链路本地）
  - seam：`OneBotUnavailable.category`（公共错误类别）
  - 验收：替身返回 302/307/308 时不发出下一跳、类别正确；2MiB+1B 请求体在 `socket.send` 前被拒（替身零请求记录）；解析出公网地址的 http 目标拒绝
- [x] 2.4 实现错误分类与 `close()`：401/403→`authentication`；超时→`timeout`；断连→`transport_unavailable`；`retcode` 1404/1400/100 交上层结构化处理；`close()` 幂等且受 5 秒卸载预算约束
  - seam：`OneBotUnavailable.category` / `AiohttpOneBotTransport.close`
  - 验收：替身分别返回 401、超时、断连时类别正确；重复 `close()` 第二次立即返回不挂起

## 3. 客户端与材料分派

- [x] 3.1 `snowluma_client.py`：`prepare_source` 保留三步只读核验（`get_login_info`→`get_group_list`→`get_msg` 归属），产出带 `kind`（`forward`/`image`/`text`）的 `PreparedSource`
  - seam：`snowluma_client.SnowLumaClient.prepare_source`（导出方法）
  - 验收：账号不符/目标不在名册/源群归属错/源不存在分别返回可定位 `PrepareRejected`；顶层唯一卡片→`forward`、无卡片含图片段→`image`、其余→`text`
- [x] 3.2 实现发送分派：`forward`→`forward_group_single_msg`、`image`→`send_msg` 群分支段直发、`text`→`send_forward_msg` 单节点卡片、评论→`send_msg` 普通文本
  - seam：`SnowLumaClient.send_source` / `send_comment`（导出方法）
  - 验收：内存传输替身记录到的动作名与 params 与类型一一对应；空白评论不发出任何请求
- [x] 3.3 实现文字节点构造：`{"type":"node","data":{"uin","name","content"}}`，`uin`/`name` 仅取源消息 sender（`card` 优先、回退 `nickname`，控制字符压平、限长），`content` 为源段原样（含 at）；不设置 `summary`/`news`/`prompt`/`source`
  - seam：内存传输替身捕获的 `send_forward_msg` 请求体（协议边界即断言面）
  - 验收：替身断言 `uin`/`name` 与源消息 sender 一致、`content` 与源段数组逐段相等（含 at 段）、无预览类参数
- [x] 3.4 回执核验收扎负数 ID：`_verify_target` 复用四项证据；`message_id` 按原值逐字比对；`send_forward_msg` 额外返回的 `res_id`/`forward_id` 不入账不进结果
  - seam：`SourceSendResult.status` / `target_message_id`（`shared.result` 公开类型）
  - 验收：替身返回负值 ID 时回查通过记 `sent`；`retcode=100` 不存在记 `failed`；缺 ID 记 `unknown`；结果体无 `res_id` 字段
- [x] 3.5 收缩 `send_material.py` 为只服务图片直发路径：段顺序保留、at 文本化（三形态）、不支持段/缺媒体/回复段整条拒绝；移除卡片资源材料职责
  - seam：`send_material.build_source_material` / `source_outbound_segments`（导出函数）
  - 验收：图文顺序与文本化输出逐段符合；reply 段触发拒绝；模块不再导出卡片材料路径
- [x] 3.6 补客户端与材料单测：三条分派成功/明确失败/待核实路径、节点字段仅来自源消息、at 分路径处理、负数 ID 核验
  - seam：`tests/test_snowluma_client.py` + `tests/test_send_material.py`（pytest 可达）
  - 验收：`uv run --no-project --with pytest pytest tests/test_snowluma_client.py tests/test_send_material.py -q` 全绿

## 4. 预检与编排衔接

- [x] 4.1 `forward_precheck.py`：保留凭据换会话、白名单、异群与顶层唯一卡片校验（混排拒绝）；卡片材料改为只取标识与归属核验
  - seam：`forward_precheck.PreparedForward` / `whitelist_entries` 等导出（pytest 可达）
  - 验收：顶层卡片+文字混排拒绝；纯卡片放行且 `onebot_message_id` 来自可信映射；白名单口径三个导出行为不变
- [x] 4.2 确认 `forward_delivery.py` 编排不变：账本先查→准备→复验→占位→发送→落账→0.5 秒→评论占位→发送→落账；清除 ws 措辞
  - seam：`forward_delivery.deliver_forward`（导出协程）
  - 验收：内存替身全链绿；`grep -i websocket forward_delivery.py` 无残留；评论延迟仍为注入时钟常量
- [x] 4.3 `shared/send_steps.py` 等共享类型按需增加 `kind` 与新 reason 字面量，不新建第二套状态机
  - seam：`shared.send_steps.PreparedSource`（导出类型）
  - 验收：`kind` 三值可用；`shared.result.render_forward_result` 对全部状态渲染无分支遗漏

## 5. 组合根与注册

- [x] 5.1 `plugin.py`：组合根改装配 HTTP 客户端（替换 ws transport 构造），卸载路径移除连接关闭阶段、保留 5 秒共享预算与任务表定位告警
  - seam：`plugin.create_plugin`（公共工厂，支持测试注入）
  - 验收：无参创建成功；卸载不调用任何 ws 关闭；任务表仍带源 ID/目标/阶段描述
- [x] 5.2 回归组件注册：一个群聊工具 + 两个 Hook、`get_components()` 顶层 `chat_scope` 修正、SDK 根导出白名单
  - seam：`plugin.get_components`（公共入口）
  - 验收：组件声明三件且 `chat_scope` 在顶层；hook 模式为字符串字面量；导入面符合根 `__all__`

## 6. 测试

- [x] 6.1 新建 `tests/test_snowluma_transport.py` 与替身：HTTP 语义全覆盖（禁重定向、body 上限、错误分类、无重试、令牌仅经头、repr 隐藏）
  - seam：`tests/test_snowluma_transport.py`（pytest 可达，替身经 `session_factory` 注入）
  - 验收：默认档 `pytest tests/test_snowluma_transport.py -q` 与 aiohttp 档均全绿；断言不触真实网络
- [x] 6.2 新建 `tests/test_snowluma_client.py`：三类分派、准备核验、回执（含负数 ID）、unknown/failed 分类
  - seam：`tests/test_snowluma_client.py`
  - 验收：默认档全绿；每条用例只经 `SnowLumaClient` 公共方法
- [x] 6.3 新建 `tests/test_direct_acceptance.py`：真实 aiohttp 回环 **HTTP** 端点复刻「Hook/Tool→预检→账本→发送→回执→落账」互证
  - seam：`tests/test_direct_acceptance.py`（aiohttp 档才运行）
  - 验收：`--with aiohttp` 档全绿；端点只绑 `127.0.0.1`；用例间无共享状态
- [x] 6.4 修正既有用例至新口径：auto_entry / lifecycle / plugin / precheck / ledger / result
  - seam：各 `tests/test_*.py`（pytest 可达）
  - 验收：两档 pytest 全绿，无 skip 掩盖（aiohttp 档零 skip）
- [x] 6.5 记录两档验证实际结果（通过数/skip 数），不沿用旧数字
  - seam：README「离线开发验证」小节
  - 验收：README 数字与当次实跑一致（由 7.1 写入、8.3 核验：775/867/22）

## 7. 文档

- [x] 7.1 `README.md`：配置表改 `http_url`；支持类型改为「卡片消息转发/单节点卡片文字/普通消息图片」；结果状态与评论口径同步
  - seam：README「配置」「支持与拒绝类型」两节（人工核对清单）
  - 验收：全文无 `websocket_url` 字样；三类型描述与 delta spec 一致；不含凭证示例
- [x] 7.2 `docs/direct-snowluma-operations.md`：停机/开启顺序改 HTTP 口径；写明 3000 与 3001 可能各自持 Token、body 2MiB 上限、禁重定向、明文规则
  - seam：运维文档第 2/4 节（人工核对清单）
  - 验收：操作步骤无 ws 连接动作；三条新约束逐条在文
- [x] 7.3 `docs/real-qq-validation.md`：联验清单补三项——卡片内 at 客户端提醒行为、单节点卡片展开观感、服务端 body 精确阈值
  - seam：联验清单新增条目（人工核对清单）
  - 验收：三项均列为未验证且注明归属 U6/U7
- [x] 7.4 `AGENTS.md`：直连相关段（模块职责、关键规则、验证数字）按实现结果更新
  - seam：AGENTS.md「模块职责」「持久事实、提交与生命周期」两节
  - 验收：ws 传输描述全部替换；验证数字与 6.5 实跑一致；CLAUDE.md 软链接仍指向 AGENTS.md

## 8. 验证与收尾

- [x] 8.1 运行 `uv run --no-project --with pyright pyright`，生产代码零错误并记录 `filesAnalyzed`
  - seam：pyright 公共命令
  - 验收：零错误；数字写入 README 验证小节
- [x] 8.2 运行 `openspec validate direct-snowluma-http-forwarding --strict`
  - seam：openspec CLI
  - 验收：`is valid`
- [x] 8.3 全量重跑两档 pytest 并记录真实结果；确认历史目录不被收集
  - seam：两档 pytest 公共命令
  - 验收：`history/` 与 `openspec/changes/archive/` 零收集；数字与 README 一致
- [x] 8.4 红线自检：无 `ctx.send.*`、无任意动作代理、URL/日志/账本不含凭证、日志不含正文与签名媒体地址、U1–U8 归档结论未被改写
  - seam：`grep` 公共命令 + 人工审查归档 verification 目录
  - 验收：五项均通过；归档 `unverified-server-contracts.md` 无修改记录
