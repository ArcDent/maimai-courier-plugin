"""第 4 组账本行为：经持久状态模块的**导出接口**驱动，不测试私有 SQL 辅助函数。

用例只断言账本对外承诺的行为：唯一键构成、四种状态的区分、评论结果附带、参数
绑定与持久化、事务纪律与失败可区分性。启动恢复（遗留 sending）与损坏库安全关闭
见本文件后续小节。
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from delivery_ledger import (
    LEDGER_FILE_NAME,
    STATUS_FAILED,
    STATUS_SENDING,
    STATUS_SENT,
    STATUS_UNKNOWN,
    DeliveryLedger,
    LedgerStatus,
    LedgerUnavailable,
    LedgerUnavailableError,
    Reservation,
    open_ledger,
)

from tests.support import FakeClock, ledger_key, open_test_ledger


# --------------------------------------------------------------------------- 4.1


def test_账本键恰为账号源群源消息与目标群且不含评论(tmp_path: Path) -> None:
    ledger = open_test_ledger(tmp_path)
    key = ledger_key()

    reservation = ledger.reserve(key)
    assert isinstance(reservation, Reservation)
    assert reservation.granted is True
    assert reservation.record.status == STATUS_SENDING
    assert reservation.record.key == key

    for label, other in (
        ("账号", replace(key, account_id="99998")),
        ("源群", replace(key, source_group_id="10555")),
        ("源消息", replace(key, source_message_id="m-other")),
        ("目标群", replace(key, target_group_id="20003")),
    ):
        assert ledger.get(other) is None, f"{label}不同即为不同键，不应命中同一条记录"

    assert ledger.mark_sent(key, "delivered-9") is True
    assert ledger.record_comment(key, status=STATUS_SENT, message_id="comment-7") is True
    record = ledger.get(key)
    assert record is not None and record.comment_message_id == "comment-7", (
        "评论不参与键：写入评论后仍按原键读回，不需要新键"
    )


def test_账本区分四种状态并保存评论结果(tmp_path: Path) -> None:
    ledger = open_test_ledger(tmp_path)
    sending = ledger_key(message_id="m-sending")
    sent = ledger_key(message_id="m-sent")
    failed = ledger_key(message_id="m-failed")
    unknown = ledger_key(message_id="m-unknown")

    assert ledger.reserve(sending).record.status == STATUS_SENDING

    assert ledger.reserve(sent).granted is True
    assert ledger.mark_sent(sent, "delivered-9") is True
    assert ledger.record_comment(sent, status=STATUS_SENT, message_id="comment-7") is True

    assert ledger.reserve(failed).granted is True
    assert ledger.mark_failed(failed, "明确未发送") is True

    assert ledger.reserve(unknown).granted is True
    assert ledger.mark_unknown(unknown, "回执丢失") is True

    record = ledger.get(sent)
    assert record is not None
    assert record.status == STATUS_SENT
    assert record.target_message_id == "delivered-9", "成功记录必须保存真实目标消息 ID"
    assert record.comment_status == STATUS_SENT, "评论结果附属于源发送记录"
    assert record.comment_message_id == "comment-7"
    assert ledger.get(failed).status == STATUS_FAILED
    assert ledger.get(unknown).status == STATUS_UNKNOWN

    ledger.close()
    reopened = open_test_ledger(tmp_path)
    again = reopened.get(sent)
    assert again is not None
    assert (again.status, again.target_message_id) == (STATUS_SENT, "delivered-9")
    assert (again.comment_status, again.comment_message_id) == (STATUS_SENT, "comment-7"), (
        "评论结果必须跨重开账本保留"
    )
    assert reopened.get(failed).status == STATUS_FAILED
    assert reopened.get(unknown).status == STATUS_UNKNOWN


def test_SQL参数绑定且特殊字符ID按字面量存取(tmp_path: Path) -> None:
    ledger = open_test_ledger(tmp_path)
    hostile = "m-1'; DROP TABLE delivery_ledger; --"
    key = ledger_key(message_id=hostile)

    assert ledger.reserve(key).granted is True
    assert ledger.mark_sent(key, "delivered-'; DROP TABLE delivery_ledger; --") is True

    record = ledger.get(key)
    assert record is not None
    assert record.key.source_message_id == hostile, "特殊字符必须按字面量存取（参数绑定）"
    assert record.target_message_id == "delivered-'; DROP TABLE delivery_ledger; --"

    other = ledger.reserve(ledger_key(message_id="m-2"))
    assert other.granted is True, "表结构必须完好，注入字符串不得被当作 SQL 执行"


# --------------------------------------------------------------- 事务边界与失败可区分


class _FlakyCommitConnection(sqlite3.Connection):
    """可注入一次提交失败的连接：用真实 sqlite 连接表达存储故障，不伪造账本接口。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._fail_next_commit = False

    def fail_next_commit(self) -> None:
        """让下一次 `commit()` 抛错（不写入任何数据）。"""

        self._fail_next_commit = True

    def commit(self) -> None:
        if self._fail_next_commit:
            self._fail_next_commit = False
            raise sqlite3.OperationalError("注入的提交失败")
        super().commit()


