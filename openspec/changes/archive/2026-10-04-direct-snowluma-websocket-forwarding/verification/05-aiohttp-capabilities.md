# 1.5 aiohttp 握手保护与有界关闭

> 修订提示（2026-10-04）：本文所记地址信任口径已由 design D11 修订，见 verification/09-deployment-revision.md。

日期：2026-10-02。实际使用 **aiohttp 3.14.3** 的 uv 临时环境核实公开签名、对应源码及
本地模拟握手；未全局安装、未读真实凭证、未连接真实 OneBot。继续优先 `aiohttp>=3.14.3`，
这是下限版本的证据，不宣称未来版本无需重跑传输契约测试。

## 真实接口

以下抄录实际签名参数及默认值，省略类型注解，保留全部参数；`sentinel` 是库内部缺省哨兵：

- `ClientSession.ws_connect(self, url, *, method='GET', protocols=(), timeout=sentinel, receive_timeout=None, autoclose=True, autoping=True, heartbeat=None, auth=None, origin=None, params=None, headers=None, proxy=None, proxy_auth=None, ssl=True, verify_ssl=None, fingerprint=None, ssl_context=None, server_hostname=None, proxy_headers=None, compress=0, max_msg_size=4194304, decode_text=True)`。
- `ClientSession.request(self, method, url, **kwargs)`；HTTP 请求层的 `allow_redirects=True`
  在 `_request` 签名中存在，**`ws_connect` 没有 `allow_redirects` 参数**，不允许编造。
- `ClientWSTimeout(ws_receive=None, ws_close=None)`；实际 WS 缺省对象的 `ws_close=10.0`。
- `ClientWebSocketResponse.close(self, *, code=WSCloseCode.OK, message=b'') -> bool`，是协程；
  `ClientSession.close(self)` 也是协程，会等待连接器关闭。
- `ClientSession` 有公开 `trace_configs` 参数；`TraceConfig.on_request_redirect` 可注册异步回调。
  默认 `trust_env=False`，后续显式保持，避免环境代理/认证参与连接。

可复核命令（已执行同等完整签名检查）：
`uv run --no-project --with 'aiohttp==3.14.3' python -c 'import aiohttp,inspect; print(aiohttp.__version__); print(inspect.signature(aiohttp.ClientSession.ws_connect)); print(inspect.signature(aiohttp.ClientWSTimeout)); print(inspect.signature(aiohttp.ClientWebSocketResponse.close))'`。

## 证书、重定向与固定目标

`ssl=True` 经连接器选中验证上下文；`aiohttp/connector.py` 900–901 行调用
`ssl.create_default_context()`，没有关闭证书或主机名校验。不能传 `ssl=False`，也不接受
忽略证书的自定义上下文；非本机必须 `wss` 是本插件额外的配置限制。

3.14.3 的 `_ws_connect` 内部调用 `self.request(...)`，未传 `allow_redirects=False`。
可行方案不需要换库、子类化或复制握手实现：专属 session 安装一个**无条件阻断重定向**
的公开 `on_request_redirect` 回调，关闭该 `params.response` 后抛安全分类异常；
回调不回显 URL/头。只使用 Bearer 头、URL 禁凭证，配合禁代理及固定目标。

证据：回调发生在更新 URL 和下一次请求之前；抛错会中断重定向，随后有界关闭本 session。

```886:894:aiohttp/client.py
                    if resp.status in (301, 302, 303, 307, 308) and allow_redirects:

                        for trace in traces:
                            await trace.send_request_redirect(
                                method, url.update_query(params), headers, resp
                            )

                        redirects += 1
                        history.append(resp)
```

该引用对应 **3.14.3**，不是本仓库源码。下一轮 URL 赋值在981行、循环继续在984行。
回调必须由传输创建 session 时固定安装，不让调用者替换；测试经公开 WS 接口验证，
不用先带凭证跟过去再检查响应历史。仅设置 `raise_for_status` 太晚，不是替代方案。

### 实际本地握手实验

运行命令为 `uv run --no-project --with aiohttp==3.14.3 python -c <内存核实程序>`：
两个 `aiohttp.web` 回环临时端点，一个返回301/302/303/307/308到另一个端口，客户端带
合成 Bearer 头并在 redirect trace 抛 `RedirectBlocked`；逐次断言异常及计数。
实测退出码0：**原端点5次请求，重定向目标0次请求、0次认证头**。仅实验网络库，不写业务实现。
本轮实验回调直接抛错、退出时关闭 session；后续实现可先关闭 trace 中的 response，
避免把资源释放只留给外层。正常101握手、多客户端、帧上限及不合作清理完整测试归第3组。

## 帧大小、取消与清理边界

- `max_msg_size=4*1024*1024` 已是实际参数，传入 `WebSocketReader`；限制完整消息
  （包括分片合成），不设0无限制。`compress=0` 是默认值，保持不协商压缩。
  超限/非法帧使当前连接不可用，已可能发送的动作仍 unknown。
- `receive()` 捕获 `CancelledError/TimeoutError` 后设置异常关闭码并继续抛出，
  取消不是发送未发生证明；`send_json/send_str` 开始前必须记录提交边界。
- `close()` 先唤醒接收并等 `_close_wait`，再等 writer，再进入 `ws_close` 限制的读循环。
  **仅 ws_close 不能约束前两段**；其取消分支关闭底层 response 并重新抛取消。
- 必须用注入 `Clock` 的绝对期限统管排队/握手/动作10秒、回执3秒和卸载总5秒；
  不能把库缺省关闭10秒或旧握手30秒叠加在外。超时取消后也不能无界 gather。
  不合作任务报告未退出、保持新提交关闭，不能伪称已经清理或释放仍被使用的账本。
- 库原生超时负责底层保底，插件期限仍由时钟接缝和有限任务等待驱动；库 API 本身
  没有接受本项目 Clock 的参数，不编造。

## 本次读取的源码指纹

摘要为 `inspect.getsource(对象)` 的UTF-8文本 SHA-256（不是整个 wheel），行号来自
`inspect.getsourcelines`；没有运行库以外的远端业务代码：

- `ClientSession._ws_connect`：`e9761ab2c8498557004478ae4d76383474798d0e17dc0067e8f2c4c173973a5b`
- `ClientSession._request`（537行起）：`ed20e5b597a9a6ea78d497e98b571c9a936b6fa1266dd7041051d4e6fb61f2b3`
- `ClientWebSocketResponse.receive`（385行起）：`d2c3590c149cd11ca5f16cb6f6bc7dbd3ec535b79a22bc90c3c50e58babf2e64`
- `ClientSession.close`（1523行起）：`f97205caf23f69424074f243fe36fbd3a3c6b80f194575d14f540312c4c262fd`
- `TCPConnector._get_ssl_context`（1246行起）：`0f3a72a2828dc64435c7f967e8d0c5a527be6dfebbfc593f858d50a39826e74c`

库核实通过不代表业务直连实现完成；本组没有制造业务红绿测试，也没有改依赖清单。
