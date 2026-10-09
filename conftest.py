"""pytest 入口：把仓库根注册为合成包，并在导入插件前装入 fake `maibot_sdk`。

插件目录名含连字符、无法作为模块名被测试引用，因此这里把仓库根注册成合成包
`maimai_courier_plugin`。测试与生产一律经该包路径导入（`maimai_courier_plugin.config`、
`maimai_courier_plugin.shared.delivery`），本文件不注册任何顶层别名：少一套名字映射，
就少一条「两处 import 指向不同模块对象」的分叉路径。公共加载另由无 conftest 的
子进程验证（`tests/test_repo_isolation.py`）。仅影响 pytest 运行，MaiBot 运行时不会
加载本文件。
"""

from __future__ import annotations

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
