"""3.9 包含新结果的回滚导出（Migration Plan 7；跨群规格「旧账本迁移与回滚防重」）。

- 服务停止发送后（状态目录独占锁可用）导出旧 schema 兼容快照，包含**旧记录与新投递**；
- 旧公共入口（`open_ledger` / `get` / `reserve`）能打开导出文件并阻断全部成功/未知键：
  包括「源已发但评论不确定」的记录（仍阻止源重发）；明确失败保留为可重试；
- 不确定的源状态保守阻断（在途 → 待核实）、评论不自动补齐；
- 导出失败不改写服务账本，也不覆盖已有输出文件；只靠迁移前快照不能恢复发送。
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from delivery_ledger import (
    DeliveryLedger,
    STATUS_FAILED,
    STATUS_SENT,
    STATUS_UNKNOWN,
    open_ledger,
)

from service.ledger_migration import (
    ExportOutcome,
    FreshOutcome,
    initialize_fresh,
    migrate_legacy_snapshot,
)
from service.ledger_store import (
    DELIVERY_TABLE,
    SERVICE_LEDGER_FILE_NAME,
    ServiceLedger,
    open_service_ledger,
)
from shared.delivery import STATUS_SENDING, DeliveryKey, LedgerUnavailable
from shared.protocol import delivery_id_for

from tests.service_support import (
    LegacyRecordSpec,
    build_legacy_snapshot,
    close_ledger,
    open_state_ledger,
)
from tests.support import FakeClock, ledger_key

ROOT = Path(__file__).resolve().parent.parent

MIGRATED_SENT = ledger_key(message_id="m-migrated-sent")
MIGRATED_UNKNOWN = ledger_key(message_id="m-migrated-unknown")
NEW_SENT = ledger_key(message_id="m-new-sent")
NEW_COMMENT_UNKNOWN = ledger_key(message_id="m-new-comment-unknown")
NEW_SENDING = ledger_key(message_id="m-new-sending")
NEW_FAILED = ledger_key(message_id="m-new-failed")
ALL_KEYS = (
    MIGRATED_SENT,
    MIGRATED_UNKNOWN,
    NEW_SENT,
    NEW_COMMENT_UNKNOWN,
    NEW_SENDING,
    NEW_FAILED,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _legacy_key_set(path: Path) -> set[tuple[str, str, str, str]]:
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


def _finalize(
    ledger: ServiceLedger,
    key: DeliveryKey,
    *,
    attempt_id: str,
    status: str,
    message: str = "",
) -> None:
    assert asyncio.run(
        ledger.finalize_source(
            key, attempt_id=attempt_id, status=status, target_message_id=message
        )
    ) is True


def _build_service_state(tmp_path: Path) -> Path:
    """迁移一条旧记录，再产生新投递（含源已发但评论不确定、在途与明确失败）。"""

    state_dir = tmp_path / "state"
    snapshot = build_legacy_snapshot(
        tmp_path / "legacy",
        tmp_path / "legacy-snapshot.sqlite3",
        [
            LegacyRecordSpec(
                key=MIGRATED_SENT,
                status="sent",
                target_message_id="m-old-target",
                comment_status="sent",
                comment_message_id="m-old-comment",
            ),
            LegacyRecordSpec(key=MIGRATED_UNKNOWN, status="unknown"),
        ],
    )
    migrated = migrate_legacy_snapshot(state_dir, snapshot, clock=FakeClock())
    assert hasattr(migrated, "ledger_identity"), migrated

    ledger = open_state_ledger(state_dir)
    try:
        assert asyncio.run(
            ledger.prepare_attempt(
                NEW_SENT,
                attempt_id="attempt-sent",
                request_id="req-1",
                session_id="sess-1",
                policy_generation=1,
                comment="已发送的评论",
            )
        ) is not None
        assert _reserve(ledger, NEW_SENT, attempt_id="attempt-sent") is True
        _finalize(ledger, NEW_SENT, attempt_id="attempt-sent", status=STATUS_SENT, message="m-new-target")
        assert asyncio.run(ledger.reserve_comment(NEW_SENT, attempt_id="attempt-sent")).granted is True
        assert asyncio.run(
            ledger.finalize_comment(
                NEW_SENT, attempt_id="attempt-sent", status="sent", comment_message_id="m-new-comment"
            )
        ) is True

        assert asyncio.run(
            ledger.prepare_attempt(
                NEW_COMMENT_UNKNOWN,
                attempt_id="attempt-comment-unknown",
                request_id="req-2",
                session_id="sess-1",
                policy_generation=1,
                comment="评论回执丢失",
            )
        ) is not None
        assert _reserve(ledger, NEW_COMMENT_UNKNOWN, attempt_id="attempt-comment-unknown") is True
        _finalize(
            ledger,
            NEW_COMMENT_UNKNOWN,
            attempt_id="attempt-comment-unknown",
            status=STATUS_SENT,
            message="m-target-comment-unknown",
        )
        assert asyncio.run(
            ledger.reserve_comment(NEW_COMMENT_UNKNOWN, attempt_id="attempt-comment-unknown")
        ).granted is True
        assert asyncio.run(
            ledger.finalize_comment(
                NEW_COMMENT_UNKNOWN,
                attempt_id="attempt-comment-unknown",
                status=STATUS_UNKNOWN,
                reason="评论回执不可确认",
            )
        ) is True

        assert _reserve(ledger, NEW_SENDING, attempt_id="attempt-sending") is True

        assert _reserve(ledger, NEW_FAILED, attempt_id="attempt-failed") is True
        _finalize(ledger, NEW_FAILED, attempt_id="attempt-failed", status=STATUS_FAILED)
    finally:
        close_ledger(ledger)
    return state_dir


def test_导出包含旧记录与新投递且旧公共接口阻断(tmp_path: Path) -> None:
    from service.ledger_migration import export_compatible_snapshot

    state_dir = _build_service_state(tmp_path)
    output = tmp_path / "compat-export.sqlite3"

    outcome = export_compatible_snapshot(state_dir, output)

    assert isinstance(outcome, ExportOutcome), outcome
    assert outcome.records == len(ALL_KEYS)
    assert dict(outcome.statuses) == {
        STATUS_SENT: 3,
        STATUS_UNKNOWN: 2,
        STATUS_FAILED: 1,
    }, "在途源状态必须保守导出为待核实"
    assert _legacy_key_set(output) == _keys_to_tuples(ALL_KEYS)

    legacy = open_ledger(output, clock=FakeClock())
    assert isinstance(legacy, DeliveryLedger), legacy
    try:
        sent = legacy.get(MIGRATED_SENT)
        assert sent is not None
        assert (sent.status, sent.target_message_id) == (STATUS_SENT, "m-old-target")
        assert (sent.comment_status, sent.comment_message_id) == (STATUS_SENT, "m-old-comment")

        for key in (MIGRATED_SENT, MIGRATED_UNKNOWN, NEW_SENT, NEW_COMMENT_UNKNOWN, NEW_SENDING):
            assert legacy.reserve(key).granted is False, f"成功/未知键必须阻断重发：{key.source_message_id}"

        comment_uncertain = legacy.get(NEW_COMMENT_UNKNOWN)
        assert comment_uncertain is not None
        assert comment_uncertain.status == STATUS_SENT
        assert comment_uncertain.comment_status == STATUS_UNKNOWN, "评论不确定不得被补齐或抹掉"
        assert comment_uncertain.target_message_id == "m-target-comment-unknown"

        assert legacy.get(NEW_FAILED).status == STATUS_FAILED, "明确失败保留为可重试"
    finally:
        legacy.close()

    pre_migration = _legacy_key_set(tmp_path / "legacy-snapshot.sqlite3")
    exported = _legacy_key_set(output)
    new_keys = _keys_to_tuples((NEW_SENT, NEW_COMMENT_UNKNOWN, NEW_SENDING, NEW_FAILED))
    assert new_keys <= exported - pre_migration, (
        "导出必须包含迁移后新增的投递（只靠迁移前快照不能恢复发送）"
    )


def _keys_to_tuples(keys: tuple[DeliveryKey, ...]) -> set[tuple[str, str, str, str]]:
    return {
        (key.account_id, key.source_group_id, key.source_message_id, key.target_group_id)
        for key in keys
    }


def test_导出失败不改写服务账本且不覆盖已有输出(tmp_path: Path) -> None:
    from service.ledger_migration import export_compatible_snapshot

    state_dir = _build_service_state(tmp_path)
    ledger_file = state_dir / SERVICE_LEDGER_FILE_NAME
    digest = _sha256(ledger_file)

    existing = tmp_path / "already-there.sqlite3"
    existing.write_bytes("既有内容，不得被覆盖".encode("utf-8"))
    refused = export_compatible_snapshot(state_dir, existing)
    assert isinstance(refused, LedgerUnavailable)
    assert existing.read_bytes() == "既有内容，不得被覆盖".encode("utf-8")
    assert _sha256(ledger_file) == digest

    missing_dir = export_compatible_snapshot(
        state_dir, tmp_path / "不存在的目录" / "out.sqlite3"
    )
    assert isinstance(missing_dir, LedgerUnavailable)
    assert _sha256(ledger_file) == digest, "导出失败不得改写服务账本"

    good = tmp_path / "ok.sqlite3"
    ok = export_compatible_snapshot(state_dir, good)
    assert isinstance(ok, ExportOutcome)
    assert _sha256(ledger_file) == digest, "导出只读：成功也不得改写服务账本"


def test_导出要求服务已停止发送(tmp_path: Path) -> None:
    from service.ledger_migration import export_compatible_snapshot

    state_dir = _build_service_state(tmp_path)
    holder = open_state_ledger(state_dir)
    try:
        blocked = export_compatible_snapshot(state_dir, tmp_path / "out.sqlite3")
        assert isinstance(blocked, LedgerUnavailable), "账本被独占时必须拒绝导出"
        assert "独占" in blocked.reason or "另一个" in blocked.reason, blocked.reason
    finally:
        close_ledger(holder)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "service",
            "ledger",
            "export",
            "--state-dir",
            str(state_dir),
            "--output",
            str(tmp_path / "cli-export.sqlite3"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    exported = tmp_path / "cli-export.sqlite3"
    assert exported.exists()
    legacy = open_ledger(exported, clock=FakeClock())
    assert isinstance(legacy, DeliveryLedger)
    try:
        assert legacy.reserve(NEW_SENT).granted is False, "CLI 导出同样必须保留阻断作用"
    finally:
        legacy.close()


def test_导出拒绝身份不符与未初始化账本(tmp_path: Path) -> None:
    from service.ledger_migration import export_compatible_snapshot

    state_dir = _build_service_state(tmp_path)
    probe = open_state_ledger(state_dir)
    try:
        identity = asyncio.run(probe.identity())
    finally:
        close_ledger(probe)

    mismatch = export_compatible_snapshot(
        state_dir, tmp_path / "wrong-id.sqlite3", confirm_ledger_id="0" * 32
    )
    assert isinstance(mismatch, LedgerUnavailable)
    assert "不符" in mismatch.reason, mismatch.reason

    empty_dir = tmp_path / "empty"
    fresh = initialize_fresh(empty_dir, clock=FakeClock())
    assert isinstance(fresh, FreshOutcome)
    uninitialized = export_compatible_snapshot(
        tmp_path / "no-ledger", tmp_path / "empty-export.sqlite3"
    )
    assert isinstance(uninitialized, LedgerUnavailable), "未初始化的目录不得导出成「空历史」"
    assert "尚未初始化" in uninitialized.reason, uninitialized.reason

    uninitialized_with_id = export_compatible_snapshot(
        tmp_path / "no-ledger",
        tmp_path / "empty-export-2.sqlite3",
        confirm_ledger_id=identity,
    )
    assert isinstance(uninitialized_with_id, LedgerUnavailable)

    absent = export_compatible_snapshot(tmp_path / "absent", tmp_path / "x.sqlite3")
    assert isinstance(absent, LedgerUnavailable)


def test_导出期间目标被他人创建时拒绝覆盖(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """「目标不存在」检查与落盘之间的竞态不得覆盖别人的文件（必须原子创建）。"""

    from service import ledger_migration
    from service.ledger_migration import export_compatible_snapshot

    state_dir = _build_service_state(tmp_path)
    ledger_file = state_dir / SERVICE_LEDGER_FILE_NAME
    digest = _sha256(ledger_file)
    target = tmp_path / "raced.sqlite3"
    racing_content = "竞态写入者的内容，不得被覆盖".encode("utf-8")

    original = ledger_migration.read_legacy_snapshot

    def read_and_race(path: Path) -> object:
        """在回读校验的时点创建目标文件：模拟检查通过后出现的并发写入者。"""

        verified = original(path)
        target.write_bytes(racing_content)
        return verified

    monkeypatch.setattr(ledger_migration, "read_legacy_snapshot", read_and_race)

    outcome = export_compatible_snapshot(state_dir, target)

    assert isinstance(outcome, LedgerUnavailable), "目标在竞态中出现时必须失败，不得覆盖"
    assert target.read_bytes() == racing_content, "竞态创建的既有文件必须原样保留"
    assert not list(tmp_path.glob(f"{target.name}.tmp-*")), "失败后不得残留临时文件"
    assert _sha256(ledger_file) == digest, "失败不得改写服务账本"
