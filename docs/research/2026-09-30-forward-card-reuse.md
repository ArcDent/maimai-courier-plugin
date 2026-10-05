# 原合并转发卡片跨群复用：宿主、适配器与网关核实

核实日期：2026-09-30；同日根据 `arclinux` 已部署适配器源码补充更新。问题：能否不展开、重建卡片内部节点，而把原合并转发作为一条消息发往其他群？

## 结论

**网关和适配器公开 API 已有按原消息 ID 跨群转发的实现；但当前宿主正常 `send.*` 链不能直接复用卡片资源：入站适配器不保留普通 `forward` 段的原资源 ID，出站引用字典会降级成 `[unsupported:forward]` 文本。**

三层结论现已明确：

- 网关：支持按原资源 ID 普通群发送，也有按源消息 ID 从缓存转发整条消息的入口。
- 宿主正常发送链：`send.forward` 要求节点内容；`send.custom`、`send.hybrid` 能将引用字典交给适配器，但已部署 SnowLuma 适配器没有对应的出站编码支持，**不能直接拿这两个通道发送资源引用**。
- 适配器公开 API：声明了 `adapter.snowluma.message.forward_group_single_msg`，可经宿主 `api.call` 调用；它直接调用 OneBot 动作，**不是宿主 `send_service` 正常投递流程，不自动获得该流程的落库、历史同步与路由策略保证**。当前搬运插件也未声明 `api.call`。

因此，技术上存在整卡资源复用通路；当前插件若要继续满足正常存储、历史可读、权限、防重与提及转换约定，仍需适配器/发送链适配，不能只删除内容预检，也不能将公开动作接口直接当作等价替代。

## 核实边界

通过 SSH 只读 `arcwsl:~/github/REFERENCE/` 当前源码，重新核实提交，不依赖旧笔记：

- MaiBot：`a995cc4553c174fe00d2d44f8ce3d11746f920d4`。
- maibot-plugin-sdk：`4d8e577d409fc7886e4a9f19dd94bea829081e4f`。
- SnowLuma：`7726a3899a3cae2b48119c255a7f368bd6ee2bc7`。
- 三个参考仓库的工作树检查均为空。本次没有更新参考仓库、读取凭证/运行配置/线上消息、部署或发送消息。
- 初次核实的参考目录只有示例插件；后经用户授权，补读 `arclinux` 已部署适配器源码和公开 manifest。源码目录为 `/home/arc/maimai/MaiBot/data/MaiMBot/plugins/maibot-team_snowluma-adapter`，manifest 标识为 `maibot-team.snowluma-adapter`，版本 `1.0.3`。同机 `MaiBot-Napcat-Adapter` 的 manifest 版本为 `1.4.0`，抽查其入站、出站编码器，两个限制同样存在。
- 未读取启停配置或在线路由状态，因此这里证明的是部署目录中的源码行为，不声称哪一个适配器当前实际承载消息；运行中的网关版本仍不能由参考仓库版本代替。
- 新增一次本地、内存中的纯函数验证：仅提取已审阅的 `_build_dict_component_segment`，用合成资源标识调用，确认输出为普通文本 `[unsupported:forward]`。未导入/启动适配器、未调用平台 API、未运行远端服务或仓库测试。

以下引用中 `MaiBot/`、`maibot-plugin-sdk/`、`snowluma/` 指远端参考仓库；`snowluma-adapter/` 指上述 `arclinux` 部署源码目录；插件路径相对当前项目。部署源码以以下 SHA-256 固定，不将 manifest 版本号当作源码提交：

