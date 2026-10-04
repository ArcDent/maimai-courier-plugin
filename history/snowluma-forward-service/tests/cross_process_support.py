"""第 7 组跨进程验收支持：真实子进程 + 真实回环 WebSocket 模拟 OneBot。

供 7.2/7.3 的「跨进程」验收使用（不得只用同进程模拟）：

- `MockOneBotServer`：真实回环 WebSocket 服务端，讲第 1 组核实的 OneBot 动作协议
  （`get_login_info` / `get_msg` / `get_group_list` / `send_msg`），校验 Bearer 凭证，
  记录调用与发送，可注入错误响应、断线、悬停在途（供「已发出但回执未到」用例）；
- `ServiceProcess` / `start_service_process`：以 `python -m service start --config …`
  启动**真实子进程**服务，等待真实监听就绪，支持 SIGTERM 停止与 SIGKILL 强杀；
- `run_client_process`：以独立 `python tests/client_process.py <job>` 子进程驱动真实
  插件工具调用（真实 HTTP 客户端 → 真实服务子进程 → 模拟 OneBot）；
- `write_service_config`：写服务端受控配置（凭证只以环境变量名出现，不落盘）。

所有地址固定在本机回环 `127.0.0.1`；不连接真实 OneBot/QQ，不发送真实消息。

本模块同时承载验收设施共用的纯算法与 CLI 入口（`authoritative_message_id`、
`ledger_identity_from`、`run_service_cli`）：其他验收模块从这里复用，避免同一算法或
同一启动方式出现第二份实现。导入本模块**不需要 aiohttp**——网络库只在真正启动回环
服务时才装载，无 aiohttp 的环境仍可用这些纯入口。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # 网络库按需装载：纯算法与同步 CLI 入口在没有 aiohttp 的环境也可被复用
    import aiohttp
    from aiohttp import web
except ImportError:  # pragma: no cover - 需要回环服务的用例各自 importorskip("aiohttp")
    aiohttp = None  # type: ignore[assignment]
    web = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parent.parent
"""仓库根：子进程服务与客户端驱动都以此为工作目录。"""

DEFAULT_SERVICE_TOKEN_ENV = "MC_E2E_SERVICE_TOKEN"
"""服务端 HTTP 凭证环境变量名（测试进程临时注入子进程环境，不落盘）。"""

DEFAULT_SERVICE_TOKEN = "e2e-service-token"
"""合成服务凭证（只在进程环境里传递）。"""

DEFAULT_ONEBOT_TOKEN_ENV = "MC_E2E_ONEBOT_TOKEN"
"""OneBot 连接凭证环境变量名（只出现在服务配置与子进程环境里）。"""

DEFAULT_ONEBOT_TOKEN = "e2e-onebot-token"
"""合成 OneBot 凭证。"""


LEDGER_IDENTITY_PATTERN = re.compile(r"账本身份：([0-9a-f]{32})")
"""服务 CLI 输出里的账本身份行（32 位十六进制摘要）。"""


def authoritative_message_id(sequence: int, group_id: str) -> int:
    """按第 1 组 1.4 核实的算法形态给出权威消息 ID（sha1 前 4 字节有符号）。

    这是测试设施里该算法的**唯一**实现：`tests/service_support.FakeOneBot` 等调用点
    都从这里复用，避免同一算法两份实现各自漂移。
    """

    digest = hashlib.sha1(f"{sequence}:{group_id}:group_message".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big", signed=True)


def ledger_identity_from(output: str) -> str:
    """从服务 CLI 输出解析账本身份；缺失时直接断言失败（不猜造、不跳过）。"""

    match = LEDGER_IDENTITY_PATTERN.search(output)
    assert match, output
    return match.group(1)


READY_LINE_PATTERN = re.compile(r"HTTP 服务端已就绪并监听 127\.0\.0\.1:(\d+)")
"""服务就绪行：从系统分配的真实端口解析监听地址（监听端口 0 时无端口竞态）。"""


@dataclass
class MockOneBotServer:
    """真实回环 WebSocket 的模拟 OneBot：可暂停、可断线、可注入错误响应。

    与内存替身 `tests.service_support.FakeOneBot` 同一返回形状；差别是这里走真实
    WebSocket，因此可以被**独立进程**的服务端连接。`hold(action)` 让该动作的记录
    先发生、响应悬停到 `release()`（用于「平台已收到发送但回执未到」的时序注入）。
    """

    account_id: int = 10000
    nickname: str = "搬运喵"
    groups: tuple[str, ...] = ("20002",)
    token: str = DEFAULT_ONEBOT_TOKEN
    messages: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    send_calls: list[dict[str, Any]] = field(default_factory=list)
    send_count: int = 0
    query_counts: dict[str, int] = field(default_factory=dict)
    _failures: dict[str, list[Mapping[str, Any]]] = field(default_factory=dict, repr=False)
    _errors: dict[str, list[BaseException]] = field(default_factory=dict, repr=False)
    _held: set[str] = field(default_factory=set, repr=False)
    _hold_event: asyncio.Event | None = field(default=None, repr=False)
    _sockets: list[Any] = field(default_factory=list, repr=False)
    _runner: Any = field(default=None, repr=False)
    _port: int = 0

    # ------------------------------------------------------------------ 生命周期

    @property
    def endpoint(self) -> str:
        """受控连接地址（回环 + 真实端口）。"""

        return f"ws://127.0.0.1:{self._port}/onebot"

    async def start(self) -> "MockOneBotServer":
        if web is None:  # pragma: no cover - 跨进程用例已用 importorskip 守卫
            raise RuntimeError("模拟 OneBot 需要 aiohttp（本环境未安装）")
        app = web.Application()
        app.router.add_get("/onebot", self._handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host="127.0.0.1", port=0)
        await site.start()
        addresses = list(getattr(runner, "addresses", ()) or ())
        if not addresses:
            await runner.cleanup()
            raise AssertionError("模拟 OneBot 已监听但无法确认真实回环端口（addresses 为空）")
        host, port = addresses[0]
        if not isinstance(host, str) or not host or not isinstance(port, int) or port <= 0:
            await runner.cleanup()
            raise AssertionError(f"模拟 OneBot 无法确认有效回环地址：{addresses!r}")
        self._port = int(port)
        self._runner = runner
        return self

    async def stop(self) -> None:
        await self.disconnect()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ------------------------------------------------------------------ 注入面

    def fail_next(self, action: str, response: Mapping[str, Any]) -> None:
        """下一次该动作返回注入的错误响应。"""

        self._failures.setdefault(action, []).append(dict(response))

    def raise_next(self, action: str, error: BaseException) -> None:
        """下一次该动作断开连接（模拟调用中断线）。"""

        self._errors.setdefault(action, []).append(error)

    def hold(self, action: str) -> None:
        """该动作下一次调用先记录、再悬停，直到 `release()`。"""

        self._held.add(action)
        if self._hold_event is None:
            self._hold_event = asyncio.Event()

    def release(self) -> None:
        """放行悬停中的动作响应。"""

        if self._hold_event is not None:
            self._hold_event.set()

    async def disconnect(self) -> None:
        """断开全部连接（此后调用在服务端表现为 OneBot 不可用）。"""

        for socket in list(self._sockets):
            try:
                await socket.close()
            except Exception:  # pragma: no cover - 关闭失败不改变测试结论
                pass
        self._sockets.clear()

    def store(self, message: Mapping[str, Any]) -> None:
        """写入一条 `get_msg` 可见的合成存储副本。"""

        self.messages[str(message["message_id"])] = dict(message)

    # ------------------------------------------------------------------ WebSocket

    async def _handle(self, request: web.Request) -> web.WebSocketResponse:
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            return web.Response(status=401, text="missing or invalid OneBot credential")
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        self._sockets.append(socket)
        try:
            async for message in socket:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                payload = json.loads(message.data)
                action = str(payload.get("action") or "")
                if self._errors.get(action):
                    self._errors[action].pop(0)
                    await socket.close(code=1011, message=b"injected disconnect")
                    break
                response = await self._dispatch(action, payload.get("params") or {})
                if response is None:
                    break
                await socket.send_json({**response, "echo": payload.get("echo")})
        except (ConnectionResetError, RuntimeError):
            pass  # 连接被对端强杀：本连接不再有响应可发，不改变已记录的事实
        finally:
            if socket in self._sockets:
                self._sockets.remove(socket)
        return socket

    async def _dispatch(self, action: str, params: Mapping[str, Any]) -> Mapping[str, Any] | None:
        if action == "get_login_info":
            self._record(action)
            injected = self._take_failure(action)
            if injected is not None:
                return injected
            return _ok({"user_id": self.account_id, "nickname": self.nickname})
        if action == "get_group_list":
            self._record(action)
            injected = self._take_failure(action)
            if injected is not None:
                return injected
            return _ok(
                [{"group_id": group, "group_name": f"合成群 {group}"} for group in self.groups]
            )
        if action == "get_msg":
            message_id = str(params.get("message_id") or "")
            self._record(action, target=message_id)
            injected = self._take_failure(action)
            if injected is not None:
                return injected
            stored = self.messages.get(message_id)
            if stored is None:
                return {"status": "failed", "retcode": 100, "message": "message not found"}
            return _ok(dict(stored))
        if action == "send_msg":
            target_group_id = str(params.get("group_id") or "")
            segments = [dict(segment) for segment in (params.get("message") or ())]
            self.send_count += 1
            call = {
                "operation": "send_msg",
                "target": target_group_id,
                "message": segments,
            }
            self.calls.append(call)
            self.send_calls.append(call)
            injected = self._take_failure(action)
            if injected is not None:
                return injected
            receipt, stored = self._receipt(self.send_count, target_group_id, segments)
            self.messages[str(receipt["message_id"])] = stored
            if action in self._held:
                self._held.discard(action)
                assert self._hold_event is not None
                await self._hold_event.wait()
            return _ok(receipt)
        return {"status": "failed", "retcode": 1404, "message": f"unsupported action {action}"}

    def _receipt(
        self, sequence: int, target_group_id: str, segments: Sequence[Mapping[str, Any]]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        message_id = authoritative_message_id(sequence, target_group_id)
        stored = {
            "message_type": "group",
            "message_id": message_id,
            "message_seq": sequence,
            "group_id": target_group_id,
            "user_id": self.account_id,
            "sender": {"user_id": self.account_id},
            "message": [dict(segment) for segment in segments],
        }
        return {"message_id": message_id, "message_seq": sequence}, stored

    def _record(self, action: str, *, target: str = "") -> None:
        self.calls.append({"operation": action, "target": target})
        self.query_counts[action] = self.query_counts.get(action, 0) + 1

    def _take_failure(self, action: str) -> Mapping[str, Any] | None:
        pending = self._failures.get(action)
        if pending:
            return pending.pop(0)
        return None

    # ------------------------------------------------------------------ 报告

    def sends(self) -> list[list[dict[str, Any]]]:
        """全部平台发送的段序列（按发生顺序）。"""

        return [list(call["message"]) for call in self.send_calls]


def _ok(data: Any) -> Mapping[str, Any]:
    return {"status": "ok", "retcode": 0, "data": data}


# --------------------------------------------------------------------------- 服务子进程


def write_service_config(
    path: Path,
    *,
    state_dir: Path,
    port: int,
    onebot_endpoint: str,
    ledger_id: str = "",
    token_env_var: str = DEFAULT_SERVICE_TOKEN_ENV,
    onebot_token_env_var: str = DEFAULT_ONEBOT_TOKEN_ENV,
    source_groups: Sequence[str] = ("10001",),
    target_groups: Sequence[str] = ("20002",),
    listen_host: str = "127.0.0.1",
    account_id: str = "10000",
    client_identity: str = "maibot-plugin",
) -> Path:
    """写服务端受控配置：凭证只以环境变量名出现（绝不把凭证值写进文件）。

    `port=0` 表示让系统在绑定时分配临时端口；服务就绪行会报告真实端口，调用方据
    此解析实际地址（测试设施因此不需要「先探空闲端口」的无竞态前置步骤）。
    """

    def array(values: Sequence[str]) -> str:
        return "[" + ", ".join(json.dumps(value) for value in values) + "]"

    path.write_text(
        "\n".join(
            [
                "[service]",
                f"client_identity = {json.dumps(client_identity)}",
                f"account_id = {json.dumps(account_id)}",
                f"state_dir = {json.dumps(str(state_dir))}",
                f"ledger_id = {json.dumps(ledger_id)}",
                f"listen_host = {json.dumps(listen_host)}",
                f"listen_port = {port}",
                f"token_env_var = {json.dumps(token_env_var)}",
                f"onebot_endpoint = {json.dumps(onebot_endpoint)}",
                f"onebot_token_env_var = {json.dumps(onebot_token_env_var)}",
                f"allowed_source_groups = {array(source_groups)}",
                f"allowed_target_groups = {array(target_groups)}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


@dataclass
class ServiceProcess:
    """真实服务子进程：stderr 持续收集，支持 SIGTERM 停止与 SIGKILL 强杀。"""

    process: Any
    port: int
    state_dir: Path
    config_path: Path
    stderr_lines: list[str] = field(default_factory=list)
    _reader: Any = field(default=None, repr=False)

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self.port}"

    @property
    def base_url(self) -> str:
        return f"http://{self.address}"

    @property
    def alive(self) -> bool:
        return self.process.returncode is None

    def stderr_text(self) -> str:
        return "\n".join(self.stderr_lines)

    async def stop(self, *, timeout: float = 20.0) -> int:
        """SIGTERM 停止并等待有界退出；超时强杀（返回退出码）。"""

        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout)
            except asyncio.TimeoutError:  # pragma: no cover - 服务退出应很快
                self.process.kill()
                await self.process.wait()
        await self._drain()
        return int(self.process.returncode)

    async def kill(self) -> int:
        """SIGKILL 强杀（模拟进程崩溃：未落盘的 `sending` 留给下次启动恢复）。"""

        if self.process.returncode is None:
            self.process.kill()
            await self.process.wait()
        await self._drain()
        return int(self.process.returncode)

    async def _drain(self) -> None:
        if self._reader is not None:
            try:
                await asyncio.wait_for(self._reader, timeout=5.0)
            except asyncio.TimeoutError:  # pragma: no cover - 读取任务应随进程结束
                self._reader.cancel()
            self._reader = None


async def start_service_process(
    *,
    state_dir: Path,
    onebot_endpoint: str,
    port: int = 0,
    ledger_id: str = "",
    service_token: str = DEFAULT_SERVICE_TOKEN,
    service_token_env: str = DEFAULT_SERVICE_TOKEN_ENV,
    onebot_token: str = DEFAULT_ONEBOT_TOKEN,
    onebot_token_env: str = DEFAULT_ONEBOT_TOKEN_ENV,
    source_groups: Sequence[str] = ("10001",),
    target_groups: Sequence[str] = ("20002",),
    config_path: Path | None = None,
    ready_timeout: float = 30.0,
) -> ServiceProcess:
    """以真实子进程启动服务并等待监听就绪（就绪行出现在 stderr）。

    监听端口缺省为 0：由系统在绑定时分配真实端口，再从就绪行解析出实际端口——测试
    侧不再「先探空闲端口、再让服务绑定」，因此不存在端口被并发抢占的 TOCTOU 竞态。
    绑定失败时服务立即非零退出，等待循环随即报错；就绪行始终不出现则在 `ready_timeout`
    内明确失败并强杀，绝不长时间挂起或静默通过。
    """

    if config_path is not None:
        path = config_path
    else:
        # 端口不再可当文件名的区分度：用状态目录名保证同一目录只对应一份受控配置。
        path = state_dir.parent / f"service-{state_dir.name}.toml"
    state_dir.mkdir(parents=True, exist_ok=True)
    write_service_config(
        path,
        state_dir=state_dir,
        port=port,
        onebot_endpoint=onebot_endpoint,
        ledger_id=ledger_id,
        token_env_var=service_token_env,
        onebot_token_env_var=onebot_token_env,
        source_groups=source_groups,
        target_groups=target_groups,
    )
    environment = dict(os.environ)
    environment[service_token_env] = service_token
    environment[onebot_token_env] = onebot_token
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "service",
        "start",
        "--config",
        str(path),
        cwd=ROOT,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    service = ServiceProcess(
        process=process, port=port, state_dir=state_dir, config_path=path
    )
    service._reader = asyncio.create_task(_collect_stderr(service))
    deadline = asyncio.get_running_loop().time() + ready_timeout
    while True:
        bound = _ready_port(service.stderr_lines)
        if bound is not None:
            service.port = bound
            return service
        if not service.alive:
            raise AssertionError(
                f"服务子进程提前退出（退出码 {process.returncode}）：\n{service.stderr_text()}"
            )
        if asyncio.get_running_loop().time() > deadline:
            await service.kill()
            raise AssertionError(f"服务子进程未在期限内就绪：\n{service.stderr_text()}")
        await asyncio.sleep(0.02)


def _ready_port(lines: Sequence[str]) -> int | None:
    """从服务 stderr 行解析真实监听端口；通知行尚未打印时返回 `None`。"""

    for line in lines:
        match = READY_LINE_PATTERN.search(line)
        if match:
            return int(match.group(1))
    return None


async def _collect_stderr(service: ServiceProcess) -> None:
    """持续读取子进程 stderr（同时保留日志文本供 7.4 的数据最小化断言）。"""

    stream = service.process.stderr
    if stream is None:  # pragma: no cover - 构造时总是给了 PIPE
        return
    while True:
        line = await stream.readline()
        if not line:
            return
        service.stderr_lines.append(line.decode("utf-8", "replace").rstrip("\n"))


# --------------------------------------------------------------------------- 客户端子进程


async def run_client_process(
    job: Mapping[str, Any],
    *,
    env_extra: Mapping[str, str] | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """以独立 Python 子进程执行一次真实插件工具调用，返回子进程报告。

    作业描述写到临时文件（**不含凭证**）；凭证只经子进程环境变量传递。子进程最后
    一行 stdout 是 JSON 报告（`result` / `arguments` / `host`）。
    """

    job_path = Path(str(job["job_path"]))
    job_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    environment = dict(os.environ)
    environment.update(env_extra or {})
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(ROOT / "tests" / "client_process.py"),
        str(job_path),
        cwd=ROOT,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise
    text_out = stdout.decode("utf-8", "replace")
    text_err = stderr.decode("utf-8", "replace")
    if process.returncode != 0:
        raise AssertionError(
            f"客户端子进程退出码 {process.returncode}：\n{text_out}\n{text_err}"
        )
    lines = [line for line in text_out.splitlines() if line.strip()]
    assert lines, f"客户端子进程没有输出：\n{text_err}"
    decoded = json.loads(lines[-1])
    assert isinstance(decoded, dict) and decoded.get("ok") is True, decoded
    return decoded


async def start_client_process(
    job: Mapping[str, Any],
    *,
    env_extra: Mapping[str, str] | None = None,
) -> Any:
    """启动客户端子进程但不等待结束（供「进程中途被强杀」用例）。"""

    job_path = Path(str(job["job_path"]))
    job_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    environment = dict(os.environ)
    environment.update(env_extra or {})
    return await asyncio.create_subprocess_exec(
        sys.executable,
        str(ROOT / "tests" / "client_process.py"),
        str(job_path),
        cwd=ROOT,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def kill_process(process: Any) -> tuple[str, str]:
    """强杀子进程并排空管道（进程已退出时同样安全）。"""

    if process.returncode is None:
        process.kill()
    stdout, stderr = await process.communicate()
    return stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")


async def http_json(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    token: str,
    payload: Mapping[str, Any] | None = None,
    raw_body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, Mapping[str, Any]]:
    """对真实回环服务发一次 JSON 请求（认证头可选；返回状态码与响应体）。"""

    merged = dict(headers or {})
    if token:
        merged.setdefault("Authorization", f"Bearer {token}")
    data: Any = raw_body
    if payload is not None:
        merged["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    async with session.request(method, url, data=data, headers=merged) as response:
        body = await response.read()
        try:
            decoded = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            decoded = {"raw": body.decode("utf-8", "replace")}
        return int(response.status), decoded


async def wait_for(predicate: Any, *, ticks: int = 2000, interval: float = 0.01) -> None:
    """等待条件成立（真实短间隔轮询；不用长等待，仅用于子进程时序同步）。"""

    for _ in range(ticks):
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("等待条件超时（跨进程验收设施）")


async def run_service_cli(
    *args: str,
    env_extra: Mapping[str, str] | None = None,
    timeout: float = 60.0,
) -> tuple[int, str, str]:
    """以真实子进程运行 `python -m service <args...>`，返回（退出码, stdout, stderr）。"""

    environment = dict(os.environ)
    environment.update(env_extra or {})
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "service",
        *args,
        cwd=ROOT,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    return (
        int(process.returncode),
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


def source_message(
    *,
    message_id: str,
    group_id: str = "10001",
    user_id: str = "34567",
    message: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """合成网关存储副本（与 `tests.service_support.group_source_message` 同形）。"""

    return {
        "message_type": "group",
        "message_id": message_id,
        "message_seq": 0,
        "group_id": group_id,
        "user_id": user_id,
        "sender": {"user_id": user_id},
        "message": [dict(segment) for segment in message],
    }
