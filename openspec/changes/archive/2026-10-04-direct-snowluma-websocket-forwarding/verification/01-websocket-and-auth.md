# 1.1 正向 WebSocket 认证、Universal 事件与多客户端动作响应路由

核实日期：2026-10-02。性质：**只读客户端侧核实**。来源：`arclinux` 部署机上的 SnowLuma
适配器源码与 MaiBot 宿主源码。未连接任何真实端点，未读取凭证/运行配置/`data/snowluma`
运行目录，未在远端写入。

本记录只证明「部署目录中的客户端源码如何连、如何认证、如何区分响应与事件」，
不证明该客户端当前已加载、正在运行或实际承载入站路由。
SnowLuma 网关**服务端**源码本轮不可得，凡只能由服务端确认的事实一律标注「未复核」，
汇总见 [unverified-server-contracts.md](unverified-server-contracts.md)。

## 1. 核实对象标识

适配器源码目录（下文记作 `$ADAPTER`）：

```
/home/arc/maimai/MaiBot/data/MaiMBot/plugins/maibot-team_snowluma-adapter
```

- manifest 标识：`id = "maibot-team.snowluma-adapter"`，`version = "1.0.3"`，
  `plugin_type = "adapter"`（`$ADAPTER/_manifest.json`）。
- 本轮仅访问下述源码目录，不以其它目录的存在、内容或启停情况作为证据。
- 宿主 MaiBot 部署源码：`/home/arc/maimai/MaiBot`；既有核实记录登记的提交为
  `982e832a23a4ec91c67b15ad2af90ce8d62f5e1e`，本次接续以源码字节摘要复核，
  不据提交号推断工作树或运行版本。

关键文件内容摘要：本次接续逐一执行 `ssh -o BatchMode=yes -o ConnectTimeout=10 arclinux 'cat -- <完整源码路径>'`，
在本地内存中计算 SHA-256；下列 15 个文件均与已有记录一致。
公开 `_manifest.json` 的 SHA-256 为 `010d79464176fac100e04d018ad26829465cf7daa3da962b16602a88abbeb6b1`。
这是源码读取命令，不运行远端源码；未访问其它插件、凭证或运行数据。

| 文件 | SHA-256 |
| --- | --- |
| `transport.py` | `566cab61ed4e736cc19bf040db9a93fa9ac895abd44c4222aebb03238f68e399` |
| `config.py` | `d7e3b3135f0878d4888e3e92bc04bb8fc10e6e5de63ad76ea67b09e12c08b7de` |
| `profile.py` | `a8358b161c09133c71100bb2e2e7c51966620ae56874c19680a6d929c015b76b` |
| `heartbeat_monitor.py` | `72a8a75989562ea20a97ea303a13406aba5f780cc62981931c683487e4e3ab28` |
| `runtime/router.py` | `6fe281ecd340ca19c896cfdc78a25c20036a54ba171338fafd07d269b5bdc06f` |
| `runtime/builder.py` | `d77129fba71c95ac4f313bcdf28a12ff962f5c0e2e337a92f9648f50e8d43c45` |
| `plugin.py` | `e90714f886b52bc893e8c7a336af5e67ba528ce6fc39cda9107268fa133f1a31` |
| `apis/message.py` | `906baec360c9639e7cbd10bb71fcbcb1039a0558adca8107c44eaf1d9f095490` |
| `apis/support.py` | `24eb5d0a06501b3ef9a93754f76db782857ef58305fb82b7cdd01112eef9782b` |
| `services/query_service.py` | `2156c4da7ed307a4fa8b291a8b5efa0f5df760aeda09eb29ec321f575d5e320b` |
| `services/action_service.py` | `85eb083efd08e11818d0a5968a1976ac0f3597a8cb58fd1b0a2539b67a9b8a5b` |
| `codecs/inbound/message_codec.py` | `b88164c656acaf135120bac7ad7cea221200b4f0a0c569d2f5687f3511a770b4` |
| `codecs/outbound/segment_encoder.py` | `32acce180228b148ab84e95c8777e090bbcc502114a34214b0a0817ede2914e6` |
| `codecs/outbound/message_codec.py` | `d2dce6a4b2ca0a77da9722c91ede1b54279be6719da6eade8d7694b57905acce` |

`plugin.py` 与 `apis/*`、`services/*`、`codecs/*` 的哈希与既有研究报告
（`docs/research/2026-09-30-forward-card-reuse.md`）记录的 9 个值逐一相同；
`transport.py` / `config.py` / `profile.py` / `heartbeat_monitor.py` /
`runtime/router.py` / `runtime/builder.py` 是本轮新增采集。

## 2. 客户端连接与握手（已核实）

适配器是一个**正向 WebSocket 客户端**（不是服务端）：
`$ADAPTER/transport.py:1` 文件首行 `"""OneBot 正向 WebSocket 传输层（SnowLuma / SnowLuma / NapCat 通用）。"""`。

