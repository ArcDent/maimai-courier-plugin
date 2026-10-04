# 1.2 可信宿主身份与 OneBot 源消息对应

核实日期：2026-10-02。接续时重新读取 `arclinux` 已授权源码，未运行远端代码、未查询真实消息。
宿主根目录记为 H=`/home/arc/maimai/MaiBot`；适配器 A 见 [01](01-websocket-and-auth.md)。
**源码证明特定入站链保留标识，不证明当前在线路由、实际登录账号或任意历史消息都走该链。**

## 已核实的公开输入

- H/`src/plugin_runtime/capabilities/data.py` 的 `message.get_by_id` 接收 `message_id`、
  `chat_id`（兼容 `stream_id`）、`include_binary_data`，返回 `success/message` 封装。
- 同文件 `chat.get_all_streams` 返回 `success/streams`；每个会话含 `session_id`、
  `platform`、`account_id`、`scope`、`group_id`、`is_group_session`；`all_platforms` 有明确分支。
- `chat.receive.after_process` 传入处理后的序列化 `message`；它发生在
  `chat_manager.register_message` 前，故保留自动入口有限可读等待。Hook 本身不是 observe
  专属；**本插件的订阅**使用 observe，不把宿主 Hook 规格写成仅 observe。
- 源工具会话继续来自既有宿主 Planner Hook 一次性凭据；消息正文或模型自填账号均不授予权限。
- SDK 解包已在公开发布的 **2.8.0** 固定提交与发布包逐字节复核；这是本插件声明的
  SDK 下限契约，详见下节。**线上实际加载版本仍未确认**，不由源码依赖声明推断。

```404:415:/home/arc/maimai/MaiBot/src/plugin_runtime/capabilities/data.py
            chat_id = str(args.get("chat_id") or args.get("stream_id") or "").strip()
            include_binary_data = bool(args.get("include_binary_data", False))
            message = message_service.get_message_by_id(
                message_id=message_id,
                chat_id=chat_id or None,
            )
            serialized_message = (
                self._serialize_messages([message], include_binary_data=include_binary_data)[0]
                if message is not None
                else None
            )
            return {"success": True, "message": serialized_message}
```

H/`src/services/message_service.py` 98–113 行把 `chat_id` 作为 `session_id` 与 `message_id`
共同交给查询，不从消息 ID 猜源群；序列化原值见下。

## SDK 公开调用、解包与适用版本（1.2 证据修复）

### 版本边界与可追溯来源

- 本插件 `_manifest.json` 的 SDK 范围为 `2.8.0–2.99.99`；本轮选择其**明确声明的下限
  2.8.0** 验证接口，而不是任意选一个新版本。此结论限于下述固定源码，不声称已逐一
  验证整个允许范围，也不改变既有 SDK/宿主版本声明。
