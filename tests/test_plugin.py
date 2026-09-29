"""插件骨架测试：组件注册、生命周期名称与对复读插件的独立性。"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import sys
from pathlib import Path

from plugin import CourierPlugin, create_plugin
from tests.fake_maibot_sdk import SDK_ROOT_EXPORTS
from tests.support import FakeClock, find_hook, find_tool, make_plugin, merged_forward_message

ROOT = Path(__file__).resolve().parent.parent

# 复读插件的模块/包名：本插件的生产代码不得 import 或以任何形式依赖它们。
_OLD_PROJECT_MODULES = {"forwards", "reactions", "repeats", "repeater_plugin"}


def _production_files() -> list[Path]:
    """仓库根下的生产模块（conftest.py 是 pytest 专用，不算生产代码）。"""

    return sorted(path for path in ROOT.glob("*.py") if path.stem != "conftest")


def test_工厂可创建插件并注册群聊工具与观察Hook() -> None:
    plugin = create_plugin()
    assert isinstance(plugin, CourierPlugin)

    tool = find_tool(plugin, "forward_message")
    assert tool["chat_scope"] == "group"
    assert "chat_scope" not in tool["metadata"], "chat_scope 只保留在组件声明顶层"

    hook = find_hook(plugin, "chat.receive.after_process")
    assert hook["metadata"]["mode"] == "observe"
    assert hook["metadata"]["handler_name"] == "observe_received_message"


def test_三生命周期名称与宿主调用契约一致(tmp_path: Path) -> None:
    plugin, _ = make_plugin(tmp_path)
    for name in ("on_load", "on_unload", "on_config_update"):
        assert inspect.iscoroutinefunction(getattr(plugin, name)), f"{name} 必须是 async 生命周期"

    asyncio.run(plugin.on_load())
    asyncio.run(plugin.on_config_update("self", {}, ""))
    asyncio.run(plugin.on_unload())


def test_生产代码不依赖复读插件() -> None:
    imported: set[str] = set()
    for path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
    assert not (_OLD_PROJECT_MODULES & imported), f"生产代码引入了旧项目模块: {sorted(imported)}"
    assert not (_OLD_PROJECT_MODULES & set(sys.modules)), "测试进程加载了旧项目模块"


def test_生产代码只导入SDK顶层导出的名字() -> None:
    """真实 SDK 根只导出 `__all__` 里的名字（`HookMode` 等枚举不在其中）。"""

    imported: set[str] = set()
    for path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "maibot_sdk":
                imported.update(alias.name for alias in node.names)
    assert imported, "生产代码应经 maibot_sdk 顶层导入 SDK 面"
    unknown = imported - SDK_ROOT_EXPORTS
    assert unknown == set(), f"导入了真实 SDK 顶层不存在的名字: {sorted(unknown)}"


def test_manifest_声明独立插件与已核实能力() -> None:
    manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 2
    assert manifest["id"] == "arcdent.maimai-courier-plugin"
    assert manifest["version"] == "0.1.0"
    assert manifest["dependencies"] == []
    # 能力名逐一在 Host src/plugin_runtime/capabilities/registry.py 核实（第 1 组证据）；
    # 能力随插件整体声明、由第 3–7 组逐步消费，Hook 名称不是能力，不进本列表。
    # 第 3 组新增消费 `chat.get_all_streams`（registry.py:50，用于解析可信来源会话）。
    # 第 5 组新增消费 `send.image` / `send.hybrid`（registry.py:35-36，图片与图文混排
    # 的发送通路，与 `send.text` / `send.forward` 同属已核实的 send 能力族）。
    assert set(manifest["capabilities"]) == {
        "message.get_by_id",
        "chat.get_all_streams",
        "chat.open_session",
        "send.text",
        "send.image",
        "send.hybrid",
        "send.forward",
        "maisaka.proactive.trigger",
    }
    assert manifest["sdk"] == {"min_version": "2.8.0", "max_version": "2.99.99"}
    # Host 下限取线上部署机容器自报版本 1.2.5（其宿主代码与第 1 组参照 Host
    # `a995cc4` 的能力名/Hook 行号逐一对齐）；声明 1.3.2 会被线上宿主按版本校验
    # 拒绝加载，故下调下限而 SDK 声明不变（线上 SDK 2.8.2 满足 2.8.0-2.99.99）。
    assert manifest["host_application"]["min_version"] == "1.2.5"


def test_默认总开关关闭时工具调用不发送(tmp_path: Path) -> None:
    plugin, ctx = make_plugin(tmp_path)

    result = asyncio.run(
        plugin.forward_message(message_id="m-forward-1", target_group_id="20002", comment="看看这个")
    )

    assert result["success"] is False
    assert "总开关" in result["reason"]
    assert ctx.send.calls == [], "默认关闭时不得产生任何发送"


def test_默认总开关关闭时消息输入不发送(tmp_path: Path) -> None:
    plugin, ctx = make_plugin(tmp_path)

    asyncio.run(plugin.observe_received_message(**merged_forward_message()))

    assert ctx.send.calls == [], "默认关闭时消息输入不得产生任何发送"


def test_注入可控时钟后总开关关闭不产生等待与发送(tmp_path: Path) -> None:
    clock = FakeClock()
    plugin, ctx = make_plugin(tmp_path, clock=clock)

    asyncio.run(plugin.on_load())
    result = asyncio.run(
        plugin.forward_message(message_id="m-forward-1", target_group_id="20002")
    )

    assert result["success"] is False
    assert ctx.send.calls == []
    assert clock.sleeps == [], "总开关关闭时不得经注入时钟登记任何等待"
