"""共享纯逻辑包：投递类型、结果、时钟、标识符、提及与配置规则，各子模块单点定义。

本包不做符号聚合（0 兼容层）：调用方一律显式导入子模块（`from .shared.clock import
Clock`），包顶层不重导出任何名字；各子模块的职责见其自身文档串。
"""

from __future__ import annotations
