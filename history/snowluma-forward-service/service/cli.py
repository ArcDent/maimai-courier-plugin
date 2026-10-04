"""服务端命令行：`python -m service <命令>`（design D1/D5/D10）。

命令面（本组公布，后续组沿用同一命名，不再改写）：

- `start [--config PATH]`：启动服务；配置缺失或不可用即明确失败，不发送任何消息；
- `ledger fresh --state-dir DIR`：新装初始化（生成稳定账本身份）；
- `ledger migrate --state-dir DIR --snapshot PATH [--confirm-ledger-id ID]`：
  从本插件旧账本只读迁移；
- `ledger export --state-dir DIR --output PATH [--confirm-ledger-id ID]`：
  兼容导出（含新结果的旧 schema 快照）；
- `ledger status --state-dir DIR`：只读查看账本身份与状态。

本模块只做参数解析与失败面；真实启动传输与账本操作经 `ServiceCommands` 接缝注入。
默认实现（`ServiceCommandRunner`）已接通：`start`（配置 → 凭证 → 账本独占打开与绑定
校验 → 启动恢复）与全部账本命令（`fresh` / `migrate` / `export` / `status`）；传输本身
由第 4–6 组接入，未接通时如实报告为非零退出，绝不静默成功。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from shared.clock import Clock, SystemClock
from shared.delivery import LedgerUnavailable, LedgerUnavailableError

from .auth import credential_available
from .config import ConfigUnavailable, ServiceConfig, load_service_config
from .gateway import AccountUnavailable, SnowLumaGateway
from .ledger_migration import (
    LedgerStatusReport,
    export_compatible_snapshot,
    initialize_fresh,
    migrate_legacy_snapshot,
    read_ledger_status,
)
from .ledger_store import SERVICE_LEDGER_FILE_NAME, open_service_ledger
from .onebot import (
    OneBotTransport,
    OneBotUnresolved,
    build_onebot_transport,
    close_transport,
    resolve_onebot_connection,
)
from .server import (
    SERVE_EXIT_OK,
    RunningService,
    ServeSeam,
    ServerUnavailable,
    build_running_service,
    start_http_server,
)

EXIT_OK = 0
"""成功。"""

EXIT_USAGE = 2
"""命令行用法错误（与 argparse 的约定一致）。"""

EXIT_CONFIG = 3
"""配置缺失、不可读或未通过校验：不启动、不发送。"""

EXIT_UNAVAILABLE = 4
"""服务尚未可用：传输未接通，或账本尚未初始化（都不是「已就绪」）。"""

EXIT_LEDGER = 5
"""账本故障：损坏、schema 不符、身份不匹配、被独占或恢复失败——关闭发送。"""

CONFIG_ENV_VAR = "MAIMAI_COURIER_SERVICE_CONFIG"
"""服务配置路径的环境变量名（非敏感路径；凭证值由各自环境变量提供，见 2.5）。"""


@dataclass(frozen=True, slots=True)
class StartOptions:
    """`start` 的解析结果。"""

    config: str = ""


@dataclass(frozen=True, slots=True)
class LedgerInitOptions:
    """`ledger fresh` / `ledger migrate` 的解析结果。"""

    state_dir: str = ""
    snapshot: str = ""
    confirm_ledger_id: str = ""


@dataclass(frozen=True, slots=True)
class LedgerExportOptions:
    """`ledger export` 的解析结果。"""

    state_dir: str = ""
    output: str = ""
    confirm_ledger_id: str = ""


@dataclass(frozen=True, slots=True)
class LedgerStatusOptions:
    """`ledger status` 的解析结果。"""

    state_dir: str = ""


class ServiceCommands(Protocol):
    """CLI 与实现之间的接缝：后续组替换默认实现，命令面不变。"""

    def start(self, options: StartOptions) -> int: ...

    def ledger_fresh(self, options: LedgerInitOptions) -> int: ...

    def ledger_migrate(self, options: LedgerInitOptions) -> int: ...

    def ledger_export(self, options: LedgerExportOptions) -> int: ...

    def ledger_status(self, options: LedgerStatusOptions) -> int: ...


class TransportFactory(Protocol):
    """传输工厂：按受控配置构造 OneBot 传输（测试注入模拟 OneBot，不连接真实地址）。"""

    def __call__(self, config: ServiceConfig, clock: Clock) -> "OneBotTransport | OneBotUnresolved": ...


def _report(message: str) -> None:
    """把失败原因写到 stderr（不含凭证与正文）。"""

    print(message, file=sys.stderr)


async def _serve_until_stopped(service: RunningService) -> int:
    """默认服务循环：等待停止信号（SIGINT/SIGTERM）后返回退出码。"""

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    installed: list[int] = []
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_number, stop.set)
            installed.append(signal_number)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - 平台差异
            continue
    try:
        await stop.wait()
    finally:
        for signal_number in installed:
            try:
                loop.remove_signal_handler(signal_number)
            except (NotImplementedError, RuntimeError):  # pragma: no cover - 平台差异
                continue
    return SERVE_EXIT_OK


def _ledger_exit_code(state_dir: str) -> int:
    """账本不可用时的退出码：文件存在 → 账本故障；不存在 → 尚未初始化（未部署完成）。"""

    return EXIT_LEDGER if (Path(state_dir) / SERVICE_LEDGER_FILE_NAME).exists() else EXIT_UNAVAILABLE


def _print_ledger_status(report: LedgerStatusReport) -> None:
    """打印只读账本报告（身份、计数与导入摘要；不含任何正文或凭证）。"""

    print(f"账本身份：{report.ledger_identity or '（尚未初始化）'}")
    print(f"schema 版本：{report.schema_version or '（未知）'}")
    counts = "，".join(f"{status}={count}" for status, count in sorted(report.deliveries.items()))
    print(f"投递记录：{counts or '（无）'}（共 {sum(report.deliveries.values())} 条）")
    print(f"尝试记录：{report.attempts} 条")
    for item in report.imports:
        print(f"已导入快照 {item.snapshot_sha256[:16]}…：{item.records} 条")


class ServiceCommandRunner:
    """默认实现：`start` 顺序为 配置 → 凭证 → 账本（独占 + 绑定校验）→ 启动恢复 →
    OneBot 连接与账号就绪 → 真实 HTTP 服务端就绪 → 服务循环（等待停止信号）→
    有界退出；账本初始化/迁移/导出由 3.6–3.9 接通。

    任一步失败都明确报告并以非零退出码结束，绝不建立发送任务。`start` 在连接与账号
    就绪前不监听任何端口；监听成功后打印的「已就绪」只表示接口可用与账本绑定已校验，
    不表示任何发送已发生（发送仍需客户端阶段授权）。
    """

    def __init__(
        self,
        *,
        transport_factory: TransportFactory | None = None,
        serve: ServeSeam | None = None,
    ) -> None:
        self._transport_factory = transport_factory or (
            lambda config, clock: build_onebot_transport(config)
        )
        self._serve = serve or _serve_until_stopped

    def start(self, options: StartOptions) -> int:
        config = options.config or os.environ.get(CONFIG_ENV_VAR, "")
        if not config:
            _report(f"缺少服务配置路径：请用 --config 指定，或设置环境变量 {CONFIG_ENV_VAR}")
            return EXIT_CONFIG
        loaded = load_service_config(Path(config))
        if isinstance(loaded, ConfigUnavailable):
            _report(f"{loaded.reason}，已停止启动（未建立任何网络连接）")
            return EXIT_CONFIG
        if not credential_available(os.environ, loaded.token_env_var):
            _report(
                f"凭证环境变量 {loaded.token_env_var} 未设置或为空，拒绝启动"
                "（服务不会接受任何请求，也不会产生任何发送）"
            )
            return EXIT_CONFIG
        return asyncio.run(self._serve_service(loaded))

    async def _serve_service(self, loaded: ServiceConfig) -> int:
        """同一事件循环里完成账本 → 恢复 → 连接 → HTTP 服务与有界退出（不跨 loop 传递对象）。"""

        ledger_path = Path(loaded.state_dir) / SERVICE_LEDGER_FILE_NAME
        clock = SystemClock()
        outcome = open_service_ledger(
            Path(loaded.state_dir), expected_ledger_id=loaded.ledger_id, clock=clock
        )
        if isinstance(outcome, LedgerUnavailable):
            _report(f"服务账本不可用：{outcome.reason}；已关闭发送（不建立任何发送任务）")
            return EXIT_LEDGER if ledger_path.exists() else EXIT_UNAVAILABLE
        ledger = outcome
        transport: OneBotTransport | None = None
        try:
            try:
                report = await ledger.recover_interrupted()
            except LedgerUnavailableError as error:
                _report(
                    f"启动恢复失败：{error.reason}；已关闭发送"
                    "（遗留占位继续阻断重发，绝不自动续发源或评论）"
                )
                return EXIT_LEDGER
            _report(
                "服务账本已就绪并完成启动恢复"
                f"（源在途归并为待核实 {report.sources_recovered} 条、"
                f"评论在途归并为待核实 {report.comments_recovered} 条、"
                f"失效准备 {report.prepares_expired} 条）"
            )
            resolved = resolve_onebot_connection(loaded, os.environ)
            if isinstance(resolved, OneBotUnresolved):
                _report(f"OneBot 连接不可用：{resolved.reason}；已关闭发送（未建立发送任务）")
                return EXIT_CONFIG if resolved.config_failure else EXIT_UNAVAILABLE
            candidate = self._transport_factory(loaded, clock)
            if isinstance(candidate, OneBotUnresolved):
                _report(
                    f"OneBot 连接不可用：{candidate.reason}；已关闭发送（未建立发送任务）"
                )
                return EXIT_CONFIG if candidate.config_failure else EXIT_UNAVAILABLE
            transport = candidate
            gateway = SnowLumaGateway(
                transport=transport, account_id=loaded.account_id, clock=clock
            )
            ready = await gateway.connect()
            if isinstance(ready, AccountUnavailable):
                _report(f"OneBot 连接或账号未就绪：{ready.reason}；已关闭发送")
                return EXIT_UNAVAILABLE
            _report(f"OneBot 连接与账号已就绪（绑定账号 {ready.account_id}）")

            identity = await ledger.identity()
            built = build_running_service(
                config=loaded,
                ledger=ledger,
                gateway=gateway,
                clock=clock,
                ledger_identity=identity,
            )
            if isinstance(built, ServerUnavailable):
                _report(f"{built.reason}；已关闭发送（未对外提供任何接口）")
                return EXIT_CONFIG
            return await self._serve_http(built)
        finally:
            if transport is not None:
                await close_transport(transport)
            await ledger.close()

    async def _serve_http(self, service: RunningService) -> int:
        """启动真实 HTTP 服务端 → 报告监听地址 → 运行服务循环 → 有界退出与告警。"""

        started = await start_http_server(service.app, service.config)
        if isinstance(started, ServerUnavailable):
            _report(f"HTTP 服务端不可用：{started.reason}；已关闭发送（未对外提供任何接口）")
            return EXIT_UNAVAILABLE
        service.host, service.port = started.host, started.port
        try:
            _report(
                f"HTTP 服务端已就绪并监听 {started.address}"
                "（协议 v1；认证、账本绑定与账号已校验。接口可用不代表已有发送）"
            )
            return await self._serve(service)
        finally:
            await started.stop()
            report = await service.coordinator.shutdown()
            for item in report.leftover:
                _report(
                    f"服务退出超时（已等待 {report.waited_seconds:g} 秒）：{item} 未能确定结束；"
                    "账本保留在途占位，下次启动归并为待核实（不自动重发）"
                )

    def ledger_fresh(self, options: LedgerInitOptions) -> int:
        if not options.state_dir:
            _report("缺少 --state-dir：账本状态目录必须显式指定")
            return EXIT_USAGE
        outcome = initialize_fresh(
            Path(options.state_dir),
            confirm_ledger_id=options.confirm_ledger_id,
            clock=SystemClock(),
        )
        if isinstance(outcome, LedgerUnavailable):
            _report(f"账本新装初始化失败：{outcome.reason}")
            return EXIT_LEDGER
        if outcome.created:
            print(f"账本新装初始化完成。账本身份：{outcome.ledger_identity}")
            print("请把该身份配置为服务端与客户端的预期账本 ID（凭证不写入账本）。")
        else:
            print(
                "账本已初始化（确认既有身份，未改动任何记录）。"
                f"账本身份：{outcome.ledger_identity}"
            )
        return EXIT_OK

    def ledger_migrate(self, options: LedgerInitOptions) -> int:
        if not options.state_dir or not options.snapshot:
            _report("缺少 --state-dir 或 --snapshot：迁移必须显式指定状态目录与旧库快照")
            return EXIT_USAGE
        outcome = migrate_legacy_snapshot(
            Path(options.state_dir),
            Path(options.snapshot),
            confirm_ledger_id=options.confirm_ledger_id,
            clock=SystemClock(),
        )
        if isinstance(outcome, LedgerUnavailable):
            _report(f"旧账本迁移失败：{outcome.reason}；发送保持关闭，原库未被改动")
            return EXIT_LEDGER
        if outcome.already_imported:
            print(
                "该快照此前已导入（幂等，不重复记录）。"
                f"账本身份：{outcome.ledger_identity}"
            )
            return EXIT_OK
        legacy = "，".join(
            f"{status}={count}" for status, count in sorted(outcome.legacy_statuses.items())
        )
        print(f"旧账本迁移完成。账本身份：{outcome.ledger_identity}")
        print(
            f"导入投递记录 {outcome.records} 条（快照原状态：{legacy or '无'}；"
            f"另有 {outcome.unchanged} 条与既有记录一致）；原库未被改动。"
        )
        print(f"快照摘要：{outcome.snapshot_sha256}")
        return EXIT_OK

    def ledger_export(self, options: LedgerExportOptions) -> int:
        if not options.state_dir or not options.output:
            _report("缺少 --state-dir 或 --output：导出必须显式指定状态目录与输出路径")
            return EXIT_USAGE
        outcome = export_compatible_snapshot(
            Path(options.state_dir),
            Path(options.output),
            confirm_ledger_id=options.confirm_ledger_id,
        )
        if isinstance(outcome, LedgerUnavailable):
            _report(f"兼容导出失败：{outcome.reason}；服务账本未被改写")
            return EXIT_LEDGER
        statuses = "，".join(f"{status}={count}" for status, count in sorted(outcome.statuses.items()))
        print(f"兼容导出完成：{outcome.output}")
        print(
            f"导出投递记录 {outcome.records} 条（{statuses or '无'}）；"
            "已在写入后校验键集合与数量，服务账本未被改写。"
        )
        print("回滚前请确认：在途状态已保守导出为待核实，评论不自动补齐。")
        return EXIT_OK

    def ledger_status(self, options: LedgerStatusOptions) -> int:
        if not options.state_dir:
            _report("缺少 --state-dir：账本状态目录必须显式指定")
            return EXIT_USAGE
        report = read_ledger_status(Path(options.state_dir))
        if isinstance(report, LedgerUnavailable):
            _report(f"账本状态不可读：{report.reason}")
            return _ledger_exit_code(options.state_dir)
        _print_ledger_status(report)
        return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    """构造完整命令面（帮助即公开契约；子命令名后续组沿用）。"""

    parser = argparse.ArgumentParser(
        prog="python -m service",
        description="麦麦搬运喵的独立 SnowLuma 转发服务（HTTP/JSON v1）",
        epilog="\n".join(
            [
                "示例：",
                "  python -m service start --config /etc/maimai-courier/service.toml",
                "  python -m service ledger fresh --state-dir /var/lib/maimai-courier",
                "  python -m service ledger migrate --state-dir /var/lib/maimai-courier \\",
                "      --snapshot ./delivery_ledger.sqlite3 --confirm-ledger-id <账本身份>",
                "  python -m service ledger export --state-dir /var/lib/maimai-courier \\",
                "      --output ./compat.sqlite3",
                "  python -m service ledger status --state-dir /var/lib/maimai-courier",
            ]
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", metavar="{start,ledger}")

    start = subparsers.add_parser("start", help="启动服务（默认回环监听；缺配置即失败）")
    start.add_argument(
        "--config",
        default="",
        metavar="PATH",
        help=f"服务配置文件路径（缺省读环境变量 {CONFIG_ENV_VAR}）",
    )
    start.set_defaults(handler_name="start")

    ledger = subparsers.add_parser("ledger", help="账本管理：fresh / migrate / export / status")
    ledger_subparsers = ledger.add_subparsers(
        dest="ledger_command",
        metavar="{fresh,migrate,export,status}",
    )

    fresh = ledger_subparsers.add_parser("fresh", help="新装初始化：生成稳定账本身份")
    fresh.add_argument("--state-dir", default="", metavar="DIR", help="服务状态目录")
    fresh.add_argument(
        "--confirm-ledger-id",
        default="",
        metavar="ID",
        help="确认新账本身份（已有状态不得隐式覆盖成 fresh）",
    )
    fresh.set_defaults(handler_name="ledger_fresh")

    migrate = ledger_subparsers.add_parser("migrate", help="从本插件旧账本只读迁移")
    migrate.add_argument("--state-dir", default="", metavar="DIR", help="服务状态目录")
    migrate.add_argument("--snapshot", default="", metavar="PATH", help="旧账本快照路径")
    migrate.add_argument(
        "--confirm-ledger-id",
        default="",
        metavar="ID",
        help="确认迁移目标账本身份（与客户端预期一致才启用发送）",
    )
    migrate.set_defaults(handler_name="ledger_migrate")

    export = ledger_subparsers.add_parser("export", help="兼容导出：含新结果的旧 schema 快照")
    export.add_argument("--state-dir", default="", metavar="DIR", help="服务状态目录")
    export.add_argument("--output", default="", metavar="PATH", help="导出文件路径")
    export.add_argument(
        "--confirm-ledger-id",
        default="",
        metavar="ID",
        help="确认导出源账本身份（不匹配即拒绝导出）",
    )
    export.set_defaults(handler_name="ledger_export")

    status = ledger_subparsers.add_parser("status", help="只读查看账本身份与状态")
    status.add_argument("--state-dir", default="", metavar="DIR", help="服务状态目录")
    status.set_defaults(handler_name="ledger_status")

    return parser


def _options_for(args: argparse.Namespace) -> object:
    """把 argparse 结果折叠成对应命令的选项数据类。"""

    handler = args.handler_name
    if handler == "start":
        return StartOptions(config=args.config)
    if handler in ("ledger_fresh", "ledger_migrate"):
        return LedgerInitOptions(
            state_dir=args.state_dir,
            snapshot=getattr(args, "snapshot", ""),
            confirm_ledger_id=getattr(args, "confirm_ledger_id", ""),
        )
    if handler == "ledger_export":
        return LedgerExportOptions(
            state_dir=args.state_dir,
            output=args.output,
            confirm_ledger_id=args.confirm_ledger_id,
        )
    return LedgerStatusOptions(state_dir=args.state_dir)


def main(argv: Sequence[str] | None = None, *, commands: ServiceCommands | None = None) -> int:
    """解析参数并分发到命令实现；缺命令或未知命令按用法错误退出。"""

    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else sys.argv[1:])
    handler_name = getattr(args, "handler_name", None)
    if not handler_name:
        parser.print_help(sys.stdout)
        return EXIT_USAGE
    implementation = commands or ServiceCommandRunner()
    handler = getattr(implementation, handler_name)
    return int(handler(_options_for(args)))
