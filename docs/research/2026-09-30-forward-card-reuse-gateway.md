# 原合并转发卡片复用：SnowLuma 网关核实

核实日期：2026-09-30。本篇保留网关源码证据；同日后续已通过 `ssh arclinux` 补齐已部署 QQ 适配器核查，宿主、SDK、适配器与当前插件的完整结论见[主报告](2026-09-30-forward-card-reuse.md)。

**适配器补充结论：** 部署源码中的 SnowLuma 适配器 `1.0.3` 对普通 `forward` 入站只返回展开节点，没有保留原资源 ID；出站 `dict` 包装的 `forward` 会被编码为 `[unsupported:forward]` 文本。另一方面，适配器已经公开 `adapter.snowluma.message.forward_group_single_msg`（并保留 `adapter.napcat.*` 原名），可以经宿主 `api.call` 调用，但这条动作调用不走宿主正常 `send_service`，不能直接继承落库、历史同步及目标会话路由保证。以上为部署源码与本地纯函数验证的结果，未调用线上 API、未发送测试消息，也未确认当前在线路由选择。

## 基线与边界

- 只读参考仓库：`arcwsl:~/github/REFERENCE/snowluma`。
- 本次实际读取的 `HEAD`：`7726a3899a3cae2b48119c255a7f368bd6ee2bc7`，与指定旧基线一致；本次 `git --no-optional-locks status --short` 输出为空。
- 重新通过 SSH 读取当前源码和限定测试片段；没有仅引用旧笔记，没有运行仓库代码或测试，没有读取运行配置、凭证、真实消息，没有发送、部署或修改远端。
- 下列代码引用的路径均相对该 SnowLuma 仓库，而非当前插件仓库。

## 结论

1. **网关存在“复用原资源 ID、不展开也不重新上传内部节点”的群发送路径**：普通 `send_group_msg` 的消息中放入 `forward` 段，`data.id` 为原转发资源 ID。该字段不是源消息的 OneBot `message_id`。
2. **这不是外层卡片字节级原样搬运**：接收、发送的 `forward` 段编解码只保留资源 ID；协议发送器重新生成 `com.tencent.multimsg` 外层卡片，默认标题、摘要、预览条目、条数及 UUID 不保证与原卡片一致。
3. **`node` 引用消息 ID 是另一条路径**：网关查本地消息缓存、解析消息段、构建节点，再上传一个新合并转发资源；并非原卡片整体直发。若引用的消息本身是转发卡片，通常是“新外层节点内放原资源卡片”，而非展开原卡片的内部节点。
4. `forward_group_single_msg` 也能读取缓存原消息后普通发送；原消息若是 `forward` 段，会沿用资源 ID，但仍重建外层卡片。它不是原始协议包透传。
5. **跨群资源可打开性、有效期及卡片内部提及是否绝不触发外部提醒仍未实测**。代码证明目标群可指定、原资源 ID 会被带出；不能单凭这些代码承诺 QQ 服务端接受任意旧资源或保证所有客户端行为。

## 一、资源 ID 直发路径

### OneBot 入口与实际接线

`send_group_msg` 使用目标 `group_id` 调用普通群消息发送器，返回新目标消息的 `message_id`；`send_msg` 的群分支亦走同一路径。

```91:99:packages/onebot/src/actions/message.ts
  groupAction({
    name: 'send_group_msg',
    summary: '发送群消息',
    returns: '{ message_id: number }',
    params: { message: f.message(), auto_escape: f.bool().default(false) },
    run: async (p, ctx) => {
      const result = await ctx.sendGroupMessage(p.group_id, p.message, p.auto_escape);
      return okResponse({ message_id: result.messageId });
    },
```

```89:89:packages/onebot/src/instance-context.ts
    sendGroupMessage: (groupId, message, autoEscape) => sendGroupMessage(ref, groupId, message, autoEscape),
```

普通解析器将段交给对应编解码器，不因 `forward` 段先去下载节点。这里的发送字段确切为 `data.id`，该编解码器没有读取 `data.res_id` 或 `data.forward_id` 别名；接收侧也只输出 `id`。

```395:398:packages/onebot/src/message-parser.ts
  const codec = getElementCodec(normalizedType);
  if (codec?.fromSegment) {
    const element = await codec.fromSegment(data, options);
    if (!element) {
```