- `codecs/inbound/message_codec.py`：`b88164c656acaf135120bac7ad7cea221200b4f0a0c569d2f5687f3511a770b4`。
- `codecs/inbound/cards.py`：`909d512a94f920ca93eec1c077b044ebe5cc71ccf23f29d358b2ac89ebcc4b7b`。
- `codecs/outbound/segment_encoder.py`：`32acce180228b148ab84e95c8777e090bbcc502114a34214b0a0817ede2914e6`。
- `codecs/outbound/message_codec.py`：`d2dce6a4b2ca0a77da9722c91ede1b54279be6719da6eade8d7694b57905acce`。
- `apis/message.py`：`906baec360c9639e7cbd10bb71fcbcb1039a0558adca8107c44eaf1d9f095490`。
- `apis/support.py`：`24eb5d0a06501b3ef9a93754f76db782857ef58305fb82b7cdd01112eef9782b`。
- `services/query_service.py`：`2156c4da7ed307a4fa8b291a8b5efa0f5df760aeda09eb29ec321f575d5e320b`。
- `services/action_service.py`：`85eb083efd08e11818d0a5968a1976ac0f3597a8cb58fd1b0a2539b67a9b8a5b`。
- `plugin.py`：`e90714f886b52bc893e8c7a336af5e67ba528ce6fc39cda9107268fa133f1a31`。

## 一、现有转发接口要求节点，不接受单独的原消息 ID

宿主 `send.forward` 读取 `messages` 数组，节点必须包含 `segments` 或 `content`；只有 `message_id`、没有内容的节点会被跳过。这个 `message_id` 字段不是“按 ID 查原消息后转发”的指令。

```477:491:MaiBot/src/plugin_runtime/capabilities/core.py
        stream_id = str(args.get("stream_id", ""))
        messages = args.get("messages")
        sync_to_maisaka_history = bool(args.get("sync_to_maisaka_history", False))
        maisaka_source_kind = str(args.get("maisaka_source_kind", "plugin_send") or "plugin_send")
        if not isinstance(messages, list) or not messages or not stream_id:
            return self._build_send_result(args, error="缺少必要参数 messages 或 stream_id")

        forward_nodes: List[Dict[str, Any]] = []
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            raw_segments = message.get("segments") or message.get("content") or []
            segments = self._normalize_plugin_segments(raw_segments)
            if not segments:
                continue
```

当前插件确实传入了逐节点构建后的材料：

```53:56:host_send.py
    if material.kind == "forward":
        return await _deliver(
            ctx.send.forward([dict(node) for node in material.nodes], stream_id, **SEND_OPTIONS)
        )
```

## 二、宿主可承载引用字典，但已部署适配器会将其降级

SDK `custom` 代理调用已注册的 `send.custom`，宿主接受 `message_type` 与任意 `content`，交给正常发送服务。该通路显式支持存储、Maisaka 历史同步和详细回执。

```193:204:maibot-plugin-sdk/maibot_sdk/capabilities/send.py
        return cast(
            bool | SendResult,
            await self._ctx.call_capability(
                "send.custom",
                message_type=custom_type,
                content=data,
                custom_type=custom_type,
                data=data,
                stream_id=stream_id,
                return_details=return_details,
                **kwargs,
            ),
```

宿主遇到未专门处理的自定义类型，会保留类型及内容为 `DictComponent`，而非解析卡片节点：

```349:358:MaiBot/src/services/send_service.py
    return MessageSequence(
        components=[
            DictComponent(
                data={
                    "type": normalized_type,
                    "data": deepcopy(content),
                }
            )
        ]
    )
```

发送给插件式消息网关时，宿主序列化消息并交给适配器；`DictComponent` 仍是一个 `dict` 包装段。新增核实确认：已部署适配器没有将内层 `forward` 解包为 OneBot 资源引用，而是回退为文本（见本节后半）。

```165:165:MaiBot/src/plugin_runtime/host/message_utils.py
        return {"type": "dict", "data": component.data}
```

```111:117:MaiBot/src/platform_io/drivers/plugin_driver.py
        try:
            message_dict = PluginMessageUtils._session_message_to_dict(message)
            response = await self._supervisor.invoke_message_gateway(
                plugin_id=plugin_id,
                component_name=self._component_name,
                args={
                    "message": message_dict,
```

另一个已有通道 `send.hybrid` 同样经 `PluginMessageUtils._message_sequence_from_dict`。其中 `forward.data` 若不是节点列表、无法生成节点，会保留为 `DictComponent`。因此 `send.custom` 与 `send.hybrid` 的资源引用最终都会碰到同一个适配器编码限制。

