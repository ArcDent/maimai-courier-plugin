"""插件侧跨进程驱动：在独立 Python 进程里按作业描述执行一次 `forward_message`。

用法（测试设施 `tests.cross_process_support.run_client_process` 调用）：

    python tests/client_process.py <job.json>

作业描述是 JSON 文件，含：服务地址、凭证**环境变量名**（凭证值只经进程环境传入，
绝不写进作业文件）、预期账本身份、迁移确认标识、插件数据目录、源/目标群与源消息 ID、
宿主形态源消息段。驱动在同一进程内按测试同款装载口径装配真实插件（fake SDK 替身 +
合成包，直接复用 `tests.support.make_plugin` 的装配），经
`maisaka.planner.after_response` Hook 取得一次性来源凭据后调用公共工具，
最后一行 stdout 输出 JSON 报告：工具结果、最终参数与宿主能力调用记录。

本脚本不做任何真实平台发送：发送只发生在它连接的独立服务进程里；报告里的宿主
`send.*` 调用记录必须为零（design D9）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


def _load_job(path: Path) -> dict[str, Any]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        raise SystemExit("作业描述必须是 JSON 对象")
    return decoded


def _prepare_imports() -> None:
    """建立与 pytest 进程一致的装载口径（fake SDK + 合成包 + 模块别名）。"""

    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import conftest  # noqa: F401  # 安装 fake maibot_sdk 与合成包别名（唯一装载口径）


def main(argv: Sequence[str]) -> int:
    if len(argv) != 2:
        raise SystemExit("用法：python tests/client_process.py <job.json>")
    job = _load_job(Path(argv[1]))
    _prepare_imports()

    from maimai_courier_plugin.clock import SystemClock

    from tests.support import enabled_config, issue_source_call, make_plugin, prepare_source

    data_dir = Path(str(job["data_dir"]))
    data_dir.mkdir(parents=True, exist_ok=True)
    source_groups = tuple(job.get("source_groups") or ("10001",))
    target_groups = tuple(job.get("target_groups") or ("20002",))
    config = enabled_config(source_groups=source_groups, target_groups=target_groups)
    config["service"] = {
        "endpoint": str(job["endpoint"]),
        "client_identity": str(job.get("client_identity") or "maibot-plugin"),
        "token_env_var": str(job["token_env_var"]),
        "expected_ledger_id": str(job["expected_ledger_id"]),
        "migration_confirmation": str(job["migration_confirmation"]),
    }
    # 复用测试侧同款装配（fake SDK 上下文 + 单实例模块别名）；子进程按真实时间执行，
    # 因此显式传生产时钟——注入可控时钟会让评论等待永不醒转。
    plugin, ctx = make_plugin(
        data_dir,
        config=config,
        clock=SystemClock(),
        request_timeout_seconds=float(job.get("request_timeout_seconds") or 10.0),
    )
    raw_message = job.get("raw_message")
    prepare_source(
        ctx,
        raw_message=[dict(segment) for segment in raw_message] if raw_message is not None else None,
        message_id=str(job.get("message_id") or "m-source-1"),
    )
    arguments = issue_source_call(
        plugin,
        session_id=str(job.get("source_session_id") or "s-source-10001"),
        message_id=str(job.get("message_id") or "m-source-1"),
        target_group_id=str(job.get("target_group_id") or "20002"),
        comment=job.get("comment") or "",
    )

    async def run() -> dict[str, Any]:
        result = await plugin.forward_message(**arguments)
        if bool(job.get("unload_after_call", True)):
            await plugin.on_unload()
        return result

    result = asyncio.run(run())
    report = {
        "ok": True,
        "result": result,
        "arguments": arguments,
        "host": {
            "chat_calls": [dict(call) for call in ctx.chat.calls],
            "message_calls": [dict(call) for call in ctx.message.calls],
            "old_send_calls": list(ctx.send.calls),
            "proactive_calls": list(ctx.maisaka.proactive.calls),
            "logs": [[level, text] for level, text in ctx.logger.records],
        },
    }
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
