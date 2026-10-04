"""2.5 服务认证与受控配置加载：凭证只从环境变量取、默认回环、受控字段不可覆盖。

判据来自 design D5/D10：

- 凭证只从**指定环境变量**取；缺失或错误凭证不能查询状态、不能准备发送；
- 默认监听回环地址；跨主机必须配置 TLS 证书/私钥（或运维提供的受控加密隧道）；
- 客户端不得跟随携带认证信息的重定向——服务端本身不产生任何 3xx；
- 账号、许可上限、状态目录只来自受控配置，不接受请求或模型覆盖。
"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest

from tests.service_support import ServiceHarness

TOKEN_ENV_VAR = "MC_TEST_SERVICE_TOKEN"
SERVICE_TOKEN = "s3cret-service-token"
ACCOUNT_ID = "10000"
SOURCE_GROUP_ID = "10001"
TARGET_GROUP_ID = "20002"

BASE_CONFIG: dict[str, Any] = {
    "client_identity": "maibot-plugin",
    "account_id": ACCOUNT_ID,
    "state_dir": "/tmp/maimai-courier",
    "ledger_id": "ledger-abc",
    "listen_host": "127.0.0.1",
    "listen_port": 8790,
    "token_env_var": TOKEN_ENV_VAR,
    "allowed_source_groups": [SOURCE_GROUP_ID],
    "allowed_target_groups": [TARGET_GROUP_ID],
}


def _write_config(tmp_path: Path, **overrides: Any) -> Path:
    values = dict(BASE_CONFIG)
    values.update(overrides)
    table = "\n".join(_toml_line(key, value) for key, value in values.items())
    path = tmp_path / "service.toml"
    path.write_text(f"[service]\n{table}\n", encoding="utf-8")
    return path


def _toml_line(key: str, value: Any) -> str:
    import json as _json

    if isinstance(value, str):
        return f'{key} = {_json.dumps(value, ensure_ascii=False)}'
    if isinstance(value, bool):
        return f"{key} = {'true' if value else 'false'}"
    if isinstance(value, list):
        inner = ", ".join(_json.dumps(item, ensure_ascii=False) for item in value)
        return f"{key} = [{inner}]"
    return f"{key} = {value}"


def _gate(environ: dict[str, str] | None = None) -> Any:
    from service.auth import EnvTokenAuthGate

    return EnvTokenAuthGate(
        token_env_var=TOKEN_ENV_VAR,
        subject="maibot-plugin",
        environ={TOKEN_ENV_VAR: SERVICE_TOKEN} if environ is None else environ,
    )


def _harness(tmp_path: Path, *, environ: dict[str, str] | None = None) -> ServiceHarness:
    from service.backends import NonSendingBackends
    from service.config import load_service_config

    loaded = load_service_config(_write_config(tmp_path))
    assert not hasattr(loaded, "reason"), loaded
    return ServiceHarness(backends=NonSendingBackends(account_id=loaded.account_id), auth=_gate(environ))


def test_配置模型不接受凭证明文且凭证只从环境变量取() -> None:
    from service.config import ServiceConfig, load_service_config

    loaded = load_service_config(_write_config(Path("/tmp")))
    assert not hasattr(loaded, "reason"), loaded
    assert loaded.token_env_var == TOKEN_ENV_VAR

    forbidden = {"token", "secret", "password", "token_value", "credential", "api_key"}
    assert not forbidden.intersection({field.name for field in fields(ServiceConfig)}), (
        "配置模型只能保存凭证的环境变量名，不能保存凭证值"
    )

    missing = _gate({})
    assert missing.credential_available is False
    assert missing.authenticate(_request()) is None, "环境变量未设置时不能认证任何请求"

    correct = _gate()
    assert correct.credential_available is True
    assert correct.authenticate(_request(token=SERVICE_TOKEN)) == "maibot-plugin"
    assert correct.authenticate(_request(token="wrong")) is None
    assert correct.authenticate(_request()) is None


def test_缺失或错误凭证不能查询状态或准备发送(tmp_path: Path) -> None:
    harness = _harness(tmp_path)

    prepare_body = {
        "protocol_version": 1,
        "request_id": "req-1",
        "session_id": "sess-1",
        "policy_generation": 1,
        "source_group_id": SOURCE_GROUP_ID,
        "source_message_id": "m-source-1",
        "target_group_id": TARGET_GROUP_ID,
    }
    for label, headers in [
        ("缺少凭证", {}),
        ("错误凭证", {"Authorization": "Bearer wrong"}),
        ("非 Bearer 凭证", {"Authorization": SERVICE_TOKEN}),
    ]:
        unauthorized = harness.call(
            "POST", "/v1/forwards/prepare", payload=prepare_body, headers=headers
        )
        assert unauthorized.status == 401, (label, unauthorized)
        assert unauthorized.payload["error"] == "unauthorized", (label, unauthorized.payload)

        query = harness.call("GET", "/v1/forwards/d-1", headers=headers)
        assert query.status == 401, (label, query)

    assert harness.backends.calls == [], "未认证请求不得抵达后端边界"

    authorized = harness.call(
        "POST",
        "/v1/forwards/prepare",
        payload=prepare_body,
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )
    assert authorized.status == 200, authorized
    assert [name for name, _ in harness.backends.calls] == ["prepare"]

    header_case = harness.call(
        "GET",
        "/v1/forwards/d-1",
        headers={"authorization": f"Bearer {SERVICE_TOKEN}"},
    )
    assert header_case.status == 200, "请求头名不区分大小写"


def test_默认回环监听与跨主机必须校验证书(tmp_path: Path) -> None:
    from service.config import ServiceConfig, load_service_config, validate_service_config

    default = ServiceConfig(
        account_id=ACCOUNT_ID,
        state_dir="/tmp/maimai-courier",
        client_identity="maibot-plugin",
    )
    assert default.listen_host == "127.0.0.1", "默认必须只监听回环地址"
    assert default.loopback_only is True
    assert validate_service_config(default) is None

    for host in ("localhost", "::1"):
        assert validate_service_config(
            ServiceConfig(
                account_id=ACCOUNT_ID,
                state_dir="/tmp/x",
                client_identity="c",
                listen_host=host,
            )
        ) is None, host

    remote_without_tls = ServiceConfig(
        account_id=ACCOUNT_ID,
        state_dir="/tmp/x",
        client_identity="c",
        listen_host="0.0.0.0",
    )
    rejected = validate_service_config(remote_without_tls)
    assert rejected is not None and ("TLS" in rejected.reason or "证书" in rejected.reason)

    with_tls = ServiceConfig(
        account_id=ACCOUNT_ID,
        state_dir="/tmp/x",
        client_identity="c",
        listen_host="0.0.0.0",
        tls_cert_file="/etc/ssl/service.crt",
        tls_key_file="/etc/ssl/service.key",
    )
    assert validate_service_config(with_tls) is None

    loaded = load_service_config(_write_config(tmp_path, listen_host="0.0.0.0"))
    assert hasattr(loaded, "reason"), "跨主机缺证书的配置必须直接判为不可用"


def test_账号与许可上限只来自受控配置(tmp_path: Path) -> None:
    from shared.protocol import DeliveryKey, delivery_id_for

    harness = _harness(tmp_path)

    body = {
        "protocol_version": 1,
        "request_id": "req-1",
        "session_id": "sess-1",
        "policy_generation": 1,
        "source_group_id": SOURCE_GROUP_ID,
        "source_message_id": "m-source-1",
        "target_group_id": TARGET_GROUP_ID,
    }
    authorized = harness.call(
        "POST",
        "/v1/forwards/prepare",
        payload=body,
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )
    expected = delivery_id_for(
        DeliveryKey(
            account_id=ACCOUNT_ID,
            source_group_id=SOURCE_GROUP_ID,
            source_message_id="m-source-1",
            target_group_id=TARGET_GROUP_ID,
        )
    )
    assert authorized.payload["result"]["delivery_id"] == expected, (
        "投递身份里的账号只能来自受控配置"
    )

    for field in ("account_id", "state_dir", "limits", "listen_host", "token_env_var"):
        overridden = harness.call(
            "POST",
            "/v1/forwards/prepare",
            payload={**body, field: "x"},
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )
        assert overridden.status == 400 and overridden.payload["error"] == "unknown_field", (
            field,
            overridden,
        )


def test_服务端不产生重定向因此认证头不会随重定向泄露(tmp_path: Path) -> None:
    from shared.protocol import ALLOW_REDIRECTS

    assert ALLOW_REDIRECTS is False, "客户端不得跟随重定向（认证头不随重定向转交）"

    harness = _harness(tmp_path)
    responses = [
        harness.call("GET", "/v1/forwards/d-1"),
        harness.call(
            "GET",
            "/v1/forwards/d-1",
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        ),
        harness.call("GET", "/v1/nope"),
        harness.call(
            "POST",
            "/v1/forwards/prepare",
            payload={"protocol_version": 1},
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        ),
    ]
    assert all(response.status not in (301, 302, 303, 307, 308) for response in responses)


def test_配置不可用时明确失败(tmp_path: Path) -> None:
    from service.config import load_service_config

    missing = load_service_config(tmp_path / "不存在.toml")
    assert hasattr(missing, "reason") and "配置" in missing.reason

    broken = tmp_path / "broken.toml"
    broken.write_text("[service\n", encoding="utf-8")
    assert "配置" in load_service_config(broken).reason

    empty = tmp_path / "empty.toml"
    empty.write_text("", encoding="utf-8")
    assert "配置" in load_service_config(empty).reason

    for label, overrides in [
        ("缺账号", {"account_id": ""}),
        ("缺状态目录", {"state_dir": ""}),
        ("缺客户端身份", {"client_identity": ""}),
        ("端口非法", {"listen_port": 70000}),
        ("凭证变量名非法", {"token_env_var": "BAD NAME"}),
        ("未知字段", {"unknown_key": "x"}),
        ("账号含禁止字符", {"account_id": "<10000>"}),
    ]:
        outcome = load_service_config(_write_config(tmp_path, **overrides))
        assert hasattr(outcome, "reason"), label
        assert outcome.reason, label


def test_配置缺少必填键时按配置不可用返回而不抛异常(tmp_path: Path) -> None:
    """缺 `account_id`／`state_dir`／`client_identity` 任一必填键都必须按配置错误处理。

    对照 `load_service_config` 的公开契约（「任何异常都转成 `ConfigUnavailable`」）与
    `python -m service start` 的失败面（`EXIT_CONFIG`）：缺键要给出可定位的中文原因，
    不得以 `TypeError` 栈回溯退出。
    """

    from service.cli import EXIT_CONFIG, main
    from service.config import ConfigUnavailable, load_service_config

    for missing_key in ("account_id", "state_dir", "client_identity"):
        values = {key: value for key, value in BASE_CONFIG.items() if key != missing_key}
        table = "\n".join(_toml_line(key, value) for key, value in values.items())
        path = tmp_path / f"missing-{missing_key}.toml"
        path.write_text(f"[service]\n{table}\n", encoding="utf-8")

        loaded = load_service_config(path)
        assert isinstance(loaded, ConfigUnavailable), (missing_key, loaded)
        assert missing_key in loaded.reason, (missing_key, loaded.reason)
        assert main(["start", "--config", str(path)]) == EXIT_CONFIG, (
            f"缺 {missing_key} 必须以配置失败退出（EXIT_CONFIG），不得异常泄漏"
        )


def test_启动检查环境变量凭证与受控配置(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from service.cli import EXIT_CONFIG, EXIT_UNAVAILABLE, main

    config_path = _write_config(tmp_path)

    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    missing_credential = main(["start", "--config", str(config_path)])
    assert missing_credential == EXIT_CONFIG, "凭证环境变量缺失时拒绝启动"

    monkeypatch.setenv(TOKEN_ENV_VAR, SERVICE_TOKEN)
    assert main(["start", "--config", str(config_path)]) == EXIT_UNAVAILABLE


def _request(token: str = "") -> Any:
    from service.http_api import HttpRequest

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return HttpRequest(method="GET", path="/v1/forwards/d-1", headers=headers)