```327:329:MaiBot/src/plugin_runtime/host/message_utils.py
            if not forward_nodes:
                return DictComponent(data={"type": "forward", "data": item.get("data", [])})
            return ForwardNodeComponent(forward_components=forward_nodes)
```

已部署适配器的 `_build_dict_component_segment` 支持若干媒体及回复、提及类型，但完整分支中没有 `forward`。传入引用字典后，确定落入以下末尾分支：

```528:530:snowluma-adapter/codecs/outbound/segment_encoder.py
        if raw_type in {"image", "record", "reply", "at"} and isinstance(raw_payload, Mapping):
            return {"type": raw_type, "data": dict(raw_payload)}
        return {"type": "text", "data": {"text": f"[unsupported:{raw_type or 'dict'}]"}}
```

本地隔离该纯方法，以内层类型 `forward`、`data.id` 为合成资源标识输入，断言返回普通文本 `[unsupported:forward]` 通过。该实验不调用编码器以外的任何服务。

普通顶层 `forward` 也不是资源直转备用通道：编码器要求 `data` 是节点列表；出站消息编排识别它后，选择 `send_group_forward_msg`，即重建资源的通路。

```310:313:snowluma-adapter/codecs/outbound/segment_encoder.py
        item_data = item.get("data")
        if not isinstance(item_data, list):
            return []
        return self._build_forward_nodes(item_data)
```

```244:248:snowluma-adapter/codecs/outbound/message_codec.py
        profile = self._profile_state.profile
        forward_segments = self._build_forward_message_segments(raw_message, additional_config)
        param_key = profile.forward_param_key
        if target_group_id:
            return "send_group_forward_msg", {"group_id": target_group_id, param_key: forward_segments}
```

插件已经声明 `send.hybrid`，没有声明 `send.custom` 或 `api.call`。仅新增 capability 也不能解决上述编码问题；本次未修改能力声明或发送接缝。

## 三、原资源 ID 的取得是独立前提

宿主展开后的标准 `ForwardNodeComponent` 只有节点序列。对插件序列化时没有另附卡片的原资源 ID；数据库序列化也保存节点，而非原卡片引用。源消息 ID、节点消息 ID 和转发资源 ID 不可互换。

```156:163:MaiBot/src/plugin_runtime/host/message_utils.py
        if isinstance(component, ForwardNodeComponent):
            return {
                "type": "forward",
                "data": [
                    PluginMessageUtils._forward_component_to_dict(item, include_binary_data=include_binary_data)
                    for item in component.forward_components
                ],
            }
```

新增适配器核实将这一缺口定位到了入站转换：普通 OneBot `forward` 段的 `data.id` 仅用于查询节点；返回给宿主的段只有展开后的节点数组，失败时直接转为 `[forward]` 文本，没有保留该原资源 ID 的分支。

```754:774:snowluma-adapter/codecs/inbound/message_codec.py
        inline_messages = self._extract_forward_messages(segment_data)
        messages = inline_messages

        if messages is None:
            message_id = str(segment_data.get("id") or "").strip()
            if not message_id:
                return None

            forward_detail = await self._query_service.get_forward_message(message_id)
            if forward_detail is None:
                return self._build_text_segment("[forward]")

            messages = self._extract_forward_messages(forward_detail)

        if not isinstance(messages, list):
            return self._build_text_segment("[forward]")

        forward_nodes = await self._build_forward_nodes(messages)
        if not forward_nodes:
            return self._build_text_segment("[forward]")
        return {"type": "forward", "data": forward_nodes}
```

同时核查了 `message_info.additional_config`：它包含账号、客户端类型、目标路由字段以及可选 `platform_card_payloads`。但普通 `forward` 分支没有向后者追加数据；`cards.py` 中该列表的追加点用于 `com.tencent.miniapp_01` 小程序卡片，不能视为转发资源备份。

```323:335:snowluma-adapter/codecs/inbound/message_codec.py
            if segment_type == "json":
                converted_segments.extend(
                    await self._build_json_segments(
                        segment_data,
                        platform_card_payloads=platform_card_payloads,
                    )
                )
                continue

            if segment_type == "forward":
                if forward_segment := await self._build_forward_segment(segment_data):
                    converted_segments.append(forward_segment)
                continue
```

