"""插件骨架测试：组件注册、生命周期名称与对复读插件的独立性。"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import sys
from pathlib import Path
from typing import Any

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
    # 客户端异步依赖按 1.5 核实的受支持声明位置（`dependencies[]` 的
    # `{type:'python_package', name, version_spec}`）声明，且与宿主 pyproject 的
    # `aiohttp>=3.14.3` 同区间（`tests/test_service_cli.py` 另核项目环境声明）。
    assert manifest["dependencies"] == [
        {"type": "python_package", "name": "aiohttp", "version_spec": ">=3.14.3"}
    ]
    # 能力名逐一在 Host `src/plugin_runtime/capabilities/registry.py` 核实（第 1 组证据）；
    # 能力随插件整体声明、由第 3–7 组逐步消费，Hook 名称不是能力，不进本列表。
    # 第 6 组起全部发送改由专用服务执行：插件只保留「读源消息、解析可信来源会话、
    # 唤起源群 Planner」三项；`send.*` / `chat.open_session` 已从声明与调用图移除，
    # 没有旧发送通道 fallback（见 `test_forward_*` 的零 `ctx.send.*` 断言）。
    assert set(manifest["capabilities"]) == {
        "message.get_by_id",
        "chat.get_all_streams",
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


def test_全部支持类型与故障路径都不调用宿主旧发送通道(tmp_path: Path) -> None:
    """6.9：所有支持类型、评论与故障用例中旧 `ctx.send.*` 计数恒为零。

    新投递链只在服务端网关模块里调用平台发送（`service/gateway.py`）；插件侧
    `host_send.py` 已退出调用图，也没有 fallback。
    """

    from tests.client_support import make_client_service
    from tests.support import forward_node, forward_segment, image_segment, run_bound_forward, text_segment

    raw_messages: list[list[dict[str, Any]]] = [
        [text_segment("普通文字")],
        [image_segment()],
        [text_segment("配文"), image_segment()],
        [forward_segment(forward_node())],
    ]
    for index, raw_message in enumerate(raw_messages):
        fixture = make_client_service(tmp_path / f"type-{index}", raw_message=raw_message)
        result, _ = run_bound_forward(
            fixture.plugin, message_id="m-source-1", comment="附带评论", clock=fixture.clock
        )
        assert result["status"] == "sent", (index, result)
        assert fixture.old_send_calls() == [], (index, "新投递路径不得调用宿主旧发送通道")
        assert {call["capability"] for call in fixture.ctx.chat.calls} <= {"chat.get_all_streams"}, (
            index,
            "不得再解析宿主目标发送会话",
            fixture.ctx.chat.calls,
        )
        fixture.close()

    # 故障路径同样零旧通道调用：服务不可达与本地拒绝。
    unavailable = make_client_service(tmp_path / "unavailable")
    unavailable.transport.fail_paths.append("/v1/sessions")
    rejected, _ = run_bound_forward(unavailable.plugin, message_id="m-source-1")
    assert rejected["success"] is False
    assert unavailable.old_send_calls() == []
    unavailable.close()

    local_reject = make_client_service(tmp_path / "local-reject", raw_message=[{"type": "voice", "data": "v"}])
    rejected, _ = run_bound_forward(local_reject.plugin, message_id="m-source-1")
    assert rejected["status"] == "rejected"
    assert local_reject.old_send_calls() == []
    local_reject.close()


def test_生产代码不含旧发送或宿主旁路调用() -> None:
    """6.9：生产代码不直接调用宿主发送、数据库、上下文追加或第二个同路由网关。"""

    forbidden_prefixes = ("ctx.send", "ctx.api", "ctx.database", "ctx.gateway", "ctx.maisaka.context")
    forbidden_calls = {"send.text", "send.image", "send.hybrid", "send.forward"}
    offenders: list[str] = []

    def dotted(node: ast.AST) -> str:
        parts: list[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
        return ".".join(reversed(parts))

    for path in _production_files():
        if path.name == "host_send.py":
            continue  # 旧实现保留但已退出调用图（无 fallback），不算调用点
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            expression = ""
            if isinstance(node, ast.Call):
                expression = dotted(node.func)
            elif isinstance(node, ast.Attribute):
                expression = dotted(node)
            if not expression:
                continue
            if expression.startswith(forbidden_prefixes) or expression in forbidden_calls:
                offenders.append(f"{path.name}:{expression}")
    assert offenders == [], f"生产代码出现旧发送或宿主旁路调用: {sorted(set(offenders))}"
