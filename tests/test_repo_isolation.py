"""隔离与收集守卫、项目规则守卫（合并自已退役的 `tests/test_isolation.py` 与
`tests/test_project_rules.py`）。

两节互不相关但同属「仓库边界」守卫，单独成文件避免与组件注册用例混在一起：
- 隔离守卫：只装 SDK 替身的独立解释器可公开加载插件（不 import 退役模块或网络库、
  共享层身份不漂移、默认直连配置为 `http_url` 三键）；
- 收集守卫：真正运行收集命令，默认收集面只有活跃用例、真实回环验收按档位参与；
- 生产模块守卫：生产代码不依赖复读插件、只导入 SDK 根导出名字、不含旧发送或宿主
  旁路调用；
- 项目规则守卫：`CLAUDE.md` 是指向 `AGENTS.md` 的相对软链接，不维护两份规则副本。

历史目录（`history/`）由 pytest.ini 的 testpaths/norecursedirs 排除，这里用真实
收集命令复核该边界。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fake_maibot_sdk import SDK_ROOT_EXPORTS

ROOT = Path(__file__).resolve().parent.parent

# 复读插件的模块/包名：本插件的生产代码不得 import 或以任何形式依赖它们。
_OLD_PROJECT_MODULES = {"forwards", "reactions", "repeats", "repeater_plugin"}


def _production_files() -> list[Path]:

    return sorted(path for path in ROOT.glob("*.py") if path.stem != "conftest")


def _skips_without_aiohttp(path: Path) -> bool:

    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "importorskip" or not node.args:
            continue
        argument = node.args[0]
        if isinstance(argument, ast.Constant) and argument.value == "aiohttp":
            return True
    return False


def test_manifest_声明独立插件与已核实能力() -> None:
    manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 2
    assert manifest["id"] == "arcdent.maimai-courier-plugin"
    assert manifest["version"] == "0.1.0"
    # 直连仍复用 1.5 核实的 aiohttp 下限；插件依赖只经清单声明。
    assert manifest["dependencies"] == [
        {"type": "python_package", "name": "aiohttp", "version_spec": ">=3.14.3"}
    ]
    # 直连不增加 api.call、send.*、chat.open_session 或反向监听能力。
    assert set(manifest["capabilities"]) == {
        "message.get_by_id",
        "chat.get_all_streams",
        "maisaka.proactive.trigger",
    }
    assert manifest["sdk"] == {"min_version": "2.8.0", "max_version": "2.99.99"}
    assert manifest["host_application"]["min_version"] == "1.2.5"


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

    imported: set[str] = set()
    for path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "maibot_sdk":
                imported.update(alias.name for alias in node.names)
    assert imported, "生产代码应经 maibot_sdk 顶层导入 SDK 面"
    unknown = imported - SDK_ROOT_EXPORTS
    assert unknown == set(), f"导入了真实 SDK 顶层不存在的名字: {sorted(unknown)}"


def test_生产代码不含旧发送或宿主旁路调用() -> None:

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


def test_公开加载不依赖退役模块或网络库且默认配置为HTTP直连() -> None:

    program = r'''
import importlib.abc
import importlib.util
import sys
import types
from pathlib import Path

root = Path(sys.argv[1])

class NoRetiredImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        parts = fullname.split(".")
        if (parts[0] in {"service", "remote_client", "history", "aiohttp"}
            or "service" in parts or "remote_client" in parts
            or fullname.endswith(("shared.protocol", "shared.ledger"))):
            raise AssertionError("公开加载依赖了退役模块或网络库：" + fullname)

sys.meta_path.insert(0, NoRetiredImports())
spec = importlib.util.spec_from_file_location("maibot_sdk", root / "tests/fake_maibot_sdk.py")
sdk = importlib.util.module_from_spec(spec)
sys.modules["maibot_sdk"] = sdk
spec.loader.exec_module(sdk)
pkg = types.ModuleType("isolated_courier")
pkg.__path__ = [str(root)]
sys.modules[pkg.__name__] = pkg
from isolated_courier.plugin import create_plugin

plugin = create_plugin()
components = plugin.get_components()
# 两个群聊 Tool（forward_message / list_forward_friends）+ 两个 Hook。
assert len(components) == 4
assert sum(component.get("chat_scope") == "group" for component in components) == 2
assert plugin.build_default_config()["snowluma"] == {"http_url": "", "token": "", "token_env_var": ""}
from isolated_courier import delivery_ledger, forward_delivery, send_material
from isolated_courier import shared
from isolated_courier import auto_entry, forward_precheck, plugin, snowluma_client, snowluma_transport
from isolated_courier.shared import identifiers, mentions, send_steps
from isolated_courier.shared import clock as shared_clock, delivery as shared_delivery
from isolated_courier.shared import result as shared_result
assert shared_clock.Clock is snowluma_transport.Clock is snowluma_client.Clock
assert shared_clock.Clock is forward_delivery.Clock is delivery_ledger.Clock is plugin.Clock
assert shared_clock.SystemClock is plugin.SystemClock
assert shared_delivery.DeliveryKey is forward_delivery.DeliveryKey
assert shared_delivery.LedgerStatus is forward_delivery.SendStatus
assert shared_delivery.LedgerUnavailable is delivery_ledger.LedgerUnavailable
assert shared_delivery.LedgerUnavailableError is delivery_ledger.LedgerUnavailableError
assert shared_result.DeliveryResult is forward_delivery.DeliveryResult
assert shared_result.SourceSendResult is forward_delivery.SourceSendResult is snowluma_client.SourceSendResult
assert shared_result.render_forward_result is forward_delivery.render_forward_result
assert send_steps.SourceKind is send_material.SourceKind
assert send_steps.PreparedSource is snowluma_client.PreparedSource
assert send_steps.PreparedSource is not getattr(send_material, "PreparedSource", None)
assert send_steps.SourceSendRequest is snowluma_client.SourceSendRequest
assert send_steps.CommentSendRequest is snowluma_client.CommentSendRequest
assert not hasattr(shared, "Clock"), "shared 包顶层不得重导出符号（0 兼容层）"
assert mentions.mention_display_text is send_material.mention_display_text
assert identifiers.safe_identifier is auto_entry.safe_identifier
assert identifiers.verbatim_identifier is forward_precheck.verbatim_identifier is send_material.verbatim_identifier
assert not any("history" in Path(getattr(module, "__file__", "") or "").parts
               for module in tuple(sys.modules.values()))
print("公开工厂与组件注册通过")
'''
    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-c", program, str(ROOT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "公开工厂与组件注册通过"


@pytest.mark.parametrize("selection", [(), ("tests/",)], ids=["仓库根", "显式tests"])
def test_默认CLI仅收集活跃测试且回环验收按档位参与(selection: tuple[str, ...]) -> None:

    completed = subprocess.run(
        [sys.executable, "-m", "pytest", *selection, "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout[-4000:] + completed.stderr
    nodes = {line.split("::")[0] for line in completed.stdout.splitlines() if "::test_" in line}
    active_files = {f"tests/{path.name}" for path in (ROOT / "tests").glob("test_*.py")}
    if importlib.util.find_spec("aiohttp") is None:
        gated = {f"tests/{path.name}" for path in (ROOT / "tests").glob("test_*.py")
                 if _skips_without_aiohttp(path)}
        active_files.difference_update(gated)
    assert nodes == active_files, "根收集与 tests/ 必须同范围，历史用例不能进入默认验证"
    assert {"tests/test_snowluma_client.py", "tests/test_shared_types.py",
            "tests/test_config.py", "tests/test_auto_entry.py",
            "tests/test_delivery_ledger.py"} <= nodes
    assert not any("history" in name or "service" in name or "remote_client" in name
                   for name in nodes)


def test_CLAUDE_是指向AGENTS的软链接() -> None:

    claude = ROOT / "CLAUDE.md"
    assert claude.is_symlink(), "CLAUDE.md 必须是软链接（ln -s AGENTS.md CLAUDE.md）"
    assert claude.readlink() == Path("AGENTS.md")
    assert claude.read_text(encoding="utf-8") == (ROOT / "AGENTS.md").read_text(encoding="utf-8")
