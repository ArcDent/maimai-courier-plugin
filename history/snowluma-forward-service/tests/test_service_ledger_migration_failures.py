"""3.8 迁移损坏、冲突与中断（Migration Plan 2/3；跨群规格「旧账本迁移与回滚防重」）。

- 缺库、损坏、未知 schema 的快照都拒绝导入，且不会让空目标「变成可发送的空历史」：
  目标保持无身份，数据集绑定校验（服务启动/会话就绪检查）一律拒绝；
- 同键冲突终态停止整批导入：不覆盖、不把已有安全状态降级、不留下半份导入；
- 重复同一已完成导入（同一字节快照）幂等，不增加重复投递记录；
- 模拟「导入事务中途进程被杀」：重启后账本没有半条记录、没有身份，发送保持关闭。
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from service.ledger_migration import (
    FreshOutcome,
    MigrationOutcome,
    initialize_fresh,
    migrate_legacy_snapshot,
    read_ledger_status,
)
from service.ledger_store import (
    DELIVERY_TABLE,
    SERVICE_LEDGER_FILE_NAME,
    ServiceLedger,
    open_service_ledger,
)
from shared.delivery import (
    STATUS_FAILED,
    STATUS_SENT,
    STATUS_UNKNOWN,
    DeliveryKey,
    LedgerUnavailable,
)
from shared.protocol import delivery_id_for

from tests.service_support import (
    LegacyRecordSpec,
    build_legacy_snapshot,
    close_ledger,
    open_state_ledger,
)
from tests.support import FakeClock, ledger_key

ROOT = Path(__file__).resolve().parent.parent
EXPECTED_ID = "0" * 32


def _sent(ledger: ServiceLedger, key: DeliveryKey, *, attempt_id: str, message: str) -> None:
    assert asyncio.run(
        ledger.reserve_source(
            key,
            attempt_id=attempt_id,
            request_id=f"req-{attempt_id}",
            session_id="sess-1",
            policy_generation=1,
        )
    ).granted is True
    assert asyncio.run(
        ledger.finalize_source(
            key, attempt_id=attempt_id, status=STATUS_SENT, target_message_id=message
        )
    ) is True


def _lookup(ledger: ServiceLedger, key: DeliveryKey) -> object:
    return asyncio.run(ledger.lookup_delivery(delivery_id_for(key)))


def _rows(state_dir: Path) -> list[tuple[str, str, str]]:
    connection = sqlite3.connect(
        str(state_dir / SERVICE_LEDGER_FILE_NAME), timeout=5.0, isolation_level=None
    )
    try:
        return [
            (str(row[0]), str(row[1]), str(row[2]))
            for row in connection.execute(
                f"SELECT source_message_id, status, target_message_id FROM {DELIVERY_TABLE}"
            )
        ]
    finally:
        connection.close()


def _sending_refused(state_dir: Path) -> bool:
    """数据集绑定校验（服务启动 / 会话就绪）是否拒绝发送。"""

    outcome = open_service_ledger(state_dir, expected_ledger_id=EXPECTED_ID)
    return isinstance(outcome, LedgerUnavailable)


def test_缺库损坏与未知schema的快照都拒绝且不启用发送(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"

    missing = migrate_legacy_snapshot(state_dir, tmp_path / "不存在.sqlite3", clock=FakeClock())
    assert isinstance(missing, LedgerUnavailable)
    assert "不存在" in missing.reason
    assert _sending_refused(state_dir), "缺库失败不得让空目标变成可发送账本"
    status = read_ledger_status(state_dir)
    assert isinstance(status, LedgerUnavailable) or status.initialized is False, status

    broken = tmp_path / "broken.sqlite3"
    broken.write_bytes("这不是 SQLite".encode("utf-8") + b"\x00\xff" * 8)
    broken_digest = broken.read_bytes()
    damaged = migrate_legacy_snapshot(state_dir, broken, clock=FakeClock())
    assert isinstance(damaged, LedgerUnavailable)
    assert broken.read_bytes() == broken_digest, "拒绝时不得改写损坏快照"
    assert _sending_refused(state_dir)

    unknown_schema = tmp_path / "unknown-schema.sqlite3"
    connection = sqlite3.connect(str(unknown_schema), isolation_level=None)
    try:
        connection.execute(
            "CREATE TABLE delivery_ledger (account_id TEXT PRIMARY KEY, 状态 TEXT)"
        )
        connection.execute("INSERT INTO delivery_ledger VALUES ('10000', 'sent')")
    finally:
        connection.close()
    schema_digest = unknown_schema.read_bytes()

    rejected = migrate_legacy_snapshot(state_dir, unknown_schema, clock=FakeClock())
    assert isinstance(rejected, LedgerUnavailable)
    assert "schema" in rejected.reason, rejected.reason
    assert unknown_schema.read_bytes() == schema_digest
    assert _sending_refused(state_dir)

    no_table = tmp_path / "no-table.sqlite3"
    sqlite3.connect(str(no_table)).close()
    empty_result = migrate_legacy_snapshot(state_dir, no_table, clock=FakeClock())
    assert isinstance(empty_result, LedgerUnavailable), "没有账本表的库不得当作空历史"
    assert _sending_refused(state_dir)

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
            str(tmp_path / "不存在.sqlite3"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 5, completed.stdout + completed.stderr
    assert "关闭" in (completed.stdout + completed.stderr)


def test_冲突终态停止整批导入且不覆盖不降级(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    fresh = initialize_fresh(state_dir, clock=FakeClock())
    assert isinstance(fresh, FreshOutcome)
    conflict_key = ledger_key(message_id="m-conflict")
    new_key = ledger_key(message_id="m-new")
    ledger = open_state_ledger(state_dir)
    try:
        _sent(ledger, conflict_key, attempt_id="attempt-keep", message="m-target-keep")
    finally:
        close_ledger(ledger)

    snapshot = build_legacy_snapshot(
        tmp_path / "legacy",
        tmp_path / "legacy-snapshot.sqlite3",
        [
            LegacyRecordSpec(key=new_key, status="sent", target_message_id="m-new-target"),
            LegacyRecordSpec(key=conflict_key, status="unknown"),
        ],
    )
    snapshot_digest = snapshot.read_bytes()

    outcome = migrate_legacy_snapshot(
        state_dir, snapshot, confirm_ledger_id=fresh.ledger_identity, clock=FakeClock()
    )

    assert isinstance(outcome, LedgerUnavailable), "无法无损合并必须停止导入"
    assert "无损" in outcome.reason or "停止" in outcome.reason, outcome.reason

    ledger = open_state_ledger(state_dir)
    try:
        kept = _lookup(ledger, conflict_key)
        assert (kept.status, kept.target_message_id) == (STATUS_SENT, "m-target-keep"), (
            "已有安全状态不得被覆盖或降级"
        )
        assert _lookup(ledger, new_key) is None, "冲突必须让整批导入回滚（不留半份导入）"
        assert asyncio.run(ledger.identity()) == fresh.ledger_identity, "失败不得改写数据集身份"
    finally:
        close_ledger(ledger)
    assert snapshot.read_bytes() == snapshot_digest, "失败不得改写原快照"

    again = migrate_legacy_snapshot(
        state_dir, snapshot, confirm_ledger_id=fresh.ledger_identity, clock=FakeClock()
    )
    assert isinstance(again, LedgerUnavailable), "同一冲突快照重复提交必须确定性失败"
    assert _rows(state_dir) == [("m-conflict", STATUS_SENT, "m-target-keep")]

    weaker_dir = tmp_path / "weaker-state"
    weaker_fresh = initialize_fresh(weaker_dir, clock=FakeClock())
    assert isinstance(weaker_fresh, FreshOutcome)
    unknown_key = ledger_key(message_id="m-unknown-keep")
    unknown_ledger = open_state_ledger(weaker_dir)
    try:
        assert asyncio.run(
            unknown_ledger.reserve_source(
                unknown_key,
                attempt_id="attempt-unknown",
                request_id="req-1",
                session_id="sess-1",
                policy_generation=1,
            )
        ).granted is True
        assert asyncio.run(
            unknown_ledger.finalize_source(
                unknown_key, attempt_id="attempt-unknown", status=STATUS_UNKNOWN, reason="回执丢失"
            )
        ) is True
    finally:
        close_ledger(unknown_ledger)
    weaker_snapshot = build_legacy_snapshot(
        tmp_path / "legacy-weaker",
        tmp_path / "legacy-weaker.sqlite3",
        [LegacyRecordSpec(key=unknown_key, status="failed")],
    )

    weaker = migrate_legacy_snapshot(
        weaker_dir,
        weaker_snapshot,
        confirm_ledger_id=weaker_fresh.ledger_identity,
        clock=FakeClock(),
    )
    assert isinstance(weaker, LedgerUnavailable), "不得把已有待核实记录降级成可重试失败"
    assert _rows(weaker_dir) == [("m-unknown-keep", STATUS_UNKNOWN, "")]

    comment_dir = tmp_path / "comment-state"
    comment_fresh = initialize_fresh(comment_dir, clock=FakeClock())
    assert isinstance(comment_fresh, FreshOutcome)
    comment_key = ledger_key(message_id="m-comment-conflict")
    comment_ledger = open_state_ledger(comment_dir)
    try:
        assert asyncio.run(
            comment_ledger.prepare_attempt(
                comment_key,
                attempt_id="attempt-c",
                request_id="req-c",
                session_id="sess-1",
                policy_generation=1,
                comment="目标账本里的评论",
            )
        ) is not None
        assert asyncio.run(
            comment_ledger.reserve_source(
                comment_key,
                attempt_id="attempt-c",
                request_id="req-c",
                session_id="sess-1",
                policy_generation=1,
            )
        ).granted is True
        assert asyncio.run(
            comment_ledger.finalize_source(
                comment_key,
                attempt_id="attempt-c",
                status=STATUS_SENT,
                target_message_id="m-target-c",
            )
        ) is True
        assert asyncio.run(
            comment_ledger.reserve_comment(comment_key, attempt_id="attempt-c")
        ).granted is True
        assert asyncio.run(
            comment_ledger.finalize_comment(
                comment_key, attempt_id="attempt-c", status="sent", comment_message_id="m-comment-c"
            )
        ) is True
    finally:
        close_ledger(comment_ledger)
    comment_snapshot = build_legacy_snapshot(
        tmp_path / "legacy-comment",
        tmp_path / "legacy-comment.sqlite3",
        [LegacyRecordSpec(key=comment_key, status="sent", target_message_id="m-target-c")],
    )

    comment_conflict = migrate_legacy_snapshot(
        comment_dir,
        comment_snapshot,
        confirm_ledger_id=comment_fresh.ledger_identity,
        clock=FakeClock(),
    )
    assert isinstance(comment_conflict, LedgerUnavailable), (
        "同状态同目标但评论结果不同也不得静默覆盖（无法无损合并即停止）"
    )
    final_ledger = open_state_ledger(comment_dir)
    try:
        kept_comment = _lookup(final_ledger, comment_key)
        assert kept_comment.comment_message_id == "m-comment-c", "既有评论结果不得被覆盖"
        assert kept_comment.comment_status == STATUS_SENT
    finally:
        close_ledger(final_ledger)


def test_重复同一已完成导入幂等不增加重复记录(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    specs = [
        LegacyRecordSpec(key=ledger_key(message_id="m-a"), status="sent", target_message_id="m-target-a"),
        LegacyRecordSpec(key=ledger_key(message_id="m-b"), status="unknown"),
    ]
    snapshot = build_legacy_snapshot(
        tmp_path / "legacy", tmp_path / "legacy-snapshot.sqlite3", specs
    )
    first = migrate_legacy_snapshot(state_dir, snapshot, clock=FakeClock())
    assert isinstance(first, MigrationOutcome)
    assert first.records == 2 and first.already_imported is False
    rows_after_first = _rows(state_dir)

    copy_snapshot = tmp_path / "copy-of-snapshot.sqlite3"
    copy_snapshot.write_bytes(snapshot.read_bytes())
    repeated = migrate_legacy_snapshot(state_dir, copy_snapshot, clock=FakeClock())
    assert isinstance(repeated, MigrationOutcome)
    assert repeated.already_imported is True, "同一字节快照必须按已完成导入幂等返回"
    assert repeated.ledger_identity == first.ledger_identity
    assert _rows(state_dir) == rows_after_first, "重复导入不得增加重复投递记录"

    report = read_ledger_status(state_dir)
    assert hasattr(report, "imports")
    assert len(report.imports) == 1, "幂等重放不得重复登记导入摘要"

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
            "--confirm-ledger-id",
            first.ledger_identity,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "已导入" in (completed.stdout + completed.stderr)
    assert _rows(state_dir) == rows_after_first

    extended = build_legacy_snapshot(
        tmp_path / "legacy-extended",
        tmp_path / "legacy-extended.sqlite3",
        [
            *specs,
            LegacyRecordSpec(key=ledger_key(message_id="m-c"), status="failed"),
        ],
    )
    merged = migrate_legacy_snapshot(
        state_dir, extended, confirm_ledger_id=first.ledger_identity, clock=FakeClock()
    )
    assert isinstance(merged, MigrationOutcome), merged
    assert merged.already_imported is False
    assert (merged.records, merged.unchanged) == (1, 2), (
        "第二个快照只补新键；已有且一致的记录计数为 unchanged，不重复写入"
    )
    assert len(_rows(state_dir)) == 3
    assert merged.ledger_identity == first.ledger_identity


def test_导入摘要损坏时拒绝继续导入且不重写历史(tmp_path: Path) -> None:
    """元数据 imports 非空但不可解析：不得 fail-open 当成「从未导入」。

    幂等判定完全依赖这份摘要：把它当作空的会让重复导入绕过幂等快速路径，并在
    无冲突时静默重写摘要（丢失原统计）；只读报告同样不得把损坏摘要报告成空列表。
    """

    state_dir = tmp_path / "state"
    specs = [
        LegacyRecordSpec(key=ledger_key(message_id="m-a"), status="sent", target_message_id="m-target-a"),
        LegacyRecordSpec(key=ledger_key(message_id="m-b"), status="unknown"),
    ]
    snapshot = build_legacy_snapshot(
        tmp_path / "legacy", tmp_path / "legacy-snapshot.sqlite3", specs
    )
    first = migrate_legacy_snapshot(state_dir, snapshot, clock=FakeClock())
    assert isinstance(first, MigrationOutcome)
    rows_after_first = _rows(state_dir)

    connection = sqlite3.connect(
        str(state_dir / SERVICE_LEDGER_FILE_NAME), timeout=5.0, isolation_level=None
    )
    try:
        connection.execute(
            "UPDATE ledger_meta SET value = ? WHERE key = 'imports'", ("{ 不是 JSON",)
        )
    finally:
        connection.close()

    report = read_ledger_status(state_dir)
    assert isinstance(report, LedgerUnavailable), "损坏的导入摘要必须按不可用报告，不得当作空历史"
    assert "imports" in report.reason, report.reason

    repeated = migrate_legacy_snapshot(
        state_dir, snapshot, confirm_ledger_id=first.ledger_identity, clock=FakeClock()
    )
    assert isinstance(repeated, LedgerUnavailable), "摘要不可解析时不得继续导入（幂等无法判定）"
    assert "imports" in repeated.reason, repeated.reason

    assert _rows(state_dir) == rows_after_first, "拒绝时不得写入或重复导入任何记录"
    connection = sqlite3.connect(
        f"{(state_dir / SERVICE_LEDGER_FILE_NAME).as_uri()}?mode=ro", uri=True, timeout=5.0
    )
    try:
        kept = connection.execute("SELECT value FROM ledger_meta WHERE key = 'imports'").fetchone()
    finally:
        connection.close()
    assert kept is not None and kept[0] == "{ 不是 JSON", "拒绝时不得重写或清空既有元数据"


_IMPORT_INTERRUPTED_CODE = """
import json, sqlite3, sys
from pathlib import Path
from service.ledger_store import DELIVERY_TABLE, open_state_database, state_transaction