结论仅限定在普通 OneBot `forward` 入站路径：不能从宿主的这份展开材料直接取回原资源 ID；不等于网关缓存也丢失原始消息。适配器还公开 `get_msg`，其查询服务返回网关原始详情字典，理论上可在缓存仍存在时另行取回资源引用；本次只读源码，没有查询任何真实消息。

## 四、网关明确支持内部资源复用

SnowLuma 的普通 `forward` 段收发都使用 `data.id` 对应内部 `resId`；发送器按此资源生成外层卡片，不下载、解析或重新上传内部节点。

```491:503:snowluma/packages/onebot/src/event-converter/element-codecs.ts
  forward: {
    async toSegment(element) {
      return {
        type: 'forward',
        data: { id: element.resId ?? '' },
      };
    },
    async fromSegment(data) {
      return {
        type: 'forward',
        resId: String(data.id ?? ''),
      };
    },
```

网关还提供 `forward_group_single_msg`：以源消息 ID 查网关缓存，再向指定群发送。缓存原消息若是卡片，会沿用其内部资源 ID；缓存缺失则失败。它不是宿主注册的同名基础能力，**但新增核实已发现适配器将它公开为 API，可经宿主 `api.call` 到达**，详见下一节；不能再以“不是同名基础能力”为由推断插件完全无法调用。

```1186:1196:snowluma/packages/onebot/src/actions/extended.ts
  defineAction({
    name: 'forward_group_single_msg',
    summary: '转发单条消息到群',
    params: {
      message_id: f.messageId(),
      group_id: f.groupId(),
    },
    run: async (p, ctx) => {
      const result = await ctx.forwardSingleMsg(p.message_id, { groupId: p.group_id });
      return okResponse({ message_id: result.messageId });
    },
```

准确表述是“复用内部资源、重新编码外层卡片”，并非字节级原样转发。默认标题、摘要、预览及 UUID 可能变化。完整网关调用链、缓存限制、回复解析与回执边界见同目录 [网关核实记录](2026-09-30-forward-card-reuse-gateway.md)。

## 五、已存在的公开整条转发 API，以及它不提供的保证

适配器明确声明 `adapter.napcat.message.forward_group_single_msg`（版本 `1`、公开 API），参数 `params` 原样传给 OneBot 的同名动作。

```227:237:snowluma-adapter/apis/message.py
    @API("adapter.napcat.message.forward_group_single_msg", description="转发单条消息", version="1", public=True)
    async def api_action_forward_group_single_msg(self, params: QQApiParamsInput = None) -> Dict[str, Any]:
        """调用对端的 ``forward_group_single_msg`` 动作。

        Args:
            params: 传递给 对端的动作参数字典；具体字段请参考对端官方文档。

        Returns:
            Dict[str, Any]: 对端返回的原始响应字典。
        """
        return await self._call_napcat_action("forward_group_single_msg", params)
```

插件公共入口 `get_components()` 为所有 `adapter.napcat.*` API 同时生成 `adapter.snowluma.*` 别名，保留原处理器。因此与此部署源码对应的 SnowLuma 名称为 `adapter.snowluma.message.forward_group_single_msg`；`get_msg`、`send_msg` 也有同样别名。并存插件下 API 短名可能冲突，实际接入应确认提供方并按 SDK 支持的插件限定全名调用，不能任取同名 API。

```65:76:snowluma-adapter/plugin.py
        for component in components:
            if component["type"] != "API" or not component["name"].startswith("adapter.napcat."):
                continue
            # 保留原处理器和完整元数据，让两个前缀共享参数、返回值及能力校验。
            aliases.append(
                {
                    **component,
                    "name": "adapter.snowluma." + component["name"].removeprefix("adapter.napcat."),
                    "metadata": dict(component["metadata"]),
                }
            )
        return components + aliases
```

SDK `ctx.api.call` 经宿主 `api.call` 能力调用公开 API。宿主只负责解析 API 提供方并调用处理器，返回其结果，不进入 `send_service`：

