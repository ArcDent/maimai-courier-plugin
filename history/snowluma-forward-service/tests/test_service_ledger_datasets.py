"""3.6 显式新装初始化与数据集绑定（design D10、Migration Plan 2/4）。

- `ledger fresh` 生成稳定账本身份：跨进程重启保持一致，不同数据目录身份不同；
- 已有状态（身份/记录/旧插件账本）不得隐式覆盖成 fresh；
- 缺预期账本 ID、确认 ID 不符或连到另一个（空）数据集时禁止发送；
- 数据集元数据与客户端迁移收据不含凭证：只保存身份、schema 版本、创建时间、导入统计。
"""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

from service.ledger_migration import (
    FreshOutcome,
    LedgerStatusReport,
    initialize_fresh,
    read_ledger_status,
)
from service.ledger_store import (
    DELIVERY_TABLE,
    META_IDENTITY,
    META_TABLE,
    SERVICE_LEDGER_FILE_NAME,
    ServiceLedger,
    open_service_ledger,
)
from shared.delivery import LedgerUnavailable, LedgerUnavailableError
from shared.ledger import SourceReservation
from shared.protocol import delivery_id_for

from tests.service_support import close_ledger, open_state_ledger, state_ledger_rows
from tests.support import FakeClock, ledger_key

ROOT = Path(__file__).resolve().parent.parent
TOKEN_ENV_VAR = "MC_DATASET_TEST_TOKEN"
TOKEN_VALUE = "dataset-secret-token"
IDENTITY_PATTERN = re.compile(r"[0-9a-f]{32}")


def _fresh(state_dir: Path, *, confirm: str = "") -> FreshOutcome | LedgerUnavailable:
    return initialize_fresh(state_dir, confirm_ledger_id=confirm, clock=FakeClock())


def _identity(state_dir: Path) -> str:
    ledger = open_state_ledger(state_dir)
    try:
        return asyncio.run(ledger.identity())
    finally:
        close_ledger(ledger)