```491:503:packages/onebot/src/event-converter/element-codecs.ts
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

解析后，普通群发送器把元素交给 `message.sendGroup`。只含 `forward` 的输入不会触发此处的媒体或文件分支。

```1087:1091:packages/onebot/src/modules/message-actions.ts
  let lastReceipt: Awaited<ReturnType<typeof ref.bridge.apis.message.sendGroup>> | undefined;
  if (nonFileElements.length > 0) {
    try {
      lastReceipt = await ref.bridge.apis.message.sendGroup(groupId, nonFileElements);
      logSentMessage(true, groupId, nonFileElements);
```

协议层明确使用目标群号构建群路由，而不是从源资源提取目标。后续通过 `MessageSvc.PbSendMsg` 发送。

```368:378:packages/core/src/bridge/apis/message.ts
    const protoElems = await buildSendElems(elements, {
      bridge: this.ctx as unknown as Bridge,
      groupId,
      scene: 'group',
    });
    const random = this.ctx.nextMessageRandom();

    const request = protobuf_encode<SendMessageRequest>({
      routingHead: {
        grp: { groupCode: BigInt(groupId) },
      },
```

### 不展开内部资源，但重建外层卡片

协议元素构造器的 `forward` 分支仅调用 `makeForwardElem`；该函数读取既有 `resId` 并生成外层卡片，没有调用 `forward.fetch` 或 `forward.upload`。

```545:547:packages/protocol/src/element-builder.ts
      case 'forward':
        result.push(makeForwardElem(elem));
        break;
```

外层卡片缺省使用新 UUID、`聊天记录`、`查看转发消息`、`[聊天记录]`、空预览列表及至少为 1 的条数。OneBot `forward` 编解码器没有传入原预览字段，因此只凭该段往返不能保持原外层展示。

```262:276:packages/protocol/src/element-builder.ts
  const uniseq = (element.forwardUuid ?? '').trim() || randomUUID();

  const source = element.forwardSource && element.forwardSource.length > 0
    ? element.forwardSource
    : '聊天记录';
  const summary = element.forwardSummary && element.forwardSummary.length > 0
    ? element.forwardSummary
    : '查看转发消息';
  const prompt = element.forwardPrompt && element.forwardPrompt.length > 0
    ? element.forwardPrompt
    : '[聊天记录]';
  const news = Array.isArray(element.forwardNews) ? element.forwardNews : [];
  const tSum = element.forwardTSum && element.forwardTSum > 0
    ? element.forwardTSum
    : Math.max(news.length, 1);
```

原资源 ID 放入新卡片的 `meta.detail.resid`；外层 JSON 再压缩为 `lightApp`。所以准确说法是“内部资源复用、外层卡片重新编码”，而非“整张原卡片完全不重建”。

```297:305:packages/protocol/src/element-builder.ts
    meta: {
      detail: {
        news: news.map(n => ({ text: n.text ?? '' })),
        resid: resId,
        source,
        summary,
        uniseq,
      },
    },
```

```311:316:packages/protocol/src/element-builder.ts
  const json = JSON.stringify(lightApp);
  return {
    lightApp: {
      data: makeDeflatedPayload(json),
    },
  };
```

实际读取到的协议构造测试也断言了资源 ID 与 UUID 的位置；本次只读，未执行这些测试。

```328:336:packages/core/tests/element-builder.test.ts
  it('places resid + uniseq inside meta.detail (and uniseq matches extra.filename)', async () => {
    const [elem] = await buildSendElems(
      [{ type: 'forward', resId: 'res-XYZ', forwardUuid: 'fixed-uuid-1234' } as any],
      { bridge: fakeBridge, groupId: 12345 },
    );
    const json = decodeLightApp(elem) as any;
    expect(json.app).toBe('com.tencent.multimsg');
    expect(json.meta.detail.resid).toBe('res-XYZ');
    expect(json.meta.detail.uniseq).toBe('fixed-uuid-1234');
```

## 二、`node.id`／`node.message_id`：缓存消息复用，不是资源直发

`send_group_forward_msg` 把节点数组交给 `sendGroupForwardMessage`。后者固定经过“解析节点 → 上传新资源 → 构建预览 → 向目标群发送 → 登记回执”。返回的 `res_id`／`forward_id` 是此次上传所得资源，而 `message_id` 是目标群发送结果。

```2447:2450:packages/onebot/src/actions/extended.ts
      const messages = p.messages ?? p.message;
      if (messages === undefined) return failedResponse(RETCODE.BAD_REQUEST, 'message/messages is required');
      const result = await ctx.sendGroupForwardMsg(p.group_id, messages, readForwardPreviewMeta(raw));
      return okResponse({ message_id: result.messageId, res_id: result.forwardId, forward_id: result.forwardId });
```

```1163:1169:packages/onebot/src/modules/message-actions.ts
  const nodes = await parseForwardNodes(ref, messages, { groupId });
  const forwardId = await ref.bridge.apis.forward.upload(nodes, groupId);
  const previewElement = buildForwardPreviewElement(forwardId, nodes, true, meta);
  const receipt = await ref.bridge.apis.message.sendGroup(groupId, [previewElement]);
  const { messageId } = await finalizeSend(ref, true, groupId, receipt, [previewElement]);

  return { messageId, forwardId };
```

`node` 的非零 ID 按安全整数解析，允许带符号的数值字符串；按 `id ?? message_id` 选择字段，然后查 `messageStore.findEvent`。缓存缺失直接拒绝，并没有为此自动查询 QQ 历史消息。零或缺省走自定义节点分支，不能拿资源字符串替代消息 ID。

```1665:1674:packages/onebot/src/modules/message-actions.ts
    const messageId = parseForwardMessageId(nodeData.id ?? nodeData.message_id);
    if (messageId !== 0) {
      const event = ref.messageStore.findEvent(messageId);
      if (!event) {
        throw new MessageElementValidationError(
          'INVALID_FIELD',
          `forward node message_id not found: ${String(messageId)}`,
          'node',
          'message_id',
        );
```

缓存消息内容仍会重新解析成元素，随后复制发送者、时间等部分元数据到新节点。若缓存消息的内容是 `forward` 段，这一步得到的仍是原 `resId` 元素，而不是原转发资源里的全部消息。

```1689:1704:packages/onebot/src/modules/message-actions.ts
      const content = (event.message ?? event.raw_message ?? '') as JsonValue;
      const elements = await parseMessage(content, false);
      if (elements.length > 0) {
        const messageType = event.message_type === 'group' ? 'group' : 'private';
        const groupIdValue = toPositiveInt(event.group_id);
        nodes.push({
          userUin,
          nickname: nickname || String(userUin),
          elements,
          time: typeof event.time === 'number' ? event.time : toPositiveInt(event.time),
          msgId: toSafeSignedInteger(event.message_id),
          msgSeq: toPositiveInt(event.message_seq),
          groupId: groupIdValue > 0 ? groupIdValue : undefined,
          senderCard,
          messageType,
        });
```

协议上传器随后重新构造节点消息体，而不是透传原消息包；每个节点生成新的随机消息 ID 与序列，保留可用的原时间。

```116:119:packages/core/src/bridge/apis/forward.ts
  const elems = await buildSendElems(node.elements, sendCtx);
  const now = Math.floor(Date.now() / 1000);
  const random = Math.floor(Math.random() * 0x7fffffff) >>> 0;
  const seq = Math.floor(Math.random() * 9000000) + 1000000;
```

上传层向 `SsoSendLongMsg` 提交新消息体，必须从本次上传响应获得非空资源 ID 才继续。群上传请求带 `groupUin`，媒体构造也使用目标群上下文。

```521:527:packages/core/src/bridge/apis/forward.ts
    const selfUid = await resolveSelfUid(bridge);
    const info: SendLongMsgReq['info'] = {
      type: groupId ? 3 : 1,
      uid: { uid: groupId ? String(groupId) : selfUid },
      payload: gzipSync(Buffer.from(longMsgResult)),
    };
    if (groupId) info.groupUin = groupId;
```

显式嵌套的 `node` 内容数组会在 `parseForwardNodes` 中递归转成 `innerForward`；上传层递归上传、生成嵌套预览，并将内层消息体附加到外层上传包。这仍是新建嵌套资源，不是原资源透传。

```464:472:packages/core/src/bridge/apis/forward.ts
    for (const node of nodes) {
      if (node.innerForward && node.innerForward.length > 0) {
        const inner = await this.uploadRecursive(node.innerForward, groupId, userId);
        // NapCat piggybacks `{uuid: inner.uuid, packetMsg: inner.packetMsg}`
        // + the entire `inner.innerPacketMsg` array up to its caller. We
        // mirror that — `inner.uuid` indexes the inner level itself,
        // and `inner.innerActions` already contains deeper layers.
        myInnerActions.push({ uuid: inner.uuid, msgBody: inner.msgBody });
        myInnerActions.push(...inner.innerActions);
```

## 三、按原消息 ID 整条发送的另一入口

`forward_group_single_msg` 接收源 `message_id` 与目标 `group_id`，调用 `forwardSingleMessage`。该函数查缓存、解析原消息，再经普通发送器发送；不会把源消息自动包装成新合并转发节点。

```1186:1196:packages/onebot/src/actions/extended.ts
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

```1231:1239:packages/onebot/src/modules/message-actions.ts
  const event = ref.messageStore.findEvent(messageId);
  if (!event) throw new Error(`message not found: ${messageId}`);

  const content = (event.message ?? event.raw_message ?? '') as JsonValue;
  assertOutboundMessageInput(content, false, 'forward');
  const parsed = await parseMessage(content, false);
  if (parsed.length === 0) throw new Error('message has no content');

  const elements = parsed.map((el) => enrichForForward(ref, el));
```

```1243:1245:packages/onebot/src/modules/message-actions.ts
  if (target.groupId) {
    receipt = await ref.bridge.apis.message.sendGroup(target.groupId, elements);
    ({ messageId: messageIdOut } = await finalizeSend(ref, true, target.groupId, receipt, elements));
```

`enrichForForward` 原样返回已解析的 `forward`、`at`、`reply` 等元素。因此缓存原消息为转发卡片时，可复用内部资源 ID；若另带顶层提及，则不能把这条单消息转发能力视为自动消除提醒的工具。

## 四、嵌套回复与提及

### `reply` 在重建路径中不是完整保真

`node` 引用缓存消息、自定义普通节点内容、`forwardSingleMessage` 都调用不带解析选项的 `parseMessage(content, false)`。因未提供来源会话对应的 `resolveReplySequence`／`resolveReplyMeta`，`reply.id` 不会在这些分支按源会话可靠还原：正数退回“直接当 QQ 序列”，负数返回空并被解析器忽略；只有回复、没有其他可发送段时，最终空消息检查会拒绝。

```249:253:packages/onebot/src/event-converter/element-codecs.ts
      // Backward-compatible path: allow direct seq reply IDs.
      // A negative OneBot id is never a QQ sequence.
      if (id > 0) return { type: 'reply', replySeq: id };
      log.warn('could not quote the target message');
      return null;
```

```398:403:packages/onebot/src/message-parser.ts
    if (!element) {
      // A reply decorates another sendable segment. NapCat-compatible clients
      // treat an unusable reply target as best-effort and still deliver the
      // remaining content; a reply-only message is rejected by parseMessage's
      // final non-empty check below.
      if (normalizedType === 'reply') return null;
```

协议回复编码只写序列及已有的发送者、时间，不自动补回引用正文。资源 ID 直发则完全不进入卡片内部节点的这套解析，因此不会在网关端改写内部 `reply`；原资源中的引用在目标客户端如何显示仍未实测。

### `at` 不会自动转成无提醒文本

OneBot 编解码器把 `qq=all` 转为全体提及，把普通 QQ 号转为真正的 `at` 元素；协议构造器写入提及专用 `pbReserve`。节点上传也复用这个构造器，没有统一将节点内提及转成普通文字的分支。

```103:112:packages/protocol/src/element-builder.ts
function makeMentionElem(element: MessageElement, ctx?: SendContext): ProtoElem {
  const mentionAll = element.uid === 'all' || element.targetUin === 0;
  const targetUin = element.targetUin ?? 0;

  const extra = protobuf_encode<MentionExtraSend>({
    type: mentionAll ? 1 : 2,
    uin: mentionAll ? 0 : targetUin,
    field5: 0,
    uid: mentionAll ? 'all' : (element.uid ?? ''),
  });
```

可以确认：仅发送一个 `forward` 资源段时，网关只生成外层 `lightApp`，不会把卡片内部 `at` 提取为目标群顶层 `at`。仍不能从静态源码断言 QQ 服务端或客户端对资源内部提及“绝不外部提醒”。同样，节点上传只证明提及仍被编码进资源，不能据此认定必然提醒或必然不提醒。

协议构造器另有“回复与用户提及共存时追加回复发送者结构性提及”的分支；源代码注释声称结构性提及自身不通知、用户提及会通知，但这不是本次对嵌套卡片行为的实测证据。

## 五、目标消息回执与失败边界

普通资源直发、新节点合并转发及单消息转发均经群发送协议。缺传输成功、缺响应或明确非零结果会抛错；成功响应读取 `groupSequence`、时间等。

```395:409:packages/core/src/bridge/apis/message.ts
    const result = await this.ctx.sendRawPacket(SEND_MSG_CMD, request);
    if (!result.success || !result.gotResponse || !result.responseData) {
      throw new Error(`send group message failed: ${result.errorMessage || 'no response'}`);
    }

    const response = protobuf_decode<SendMessageResponse>(result.responseData);
    if (!response) throw new Error('failed to decode SendMessageResponse');
    if (response.result != null && response.result !== 0) {
      throw new Error(`send group message rejected: result=${response.result} err=${response.errMsg ?? ''}`);
    }

    const seq = response.groupSequence ?? 0;
    const messageId = (random & 0x7FFFFFFF) || seq;
    const timestamp = response.timestamp1 ?? Math.floor(Date.now() / 1000);
    return { messageId, sequence: seq, clientSequence: 0, random, timestamp };
```

`finalizeSend` 在有真实正数群序列时，用“序列＋目标群＋事件类型”生成 OneBot 消息 ID 并存缓存；无群序列时仍可能退回本地不透明 ID。故“网关返回非空 `message_id`”并非无条件证明有真实 QQ 群序列，更不证明资源在接收端能展开。发送后本地登记若抛异常，也不能倒推出未发送。

```613:625:packages/onebot/src/modules/message-actions.ts
  const sequenceAuthoritative = Number.isInteger(receipt.sequence) && receipt.sequence > 0;
  const eventName = isGroup
    ? GROUP_MESSAGE_EVENT
    : privateMessageEventName(true, sequenceAuthoritative);
  const opaqueMessageId = receipt.messageId ?? 0;
  // Ordinary sends use QQ's real sequence so receive/send paths derive the same
  // OneBot id. OIDB-only sends (notably group files) have no QQ sequence: keep
  // their opaque local id separate instead of feeding a file hash into protocol
  // operations as if it were a server sequence (#254).
  const messageId = sequenceAuthoritative
    ? hashMessageIdInt32(receipt.sequence, targetId, eventName)
    : Number.isInteger(opaqueMessageId) && opaqueMessageId !== 0
      ? opaqueMessageId
```

## 尚未核实

- 任意来源群资源 ID 在任意目标群是否可打开、资源失效与权限限制、QQ 不同客户端的展示差异。
- 资源内部个人／全体提及是否在所有情况下均不产生目标群外部提醒；这里只确认网关没有展开成顶层提及。
- 原资源内部回复的跨群展开效果，以及新建节点路径里缺来源回复解析器的具体客户端表现。
- **已补齐的源码核实不再列为未知：** 部署适配器不保留普通 `forward` 入站资源 ID，出站引用字典降级为文本；公开单消息转发 API 存在，但不走宿主正常投递服务。完整证据见[主报告](2026-09-30-forward-card-reuse.md)。仍未确认当前在线 API 注册、实际账号路由及该条源消息在网关缓存中的可用性。
- 原始 `json`／`xml` 卡片透传是否可另行保留外层展示，不在此次资源 ID／节点 ID 复用路径核查范围内。

本次仅新增、更新研究文档；适配器补查及本地纯函数验证记录在主报告。不修改业务代码，不据此宣称当前插件已经具备原资源直发能力。