- [公开 SDK 仓库](https://github.com/Mai-with-u/maibot-plugin-sdk) 的
  [v2.8.0 标签引用](https://api.github.com/repos/Mai-with-u/maibot-plugin-sdk/git/ref/tags/v2.8.0)
  本轮返回 commit `4d8e577d409fc7886e4a9f19dd94bea829081e4f`。
  下文 `maibot-plugin-sdk/` 引用均对应
  [该固定提交](https://github.com/Mai-with-u/maibot-plugin-sdk/tree/4d8e577d409fc7886e4a9f19dd94bea829081e4f)，
  `pyproject.toml` 3 行与 `maibot_sdk/__init__.py` 31 行都声明 `2.8.0`。
- 从 [PyPI 2.8.0 元数据](https://pypi.org/pypi/maibot-plugin-sdk/2.8.0/json) 取得
  [官方源码发布包](https://files.pythonhosted.org/packages/ba/bb/212cdf2bd4a15473a0bb91c3600fb09305946d80543c9a5fa4fb348e7945/maibot_plugin_sdk-2.8.0.tar.gz)，
  SHA-256 为 `007bf0c135525886e6004e9e2710d4c4829713965339e3a4119f3ec24d2349e7`。
  在本地内存核对包摘要，并将本节所列五个文件与固定提交原始源码逐字节比较，全部相同；
  没有安装、导入或执行下载的 SDK，没有将源码包落盘。
- 授权宿主源码 H 的 `pyproject.toml` 23 行是 `maibot-plugin-sdk>=2.5.2`，
  `uv.lock` 893–902 行锁定 **2.5.2**；两者均不是线上加载证明，锁定版本还低于本插件下限。
  `src/plugin_runtime/runner/runner_main.py` 488、548–550 行从 `maibot_sdk.context` 导入并注入
  `PluginContext`，没有在这些源码中固定实际导入位置。未进入 `.venv`、容器或其他
  运行目录查版本，也未读取运行配置；不能断言线上已安装 2.8.0 或仍使用 2.5.2。
- 宿主 Git HEAD 本轮只读取得 `982e832a23a4ec91c67b15ad2af90ce8d62f5e1e`；
  工作树文件仍以末节 SHA-256 为准。部署前必须另行确认实际加载 SDK 满足本插件范围且
  具有本节契约；若确实只装锁文件的 2.5.2，则不满足插件加载前提，不能放行。

### 公开签名与实际返回形态

`PluginContext` 在 SDK `context.py` 155–166 行挂载 `ctx.message` 与 `ctx.chat`。
两个查询的**公开类型注解都是 `Any`**；列表/字典是下述宿主实现与 SDK 解包组合产生的
运行时形态，不应伪称 SDK 签名声明了 `list[dict]` 或某个消息模型。

```20:22:maibot-plugin-sdk/maibot_sdk/capabilities/chat.py
    async def get_all_streams(self, platform: str = "qq") -> Any:
        """获取所有活跃的聊天流"""
        return await self._ctx.call_capability("chat.get_all_streams", platform=platform)
```

```33:40:maibot-plugin-sdk/maibot_sdk/capabilities/message.py
    async def get_by_id(
        self,
        message_id: str,
        *,
        chat_id: str = "",
        stream_id: str = "",
        include_binary_data: bool = False,
    ) -> Any:
```

`message.py` 50–56 行把 `message_id/chat_id/stream_id/include_binary_data` 原值传给
`call_capability("message.get_by_id", ...)`，无 ID 重编码、账号补造或会话推断。
`context.py` 257–269 行将能力名和参数放入 `cap.call` 后调用归一函数；
`_CAPABILITY_RESULT_KEYS` 在 38 行规定 `chat.get_all_streams → streams`，
67 行规定 `message.get_by_id → message`。

### 两层包装、失败与字段保留

宿主 `src/plugin_runtime/host/capability_service.py` 78–87 行将能力实现的返回值装入外层
`CapabilityResponsePayload(success=True, result=result)`；权限/协议错误或未捕获异常
则形成 RPC 错误。Runner 先拆外层 `result`，不会把业务 `success=False` 自动抛成异常：

```542:548:/home/arc/maimai/MaiBot/src/plugin_runtime/runner/runner_main.py
            if resp.error:
                raise RuntimeError(resp.error.get("message", "能力调用失败"))
            if normalized_method == "cap.call" and isinstance(resp.payload, dict) and "result" in resp.payload:
                return resp.payload.get("result")
            return resp.payload

        ctx = PluginContext(plugin_id=plugin_id, rpc_call=_rpc_call)
```

SDK 再拆能力实现的 `success/streams` 或 `success/message` 包装：

```279:285:maibot-plugin-sdk/maibot_sdk/context.py
        if not isinstance(result, dict) or "success" not in result:
            return result

        if capability in _CAPABILITY_RESULT_KEYS:
            result_key = _CAPABILITY_RESULT_KEYS[capability]
            if result_key in result:
                return result[result_key]
```

- 成功会话查询：H/`src/plugin_runtime/capabilities/data.py` 266–273 行产生 `success=True, streams=[...]`，
  SDK 返回其列表，空列表保持空列表；不会把外层封装交给插件迭代。
- 成功消息查询：同文件 396–418 行产生 `success=True, message=字典或None`，SDK 返回
  单条序列化消息字典；查无匹配（包括会话不匹配）保持 `None`，不变成成功空消息。
- 宿主上述业务失败返回 `{"success": False, "error": ...}`，**没有** `streams/message`。
  SDK 对这两个能力走到 `context.py` 296 行原样返回失败字典；RPC 错误则由 Runner 抛出。
  因此此前“宿主异常均表现为异常”的概括不准确，应区分业务失败值和 RPC 异常。
- SDK 只检查 `success` **键存在**，不检查其真假；若错误包装同时带结果键，仍会解包。
  本轮宿主这两个能力没有这种失败分支，不能把 SDK 写成通用成功校验器或字段验证器。
  缺结果键、无 `success` 或非字典的返回会原样透传，仍须由现有预检验证形状。
- 解包只取字段值，不挑选、重建或过滤列表项/消息内部字段。H/`src/plugin_runtime/capabilities/data.py` 241–255 行的
  `account_id/scope/session_id/platform/group_id/is_group_session` 完整进入会话列表；
  H/`src/plugin_runtime/host/message_utils.py` 392–396、413–428 行的 `message_info.additional_config`、
  `message_info.group_info`、`message_id`、`session_id` 也随消息字典保留。
  SDK 不验证这些字段的身份意义；账号与群归属仍按本记录后续条件链核对。

### 与冻结预检调用面的兼容性

- 当前 `forward_precheck.py` 185 行的
  `await ctx.chat.get_all_streams(platform=ALL_PLATFORMS)` 与公开签名一致；宿主
  `data.py` 230–238 行接受 `all_platforms`，SDK不改写该值。成功结果满足列表检查，
  业务失败字典不满足列表检查，RPC 异常由既有 `except` 转为 `Rejected.not_sent`。
- 当前 `forward_precheck.py` 227–231 行的
  `await ctx.message.get_by_id(message_id, chat_id=session_id, include_binary_data=True)`
  与公开签名一致。`chat_id` 来自一次性可信会话，不来自模型参数；SDK原值传递，宿主
  `data.py` 399–409 行按 `chat_id` 优先、`stream_id` 备用，空值才不限定会话。
  `message_service.py` 98–113 行将其作为 `find_messages(session_id=..., message_id=...)`
  的联合条件。当前调用始终提供可信非空会话，SDK默认空 `stream_id` 不会覆盖它。
- 正常消息字典满足既有形状检查，`None` 或业务失败字典被拒绝；RPC 异常同样被拒绝。
  当前预检还检查**存在的**返回 `session_id` 是否一致；本记录冻结的消息 ID、群与账号
  元数据进一步复验仍由后续 4.1 实现，不能把文档核实说成这些新检查已经落地。
- 结论：所核实的宿主源码与 **SDK 2.8.0** 可形成预期的成功返回链，也覆盖现有失败处理，
  无需为解决解包证据缺口更换公开接口。1.2 的源码契约证据已补齐；这不是线上成功证明，
  也不对 SDK 使用 D10 豁免。交由本组两轴复验，未执行下一组。

## 标识和账号的实际来源链

适配器 `runtime/router.py` 107–109、137、146–153 行：**本条事件**的 `payload.self_id`
作为 codec 参数及 `route_metadata.self_id`，`payload.message_id` 作为 external/dedupe ID。
旧 02 将入站 self_id 全部说成来自启动时 `get_login_info`，该结论过强，现纠正。
启动时登录查询（287–290 行）确实存在，但此处没有证明每个事件都与启动查询做了等值校验。

```110:120:/home/arc/maimai/MaiBot/data/MaiMBot/plugins/maibot-team_snowluma-adapter/codecs/inbound/message_codec.py
        # message_id 是平台侧消息的主键，缺失时无法引用/去重/撤回；
        # 明确报错丢弃该消息，不合成内部 ID 掩盖对端数据问题。
        message_id = str(payload.get("message_id") or "").strip()
        if not message_id:
            raise ValueError("入站消息缺少 message_id")
        has_picture = any(
            isinstance(segment, Mapping) and str(segment.get("type") or "") in {"image", "picture"}
            for segment in raw_message
        )
        return {
            "message_id": message_id,
```

codec 同时保存 `additional_config.self_id/napcat_message_type/client_type`、来源群与发送者。
宿主不会把这个 ID 重编码成另一套内部 ID：

```489:505:/home/arc/maimai/MaiBot/src/plugin_runtime/host/message_utils.py
        message_id = message_dict["message_id"]
        timestamp_str: str = message_dict.get("timestamp", "")
        platform = message_dict["platform"]
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("消息字典中缺少有效的 'message_id' 字段")
        if not isinstance(platform, str) or not platform:
            raise ValueError("消息字典中缺少有效的 'platform' 字段")

        # 解析时间戳
        try:
            timestamp_float = float(timestamp_str)
            timestamp = datetime.fromtimestamp(timestamp_float)
        except (ValueError, TypeError):
            timestamp = datetime.now()  # 如果解析失败，使用当前时间

        # 创建 SessionMessage 实例
        session_message = SessionMessage(message_id=message_id, timestamp=timestamp, platform=platform)
```

H/`src/plugin_runtime/host/message_utils.py` 413–428 行再次把该 `message_id`、`platform`、
`message_info`、`session_id` 返回给插件。以上是字符串保留关系，不是数字解析证明。

宿主账号还有优先级及回退，不能笼统写成所有会话 `account_id=self_id`：

```1264:1273:/home/arc/maimai/MaiBot/src/plugin_runtime/host/supervisor.py
        route_account_id, route_scope = RouteKeyFactory.extract_components(route_metadata)
        account_id = (
            route_key.account_id or route_account_id or runtime_state.account_id or gateway_entry.account_id or None
        )
        scope = route_key.scope or route_scope or runtime_state.scope or gateway_entry.scope or None
        return RouteKey(
            platform=platform,
            account_id=account_id,
            scope=scope,
        )
```

- `RouteKeyFactory` 优先读取 `platform_io_account_id/account_id/self_id/bot_account`。
- supervisor 1213–1228 行写回 route metadata，并以 `setdefault` 补
  `platform_io_account_id/platform_io_scope`；出现冲突不能假定自动修正。
- `bot.py` 524–538、626–632 行由消息元数据取账号/作用域计算会话并建会话；
  `data.py` 241–255 行序列化会话的 `account_id`。
- 本插件预期账号仍取可信 `SourceSession.account_id`，直连 `get_login_info.data.user_id`
  必须在**同连接代次**相等。源消息发送者是群友，不能要求源 `user_id == bot account_id`。

## 冻结给后续组的条件映射

1. 先按可信 session 查询宿主消息，校验 QQ 群会话、返回消息 ID/会话/群一致。
2. 只对已核实的 OneBot 入站保留链建立映射：可信消息具有上述入站元数据；`self_id`
   与会话账号一致，存在的 `platform_io_account_id` 也不得冲突。单有 QQ 平台名或数字 ID
   不足以证明链路；缺少对应信息的导入、内部生成、未知来源记录明确拒绝。
   这些元数据是宿主材料，不是模型参数；不能仅凭 `client_type` 认定在线适配器提供方。
3. `onebot_message_id` 只作为本次内存查询字段，取宿主保留值原文，不截取、不换哈希、
   不去前缀；安全形态校验只是先决条件。保留合法负 ID；布尔、空白或不能确证映射者拒绝。
4. 同连接登录账号匹配后查目标名册，再 `get_msg(onebot_message_id)`；要求合法成功结构、
   `data.message_id` 与查询值一致、`message_type=group`、`group_id` 与可信源群一致。
   ID 可由协议整数按十进制字符串比较，但不能借归一修改账本键或猜造查询 ID。
5. `get_msg` 在同账号命名空间查缓存、返回记录 ID 的含义属服务端**未复核**契约（U3）；
   查询缺数据、错记录、缓存缺失或无法确认映射均在发送前明确拒绝，不泄露响应正文。

原四字段防重键仍是 `account_id + source_group_id + 宿主 source_message_id + target_group_id`；
scope、连接、查询 ID、评论不进入键。源查询和回执查询的 ID 都不是 `forward.data.id`。

## 合成样本（约定，不是线上观测或已实现测试）

- 成功：宿主 session=`s-test`、账号=`10001`、群=`111`、消息=`-12345`；消息
  `self_id/platform_io_account_id=10001`、`napcat_message_type=group`、客户端类型为已知
  OneBot 入站；直连登录=`10001`、源查询返回 ID=`-12345`/群=`111`/类型=`group`，
  原发送者=`20002`。各项相符，允许进入材料准备，**尚未发送**。
- 错群：源查询群=`222`；错账号：登录=`99999` 或宿主元数据账号冲突；均拒绝、零发送。
- 错记录：请求 `-12345` 返回 `-12346`，即便同账号同群也拒绝。
- 无法映射：只有 `internal-12345`、数字 `12345` 但未知入站链、缺 self_id、缓存无记录；
  均拒绝，不尝试剥前缀或解析数字寻找其它记录。
- 实际在线路由：本轮没有读取启停配置或路由状态，不能把上面的条件样本写成已在线成功。

## 本轮源码摘要

均以 01 的 SSH 只读方式读取，在本地内存计算 SHA-256；下面路径相对 H。

- `src/plugin_runtime/capabilities/data.py`：`73c8bf46c0747b1abb256849faad52079814f6351e9fb2ad601d1d222a68c9f0`
- `src/plugin_runtime/host/message_utils.py`：`5a5e283db5282b8e61bbcdb9d5a7507fb177982100b40145423404272810bf83`
- `src/plugin_runtime/hook_payloads.py`：`ae31c85af432e248300cbd13abdcb83413af14af5cf5b7b58f831f881948ae72`
- `src/platform_io/route_key_factory.py`：`42f8e37c3e2f91b08833539686f21bd1ccf4f8dec0a8e38bea169031ce9d139a`
- `src/chat/message_receive/bot.py`：`b208afbd70422f6bd1b5fd6a5ec78412dd257d3495d40af327e34bc6c741862f`
- `src/plugin_runtime/host/supervisor.py`：`1fcf21a251f60e1c4f6882077b26b52188986d0587c128c6ca32456a004fd03f`
- `src/services/message_service.py`：`7ca38c20bbbbf9053786d4e4dc892ca41c63b124d749dd7a30567582eae974ff`

- `pyproject.toml`：`c694d12f0144fbeef36bbb683187d45bb31049269c2964abe965a208b3a1baa2`
- `uv.lock`：`7888879d3774b114a9301556d8ef8cff5930f3778c0b247fe5afdbea758ae405`
- `src/plugin_runtime/runner/runner_main.py`：`15214a2c5766e30d7344255b21ff1d1df0d009d8b829174c2e18acf9926bd668`
- `src/plugin_runtime/host/capability_service.py`：`d26caadd1d2cdfc608dfe9a6f0e23792d26220a2bf0b6436437f144430c268df`

本次 1.2 修复通过 SSH 只读重读了上述新增四个文件及原列的 `data.py`、
`message_service.py`、`message_utils.py`（后三个摘要均与此前一致），未重新核实无关链路。
宿主 Git 仅查询 HEAD 与限定在 `src/plugin_runtime/` 内的导入/调用位置；为只读查询
使用单次命令的 `-c safe.directory=...`，没有修改 Git 配置或任何远端文件。

SDK 五个文件由公开固定提交读取，SHA-256 如下；全部与上节官方 2.8.0 发布包字节相同：

- `maibot_sdk/context.py`：`6c366def79366d1a26be69ee4d9ef621f736fabdc44ca47c61795f11008eff3c`
- `maibot_sdk/capabilities/chat.py`：`6bde9a0fb146bdd8554f8c8347f57009800a2ce828718442a068cfef9d570a75`
- `maibot_sdk/capabilities/message.py`：`081102dcf61d48b8eac9a82b7147cce737eb041bdaf4e742b171f384d693df9b`
- `maibot_sdk/__init__.py`：`505661b7b273fe680b43b8389650d1d2a808a5630d8b53c15f043589db82b994`
- `pyproject.toml`：`61f78f21a28476a1b03a6af996176e30bb1fac8684adf51667a3f4fbf16b3227`

适配器源码摘要沿用 01，本次修复未重读适配器。SDK **2.8.0 源码解包契约已复核**；
真实加载版本、在线来源和 U1–U8 服务端事实仍待另行授权确认。以上验证只包含源码走查、
发布包摘要/逐字节比较与现有调用兼容性核对，没有业务测试、真实端点探测或 QQ 联验。
1.2 保持勾选，等待本组两轴复验；条件链无需扩大为适配器 API 或改变防重键。
