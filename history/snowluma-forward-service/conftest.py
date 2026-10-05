"""pytest 入口：把仓库根注册为合成包，并在导入插件前装入 fake `maibot_sdk`。

插件目录名含连字符、无法作为模块名被测试引用，因此这里把仓库根注册成合成包
`maimai_courier_plugin`，再以该包成员身份加载各模块，并给顶层名（`plugin` /
`config` / `clock`）建立别名，保证全程单实例。仅影响 pytest 运行，MaiBot 运行时
不会加载本文件。
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# plugin.py 顶部 import maibot_sdk，先装好替身再加载插件本体（本机不装真实 SDK）。
if "maibot_sdk" not in sys.modules:
    from tests import fake_maibot_sdk

    sys.modules["maibot_sdk"] = fake_maibot_sdk

PKG_NAME = "maimai_courier_plugin"
if PKG_NAME not in sys.modules:
    pkg = types.ModuleType(PKG_NAME)
    pkg.__path__ = [str(ROOT)]
    pkg.__package__ = PKG_NAME
    sys.modules[PKG_NAME] = pkg

# 生产模块显式清单（不用 ROOT.glob("*.py") 全量注册）：只给这些模块建顶层别名，
# 避免未来新增文件把无关顶层名（甚至标准库同名模块）注册进 sys.modules 或产生双实例。
# 名单随生产模块增加而扩展：测试要按顶层名引用生产常量/类型时，必须命中同一实例。
PRODUCTION_MODULES = (
    "clock",
    "config",
    "source_binding",
    "send_material",
    "forward_precheck",
    "delivery_ledger",
    "forward_delivery",
    "remote_client",
    "host_send",
    "auto_entry",
    "plugin",
)
for stem in PRODUCTION_MODULES:
    module = importlib.import_module(f"{PKG_NAME}.{stem}")
    sys.modules.setdefault(stem, module)

# 共享层（`shared/`）：插件侧以相对导入 `.<pkg>.shared.*` 使用，独立服务进程以顶层
# 包名 `shared.*` 导入（仓库根在 `sys.path`）。两种路径必须解析到**同一模块对象**，
# 否则同一类型会有两份定义（dataclass 相等、`is` 断言与状态共享都会失效）。这里把
# 顶层名指到插件包里的同一实例；服务进程自身运行时无需该别名。
SHARED_MODULES = (
    "clock",
    "config_rules",
    "delivery",
    "identifiers",
    "ledger",
    "mentions",
    "protocol",
    "result",
    "send_steps",
)
"""共享层子模块名单（随 `shared/` 新增模块扩展；子包聚合 `shared/__init__.py` 除外）。"""
shared_package = importlib.import_module(f"{PKG_NAME}.shared")
sys.modules.setdefault("shared", shared_package)
for stem in SHARED_MODULES:
    module = importlib.import_module(f"{PKG_NAME}.shared.{stem}")
    sys.modules.setdefault(f"shared.{stem}", module)
