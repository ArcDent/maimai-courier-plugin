"""时钟边界：评论延迟与有界等待一律经可注入时钟，测试可换成可控时钟。

生产用 `SystemClock`（单调时间 + `asyncio.sleep`）；测试注入实现了同一 `Clock`
协议的替身，无需真实等待即可确定性地推进超时与延迟。

本模块是共享层里的**唯一定义处**：插件侧 `clock.py` 与服务进程都从
`shared.clock` 导入/重导出，测试与生产不会出现两套并行协议（design D9）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol


class Clock(Protocol):
    """插件与服务端消费的最小时间面。"""

    def now(self) -> float:
        """单调时间（秒）。"""

        ...

    async def sleep(self, seconds: float) -> None:
        """等待指定秒数；非正数立即返回。"""

        ...


class SystemClock:
    """生产时钟：`time.monotonic()` + `asyncio.sleep`。"""

    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        await asyncio.sleep(seconds)
