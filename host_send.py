"""已退役的宿主旧发送实现（design D9，本变更第 6.9 组）。

本模块曾是插件的真实发送接缝：纯文字 `send.text`、单图 `send.image`、多图/混排
`send.hybrid`、合并转发 `send.forward`，并显式开启 `storage_message` 与
`sync_to_maisaka_history`、请求详细回执。

第 6 组起全部源发送与评论改由专用 SnowLuma 转发服务执行：插件侧不再调用任何宿主
发送能力，也没有 fallback（发送只发生在服务端网关模块 `service/gateway.py`）。
这里保留模块名与退役说明，使任何遗留导入不会静默指向不存在的模块，并让
`pyrightconfig.json` 的 include 与测试进程的模块别名保持有效；本模块不再导出任何
发送函数，也不含任何 `ctx.send` 调用点。

历史发送契约、成功证据口径与核实证据见 openspec change
`move-forwarding-to-snowluma-service` 的 design D1–D10 与本仓库 tasks.md 第 1/5 组；
旧账本归档与兼容导出仍由 `delivery_ledger.py` 保留（禁止删除既有状态数据）。
"""
