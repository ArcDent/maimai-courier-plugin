"""配置模型测试：字段面、默认值与 config.toml 一致性。"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from plugin import CourierPlugin

ROOT = Path(__file__).resolve().parent.parent

# design D1 排除的配置面：0.5 秒评论延迟与自动去重／等待上限是代码常量，
# 不提供随机概率、冷却或延迟配置项。
_FORBIDDEN_KEY_PARTS = ("delay", "probab", "cooldown", "random", "weight", "ttl")


def _all_keys(config: dict[str, Any], prefix: str = "") -> list[str]:
    """递归收集配置的全部字段名（嵌套 section 带父级前缀）。"""

    keys: list[str] = []
    for key, value in config.items():
        keys.append(f"{prefix}{key}")
        if isinstance(value, dict):
            keys.extend(_all_keys(value, prefix=f"{prefix}{key}."))
    return keys


def test_默认配置仅总开关自动入口白名单与宿主强制的配置版本() -> None:
    config = CourierPlugin.build_default_config()

    assert config["plugin"]["enabled"] is False, "默认总开关必须关闭"
    assert config["auto"]["enabled"] is True, "默认自动入口开启"
    assert config["scope"]["source_groups"] == []
    assert config["scope"]["target_groups"] == []
    assert set(config) == {"plugin", "auto", "scope"}
    # config_version 是宿主强制项：SDK extract_plugin_config_version 缺它即抛
    # PluginConfigVersionError，不是本插件的业务开关。
    assert set(config["plugin"]) == {"enabled", "config_version"}
    assert set(config["auto"]) == {"enabled"}
    assert set(config["scope"]) == {"source_groups", "target_groups"}


def test_默认配置不含延迟概率与冷却字段() -> None:
    keys = _all_keys(CourierPlugin.build_default_config())
    assert "plugin.enabled" in keys, "字段遍历必须真的读到配置字段"
    offenders = [key for key in keys if any(part in key.lower() for part in _FORBIDDEN_KEY_PARTS)]
    assert offenders == [], f"配置出现 design D1 排除的字段: {offenders}"


def test_config_toml_与配置模型默认值一致() -> None:
    parsed = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
    assert parsed == CourierPlugin.build_default_config()