### 2.1 握手地址：`ws://<host>:<port>`，无路径

```386:393:/home/arc/maimai/MaiBot/data/MaiMBot/plugins/maibot-team_snowluma-adapter/config.py
    def build_ws_url(self) -> str:
        """构造正向 WebSocket 地址（不含鉴权参数，鉴权在传输层按画像附加）。

        Returns:
            str: 供适配器作为客户端连接的 WebSocket 地址。
        """

        return f"ws://{self.server}:{self.port}"
```

配置字段只有 `server` / `port` / `token` / `heartbeat_interval` /
`reconnect_delay_sec` / `action_timeout_sec` / `connection_id` / `client_type`
（`config.py:240-385`，`QQServerConfig`），没有路径字段。配置代码构造根路径 URL，不能据此确认实际配置或连接。**未复核**：截图里两个 `0.0.0.0:3001/3002` 端点是否都
接受根路径、是否还有一个未写入适配器配置的路径，只能由服务端或联验确认。

### 2.2 认证：URL 查询参数 + `Authorization: Bearer` 双通道

```84:99:/home/arc/maimai/MaiBot/data/MaiMBot/plugins/maibot-team_snowluma-adapter/transport.py
    def _build_connect_url(self, server_config: QQServerConfig) -> str:
        """构造连接地址。

        token 同时以 URL ``access_token`` 参数携带（OneBot v11 标准方式之一），
        与 ``Authorization: Bearer`` 头双通道并发；SnowLuma 与 NapCat 各取所需。
        ...
        base_url = server_config.build_ws_url()
        if not server_config.token:
            return base_url
        return f"{base_url}?{urlencode({'access_token': server_config.token})}"
```

```365:380:/home/arc/maimai/MaiBot/data/MaiMBot/plugins/maibot-team_snowluma-adapter/transport.py
    def _build_headers(self, server_config: QQServerConfig) -> Dict[str, str]:
        """构造连接所需的握手请求头。

        token 以 ``Authorization: Bearer`` 头携带（OneBot v11 标准方式之一），
        与 URL ``access_token`` 参数双通道并发，两类服务端各取所需。
        ...
        if not server_config.token:
            return {}
        return {"Authorization": f"Bearer {server_config.token}"}
```

对照截图：两个端点均「已设 Token」，与客户端配置了 token 才发送凭证一致；但
**未复核** token 是否必填、两个端点的 token 是否相同、服务端比对哪一路（URL 或头）。

### 2.3 连接建立方式

```195:220:/home/arc/maimai/MaiBot/data/MaiMBot/plugins/maibot-team_snowluma-adapter/transport.py
            timeout = ClientTimeout(total=None, connect=10)
            ...
                async with ClientSession(headers=self._build_headers(server_config), timeout=timeout) as session:
                    async with session.ws_connect(ws_url, heartbeat=server_config.heartbeat_interval or None) as ws:
                        self._ws = ws
                        ...
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._logger.warning(...)
            finally:
                self._ws = None
                await self._notify_connection_closed()
                self._fail_pending_actions("连接已中断")

            if self._stop_requested:
                break

            await asyncio.sleep(server_config.reconnect_delay_sec)
```

即：`aiohttp.ClientSession.ws_connect`，连接超时 10 秒、总超时不设、协议级
WebSocket `heartbeat` 保活、断线后按 `reconnect_delay_sec` 循环重连、断连时
所有待决动作失败（`_fail_pending_actions`，`transport.py:354-361`）。重连**不会**
重放旧动作，只重连；这是客户端实现，不代表我们插件应照抄重连策略（design D2 要求
按合法请求建连、不维持后台自动重连循环）。

### 2.4 动作请求、`echo` 与响应归属

```151:179:/home/arc/maimai/MaiBot/data/MaiMBot/plugins/maibot-team_snowluma-adapter/transport.py
    async def call_action(self, action_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """发送 OneBot 动作并等待对应的 echo 响应。"""
        ...
        echo_id = uuid4().hex
        loop = asyncio.get_running_loop()
        response_future: asyncio.Future[Dict[str, Any]] = loop.create_future()
        self._pending_actions[echo_id] = response_future

        request_payload = {"action": action_name, "params": params, "echo": echo_id}
        try:
            async with self._send_lock:
                await ws.send_str(json.dumps(request_payload, ensure_ascii=False))
            return await asyncio.wait_for(response_future, timeout=server_config.action_timeout_sec)
        finally:
            self._pending_actions.pop(echo_id, None)
```

结论（客户端侧）：请求形如 `{"action": …, "params": …, "echo": <32 位十六进制>}`；
同连接内用 `echo` 关联响应；单实例内 `_pending_actions` 可同时挂多个 echo（由
`_send_lock` 串行写帧、异步等待）。

### 2.5 事件与响应的判别（客户端侧规则）

