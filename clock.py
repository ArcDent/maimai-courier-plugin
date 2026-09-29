"""时钟边界：评论延迟与有界等待一律经可注入时钟，测试可换成可控时钟。

生产用 `SystemClock`（单调时间 + `asyncio.sleep`）；测试注入实现了同一 `Clock`
协议的替身，无需真实等待即可确定性地推进超时与延迟。
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol


class Clock(Protocol):
    """插件消费的最小时间面。"""

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