def _run_module(*args: str, environ: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.update(environ or {})
    return subprocess.run(
        [sys.executable, "-m", "service", *args],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_新装初始化生成稳定账本身份且跨进程重启一致(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"

    outcome = _fresh(state_dir)

    assert isinstance(outcome, FreshOutcome), outcome
    assert outcome.created is True
    assert IDENTITY_PATTERN.fullmatch(outcome.ledger_identity), (
        "账本身份必须是安全可引用的稳定标识符"
    )

    assert _identity(state_dir) == outcome.ledger_identity, "重开账本身份必须一致"

    status = _run_module("ledger", "status", "--state-dir", str(state_dir))
    assert status.returncode == 0, status.stdout + status.stderr
    assert outcome.ledger_identity in (status.stdout + status.stderr), "跨进程读取身份必须一致"

    profile = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                [
                    "import asyncio, json, sys",
                    "from pathlib import Path",
                    "from service.ledger_store import open_service_ledger",
                    "ledger = open_service_ledger(Path(sys.argv[1]))",
                    "print(json.dumps({'identity': asyncio.run(ledger.identity())}))",
                ]
            ),
            str(state_dir),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert profile.returncode == 0, profile.stderr
    assert outcome.ledger_identity in profile.stdout

    other_dir = tmp_path / "other-state"
    other = _fresh(other_dir)
    assert isinstance(other, FreshOutcome)
    assert other.ledger_identity != outcome.ledger_identity, "不同数据集身份必须不同"


def test_已有状态不得隐式覆盖成fresh(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    first = _fresh(state_dir)
    assert isinstance(first, FreshOutcome)

    implicit = _fresh(state_dir)
    assert isinstance(implicit, LedgerUnavailable)
    assert "fresh" in implicit.reason and first.ledger_identity in implicit.reason, implicit.reason

    wrong = _fresh(state_dir, confirm="0" * 32)
    assert isinstance(wrong, LedgerUnavailable)
    assert "不符" in wrong.reason, wrong.reason

    confirmed = _fresh(state_dir, confirm=first.ledger_identity)
    assert isinstance(confirmed, FreshOutcome)
    assert confirmed.created is False and confirmed.ledger_identity == first.ledger_identity, (
        "确认既有身份必须幂等，不得改动记录"
    )

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    stray = _fresh(empty_dir, confirm=first.ledger_identity)
    assert isinstance(stray, LedgerUnavailable), "空目标不接受属于另一个数据集的确认 ID"

    legacy_dir = tmp_path / "legacy-install"
    legacy_dir.mkdir()
    connection = sqlite3.connect(str(legacy_dir / "delivery_ledger.sqlite3"), isolation_level=None)
    try:
        connection.execute(
            f"""
            CREATE TABLE {DELIVERY_TABLE} (
                account_id TEXT NOT NULL,
                source_group_id TEXT NOT NULL,
                source_message_id TEXT NOT NULL,
                target_group_id TEXT NOT NULL,
                status TEXT NOT NULL,
                target_message_id TEXT NOT NULL DEFAULT '',
                comment_status TEXT NOT NULL DEFAULT '',
                comment_message_id TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (account_id, source_group_id, source_message_id, target_group_id)
            )
            """
        )
        connection.execute(
            f"INSERT INTO {DELIVERY_TABLE} VALUES ('10000','10001','m-old','20002','sent','m-1','','','',1,2)"
        )
    finally:
        connection.close()

    with_legacy = _fresh(legacy_dir)
    assert isinstance(with_legacy, LedgerUnavailable), "已有安装只允许 migrate"
    assert "migrate" in with_legacy.reason, with_legacy.reason

    records_first = _fresh(tmp_path / "records-only")
    assert isinstance(records_first, FreshOutcome)
    ledger = open_state_ledger(tmp_path / "records-only", clock=FakeClock())
    try:
        asyncio.run(
            ledger.reserve_source(
                ledger_key(),
                attempt_id="attempt-1",
                request_id="req-1",
                session_id="sess-1",
                policy_generation=1,
            )
        )
    finally:
        close_ledger(ledger)
    connection = sqlite3.connect(
        str(tmp_path / "records-only" / SERVICE_LEDGER_FILE_NAME), isolation_level=None
    )
    try:
        connection.execute(f"DELETE FROM {META_TABLE} WHERE key = ?", (META_IDENTITY,))
    finally:
        connection.close()
    orphan = _fresh(tmp_path / "records-only")
    assert isinstance(orphan, LedgerUnavailable), "无身份但有记录的账本不得当作空账本重新新装"
    assert "来源不明" in orphan.reason, orphan.reason


def test_缺预期账本ID或身份不符时禁止发送(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    fresh = _fresh(state_dir)
    assert isinstance(fresh, FreshOutcome)

    unbound = open_service_ledger(state_dir)
    assert isinstance(unbound, ServiceLedger), "管理/测试用的无绑定打开仍可用"
    close_ledger(unbound)

    missing = open_service_ledger(state_dir, expected_ledger_id="")
    assert isinstance(missing, LedgerUnavailable)
    assert "预期" in missing.reason, missing.reason

    mismatched = open_service_ledger(state_dir, expected_ledger_id="0" * 32)
    assert isinstance(mismatched, LedgerUnavailable)
    assert "不符" in mismatched.reason, mismatched.reason

    bound = open_service_ledger(state_dir, expected_ledger_id=fresh.ledger_identity)
    assert isinstance(bound, ServiceLedger)
    close_ledger(bound)

    another_empty = tmp_path / "another-empty"
    another_empty.mkdir()
    wrong_dataset = open_service_ledger(
        another_empty, expected_ledger_id=fresh.ledger_identity
    )
    assert isinstance(wrong_dataset, LedgerUnavailable), (
        "误连另一空库必须禁止发送（不得按地址自动信任新数据集）"
    )

    config = tmp_path / "service.toml"
    config.write_text(
        "\n".join(
            [
                "[service]",
                'client_identity = "maibot-plugin"',
                'account_id = "10000"',
                f'state_dir = "{state_dir}"',
                f'ledger_id = "{"0" * 32}"',
                f'token_env_var = "{TOKEN_ENV_VAR}"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    started = _run_module(
        "start", "--config", str(config), environ={TOKEN_ENV_VAR: TOKEN_VALUE}
    )
    output = started.stdout + started.stderr
    assert started.returncode == 5, output
    assert "账本" in output and "不符" in output, output


def test_数据集元数据与迁移收据不含凭证(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"

    completed = _run_module(
        "ledger", "fresh", "--state-dir", str(state_dir), environ={TOKEN_ENV_VAR: TOKEN_VALUE}
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    ledger_file = state_dir / SERVICE_LEDGER_FILE_NAME
    assert TOKEN_VALUE.encode() not in ledger_file.read_bytes(), "凭证不得写入数据集元数据"

    connection = sqlite3.connect(str(ledger_file), isolation_level=None)
    try:
        meta = {str(row[0]): str(row[1]) for row in connection.execute(f"SELECT key, value FROM {META_TABLE}")}
    finally:
        connection.close()

    assert set(meta) <= {"schema_version", "ledger_identity", "created_at", "imports"}, meta
    forbidden = ("token", "secret", "password", "credential", "env")
    for key, value in meta.items():
        assert not any(word in key.lower() for word in forbidden), key
        assert not any(word in value.lower() for word in forbidden), (key, value)


def test_账本状态只读报告身份与计数(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    fresh = _fresh(state_dir)
    assert isinstance(fresh, FreshOutcome)
    ledger = open_state_ledger(state_dir, clock=FakeClock())
    try:
        sent_key = ledger_key(message_id="m-sent")
        assert asyncio.run(
            ledger.reserve_source(
                sent_key,
                attempt_id="attempt-sent",
                request_id="req-1",
                session_id="sess-1",
                policy_generation=1,
            )
        ).granted is True
        asyncio.run(
            ledger.finalize_source(
                sent_key,
                attempt_id="attempt-sent",
                status="sent",
                target_message_id="m-target-1",
            )
        )
        asyncio.run(
            ledger.reserve_source(
                ledger_key(message_id="m-sending"),
                attempt_id="attempt-sending",
                request_id="req-2",
                session_id="sess-1",
                policy_generation=1,
            )
        )
    finally:
        close_ledger(ledger)

    report = read_ledger_status(state_dir)

    assert isinstance(report, LedgerStatusReport), report
    assert report.initialized is True
    assert report.ledger_identity == fresh.ledger_identity
    assert report.schema_version == "1"
    assert dict(report.deliveries) == {"sent": 1, "sending": 1}
    assert report.attempts == 2
    assert report.imports == ()
    assert state_ledger_rows(state_dir) == [
        ("m-sent", "sent", "m-target-1"),
        ("m-sending", "sending", ""),
    ]

    status = _run_module("ledger", "status", "--state-dir", str(state_dir))
    assert status.returncode == 0, status.stdout + status.stderr
    assert fresh.ledger_identity in status.stdout

    absent = read_ledger_status(tmp_path / "absent")
    assert isinstance(absent, LedgerUnavailable), "未初始化的目录必须如实报告不可用"
    absent_cli = _run_module("ledger", "status", "--state-dir", str(tmp_path / "absent"))
    assert absent_cli.returncode != 0
