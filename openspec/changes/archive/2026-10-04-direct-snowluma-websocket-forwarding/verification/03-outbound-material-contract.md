# 1.3 固定发送动作与材料契约

日期：2026-10-02。**服务端未复核**；以下是 D10 允许的离线实现假设，不是在线支持声明。
本轮直接证据为 01/02 的部署客户端源码；替代来源为
`docs/research/2026-09-30-forward-card-reuse-gateway.md`（旧参考版本
`7726a3899a3cae2b48119c255a7f368bd6ee2bc7`）和既有 `service/materials.py/onebot.py`。
旧报告的服务端引用本轮没有重新读取。待确认项按 [汇总](unverified-server-contracts.md) U4–U6 管理。

## 唯一动作及输出字段

- 固定 **`send_msg` 群分支**：`params.message_type="group"`、`group_id=可信目标群`、
  `message=结构化段数组`；请求封装另含 `action/params/echo`。源与评论共用；
  控制面 `stage` 不进入协议。旧网关报告“一、资源 ID 直发路径”记录该群分支与
  `send_group_msg` 共用普通群发送器；本轮冻结前者，不失败后换动作。
- 原卡片唯一输出：`{"type":"forward","data":{"id":"resource-test-A"}}`。
  `id` 只来自 02 已核验源记录的唯一顶层卡片。它是**资源 ID**，不是查询源记录的
  `message_id`，也不是新目标回执 ID。只保留此引用，不展开、上传或清洗内部节点。
- 普通文本及评论：`{"type":"text","data":{"text":"原文"}}`。字符串里的
  `[CQ:at,qq=all]`、命令和提示仍是文本，不解释为协议。不拼接裸 CQ 字符串发送。
- 图片冻结候选：`{"type":"image","data":{"url":"https://images.example.org/test.png"}}`。
  URL 必须来自可信 OneBot 源记录，例子仅用于离线模拟，**本轮没有访问该地址**。
  这沿用旧实现/方向报告的 HTTPS `data.url` 路径；服务端实际接受字段及安全获取语义
  都未复核，不能从“HTTPS”推出“不会访问内网”。
- 单图、多图和图文混排均按原段次序发送一条；任一段不完整就整条拒绝，不丢图、不转描述。

## 图片的安全前提与保守边界

离线契约保留 HTTPS URL 图片路径，但区分两层：

1. 插件可检查的输入边界：仅信任源查询；要求合法 ASCII HTTPS URL，拒绝本机路径、
   `file://`、用户信息、反斜杠、非 HTTPS、回环/私有/保留 IP、混淆数字地址；不读取
   模型指定的文件或 URL。媒体不缓存、不落账、不回显签名地址。
2. 远端获取安全前提 U5：网关必须在解析 DNS 及重定向后仍只访问合法公网 HTTPS
   媒体，不把路径/凭证或任意内网资源当媒体。旧 `_safe_media_url` 只做字面量检查，
   **不能证明 DNS/重定向安全**；离线模拟使用受控公网媒体样本并验证拒绝恶意输入，
   不能据此声称线上图片安全。授权联验前必须核实此前提；不成立则停止并返回规划，
   不能以“发后未知”补救已发生的越界资源访问。

这冻结的是可实现的材料形状及启用前提，不是新增自动下载、上传服务或取消图片功能。
若无法确认 HTTPS 获取安全，需要重新对齐安全重放路径，不允许实现者悄悄绕过。

## 普通提及与卡片完整性

- `at.data.qq` 为明确个人标识或 `all`；普通消息复用 `shared/mentions.py` 的中文呈现。
  可选 `card/nickname/name` 只作源显示信息，压平控制字符，不查询目标成员、不截断对象。
  转换后没有顶层 `at`，保持原换行与图文顺序；对象不明整条拒绝。
- 唯一卡片不检查内部未知段/回复/提及；资源为空、多卡片或卡片与其他顶层内容混排拒绝。
- 旧报告只支持“内部资源复用、外层卡片重新编码”的假设，跨群展开、有效期和内部提醒
  行为留在 U6；不承诺外层标题、预览或 UUID 逐字相同。

## 本轮客户端证据及纠偏

适配器普通 `forward` 入站 754–774 行用资源 ID 查询后只交付展开节点，失败变为文本，
并没有保存原资源；宿主展开节点不可作为资源 ID。出站 `dict/forward` 仍降级：

```528:530:/home/arc/maimai/MaiBot/data/MaiMBot/plugins/maibot-team_snowluma-adapter/codecs/outbound/segment_encoder.py
        if raw_type in {"image", "record", "reply", "at"} and isinstance(raw_payload, Mapping):
            return {"type": raw_type, "data": dict(raw_payload)}
        return {"type": "text", "data": {"text": f"[unsupported:{raw_type or 'dict'}]"}}
```

旧方向报告“只能 url、file 必然是本机路径、网关不接受 base64”的绝对结论没有本轮
服务端证据，不能继承为事实。客户端源码实际构造 base64 图片：

```178:181:/home/arc/maimai/MaiBot/data/MaiMBot/plugins/maibot-team_snowluma-adapter/codecs/outbound/segment_encoder.py
        binary_base64 = str(item.get("binary_data_base64") or "").strip()
        if not binary_base64:
            return []
        return [{"type": "image", "data": {"file": f"base64://{binary_base64}", "sub_type": 0}}]
```

这只能反证“客户端没有此种材料路径”，不能证明服务端接受它；本轮不因此自动改用
base64、宿主路径或额外上传。HTTPS 字段假设来源与替代候选证据分开保存。

## 合成验收载荷约定

- 卡片：源记录 `message_id=-12345`，唯一段资源 `resource-test-A`；发送仅带资源段，
  绝不用 `-12345` 充资源。卡片内部即便有回复/提及也不展开。
- 图文：`text("甲\n") → image(url-A) → image(url-B) → text("乙")`；保持四段顺序。
- 提及：`text("甲") → at(qq=20002,name=乙) → image(url-A)`；第一段归并为
  `甲［提及：乙（20002）］`，下一段仍是图片，不增加换行。
- 拒绝：空资源、双卡片、卡片加文字、缺媒体、内网 URL、对象不明的 at、不支持普通段；
  均只在准备阶段拒绝，发送数为零。

本组未执行产品发送测试；这些是第4组公开入口的合成约定，库实验另见 05。
