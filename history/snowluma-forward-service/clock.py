"""时钟边界：评论延迟与有界等待一律经可注入时钟，测试可换成可控时钟。

生产用 `SystemClock`（单调时间 + `asyncio.sleep`）；测试注入实现了同一 `Clock`
协议的替身，无需真实等待即可确定性地推进超时与延迟。

定义已迁入共享层 `shared/clock.py`（服务进程同样需要时钟且不能依赖插件侧模块，
design D9）；本模块只做兼容重导出，插件侧既有导入入口与对象身份不变。
"""

from __future__ import annotations

from .shared.clock import Clock, SystemClock

__all__ = ["Clock", "SystemClock"]
