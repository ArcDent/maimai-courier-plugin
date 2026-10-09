"""时钟边界：评论延迟与有界等待一律经可注入时钟。

生产用 `SystemClock`（单调时间 + `asyncio.sleep`）；测试注入同一 `Clock` 协议的
替身，无需真实等待即可确定性推进超时与延迟。`Clock` 协议在本模块唯一定义，
账本、编排、客户端与组合根都从这里导入，不出现两套并行协议。
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol


class Clock(Protocol):
    """有界等待与代码常量超时共用的最小时间面。"""

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
