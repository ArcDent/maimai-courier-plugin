"""6.1 客户端服务配置与迁移绑定检查（design D2/D7/D10）。

覆盖：新增 `[service]` 配置字段与默认安全值、旧配置补字段不覆盖原值、本地
（不联网）绑定检查的六类拒绝、`on_load` 的就绪告警，以及「拒绝时不构造任何服务
请求、也不走旧发送通道」。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from plugin import CourierPlugin
from remote_client import (
    SERVICE_BINDING_RECEIPT_FILE_NAME,
    RemoteUnavailable,
    ServiceBinding,
    check_service_binding,
)

from tests.support import (
    SOURCE_GROUP_ID,
    SOURCE_SESSION_ID,
    TARGET_GROUP_ID,
    make_plugin,
    prepare_source,
    run_bound_forward,
)

RECEIPT_NAME = SERVICE_BINDING_RECEIPT_FILE_NAME
EXPECTED_LEDGER_ID = "ledger-identity-1"
MIGRATION_CONFIRMATION = "migrate-2026-10-02"
TOKEN_ENV_VAR = "COURIER_SERVICE_TOKEN"
SERVICE_TOKEN = "synthetic-service-token"


def _service_config(
    *,
    expected_ledger_id: str = EXPECTED_LEDGER_ID,
    migration_confirmation: str = MIGRATION_CONFIRMATION,
    endpoint: str = "http://127.0.0.1:8787",
    token_env_var: str = TOKEN_ENV_VAR,
) -> dict[str, Any]:
    """把启用状态的插件配置补上 `[service]` 绑定字段（其余走模型默认）。"""

    return {
        "plugin": {"enabled": True},
        "scope": {"source_groups": [SOURCE_GROUP_ID], "target_groups": [TARGET_GROUP_ID]},
        "service": {
            "endpoint": endpoint,
            "token_env_var": token_env_var,
            "expected_ledger_id": expected_ledger_id,
            "migration_confirmation": migration_confirmation,
        },
    }


def _binding(**overrides: Any) -> ServiceBinding:
    values: dict[str, Any] = {
        "endpoint": "http://127.0.0.1:8787",
        "client_identity": "maibot-plugin",
        "token_env_var": TOKEN_ENV_VAR,
        "binding": ServiceBinding(
            expected_ledger_id=EXPECTED_LEDGER_ID,
            migration_confirmation=MIGRATION_CONFIRMATION,
        ),
        "receipt_path": Path("/nonexistent/service_binding.json"),
        "environ": {TOKEN_ENV_VAR: SERVICE_TOKEN},
    }
    values.update(overrides)
    return check_service_binding(**values)


def test_默认配置新增服务字段且默认安全() -> None:
    """新增 `[service]` 字段面：总开关仍关闭、白名单为空、候选默认仅卡片。"""

    config = CourierPlugin.build_default_config()

    assert set(config) == {"plugin", "auto", "scope", "service"}
    assert config["plugin"]["enabled"] is False, "新增字段不得改变默认关闭"
    assert config["scope"]["source_groups"] == []
    assert config["scope"]["target_groups"] == []
    assert config["auto"]["message_types"] == ["forward"], "默认候选仍仅为合并转发"
    assert set(config["service"]) == {
        "endpoint",
        "client_identity",
        "token_env_var",
        "expected_ledger_id",
        "migration_confirmation",
    }
    assert config["service"]["endpoint"] == "http://127.0.0.1:8787", "默认只连本机回环"
    assert config["service"]["token_env_var"] == TOKEN_ENV_VAR
    assert config["service"]["expected_ledger_id"] == "", "未绑定数据集时禁止发送"
    assert config["service"]["migration_confirmation"] == "", "未确认迁移时禁止发送"
    # 服务节不得出现任何凭证明文字段：只保存环境变量名。
    for key in config["service"]:
        assert "token" not in key or key == "token_env_var", key


def test_旧配置缺少服务字段时补默认不覆盖原值() -> None:
    """旧配置加载：新字段补默认，旧的开关、白名单与版本原样保留。"""

    plugin = CourierPlugin()
    plugin.set_plugin_config(
        {
            "plugin": {"enabled": True, "config_version": "1.0.0"},
            "auto": {"enabled": False},
            "scope": {"source_groups": ["111111"], "target_groups": ["222222"]},
        }
    )

    config = plugin.config
    assert config.plugin.enabled is True and config.plugin.config_version == "1.0.0"
    assert config.auto.enabled is False
    assert config.scope.source_groups == ["111111"] and config.scope.target_groups == ["222222"]
    assert config.service.expected_ledger_id == "" and config.service.migration_confirmation == ""
    assert config.service.endpoint == "http://127.0.0.1:8787"


def test_静态绑定检查逐项拒绝() -> None:
    """地址、变量名、凭证、预期账本、迁移确认与收据六类不符都返回可定位原因。"""

    assert _binding() is None, "完整配置必须通过静态检查"

    missing_credential = _binding(environ={})
    assert isinstance(missing_credential, RemoteUnavailable)
    assert TOKEN_ENV_VAR in missing_credential.reason and SERVICE_TOKEN not in missing_credential.reason

    cases = {
        "地址": _binding(endpoint="ftp://127.0.0.1:1"),
        "远程明文": _binding(endpoint="http://10.0.0.9:8787"),
        "变量名": _binding(token_env_var="9BAD-NAME"),
        "预期账本": _binding(
            binding=ServiceBinding(expected_ledger_id="", migration_confirmation=MIGRATION_CONFIRMATION)
        ),
        "迁移确认": _binding(
            binding=ServiceBinding(expected_ledger_id=EXPECTED_LEDGER_ID, migration_confirmation="")
        ),
    }
    for label, outcome in cases.items():
        assert isinstance(outcome, RemoteUnavailable), label
        assert outcome.reason


def test_迁移收据与配置不一致或损坏时拒绝(tmp_path: Path) -> None:
    """收据记录的账本/迁移标识与配置不符 → 拒绝；损坏收据不得当作未绑定。"""

    receipt_path = tmp_path / RECEIPT_NAME
    receipt_path.write_text(
        json.dumps(
            {
                "ledger_identity": "another-ledger",
                "migration_confirmation": MIGRATION_CONFIRMATION,
            }
        ),
        encoding="utf-8",
    )
    mismatch = _binding(receipt_path=receipt_path)
    assert isinstance(mismatch, RemoteUnavailable)
    assert "不一致" in mismatch.reason

    receipt_path.write_text("{not json", encoding="utf-8")
    broken = _binding(receipt_path=receipt_path)
    assert isinstance(broken, RemoteUnavailable)
    assert "收据" in broken.reason

    receipt_path.write_text(
        json.dumps(
            {
                "ledger_identity": EXPECTED_LEDGER_ID,
                "migration_confirmation": MIGRATION_CONFIRMATION,
            }
        ),
        encoding="utf-8",
    )
    assert _binding(receipt_path=receipt_path) is None, "一致的收据必须通过"


def test_未绑定服务时工具拒绝且不构造服务请求(tmp_path: Path) -> None:
    """缺凭证时不建立任何服务请求（也不加载网络库），并零旧发送通道调用。"""

    constructed: list[str] = []

    def transport_factory(section: Any) -> Any:
        constructed.append(str(getattr(section, "endpoint", "")))
        raise AssertionError("静态检查失败时不得构造任何传输")

    plugin, ctx = make_plugin(
        tmp_path,
        config=_service_config(),
        transport_factory=transport_factory,
        environ={},
    )
    prepare_source(ctx)

    result, _arguments = run_bound_forward(plugin, clock=None)

    assert result["success"] is False
    assert result["status"] == "rejected"
    assert TOKEN_ENV_VAR in result["reason"] and SERVICE_TOKEN not in result["reason"]
    assert constructed == [], "缺凭证时不得构造任何传输"
    assert ctx.send.calls == [], "拒绝时不得调用任何旧发送通道"


def test_未配置预期账本时工具拒绝(tmp_path: Path) -> None:
    plugin, ctx = make_plugin(
        tmp_path,
        config=_service_config(expected_ledger_id="", migration_confirmation=""),
        environ={TOKEN_ENV_VAR: SERVICE_TOKEN},
    )
    prepare_source(ctx)

    result, _arguments = run_bound_forward(plugin)

    assert result["success"] is False and result["status"] == "rejected"
    assert "预期账本" in result["reason"]
    assert ctx.send.calls == []


def test_收据不一致时工具拒绝(tmp_path: Path) -> None:
    plugin, ctx = make_plugin(
        tmp_path,
        config=_service_config(),
        environ={TOKEN_ENV_VAR: SERVICE_TOKEN},
    )
    (ctx.paths.data_dir / RECEIPT_NAME).write_text(
        json.dumps(
            {
                "ledger_identity": "another-ledger",
                "migration_confirmation": MIGRATION_CONFIRMATION,
            }
        ),
        encoding="utf-8",
    )
    prepare_source(ctx)

    result, _arguments = run_bound_forward(plugin)

    assert result["success"] is False and result["status"] == "rejected"
    assert "收据" in result["reason"]
    assert ctx.send.calls == []


def test_on_load_未就绪时如实告警且不联网(tmp_path: Path) -> None:
    """on_load 只做本地绑定检查：未配置时告警一次，不建立会话/传输。"""

    constructed: list[str] = []

    def transport_factory(section: Any) -> Any:
        constructed.append(str(getattr(section, "endpoint", "")))
        return None

    plugin, ctx = make_plugin(
        tmp_path,
        config=_service_config(expected_ledger_id="", migration_confirmation=""),
        transport_factory=transport_factory,
        environ={TOKEN_ENV_VAR: SERVICE_TOKEN},
    )

    asyncio.run(plugin.on_load())

    assert constructed == [], "加载时不得构造传输"
    warnings = "\n".join(ctx.logger.texts("warning"))
    assert "预期账本" in warnings, warnings
    assert SERVICE_TOKEN not in warnings


def test_on_load_已配置时记录绑定就绪(tmp_path: Path) -> None:
    plugin, ctx = make_plugin(
        tmp_path,
        config=_service_config(),
        environ={TOKEN_ENV_VAR: SERVICE_TOKEN},
    )

    asyncio.run(plugin.on_load())

    infos = "\n".join(ctx.logger.texts("info"))
    assert EXPECTED_LEDGER_ID in infos
    assert SERVICE_TOKEN not in infos


def test_凭证值不写入本地迁移收据(tmp_path: Path) -> None:
    """收据只含账本身份与迁移确认标识：任何凭证字节都不得落盘。"""

    from remote_client import ServiceReceipt, write_service_receipt

    receipt_path = tmp_path / RECEIPT_NAME
    assert write_service_receipt(
        receipt_path,
        ServiceReceipt(
            ledger_identity=EXPECTED_LEDGER_ID,
            migration_confirmation=MIGRATION_CONFIRMATION,
        ),
    ) is None
    raw = receipt_path.read_text(encoding="utf-8")
    assert SERVICE_TOKEN not in raw
    assert json.loads(raw) == {
        "ledger_identity": EXPECTED_LEDGER_ID,
        "migration_confirmation": MIGRATION_CONFIRMATION,
    }
