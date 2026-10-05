"""服务端 v1 HTTP 路由与参数校验骨架（design D4/D5）。

- 路由表就是 design D4 公布的九个接口；每个合法请求只做「认证 → 请求体与字段校验 →
  分发到后端边界」，本层**没有任何平台发送能力**；
- 认证在解析请求体之前完成：未认证的请求既拿不到校验细节，也到不了后端；
- 非法 JSON、未知字段（含任意动作/连接地址字段）、错误协议版本、非法路径标识符与
  超大请求体一律拒绝；未知路径与错误方法分别以 404 / 405 区分；
- 返回体只含安全错误类别与中文原因，不回显请求正文、URL 或凭证（design D5）。

真实的 aiohttp 传输适配与真实协调器由后续组接入；本层是纯 Python、可在内存里直接
驱动，测试无需网络也不真实等待。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Final, Mapping, Protocol
from urllib.parse import unquote

from shared.identifiers import verbatim_identifier
from shared.protocol import (
    ERROR_INVALID_ID,
    InvalidRequest,
    parse_cancel_request,
    parse_heartbeat_request,
    parse_policy_update_request,
    parse_prepare_request,
    parse_session_create,
    parse_stage_execution_request,
)

from .backends import ServiceBackends, ServiceFault

MAX_BODY_BYTES: Final[int] = 64 * 1024
"""请求体大小上限（design D5：请求体必须受限，超限不进入解析）。"""

# 服务端 HTTP 状态码的唯一声明处：路由层、会话注册表与协调器都从这里导入。
HTTP_OK: Final[int] = 200
HTTP_BAD_REQUEST: Final[int] = 400
HTTP_UNAUTHORIZED: Final[int] = 401
HTTP_FORBIDDEN: Final[int] = 403
HTTP_NOT_FOUND: Final[int] = 404
HTTP_METHOD_NOT_ALLOWED: Final[int] = 405
HTTP_CONFLICT: Final[int] = 409
HTTP_PAYLOAD_TOO_LARGE: Final[int] = 413
HTTP_TOO_MANY_REQUESTS: Final[int] = 429
HTTP_INTERNAL_ERROR: Final[int] = 500
HTTP_SERVICE_UNAVAILABLE: Final[int] = 503

ERROR_INVALID_JSON: Final[str] = "invalid_json"
"""请求体不是合法 JSON。"""

ERROR_BODY_TOO_LARGE: Final[str] = "body_too_large"
"""请求体超过大小上限。"""

ERROR_UNAUTHORIZED: Final[str] = "unauthorized"
"""缺少或无效的服务凭证。"""

ERROR_NOT_FOUND: Final[str] = "not_found"
"""未知接口路径。"""

ERROR_METHOD_NOT_ALLOWED: Final[str] = "method_not_allowed"
"""路径存在但不支持该方法。"""

ERROR_UNIMPLEMENTED_BACKEND: Final[str] = "unimplemented_backend"
"""路由存在但没登记后端适配器（服务端内部错误，绝不静默落到别的后端调用）。"""


@dataclass(frozen=True, slots=True)
class HttpRequest:
    """传输早已完成的请求：方法、路径、请求头与原始请求体。"""

    method: str
    path: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """待传输的响应：状态码 + JSON 可序列化载荷。"""

    status: int
    payload: Mapping[str, Any]


class AuthGate(Protocol):
    """认证门：返回认证主体标识；`None` 表示凭证缺失或错误（401）。

    真实实现（`service.auth`）只从指定环境变量取凭证并与请求头比对；本层不读凭证。
    """

    def authenticate(self, request: HttpRequest) -> str | None: ...


@dataclass(frozen=True, slots=True)
class _Route:
    """一条路由：方法 + 固定段/参数段模式 + 对应后端方法名。"""

    method: str
    pattern: tuple[str, ...]
    backend_method: str


_ROUTES: Final[tuple[_Route, ...]] = (
    _Route("POST", ("v1", "sessions"), "create_session"),
    _Route("POST", ("v1", "sessions", "{session_id}", "heartbeat"), "heartbeat"),
    _Route("PUT", ("v1", "sessions", "{session_id}", "policy"), "update_policy"),
    _Route("DELETE", ("v1", "sessions", "{session_id}"), "deregister"),
    _Route("POST", ("v1", "forwards", "prepare"), "prepare"),
    _Route("POST", ("v1", "forwards", "{delivery_id}", "source"), "execute_source"),
    _Route("POST", ("v1", "forwards", "{delivery_id}", "comment"), "execute_comment"),
    _Route("POST", ("v1", "forwards", "{delivery_id}", "cancel"), "cancel"),
    _Route("GET", ("v1", "forwards", "{delivery_id}"), "query"),
)
"""design D4 的 v1 接口清单（唯一路由来源，顺序无副作用）。"""

_Dispatcher = Callable[[ServiceBackends, Mapping[str, str], Any, str], Awaitable[HttpResponse]]
"""路由适配器：后端边界 + 路径参数 + 请求体 + 认证主体 → 响应。"""


def _dispatcher(
    backend_method: str,
    parse: Callable[[Any], Any] | None = None,
    *,
    parameter: str = "",
) -> _Dispatcher:
    """构造统一适配器：解析请求体（可选）→ 调用同名后端方法。

    同形分支由此不再逐字重复：心跳／策略更新与源／评论执行只差解析函数与路径参数名。
    适配器只按 `backend_method` 调用后端；没登记适配器的路由在分发处明确报错。
    """

    async def dispatch(
        backend: ServiceBackends,
        parameters: Mapping[str, str],
        payload: Any,
        subject: str,
    ) -> HttpResponse:
        parsed = payload
        if parse is not None:
            parsed = parse(payload)
            if isinstance(parsed, InvalidRequest):
                return _invalid(parsed)
        method = getattr(backend, backend_method)
        if parse is None:
            outcome = await method(parameters[parameter], subject=subject)  # 注销、查询
        elif parameter:
            # 心跳／策略更新／源执行／评论执行／撤销：请求体 + 一个路径参数
            outcome = await method(parameters[parameter], parsed, subject=subject)
        else:
            outcome = await method(parsed, subject=subject)  # 建立会话、准备：只有请求体
        return _backend(outcome)

    return dispatch


_DISPATCHERS: Final[Mapping[str, _Dispatcher]] = {
    "create_session": _dispatcher("create_session", parse_session_create),
    "heartbeat": _dispatcher("heartbeat", parse_heartbeat_request, parameter="session_id"),
    "update_policy": _dispatcher(
        "update_policy", parse_policy_update_request, parameter="session_id"
    ),
    "deregister": _dispatcher("deregister", parameter="session_id"),
    "prepare": _dispatcher("prepare", parse_prepare_request),
    "execute_source": _dispatcher(
        "execute_source", parse_stage_execution_request, parameter="delivery_id"
    ),
    "execute_comment": _dispatcher(
        "execute_comment", parse_stage_execution_request, parameter="delivery_id"
    ),
    "cancel": _dispatcher("cancel", parse_cancel_request, parameter="delivery_id"),
    "query": _dispatcher("query", parameter="delivery_id"),
}
"""后端方法名 → 适配器：九条路由的唯一分发来源（未登记的方法由 `_dispatch` 明确报错）。"""


class ServiceApp:
    """纯 Python 的 v1 应用：持有认证门与后端边界，不持有任何发送依赖。"""

    def __init__(self, *, backends: ServiceBackends, auth: AuthGate) -> None:
        self._backends = backends
        self._auth = auth

    async def handle(self, request: HttpRequest) -> HttpResponse:
        """处理一次请求：路由 → 认证 → 大小 → JSON → 字段 → 后端。"""

        segments = _segments(request.path)
        matched = _match(request.method, segments)
        if matched is None:
            if _path_exists(segments):
                return _error(
                    HTTP_METHOD_NOT_ALLOWED,
                    ERROR_METHOD_NOT_ALLOWED,
                    "该路径不支持此 HTTP 方法",
                )
            return _error(HTTP_NOT_FOUND, ERROR_NOT_FOUND, "未知接口路径")
        route, parameters = matched

        subject = self._auth.authenticate(request)
        if not subject:
            return _error(HTTP_UNAUTHORIZED, ERROR_UNAUTHORIZED, "缺少或无效的服务凭证")

        if len(request.body) > MAX_BODY_BYTES:
            return _error(HTTP_PAYLOAD_TOO_LARGE, ERROR_BODY_TOO_LARGE, "请求体超过大小上限")

        decoded = _decode_json(request.body)
        if isinstance(decoded, HttpResponse):
            return decoded

        for name, value in parameters.items():
            if not verbatim_identifier(value):
                return _error(HTTP_BAD_REQUEST, ERROR_INVALID_ID, f"路径参数 {name} 非法")
        return await self._dispatch(route, parameters, decoded, subject)

    async def _dispatch(
        self,
        route: _Route,
        parameters: Mapping[str, str],
        payload: Any,
        subject: str,
    ) -> HttpResponse:
        """把校验后的请求分发到后端边界；本方法不做发送。

        适配器只登记在 `_DISPATCHERS`；路由没有对应适配器时**显式报错**，不会静默
        落到别的后端调用（如 `query`）。
        """

        dispatcher = _DISPATCHERS.get(route.backend_method)
        if dispatcher is None:
            return _error(
                HTTP_INTERNAL_ERROR,
                ERROR_UNIMPLEMENTED_BACKEND,
                f"路由 {route.backend_method} 未登记后端适配器（服务端内部错误）",
            )
        return await dispatcher(self._backends, parameters, payload, subject)


def _segments(path: str) -> tuple[str, ...]:
    """拆分并解码路径段（传输层不负责的部分在此归一，空段忽略）。"""

    raw = path.split("?", 1)[0]
    return tuple(unquote(segment) for segment in raw.split("/") if segment)


def _match(method: str, segments: tuple[str, ...]) -> tuple[_Route, dict[str, str]] | None:
    """按方法与段数匹配路由；返回路由与路径参数。"""

    for route in _ROUTES:
        if route.method != method or len(route.pattern) != len(segments):
            continue
        parameters: dict[str, str] = {}
        for expected, actual in zip(route.pattern, segments):
            if expected.startswith("{") and expected.endswith("}"):
                parameters[expected[1:-1]] = actual
            elif expected != actual:
                break
        else:
            return route, parameters
    return None


def _path_exists(segments: tuple[str, ...]) -> bool:
    """该路径（字面量段一致、参数段任意）是否有任何方法的路由，用于区分 404/405。"""

    for route in _ROUTES:
        if len(route.pattern) != len(segments):
            continue
        if all(
            (expected.startswith("{") and expected.endswith("}")) or expected == actual
            for expected, actual in zip(route.pattern, segments)
        ):
            return True
    return False


def _decode_json(body: bytes) -> Any | HttpResponse:
    """解析请求体：空体按空对象处理；非法 JSON 直接 400。"""

    if not body:
        return {}
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _error(HTTP_BAD_REQUEST, ERROR_INVALID_JSON, "请求体不是合法 JSON")


def _error(status: int, error: str, reason: str) -> HttpResponse:
    """错误响应：只有类别与中文原因，不回显请求内容。"""

    return HttpResponse(status=status, payload={"ok": False, "error": error, "reason": reason})


def _invalid(invalid: InvalidRequest) -> HttpResponse:
    """参数校验失败：统一按 400 返回类别与原因。"""

    return _error(HTTP_BAD_REQUEST, invalid.error, invalid.reason)


def _backend(outcome: Any) -> HttpResponse:
    """后端结果 → 响应：`ServiceFault` 按自带状态码，其余按 200 序列化。"""

    if isinstance(outcome, ServiceFault):
        return _error(outcome.http_status, outcome.error, outcome.reason)
    return HttpResponse(status=HTTP_OK, payload={"ok": True, "result": outcome.as_payload()})
