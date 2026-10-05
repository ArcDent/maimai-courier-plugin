"""独立 SnowLuma 转发服务（design D1/D4/D5）。

本包是**服务进程**的入口，与宿主插件共用仓库根下的共享层 `shared/`：

- 以 `python -m service` 启动（仓库根在 `sys.path`，`shared` 是可导入的顶层包）；
- 不导入 `maibot_sdk`，不导入插件侧模块；网络库只在真正启动服务时才加载；
- 命令面见 `service.cli`：`start` 与 `ledger fresh|migrate|export|status`。
"""

from __future__ import annotations

__all__: list[str] = []
