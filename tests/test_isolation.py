"""第7组公共加载与验证入口：历史缺失不影响插件，历史内容只作完整性核对。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_无服务历史和第三方运行环境仍能公开创建并注册组件() -> None:
    """独立解释器只装 SDK 替身，不使用 conftest 的模块别名。"""

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
assert len(components) == 3
assert sum(component.get("chat_scope") == "group" for component in components) == 1
assert plugin.build_default_config()["snowluma"] == {"websocket_url": "", "token": "", "token_env_var": ""}
assert "shared" not in sys.modules
from isolated_courier import shared, clock, delivery_ledger, forward_delivery, send_material
from isolated_courier import auto_entry, forward_precheck, snowluma_client, snowluma_transport
from isolated_courier.shared import identifiers, mentions, send_steps
assert shared.Clock is clock.Clock is snowluma_transport.Clock is snowluma_client.Clock
assert shared.DeliveryKey is delivery_ledger.DeliveryKey
assert shared.LedgerStatus is delivery_ledger.LedgerStatus is forward_delivery.SendStatus
assert shared.DeliveryResult is forward_delivery.DeliveryResult
assert shared.SourceSendResult is forward_delivery.SourceSendResult is snowluma_client.SourceSendResult
assert shared.render_forward_result is forward_delivery.render_forward_result
assert mentions.mention_display_text is shared.mention_display_text is send_material.mention_display_text
assert identifiers.safe_identifier is auto_entry.safe_identifier
assert identifiers.verbatim_identifier is forward_precheck.verbatim_identifier is send_material.verbatim_identifier
assert send_steps.PreparedSource is send_material.PreparedSource is snowluma_client.PreparedSource
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
def test_默认CLI仅收集活跃测试且直连与纯逻辑均保留(selection: tuple[str, ...]) -> None:
    """真正运行收集命令，不执行历史业务，也不靠测试文件前缀排除。"""

    import importlib.util

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
        active_files.difference_update({"tests/test_snowluma_transport.py", "tests/test_direct_acceptance.py"})
    assert nodes == active_files, "根收集与 tests/ 必须同范围，历史用例不能进入默认验证"
    assert {"tests/test_snowluma_client.py", "tests/test_shared_types.py", "tests/test_config.py"} <= nodes
    assert not any("history" in name or "service" in name or "remote_client" in name for name in nodes)


def test_公开配置和共享导出不再提供服务控制协议() -> None:
    import config
    import shared

    assert not hasattr(config, "ServiceSection"), "旧服务节只能留在历史，不是活跃配置模型"
    assert not any(hasattr(shared, name) for name in (
        "SessionCreateRequest", "StageAuthorization", "PrepareRequest", "LedgerRepository",
    ))
    assert not any(name in sys.modules for name in (
        "remote_client", "shared.protocol", "shared.ledger",
        "maimai_courier_plugin.remote_client", "maimai_courier_plugin.shared.protocol",
        "maimai_courier_plugin.shared.ledger",
    )), "pytest 公共加载也不能借旧别名装载服务协议"


def test_历史109项基线完整且清单自身未被改写() -> None:
    """只读取已固定的源码快照，不导入或运行任何历史业务。"""

    import hashlib
    import re

    history = ROOT / "history/snowluma-forward-service"
    manifest = (history / "MANIFEST.md").read_bytes()
    assert hashlib.sha256(manifest).hexdigest() == (
        "7f8f26383872fab09163b99d13833765723d776e9db3eec533b1041ab844e582"
    )
    entries = re.findall(
        r"^- `([^`]+)`（(\d+)字节；[^\n]+）\n  - 用途：[^\n]+\n  - SHA-256：`([0-9a-f]{64})`",
        manifest.decode("utf-8"),
        re.MULTILINE,
    )
    assert len(entries) == len({name for name, _, _ in entries}) == 109
    assert sum(int(size) for _, size, _ in entries) == 1633141
    for name, size, digest in entries:
        path = history / name
        assert path.resolve().is_relative_to(history)
        assert path.is_symlink() == (name == "CLAUDE.md")
        if path.is_symlink():
            assert str(path.readlink()) == "AGENTS.md"
            data = str(path.readlink()).encode("utf-8")
        else:
            data = path.read_bytes()
        assert len(data) == int(size), name
        assert hashlib.sha256(data).hexdigest() == digest, name


def test_生产导入图不经过退役模块且原入口没有发送实现() -> None:
    import ast

    production = [path for path in ROOT.glob("*.py") if path.name != "conftest.py"]
    production += list((ROOT / "shared").glob("*.py"))
    production += list((ROOT / "service").glob("*.py"))
    forbidden = {"service", "remote_client", "history"}
    for path in production:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "") + "." + alias.name for alias in node.names]
            else:
                continue
            assert not any(forbidden.intersection(name.split(".")) for name in names), path
            assert not any("shared.protocol" in name or "shared.ledger" in name for name in names), path
    for relative in ("host_send.py", "remote_client.py", "service/__init__.py"):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        assert "退役" in (ast.get_docstring(tree) or ""), relative
        assert not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Call))
                       for node in ast.walk(tree)), relative


def test_旧独立服务CLI不再提供启动入口() -> None:
    completed = subprocess.run(
        [sys.executable, "-S", "-m", "service", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode != 0, "旧独立服务不再作为可部署CLI提供帮助或启动入口"
    assert "service.__main__" in completed.stderr
