"""3.7 旧账本只读导入（design Migration Plan 3；旧 schema 事实来自第 1 组 1.6）。

- 旧 `sent` / `unknown` 保留阻断作用，回执（目标消息 ID）与评论结果一并保留；
- 旧 `sending` 归并为待核实（不自动续发）；
- 旧明确 `failed` 保留为可被新的显式尝试使用；
- 导入核对四字段键集合、记录数量与快照字节指纹；导入前后原快照与原库都不改写；
- `ledger migrate` CLI 端到端可用，并把数据集身份与导入统计写入元数据。

旧库一律由**旧公共接口**（`open_ledger` / `reserve` / `mark_*` / `record_comment`）
构造，不复制旧 SQL。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from delivery_ledger import LEDGER_FILE_NAME, STATUS_SENT, STATUS_UNKNOWN

from service.ledger_migration import (
    MigrationOutcome,
    initialize_fresh,
    read_ledger_status,
)
from service.ledger_store import SERVICE_LEDGER_FILE_NAME, ServiceLedger, open_service_ledger
from shared.delivery import (
    STATUS_FAILED,
    STATUS_SENDING,
    STATUS_SENT,
    STATUS_UNKNOWN,
    DeliveryKey,
    LedgerUnavailable,
)
from shared.protocol import delivery_id_for

from tests.service_support import close_ledger, open_state_ledger
from tests.support import FakeClock, ledger_key, open_test_ledger

ROOT = Path(__file__).resolve().parent.parent
IDENTITY_PATTERN = re.compile(r"[0-9a-f]{32}")
DELIVERY_TABLE = "delivery_ledger"
"""旧库与服务端投递表同名（1.6 核实 + 3.1 的兼容契约）。"""

LEGACY_KEYS: dict[str, DeliveryKey] = {
    "sent": ledger_key(message_id="m-legacy-sent"),
    "unknown": ledger_key(message_id="m-legacy-unknown"),
    "sending": ledger_key(message_id="m-legacy-sending"),
    "failed": ledger_key(message_id="m-legacy-failed"),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_legacy_snapshot(tmp_path: Path) -> tuple[Path, Path]:
    """用旧公共接口合成旧账本，再复制成只读快照；返回（原库路径, 快照路径）。"""

    legacy_dir = tmp_path / "legacy-data"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    ledger = open_test_ledger(legacy_dir)
    try:
        sent = LEGACY_KEYS["sent"]
        assert ledger.reserve(sent).granted is True
        assert ledger.mark_sent(sent, "m-legacy-target-1") is True
        assert ledger.record_comment(sent, status=STATUS_SENT, message_id="m-legacy-comment-1") is True

        unknown = LEGACY_KEYS["unknown"]
        assert ledger.reserve(unknown).granted is True
        assert ledger.mark_unknown(unknown, "回执丢失") is True

        sending = LEGACY_KEYS["sending"]
        assert ledger.reserve(sending).granted is True

        failed = LEGACY_KEYS["failed"]
        assert ledger.reserve(failed).granted is True
        assert ledger.mark_failed(failed, "平台调用前未发出") is True
    finally:
        ledger.close()

    original = legacy_dir / LEDGER_FILE_NAME
    snapshot = tmp_path / "legacy-snapshot.sqlite3"
    shutil.copyfile(original, snapshot)
    return original, snapshot


def _legacy_snapshot_key_set(path: Path) -> set[tuple[str, str, str, str]]:
    """用独立的只读连接读取快照的四字段键集合（不经迁移实现）。"""

    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5.0)
    try:
        return {
            (str(row[0]), str(row[1]), str(row[2]), str(row[3]))
            for row in connection.execute(
                "SELECT account_id, source_group_id, source_message_id, target_group_id "
                f"FROM {DELIVERY_TABLE}"
            )
        }
    finally:
        connection.close()


def _service_key_set(state_dir: Path) -> set[tuple[str, str, str, str]]:
    connection = sqlite3.connect(
        str(state_dir / SERVICE_LEDGER_FILE_NAME), timeout=5.0, isolation_level=None
    )
    try:
        return {
            (str(row[0]), str(row[1]), str(row[2]), str(row[3]))
            for row in connection.execute(
                f"SELECT account_id, source_group_id, source_message_id, target_group_id "
                f"FROM {DELIVERY_TABLE}"
            )
        }
    finally:
        connection.close()


def _reserve(ledger: ServiceLedger, key: DeliveryKey, *, attempt_id: str) -> bool:
    return asyncio.run(
        ledger.reserve_source(
            key,
            attempt_id=attempt_id,
            request_id=f"req-{attempt_id}",
            session_id="sess-1",
            policy_generation=1,
        )
    ).granted


def _lookup(ledger: ServiceLedger, key: DeliveryKey) -> object:
    return asyncio.run(ledger.lookup_delivery(delivery_id_for(key)))


def test_迁移保留阻断作用回执与键集合(tmp_path: Path) -> None:
    from service.ledger_migration import migrate_legacy_snapshot

    original, snapshot = _build_legacy_snapshot(tmp_path)
    original_digest = _sha256(original)
    snapshot_digest = _sha256(snapshot)
    legacy_keys = _legacy_snapshot_key_set(snapshot)
    state_dir = tmp_path / "service-state"

    outcome = migrate_legacy_snapshot(state_dir, snapshot, clock=FakeClock())

    assert isinstance(outcome, MigrationOutcome), outcome
    assert outcome.already_imported is False
    assert outcome.records == 4
    assert dict(outcome.legacy_statuses) == {
        STATUS_SENT: 1,
        STATUS_UNKNOWN: 1,
        STATUS_SENDING: 1,
        STATUS_FAILED: 1,
    }, "导入统计必须如实反映快照里的原状态分布"
    assert dict(outcome.statuses) == {
        STATUS_SENT: 1,
        STATUS_UNKNOWN: 2,
        STATUS_FAILED: 1,
    }, "`sending` 必须按待核实归并"
    assert outcome.snapshot_sha256 == snapshot_digest
    assert IDENTITY_PATTERN.fullmatch(outcome.ledger_identity), "首次迁移必须生成账本身份"

    assert _sha256(snapshot) == snapshot_digest, "导入前后原快照字节指纹必须不变"
    assert _sha256(original) == original_digest, "迁移只读快照，不得改写旧库"
    assert _service_key_set(state_dir) == legacy_keys, "四字段键集合必须逐一对应"

    ledger = open_service_ledger(state_dir)
    try:
        sent = _lookup(ledger, LEGACY_KEYS["sent"])
        assert sent is not None
        assert (sent.status, sent.target_message_id) == (STATUS_SENT, "m-legacy-target-1")
        assert (sent.comment_status, sent.comment_message_id) == (
            STATUS_SENT,
            "m-legacy-comment-1",
        ), "旧评论结果必须保留"

        unknown = _lookup(ledger, LEGACY_KEYS["unknown"])
        assert unknown is not None and unknown.status == STATUS_UNKNOWN

        sending = _lookup(ledger, LEGACY_KEYS["sending"])
        assert sending is not None
        assert sending.status == STATUS_UNKNOWN, "旧发送中必须归并为待核实"
        assert "待核实" in sending.reason

        failed = _lookup(ledger, LEGACY_KEYS["failed"])
        assert failed is not None
        assert failed.status == STATUS_FAILED and failed.reason == "平台调用前未发出"

        assert _reserve(ledger, LEGACY_KEYS["sent"], attempt_id="attempt-sent") is False
        assert _reserve(ledger, LEGACY_KEYS["unknown"], attempt_id="attempt-unknown") is False
        assert _reserve(ledger, LEGACY_KEYS["sending"], attempt_id="attempt-sending") is False
        assert _reserve(ledger, LEGACY_KEYS["failed"], attempt_id="attempt-failed") is True, (
            "旧明确失败必须可供新的显式尝试"
        )
    finally:
        close_ledger(ledger)

    report = read_ledger_status(state_dir)
    assert hasattr(report, "ledger_identity"), report
    assert report.ledger_identity == outcome.ledger_identity
    assert len(report.imports) == 1
    assert report.imports[0].snapshot_sha256 == snapshot_digest
    assert report.imports[0].records == 4


def test_migrate_CLI端到端并可与fresh互斥(tmp_path: Path) -> None:
    _original, snapshot = _build_legacy_snapshot(tmp_path)
    state_dir = tmp_path / "service-state"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "service",
            "ledger",
            "migrate",
            "--state-dir",
            str(state_dir),
            "--snapshot",
            str(snapshot),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    assert "身份" in output, output
    identity = next(
        (token for token in re.findall(r"[0-9a-f]{32}", output)),
        "",
    )
    assert identity, output

    status = subprocess.run(
        [sys.executable, "-m", "service", "ledger", "status", "--state-dir", str(state_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert status.returncode == 0, status.stdout + status.stderr
    assert identity in status.stdout

    ledger = open_state_ledger(state_dir)
    try:
        assert asyncio.run(ledger.identity()) == identity
    finally:
        close_ledger(ledger)

    fresh = initialize_fresh(state_dir, clock=FakeClock())
    assert isinstance(fresh, LedgerUnavailable), "迁移过的账本不得再被 fresh 覆盖"
    assert "fresh" in fresh.reason