```239:272:/home/arc/maimai/MaiBot/data/MaiMBot/plugins/maibot-team_snowluma-adapter/transport.py
            async for ws_message in ws:
                if ws_message.type != WSMsgType.TEXT:
                    ...（CLOSE/CLOSED/ERROR 结束连接）
                payload = self._parse_json_message(ws_message.data)
                if payload is None:
                    continue

                if echo_id := str(payload.get("echo") or "").strip():
                    self._resolve_pending_action(echo_id, payload)
                    continue

                self._create_background_task(self._on_payload(payload), "napcat_adapter.payload")
```

客户端判别规则：**载荷带非空 `echo` → 当作动作响应**（查待决表，匹配不上就丢弃）；
**不带 `echo` → 当作事件**交给 `_on_payload`。事件再按 `post_type` 分流：

```72:95:/home/arc/maimai/MaiBot/data/MaiMBot/plugins/maibot-team_snowluma-adapter/runtime/router.py
    async def handle_transport_payload(self, payload: QQPayloadDict) -> None:
        """处理来自传输层的非 echo 载荷。"""
        post_type = str(payload.get("post_type") or "").strip()
        if post_type == "message":
            ...
        if post_type == "notice":
            ...
        if post_type == "meta_event":
            await self.handle_meta_event(payload)
        ...
        # SnowLuma 部分版本的推送可能缺失 post_type 但携带 message 字段；按消息路由兼容。
```

`plugin.py:396-398` 把 `on_payload` 接到 `handle_transport_payload`、
`on_connection_opened` 接到 `bootstrap_adapter_runtime_state`；后者在连接建立后调用
`get_login_info` 取得 `self_id`（`runtime/router.py:288` 与 `:358-374`，
`_extract_self_id_from_login_response` 读取响应字段 `user_id`）。

心跳：服务端 `meta_event` 推送里的 `interval`（毫秒）被
`heartbeat_monitor.py:107-120` `_resolve_interval_sec` 换算成秒级超时基准。

### 2.6 客户端侧与插件设计的直接结论

1. 正向 `ws`、认证头和 `echo` 关联在部署目录源码中有客户端实现；它同时发送 URL token，
   因而只能证明客户端发送 Bearer 头，不能证明服务端接受**仅 Bearer 头**。仅头认证
   按 D2 冻结为待确认前提，失败时停在握手，不退回 URL token 或适配器 API。
2. 插件**不应**照搬「URL 查询参数携带 token」：design D2 明确不在 URL 携带凭证；
   且见 [05-aiohttp-capabilities.md](05-aiohttp-capabilities.md)，aiohttp 的
   `ws_connect` 默认会跟随握手重定向，URL 中的凭证一旦被重定向地址回显则风险更高。
   只用 `Authorization: Bearer` 头。
3. 插件收到**不带 `echo` 的载荷必须当事件**、不得触发自动入口或补写宿主历史
   （delta 规格「动作关联与事件隔离」已规定）；适配器的判别规则可作为我们接收循环
   的兼容参考，但插件必须再校验响应结构（不能只看 `echo` 存在）。
4. 适配器的「断线失败全部待决动作 + 自动重连」不能直接继承：插件要求断线使账号
   核验失效、不自动重放、重连只服务后续合法调用。

## 3. 截图事实的转述与边界

旧附件对截图的转述显示两个启用条目：
均标记 `Universal` 且「已设 Token」：`0.0.0.0:3001` 当时 1 个客户端、
`0.0.0.0:3002` 当时 2 个客户端，两者「上报自身」设置不同。

本次未重新查看截图；旧附件对截图的转述只提供当时端点、客户端计数和Token设置的线索。
这些线索不能证明当前事实。截图
**不证明**：客户端身份、任意新客户端是否有动作权限、多个客户端之间动作响应是否
隔离（请求方独收还是广播）、Token 是否必填/是否两端口共用、`0.0.0.0` 之外的
可达主机地址、两个端点的路径。

## 4. 明确「未复核」的服务端侧事实

以下只能由 SnowLuma 服务端源码或真实联验确认，本轮不写入任何已核实结论（详见
汇总清单）：

- Universal 端点上的动作响应是否只发给发起请求的那个客户端；多客户端并存时是否
  按连接隔离响应；离线测试必须同时覆盖广播干扰，不能依赖独收保证。
- Token 校验方式（URL 参数/头/两者）、是否必填、两个端点是否各自独立凭证。
- 事件推送的广播范围与「上报自身」开关的具体语义（是否影响事件回流到本连接）。
- 握手路径：是否只接受 `/`；是否存在子路径或需要 `Sec-WebSocket-Protocol`。
- 服务端对帧大小、连接数、空闲超时的限制。

这些未复核项按 design D10 处理：未提交发送的握手/源预检失败可以明确拒绝；
一旦可能提交，缺证据只能待核实，不能因假设宣称成功或明确未发。真实联验另行授权，
逐项确认，失败回到规划。响应即使广播也必须靠本插件严格关联和事件隔离自保，
不能把服务端独收当作离线实现的必备安全前提。