state_dir = Path(sys.argv[1])
database = open_state_database(state_dir, writable=True, create=True)
assert hasattr(database, "connection"), database
connection = database.connection
cursor = connection.cursor()
cursor.execute("BEGIN IMMEDIATE")
cursor.execute(
    f"INSERT INTO {DELIVERY_TABLE} ("
    "account_id, source_group_id, source_message_id, target_group_id, status, "
    "target_message_id, comment_status, comment_message_id, reason, created_at, updated_at, delivery_id"
    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    ("10000", "10001", "m-interrupted", "20002", "sent", "m-target-x", "not_requested", "", "", 1.0, 2.0, "delivery-x"),
)
count = connection.execute(f"SELECT COUNT(*) FROM {DELIVERY_TABLE}").fetchone()[0]
print(json.dumps({"ready": True, "visible_rows": count}), flush=True)
sys.stdin.readline()
"""


def test_模拟中断不留下半份导入且不启用发送(tmp_path: Path) -> None:
    """子进程在未提交的导入事务里被 SIGKILL：重启后必须看不到半条记录。"""

    state_dir = tmp_path / "state"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT)
    child = subprocess.Popen(
        [sys.executable, "-c", _IMPORT_INTERRUPTED_CODE, str(state_dir)],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdin is not None
        ready = json.loads(child.stdout.readline())
        assert ready["visible_rows"] == 1, (ready, child.stderr.read() if child.stderr else "")
    finally:
        child.kill()
        child.wait(timeout=60)

    assert _rows(state_dir) == [], "被中断的导入事务必须在重启后回滚干净"
    report = read_ledger_status(state_dir)
    assert report.initialized is False, "中断的导入不得留下身份（不冒充已完成迁移）"
    assert _sending_refused(state_dir), "中断后发送必须保持关闭"

    completed = subprocess.run(
        [sys.executable, "-m", "service", "ledger", "status", "--state-dir", str(state_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "尚未初始化" in completed.stdout
