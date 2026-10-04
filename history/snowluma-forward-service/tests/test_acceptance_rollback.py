"""第 7.3 组验收：合成旧库导入 → 新投递 → 兼容导出 → 旧公共入口验证阻断。

seam：账本迁移/导出 CLI（真实子进程）与旧插件账本公共入口（`open_ledger` /
`get` / `reserve` / `mark_*` / `record_comment`）。场景记录输入（合成旧记录）、
源/评论发送次数、权威账本行与接口结果：

- 旧库快照只含合成数据、只用旧公共接口合成，导入前记录字节指纹；
- 迁移用真实 CLI 子进程，导入后原快照与旧库字节不变；
- 新投递经真实服务链（真实路由 + 真实服务账本 + 模拟 OneBot）产生，源与评论各一次；
- 兼容导出用真实 CLI 子进程；导出后用旧公共入口逐一验证：`sent` / `unknown` /
  在途归并后的 `unknown` / 新投递的 `sent` 全部阻断，只有旧明确失败可显式重试。

不连接真实群、不读取任何真实运行账本。
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

from delivery_ledger import LEDGER_FILE_NAME, DeliveryLedger, open_ledger

from tests.cross_process_support import ledger_identity_from, run_service_cli
from tests.service_support import (
    LegacyRecordSpec,
    authorize_session,
    build_legacy_snapshot,
    launch_authorized_service,
    result_of,
    state_ledger_rows,
    store_source_message,
)
from tests.support import FakeClock, SOURCE_GROUP_ID, TARGET_GROUP_ID, ledger_key


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_旧库导入新投递兼容导出后旧公共入口阻断全部键(tmp_path: Path) -> None:
    # 1) 合成旧库快照：只用旧公共接口；四字段键含成功、待核实、明确失败与在途四态。
    specs = [
        LegacyRecordSpec(
            key=ledger_key(message_id="m-legacy-sent"),
            status="sent",
            target_message_id="t-legacy-sent",
            comment_status="sent",
            comment_message_id="t-legacy-comment",
        ),
        LegacyRecordSpec(key=ledger_key(message_id="m-legacy-unknown"), status="unknown"),
        LegacyRecordSpec(key=ledger_key(message_id="m-legacy-failed"), status="failed"),
        LegacyRecordSpec(key=ledger_key(message_id="m-legacy-sending"), status="sending"),
    ]
    legacy_dir = tmp_path / "legacy"
    snapshot = tmp_path / "legacy-snapshot.sqlite3"
    build_legacy_snapshot(legacy_dir, snapshot, specs)
    snapshot_digest_before = _digest(snapshot)
    legacy_digest_before = _digest(legacy_dir / LEDGER_FILE_NAME)

    # 2) 真实 CLI 迁移导入；原快照与旧库在导入后字节不变。
    state_dir = tmp_path / "state"
    code, out, err = asyncio.run(
        run_service_cli("ledger", "migrate", "--state-dir", str(state_dir), "--snapshot", str(snapshot))
    )
    assert code == 0, out + err
    identity = ledger_identity_from(out)
    assert _digest(snapshot) == snapshot_digest_before, "迁移不得改动原快照"
    assert _digest(legacy_dir / LEDGER_FILE_NAME) == legacy_digest_before, "迁移不得改动原旧库"

    # 3) 新投递：真实服务链（真实路由 + 真实服务账本 + 模拟 OneBot），源与评论各一次。
    harness = launch_authorized_service(state_dir, initialize=False)
    store_source_message(
        harness.onebot,
        message_id="m-new-1",
        group_id=SOURCE_GROUP_ID,
        message=[{"type": "text", "data": {"text": "新投递正文"}}],
    )
    session_id = str(authorize_session(harness))
    prepared = result_of(harness.prepare(session_id, generation=1, source_message_id="m-new-1", comment="新投递评论"))
    source = result_of(
        harness.execute(
            str(prepared["delivery_id"]),
            stage="source",
            attempt_id=str(prepared["attempt_id"]),
            session_id=session_id,
            generation=1,
            challenge=str(prepared["challenge"]),
        )
    )
    harness.clock.advance(0.5)
    comment = result_of(
        harness.execute(
            str(prepared["delivery_id"]),
            stage="comment",
            attempt_id=str(prepared["attempt_id"]),
            session_id=session_id,
            generation=1,
            challenge=str(source["comment_challenge"]),
        )
    )
    rows = tuple(state_ledger_rows(harness.state_dir))
    harness.close()

    assert source["status"] == "sent" and source["target_message_id"], source
    assert comment["comment_status"] == "sent" and comment["comment_message_id"], comment
    assert harness.onebot.send_count == 2, "新投递恰一次源发送与一次评论发送"

    # 4) 真实 CLI 兼容导出（服务已停止发送：账本已关闭、独占锁已释放）。
    exported = tmp_path / "compat.sqlite3"
    export_code, export_out, export_err = asyncio.run(
        run_service_cli("ledger", "export", "--state-dir", str(state_dir), "--output", str(exported))
    )
    assert export_code == 0, export_out + export_err
    assert exported.exists() and exported.stat().st_size > 0, export_out

    # 5) 旧公共入口逐一验证：全部成功/未知/在途键阻断，只有明确失败可显式重试。
    ledger = open_ledger(exported, clock=FakeClock())
    assert isinstance(ledger, DeliveryLedger), ledger
    try:
        blocked_keys = [
            spec.key for spec in specs if spec.status in ("sent", "unknown", "sending")
        ]
        for key in blocked_keys:
            assert ledger.reserve(key).granted is False, (
                "旧公共入口必须阻断成功/未知/在途键",
                key.source_message_id,
            )
        # 在途旧记录在导入时归并为待核实：导出仍是阻断的未知口径。
        recovered = ledger.get(ledger_key(message_id="m-legacy-sending"))
        assert recovered is not None and recovered.status == "unknown", recovered
        # 旧明确失败保留可显式重试。
        assert ledger.reserve(ledger_key(message_id="m-legacy-failed")).granted is True
        # 旧成功记录保留目标 ID 与评论结果。
        sent = ledger.get(ledger_key(message_id="m-legacy-sent"))
        assert sent is not None and sent.status == "sent", sent
        assert sent.target_message_id == "t-legacy-sent", sent
        assert sent.comment_status == "sent" and sent.comment_message_id == "t-legacy-comment", sent
        # 新投递（本次服务产生）也在兼容快照里，并继续阻断。
        new_key = ledger_key(message_id="m-new-1")
        new_record = ledger.get(new_key)
        assert new_record is not None and new_record.status == "sent", new_record
        assert new_record.target_message_id, new_record
        assert ledger.reserve(new_key).granted is False, "新投递也必须阻断重发"
    finally:
        ledger.close()

    # 6) 全流程结束后原快照、旧库与导出文件都保持原样（导出不被回写）。
    exported_digest = _digest(exported)
    assert _digest(snapshot) == snapshot_digest_before, "全流程不得改动原快照"
    assert _digest(legacy_dir / LEDGER_FILE_NAME) == legacy_digest_before, "全流程不得改动原旧库"
    assert _digest(exported) == exported_digest, "导出文件不得被再次改写"

    # 场景报告：输入、发送次数、账本口径与工具结果。
    report: dict[str, Any] = {
        "input": [(spec.key.source_message_id, spec.status) for spec in specs],
        "migrated_identity": identity,
        "new_delivery": {
            "source_status": source["status"],
            "comment_status": comment["comment_status"],
            "source_sends": 1,
            "comment_sends": 1,
        },
        "ledger_rows": [list(row) for row in rows],
        "blocked_keys": [key.source_message_id for key in blocked_keys] + ["m-new-1"],
        "retryable_keys": ["m-legacy-failed"],
        "old_entry": {
            "sent_kept": sent.target_message_id,
            "unknown_recovered": recovered.status,
            "new_blocked": True,
        },
    }
    assert set(report["blocked_keys"]) == {
        "m-legacy-sent",
        "m-legacy-unknown",
        "m-legacy-sending",
        "m-new-1",
    }, report
    assert report["old_entry"]["unknown_recovered"] == "unknown", report
    assert len(report["ledger_rows"]) >= 5, report
