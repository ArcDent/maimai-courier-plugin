"""已退役的独立HTTP转发服务客户端。

完整原实现与逐文件摘要保存在 history/snowluma-forward-service/remote_client.py
及该目录 MANIFEST.md；本模块不导入历史、不访问HTTP、不提供发送或备用接口。
活跃插件仅使用自己的 SnowLuma 正向WebSocket客户端。
"""