def _flaky_ledger(tmp_path: Path) -> tuple[DeliveryLedger, _FlakyCommitConnection]:
    """先建好表，再用可注入提交失败的连接构造账本。"""

    path = tmp_path / LEDGER_FILE_NAME
    open_test_ledger(tmp_path).close()
    connection = sqlite3.connect(
        str(path),
        timeout=5.0,
        isolation_level=None,
        factory=_FlakyCommitConnection,
    )
    return DeliveryLedger(connection, FakeClock()), connection


def test_提交失败后回滚且连接仍可继续使用(tmp_path: Path) -> None:
    ledger, connection = _flaky_ledger(tmp_path)
    try:
        connection.fail_next_commit()
        with pytest.raises(LedgerUnavailableError) as failure:
            ledger.reserve(ledger_key(message_id="m-commit-fail"))

        assert str(failure.value).strip(), "提交失败必须如实报告不可用"

        resumed = ledger.reserve(ledger_key(message_id="m-after-commit-fail"))
        assert isinstance(resumed, Reservation) and resumed.granted is True, (
            f"提交失败必须回滚，连接不得停留在未结束事务: {resumed!r}"
        )
        assert ledger.get(ledger_key(message_id="m-after-commit-fail")) is not None
    finally:
        ledger.close()


def test_记录不存在与读取失败可区分(tmp_path: Path) -> None:
    ledger = open_test_ledger(tmp_path)

    assert ledger.get(ledger_key(message_id="m-missing")) is None, "记录不存在必须返回 None"

    ledger.close()
    with pytest.raises(LedgerUnavailableError) as failure:
        ledger.get(ledger_key())
    assert str(failure.value).strip(), "读取失败必须给出可定位原因，不能与「不存在」混同"


def test_写入失败与占位未命中可区分(tmp_path: Path) -> None:
    ledger = open_test_ledger(tmp_path)

    assert ledger.mark_sent(ledger_key(message_id="m-absent"), "m-delivered-x") is False, (
        "没有可更新的在途占位时返回 False（本次未登记），不得与本条失败语义混同"
    )

    ledger.close()
    with pytest.raises(LedgerUnavailableError) as failure:
        ledger.mark_sent(ledger_key(), "m-delivered-x")
    assert str(failure.value).strip(), "存储失败必须抛 LedgerUnavailableError，不能静默返回 False"


def test_恢复失败与零条可区分(tmp_path: Path) -> None:
    ledger = open_test_ledger(tmp_path)
    assert ledger.recover_interrupted() == 0, "空账本恢复零条"

    ledger.close()
    with pytest.raises(LedgerUnavailableError):
        ledger.recover_interrupted()


def test_发送状态与账本状态共用同一值域声明() -> None:
    """发送步骤的结果值域不得另行声明一份字面量（第 4 组评审遗留 #3）。

    `SendStatus` 必须是 `LedgerStatus` 本身（同一类型对象），而不是复制的
    `Literal[...]`：两份声明一旦漂移，编排的兜底分支与账本状态就会不一致。
    """

    from forward_delivery import SendStatus

    assert SendStatus is LedgerStatus, "发送状态值域必须与账本状态共用同一声明"


# --------------------------------------------------------------------------- 4.6


def test_损坏账本返回不可用且不覆盖原文件(tmp_path: Path) -> None:
    path = tmp_path / LEDGER_FILE_NAME
    path.write_bytes("这不是一个 SQLite 数据库".encode("utf-8") + b"\x00\xff" * 8)
    broken_bytes = path.read_bytes()

    outcome = open_ledger(path, clock=FakeClock())

    assert isinstance(outcome, LedgerUnavailable), f"损坏库必须返回不可用: {outcome!r}"
    assert "损坏" in outcome.reason, outcome
    assert path.exists() and path.read_bytes() == broken_bytes, (
        "不得删除、覆盖或改写损坏的状态数据"
    )