```820:826:MaiBot/src/plugin_runtime/capabilities/components.py
        try:
            response = await supervisor.invoke_api(
                plugin_id=entry.plugin_id,
                component_name=entry.handler_name,
                args=invoke_args,
                timeout_ms=resolve_component_rpc_timeout_ms(entry.timeout_ms),
            )
```

适配器内部的实际链路为 API → `_call_napcat_action` → `QQActionService.call_action` → 当前适配器的 OneBot 连接：

```227:229:snowluma-adapter/apis/support.py
        normalized_action_name = self._normalize_non_empty_string(action_name, "action_name")
        normalized_params = self._normalize_params(params)
        return await self._require_action_service().call_action(normalized_action_name, normalized_params)
```

```48:59:snowluma-adapter/services/action_service.py
        normalized_params = {str(key): value for key, value in params.items()}
        try:
            response = await self._transport.call_action(action_name, normalized_params)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise RuntimeError(f"对端动作执行失败: action={action_name} error={exc}") from exc

        if str(response.get("status") or "").lower() != "ok":
            error_message = str(response.get("wording") or response.get("message") or "unknown")
            raise RuntimeError(f"对端动作返回失败: action={action_name} message={error_message}")
        return response
```

这证实“公开入口存在”，但不能等同于现有插件可直接无损切换：

- 当前插件缺 `api.call` 能力声明；API 是否已在线注册、提供方实际账号和网关路由仍需单独确认。
- 此调用经宿主 API 转发，但绕开其正常消息投递服务；没有 `storage_message`／`sync_to_maisaka_history` 处理，也不由本次调用自动构造正常宿主投递回执。对端自发事件是否另行回流存储，未实测、不能作为保证。
- 目标群来自动作 `params.group_id`，使用提供该 API 的适配器连接，不是由正常目标会话发送链选择账号。源归属、目标白名单、账号匹配、防重、异常结果不确定性等仍须独立守住。
- 源卡片能否从网关缓存找到、跨群后能否展开，仍有资源与缓存前提；公开 API 不绕过这些限制。

## 六、后续实现选择与未验证项

1. **若保持现有正常发送契约，优先补适配器支持**：入站保留可信原资源引用（或经受控查询取得），出站将它编码为普通 `forward` 资源段，继续走宿主 `send_service`。现有宿主已能承载字典，未发现必须先新增宿主基础发送能力的证据。
2. **公开单消息转发 API 是另一条真实存在的方案，但不是即插即用替代**：若选择它，需先解决宿主正常落库、历史同步、账号路由与回执等问题；不能用追加上下文伪装成正常发送历史，也不能把“API 调用成功”当成“所有投递契约已满足”。
3. 经用户另行授权后验证跨群展开、内部回复展示、资源有效期、外层预览及个人/全体提及的提醒行为。资源引用不会由网关展开成顶层 `at`，但本次未证明 QQ 对资源内部提及绝不提醒。
4. 原资源复用不会执行现有逐节点提及转文本。即使后续证实不提醒，也仍需明确调整当前“提及转普通文本”的行为契约，不可静默跳过。
5. 核实目标群 Maisaka 的实际可读历史；仅正常存储引用字典仍不意味着模型能读取卡片内部内容。

## 报告校验

- 两份报告共 46 处代码引用，已逐行比对所标注源码及行号，全部一致。
- 主报告记录的 9 个部署源码 SHA-256 均与采集内容一致；收尾时再次通过 `ssh arclinux` 只读计算指纹，9 个文件均未偏离报告基线。
- 已清理网关附录中“尚未核实适配器”的过时表述，保留在线注册、实际账号路由、源消息缓存、客户端展开与提醒行为等真正未验证项。
- 没有修改业务代码，未运行插件全量测试；上述校验仅证明报告引用、源码基线及已记录纯函数结果，不替代实际群消息联验。

消息 `558573276` 的原始工具回执和载荷仍未读取，因此本报告不证明该条消息一定可恢复或已转发。此次仅更新研究文档并完成一次本地纯函数断言，业务代码、配置、规格及远端文件均未修改；未发消息、未部署、未调用任何在线适配器 API。
