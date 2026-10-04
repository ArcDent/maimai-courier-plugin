"""2.3 独立服务 CLI 与项目级运行环境：帮助面、缺配置失败与依赖锁定。

判据来自 design D1/D5/D10 与第 1 组 1.5 的核实证据：

- 服务入口是 `python -m service`（服务目录自带项目环境，`uv sync` / `uv run`）；
- 帮助面独立展示：启动、`ledger fresh` / `ledger migrate` 初始化与 `ledger export`
  兼容导出用法，后续组沿用这些命令名，不猜名称；
- 服务进程不导入 `maibot_sdk`，也不在帮助/导入阶段加载网络库（依赖缺失时只在真正
  启动时才明确失败）；
- 缺配置时非零退出并给出中文原因，且不建立任何网络连接、不发送；
- 依赖按 1.5 核实的宿主约束锁定（`aiohttp>=3.14.3`），只声明在项目环境里。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

EXPECTED_CLI_COMMANDS = ("start", "ledger fresh", "ledger migrate", "ledger export")


def _run_module(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "service", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_顶层帮助列出启动与账本管理用法() -> None:
    completed = _run_module("--help")

    assert completed.returncode == 0, completed.stderr
    for command in EXPECTED_CLI_COMMANDS:
        assert command in completed.stdout, (command, completed.stdout)


def test_账本管理帮助列出fresh与migrate及导出用法() -> None:
    ledger_help = _run_module("ledger", "--help")
    assert ledger_help.returncode == 0, ledger_help.stderr
    for command in ("fresh", "migrate", "export", "status"):
        assert command in ledger_help.stdout, (command, ledger_help.stdout)

    start_help = _run_module("start", "--help")
    assert start_help.returncode == 0, start_help.stderr
    assert "--config" in start_help.stdout, start_help.stdout

    migrate_help = _run_module("ledger", "migrate", "--help")
    assert migrate_help.returncode == 0, migrate_help.stderr
    for option in ("--state-dir", "--snapshot"):
        assert option in migrate_help.stdout, (option, migrate_help.stdout)

    export_help = _run_module("ledger", "export", "--help")
    assert export_help.returncode == 0, export_help.stderr
    assert "--output" in export_help.stdout, export_help.stdout


def test_服务进程不导入SDK也不在导入期加载网络库() -> None:
    code = "\n".join(
        [
            "import json, sys",
            "import service.cli",
            "import shared",
            "try:",
            "    import maibot_sdk",
            "except ImportError:",
            "    sdk_present = False",
            "else:",
            "    sdk_present = True",
            "print(json.dumps({",
            "    'sdk_present': sdk_present,",
            "    'aiohttp_loaded': 'aiohttp' in sys.modules,",
            "}))",
        ]
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["sdk_present"] is False
    assert payload["aiohttp_loaded"] is False, "帮助与导入阶段不得加载网络库"


def test_缺配置启动明确失败且不建立服务() -> None:
    missing = _run_module("start")
    assert missing.returncode != 0, missing.stdout
    assert "配置" in (missing.stdout + missing.stderr), (missing.stdout, missing.stderr)

    absent = _run_module("start", "--config", str(ROOT / "不存在的服务配置.toml"))
    assert absent.returncode != 0, absent.stdout
    assert "配置" in (absent.stdout + absent.stderr), (absent.stdout, absent.stderr)

    unknown = _run_module("不存在的命令")
    assert unknown.returncode != 0
    assert "start" in (unknown.stdout + unknown.stderr)


def test_账本命令参数按公布的选项解析() -> None:
    from dataclasses import asdict

    from service.cli import main

    recorded: list[tuple[str, dict[str, object]]] = []

    class _RecordingCommands:
        def start(self, options: object) -> int:
            recorded.append(("start", asdict(options)))
            return 0

        def ledger_fresh(self, options: object) -> int:
            recorded.append(("ledger_fresh", asdict(options)))
            return 0

        def ledger_migrate(self, options: object) -> int:
            recorded.append(("ledger_migrate", asdict(options)))
            return 0

        def ledger_export(self, options: object) -> int:
            recorded.append(("ledger_export", asdict(options)))
            return 0

        def ledger_status(self, options: object) -> int:
            recorded.append(("ledger_status", asdict(options)))
            return 0

    assert main(["ledger", "fresh", "--state-dir", "/tmp/x"], commands=_RecordingCommands()) == 0
    assert main(
        ["ledger", "migrate", "--state-dir", "/tmp/x", "--snapshot", "/tmp/old.sqlite3"],
        commands=_RecordingCommands(),
    ) == 0
    assert main(
        ["ledger", "export", "--state-dir", "/tmp/x", "--output", "/tmp/out.sqlite3"],
        commands=_RecordingCommands(),
    ) == 0
    assert main(
        ["start", "--config", "/tmp/service.toml"],
        commands=_RecordingCommands(),
    ) == 0

    assert [name for name, _ in recorded] == [
        "ledger_fresh",
        "ledger_migrate",
        "ledger_export",
        "start",
    ]
    assert recorded[0][1]["state_dir"] == "/tmp/x"
    assert recorded[1][1]["snapshot"] == "/tmp/old.sqlite3"
    assert recorded[2][1]["output"] == "/tmp/out.sqlite3"
    assert recorded[3][1]["config"] == "/tmp/service.toml"


def test_项目环境锁定已核实的网络依赖且不打包插件() -> None:
    import tomllib

    pyproject = ROOT / "pyproject.toml"
    assert pyproject.is_file(), "服务需要项目级运行环境（第 1 组 1.5 核实结果）"

    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data["project"]
    assert project["requires-python"] == ">=3.11", (
        "服务端用标准库 tomllib 解析配置（宿主 MaiBot 要求 >=3.12），不新增依赖"
    )
    assert "aiohttp>=3.14.3" in project["dependencies"], project["dependencies"]
    assert data["tool"]["uv"]["package"] is False, "项目环境只装依赖，不把插件目录打包安装"
