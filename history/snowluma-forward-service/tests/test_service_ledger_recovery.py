"""3.5 启动恢复与临时授权失效（design D7「启动恢复」）。

- 源 `sending` → `unknown`、评论 `sending` → `unknown`（可能已发出，不自动续发）；
- 未提交评论（`pending`）→ `skipped`；未消费准备（`prepared`）→ `cancelled`；
- 恢复后旧尝试（旧挑战）不能再被消费，但允许新的显式尝试重新准备；
- `sent` / `unknown` 记录不因时间或容量被清除；
- 恢复失败按不可用抛出（调用方关闭发送并告警），遗留 `sending` 继续阻断重发；
- 服务启动 CLI 执行恢复并如实报告账本状态与不可用原因。
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from service.ledger_store import (
    ATTEMPT_CANCELLED,
    ATTEMPT_PREPARED,
    COMMENT_PENDING,
    COMMENT_SKIPPED,
    DELIVERY_TABLE,
    SERVICE_LEDGER_FILE_NAME,
    ServiceLedger,
    open_service_ledger,
)
from shared.delivery import (
    STATUS_FAILED,
    STATUS_SENDING,
    STATUS_SENT,
    STATUS_UNKNOWN,
    DeliveryKey,
    LedgerUnavailable,
    LedgerUnavailableError,
)
from shared.ledger import AttemptSnapshot, DeliverySnapshot, RecoveryReport, SourceReservation
from shared.protocol import delivery_id_for

from tests.service_support import close_ledger, open_state_ledger, state_ledger_rows
from tests.support import FakeClock, ledger_key

ROOT = Path(__file__).resolve().parent.parent
TOKEN_ENV_VAR = "MC_RECOVERY_TEST_TOKEN"
TOKEN_VALUE = "recovery-secret-token"


def _key(message_id: str = "m-source-1") -> DeliveryKey:
    return ledger_key(message_id=message_id)


def _reserve(ledger: ServiceLedger, key: DeliveryKey, *, attempt_id: str) -> SourceReservation:
    return asyncio.run(
        ledger.reserve_source(
            key,
            attempt_id=attempt_id,
            request_id=f"req-{attempt_id}",
            session_id="sess-1",
            policy_generation=1,
        )
    )


def _prepare(
    ledger: ServiceLedger, key: DeliveryKey, *, attempt_id: str, comment: str = "附带评论"
) -> AttemptSnapshot:
    outcome = asyncio.run(
        ledger.prepare_attempt(
            key,
            attempt_id=attempt_id,
            request_id=f"req-{attempt_id}",
            session_id="sess-1",
            policy_generation=1,
            comment=comment,
        )
    )
    assert outcome is not None
    return outcome


def _finalize_source(
    ledger: ServiceLedger, key: DeliveryKey, *, attempt_id: str, status: str, message: str = ""
) -> None:
    assert asyncio.run(
        ledger.finalize_source(
            key, attempt_id=attempt_id, status=status, target_message_id=message
        )
    ) is True


def _recover(ledger: ServiceLedger) -> RecoveryReport:
    return asyncio.run(ledger.recover_interrupted())


def _lookup(ledger: ServiceLedger, key: DeliveryKey) -> DeliverySnapshot | None:
    return asyncio.run(ledger.lookup_delivery(delivery_id_for(key)))


def test_源与评论在途占位分别恢复为未知(tmp_path: Path) -> None:
    clock = FakeClock()
    ledger = open_state_ledger(tmp_path, clock=clock)
    source_key = _key("m-source-sending")
    comment_key = _key("m-comment-sending")
    try:
        assert _reserve(ledger, source_key, attempt_id="attempt-source").granted is True

        _prepare(ledger, comment_key, attempt_id="attempt-comment")
        assert _reserve(ledger, comment_key, attempt_id="attempt-comment").granted is True
        _finalize_source(
            ledger, comment_key, attempt_id="attempt-comment", status=STATUS_SENT, message="m-target-1"
        )
        assert asyncio.run(
            ledger.reserve_comment(comment_key, attempt_id="attempt-comment")
        ).granted is True

        close_ledger(ledger)
        restarted = open_state_ledger(tmp_path, clock=clock)
        try:
            report = _recover(restarted)

            assert report.sources_recovered == 1, "源在途占位必须归并为待核实"
            assert report.comments_recovered == 1, "评论在途占位必须归并为待核实"
            source = _lookup(restarted, source_key)
            assert source is not None
            assert (source.status, "待核实" in source.reason) == (STATUS_UNKNOWN, True)

            comment = _lookup(restarted, comment_key)
            assert comment is not None
            assert (comment.status, comment.target_message_id) == (STATUS_SENT, "m-target-1")
            assert comment.comment_status == STATUS_UNKNOWN, "评论在途归并为待核实"

            attempt = asyncio.run(restarted.lookup_attempt("attempt-comment"))
            assert attempt is not None
            assert attempt.comment_stage == STATUS_UNKNOWN
            assert asyncio.run(
                restarted.reserve_comment(comment_key, attempt_id="attempt-comment")
            ).granted is False, "恢复后不得自动补发评论"
        finally:
            close_ledger(restarted)
    finally:
        try:
            close_ledger(ledger)
        except Exception:
            pass


def test_未提交评论恢复为跳过且未消费准备失效(tmp_path: Path) -> None:
    clock = FakeClock()
    ledger = open_state_ledger(tmp_path, clock=clock)
    prepared_key = _key("m-prepared")
    submitted_key = _key("m-unsubmitted-comment")
    try:
        assert _prepare(ledger, prepared_key, attempt_id="attempt-prepared").comment_stage == (
            COMMENT_PENDING
        )
        _prepare(ledger, submitted_key, attempt_id="attempt-unsubmitted")
        assert _reserve(ledger, submitted_key, attempt_id="attempt-unsubmitted").granted is True
        _finalize_source(
            ledger,
            submitted_key,
            attempt_id="attempt-unsubmitted",
            status=STATUS_SENT,
            message="m-target-2",
        )

        report = _recover(ledger)

        assert report.prepares_expired == 2, "未消费准备与未提交评论准备都必须失效"
        assert _lookup(ledger, prepared_key) is None, "纯准备不得产生投递事实"

        prepared_attempt = asyncio.run(ledger.lookup_attempt("attempt-prepared"))
        assert prepared_attempt is not None
        assert prepared_attempt.source_stage == ATTEMPT_CANCELLED
        assert prepared_attempt.comment_stage == COMMENT_SKIPPED

        submitted_attempt = asyncio.run(ledger.lookup_attempt("attempt-unsubmitted"))
        assert submitted_attempt is not None
        assert submitted_attempt.source_stage == STATUS_SENT
        assert submitted_attempt.comment_stage == COMMENT_SKIPPED
        snapshot = _lookup(ledger, submitted_key)
        assert snapshot is not None
        assert (snapshot.status, snapshot.comment_status) == (STATUS_SENT, COMMENT_SKIPPED), (
            "未提交评论归并为跳过，源已投递事实不变"
        )

        replay = _reserve(ledger, prepared_key, attempt_id="attempt-prepared")
        assert replay.granted is False, "旧准备（旧挑战）恢复后不得再被消费"
        fresh = _reserve(ledger, prepared_key, attempt_id="attempt-fresh")
        assert fresh.granted is True, "客户端可以重新准备并开始新的显式尝试"
    finally:
        close_ledger(ledger)


def test_成功与未知记录不因时间与容量清除(tmp_path: Path) -> None:
    clock = FakeClock()
    ledger = open_state_ledger(tmp_path, clock=clock)
    sent_key = _key("m-sent")
    unknown_key = _key("m-unknown")
    failed_key = _key("m-failed")
    try:
        for key, attempt_id in (
            (sent_key, "attempt-sent"),
            (unknown_key, "attempt-unknown"),
            (failed_key, "attempt-failed"),
        ):
            assert _reserve(ledger, key, attempt_id=attempt_id).granted is True
        _finalize_source(ledger, sent_key, attempt_id="attempt-sent", status=STATUS_SENT, message="m-target-3")
        _finalize_source(ledger, unknown_key, attempt_id="attempt-unknown", status=STATUS_UNKNOWN)
        _finalize_source(ledger, failed_key, attempt_id="attempt-failed", status=STATUS_FAILED)

        clock.advance(10 * 365 * 24 * 3600)
        report = _recover(ledger)

        assert report.sources_recovered == 0, "已终态记录不参与恢复且不得被清理"
        assert state_ledger_rows(tmp_path) == [
            ("m-sent", STATUS_SENT, "m-target-3"),
            ("m-unknown", STATUS_UNKNOWN, ""),
            ("m-failed", STATUS_FAILED, ""),
        ], "成功与未知记录必须保留阻断作用（失败仍可显式重试）"
        assert _reserve(ledger, sent_key, attempt_id="attempt-sent-again").granted is False
        assert _reserve(ledger, unknown_key, attempt_id="attempt-unknown-again").granted is False
        assert _reserve(ledger, failed_key, attempt_id="attempt-failed-again").granted is True
    finally:
        close_ledger(ledger)


def test_恢复失败按不可用抛出且遗留占位继续阻断(tmp_path: Path) -> None:
    class _RecoveryFailingCursor(sqlite3.Cursor):
        def execute(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
            owner = self.connection
            failing = str(getattr(owner, "failing_statement", "") or "")
            if failing and failing in (str(args[0]) if args else ""):
                setattr(owner, "failing_statement", "")
                raise sqlite3.OperationalError("注入的恢复失败")
            return super().execute(*args, **kwargs)

    class _RecoveryFailingConnection(sqlite3.Connection):
        failing_statement: str | None = None

        def cursor(self, factory: Any = _RecoveryFailingCursor) -> sqlite3.Cursor:
            return super().cursor(factory)

    close_ledger(open_state_ledger(tmp_path))
    connection = sqlite3.connect(
        str(tmp_path / SERVICE_LEDGER_FILE_NAME),
        timeout=5.0,
        isolation_level=None,
        factory=_RecoveryFailingConnection,
    )
    assert isinstance(connection, _RecoveryFailingConnection)
    ledger = ServiceLedger(connection, FakeClock())
    key = _key("m-in-flight")
    try:
        assert _reserve(ledger, key, attempt_id="attempt-1").granted is True
        connection.failing_statement = f"UPDATE {DELIVERY_TABLE}"
        with pytest.raises(LedgerUnavailableError) as failure:
            _recover(ledger)
        assert str(failure.value).strip(), "恢复失败必须按不可用抛出"

        assert state_ledger_rows(tmp_path) == [("m-in-flight", STATUS_SENDING, "")], (
            "恢复失败不得改动遗留占位"
        )
        assert _reserve(ledger, key, attempt_id="attempt-2").granted is False, (
            "遗留 sending 继续阻断重发（绝不自动续发）"
        )
        report = _recover(ledger)
        assert report.sources_recovered == 1, "故障排除后恢复仍可正常执行"
    finally:
        close_ledger(ledger)


# ------------------------------------------------------------------- 启动 CLI


def _write_config(
    directory: Path, *, state_dir: Path, ledger_id: str = "", token_env_var: str = TOKEN_ENV_VAR
) -> Path:
    path = directory / "service.toml"
    path.write_text(
        "\n".join(
            [
                "[service]",
                'client_identity = "maibot-plugin"',
                'account_id = "10000"',
                f'state_dir = "{state_dir}"',
                f'ledger_id = "{ledger_id}"',
                f'token_env_var = "{token_env_var}"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _run_start(config: Path) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment[TOKEN_ENV_VAR] = TOKEN_VALUE
    return subprocess.run(
        [sys.executable, "-m", "service", "start", "--config", str(config)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_启动CLI执行启动恢复并如实报告账本状态(tmp_path: Path) -> None:
    from service.ledger_migration import FreshOutcome, initialize_fresh

    state_dir = tmp_path / "state"
    fresh = initialize_fresh(state_dir, clock=FakeClock())
    assert isinstance(fresh, FreshOutcome)
    ledger = open_state_ledger(state_dir)
    key = _key("m-cli-in-flight")
    try:
        assert _reserve(ledger, key, attempt_id="attempt-cli").granted is True
    finally:
        close_ledger(ledger)

    completed = _run_start(
        _write_config(tmp_path, state_dir=state_dir, ledger_id=fresh.ledger_identity)
    )
    output = completed.stdout + completed.stderr

    assert completed.returncode != 0, "传输尚未接通：不得报告启动成功"
    assert "账本" in output, output

    reopened = open_state_ledger(state_dir)
    try:
        snapshot = _lookup(reopened, key)
        assert snapshot is not None
        assert snapshot.status == STATUS_UNKNOWN, "启动 CLI 必须执行启动恢复"
    finally:
        close_ledger(reopened)

    assert TOKEN_VALUE.encode() not in (state_dir / SERVICE_LEDGER_FILE_NAME).read_bytes(), (
        "凭证不得写入数据集元数据"
    )


def test_启动CLI在账本损坏或缺失时明确告警且不发送(tmp_path: Path) -> None:
    broken_dir = tmp_path / "broken"
    broken_dir.mkdir()
    (broken_dir / SERVICE_LEDGER_FILE_NAME).write_bytes(
        "这不是 SQLite".encode("utf-8") + b"\x00\xff" * 8
    )

    broken = _run_start(_write_config(tmp_path, state_dir=broken_dir, ledger_id="ledger-x"))
    broken_output = broken.stdout + broken.stderr
    assert broken.returncode == 5, f"账本损坏必须按账本故障退出: {broken_output}"
    assert "损坏" in broken_output, broken_output

    missing = _run_start(
        _write_config(tmp_path, state_dir=tmp_path / "absent", ledger_id="ledger-x")
    )
    missing_output = missing.stdout + missing.stderr
    assert missing.returncode != 0
    assert "尚未初始化" in missing_output or "账本" in missing_output, missing_output

    assert open_service_ledger(broken_dir) is not None
    assert isinstance(open_service_ledger(broken_dir), LedgerUnavailable), (
        "公开打开入口必须拒绝损坏账本（调用方据此关闭发送）"
    )
