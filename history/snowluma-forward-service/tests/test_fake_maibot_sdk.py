"""测试替身与可控时钟的契约测试（对应 tasks 2.3 验收面）。

断言的是替身自身的公共契约：真实 SDK 被拦截、Hook 枚举按真实 SDK 精确校验、
上下文经 `_set_context` 注入且 `ctx` 只读、消息就绪可控、详细发送回执可控、
在途发送可取消、可控时钟无需真实等待、存储故障可注入、合并转发夹具使用宿主
接收侧序列化键形状、配置字段声明保留元数据且按注解校验。生产行为不在这里断言
（各实现组按自己的 seam 写行为测试）。
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import time
from pathlib import Path
from typing import Any, ClassVar, Literal

import pytest

from tests import fake_maibot_sdk
from tests.fake_maibot_sdk import FakeMessageCapability, FakePluginContext, FakeSendCapability
from tests.support import (
    HOST_ACCOUNT_ID_KEYS,
    HOST_SCOPE_KEYS,
    FakeClock,
    merged_forward_message,
)
from auto_entry import ACCOUNT_ID_KEYS


def test_替身SDK_拦截真实导入() -> None:
    assert importlib.import_module("maibot_sdk") is fake_maibot_sdk
    assert sys.modules["maibot_sdk"] is fake_maibot_sdk


def test_Hook枚举按真实SDK精确校验() -> None:
    """校验口径 = 真实 SDK 的 pydantic 枚举字段：只接受枚举成员或精确值字符串。

    对照证据：pydantic 只接受枚举成员与精确的小写值字符串；`"OBSERVE"`、
    `" observe "`、`""`、`None` 均被拒绝（maibot-plugin-sdk types.py:433-441
    的 `HookHandlerComponentInfo` 字段即该口径）。
    """

    def apply(**kwargs: Any) -> Any:
        @fake_maibot_sdk.HookHandler("test.hook", **kwargs)
        async def handler(**_: Any) -> None:
            return None

        return handler

    ok = apply(mode="observe", order="late", error_policy="log")
    assert ok._component_info.metadata["mode"] == "observe"
    assert ok._component_info.metadata["order"] == "late"
    assert ok._component_info.metadata["error_policy"] == "log"

    for bad_mode in ("OBSERVE", " observe ", "", None):
        with pytest.raises(ValueError):
            apply(mode=bad_mode)
    with pytest.raises(ValueError):
        apply(order="EARLY")
    with pytest.raises(ValueError):
        apply(error_policy=None)


def test_替身上下文的注入契约与真实SDK一致(tmp_path: Path) -> None:
    """`ctx` 是只读 property、未注入即报错；注入入口是 `_set_context`（同真实 SDK）。"""

    plugin = fake_maibot_sdk.MaiBotPlugin()
    with pytest.raises(RuntimeError):
        _ = plugin.ctx
    with pytest.raises(AttributeError):
        plugin.ctx = FakePluginContext(tmp_path / "unused")  # type: ignore[misc]

    ctx = FakePluginContext(tmp_path / "data")
    plugin._set_context(ctx)
    assert plugin.ctx is ctx


def test_可控时钟无需真实等待即可推进超时() -> None:
    async def wait_forever(clock: FakeClock) -> None:
        await clock.sleep(5.0)

    async def scenario() -> None:
        clock = FakeClock()
        task = asyncio.create_task(wait_forever(clock))
        await asyncio.sleep(0)  # 让任务跑到 sleep 登记处
        assert clock.now() == 0.0
        assert not task.done(), "未推进时间前不得醒来"
        clock.advance(4.9)
        await asyncio.sleep(0)
        assert not task.done(), "未到期的等待不得提前醒来"
        clock.advance(0.1)
        await task
        assert clock.now() == 5.0

    started = time.monotonic()
    asyncio.run(scenario())
    assert time.monotonic() - started < 0.5, "可控时钟不得真实等待"


def test_消息就绪时序可控() -> None:
    async def scenario() -> None:
        message = FakeMessageCapability(
            sequences={"m-1": [None, {"message_id": "m-1", "raw_message": []}]}
        )
        assert await message.get_by_id("m-1", chat_id="10001") is None, "首次未就绪"
        ready = await message.get_by_id("m-1", chat_id="10001")
        assert ready["message_id"] == "m-1", "第二次应返回就绪消息"
        assert [call["message_id"] for call in message.calls] == ["m-1", "m-1"]

    asyncio.run(scenario())


def test_详细发送回执与失败注入可控() -> None:
    async def scenario() -> None:
        send = FakeSendCapability()
        ok = await send.forward([{"type": "forward"}], "s-1", return_details=True)
        assert ok == {"sent": True, "message_id": "forward-1"}, "详细回执取 SDK 归一后的形态"

        send.forward_result = {"sent": False, "message_id": None}
        failed = await send.forward([{"type": "forward"}], "s-1", return_details=True)
        assert failed["sent"] is False and failed["message_id"] is None
        assert send.calls[-1]["return_details"] is True

        # 队列按调用顺序逐个覆盖：源消息与评论同为 send.text 时分别注入回执
        send.text_results = [{"sent": True, "message_id": "src-1"}, {"sent": False, "message_id": None}]
        assert await send.text("源消息", "s-1", return_details=True) == {
            "sent": True,
            "message_id": "src-1",
        }
        assert await send.text("评论", "s-1", return_details=True) == {
            "sent": False,
            "message_id": None,
        }

        # 图片与混合通路形态对齐真实 SDK 代理（参数名 image_base64 / segments）
        assert await send.image("aW1n", "s-1", return_details=True) == {
            "sent": True,
            "message_id": "image-5",
        }
        assert send.calls[-1]["image_base64"] == "aW1n"
        assert await send.hybrid([{"type": "text", "data": "混合"}], "s-1", return_details=True) == {
            "sent": True,
            "message_id": "hybrid-6",
        }
        assert send.calls[-1]["segments"] == [{"type": "text", "data": "混合"}]

    asyncio.run(scenario())


def test_在途发送可取消() -> None:
    async def scenario() -> None:
        send = FakeSendCapability()
        send.hold = asyncio.Event()
        outcome: list[object] = []

        async def deliver() -> None:
            outcome.append(await send.text("评论", "s-1", return_details=True))

        task = asyncio.create_task(deliver())
        await asyncio.sleep(0)
        assert not task.done(), "发送应停在在途状态"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert outcome == [], "被取消的发送不得产生回执"

    asyncio.run(scenario())


def test_运行时目录与数据目录分属不同根(tmp_path: Path) -> None:
    """真实宿主把两个目录分配在独立根，替身不得把运行时目录放进数据目录。

    证据：`plugin_paths.py:52-58` 的 `build_plugin_paths` 用
    `project_root/data/plugins/<id>` 作持久数据目录、`project_root/temp/plugins/<id>`
    作非持久运行时目录，两者都不是对方的子目录。若替身把 `runtime_dir` 挂在
    `data_dir` 之下，依赖该边界的插件行为在测试里会得到与真实宿主不同的结论。
    """

    context = FakePluginContext(tmp_path / "data")
    data_dir = Path(context.paths.data_dir)
    runtime_dir = Path(context.paths.runtime_dir)

    assert data_dir.is_dir() and runtime_dir.is_dir(), "两个目录都必须真实可用"
    assert runtime_dir != data_dir, "运行时目录不得等同于数据目录"
    assert data_dir not in runtime_dir.parents, "运行时目录不得位于数据目录之下"
    assert runtime_dir not in data_dir.parents, "数据目录不得位于运行时目录之下"


def test_存储故障可注入(tmp_path: Path) -> None:
    """存储边界：默认按宿主方式分配真实目录；注入故障后路径不可用，真实 IO 抛 OSError。

    故障以真实文件系统状态表达（`data_dir` 被普通文件占据），与真实 SDK
    `PluginPaths.data_dir` 契约一致，不伪造 SDK 不存在的存储接口。
    """

    healthy = FakePluginContext(tmp_path / "data")
    assert Path(healthy.paths.data_dir).is_dir(), "默认应分配真实可写目录"

    broken = FakePluginContext(tmp_path / "blocked", storage_fault=True)
    data_dir = Path(broken.paths.data_dir)
    assert not data_dir.is_dir(), "注入故障后 data_dir 必须不可用"
    with pytest.raises(OSError):
        (data_dir / "ledger.sqlite3").write_bytes(b"")


def test_合并转发夹具使用接收侧序列化键形状() -> None:
    """夹具节点键 = 宿主接收侧序列化（不得混用 send.forward 入参别名）。

    对照证据：MaiBot `src/plugin_runtime/host/message_utils.py:210-222` 产出
    `user_id/user_nickname/user_cardname/message_id/content`，反向解析见
    `:304-324`；`nickname`/`segments` 只是 `send.forward` 的入参别名
    （`src/plugin_runtime/capabilities/core.py:484-508`）。
    """

    message = merged_forward_message()["message"]
    node = message["raw_message"][0]["data"][0]
    assert set(node) == {"user_id", "user_nickname", "user_cardname", "message_id", "content"}
    assert node["user_nickname"] == "群友"
    assert node["content"] == [{"type": "text", "data": "这是被转发的原文"}]
    assert "nickname" not in node and "segments" not in node


def test_入站消息夹具使用宿主真实形状() -> None:
    """入站载荷形状守卫：`is_notify` 在 message 顶层，账号/作用域用宿主别名键。

    对照证据：宿主 `message_utils.py:39-44` 的 `MessageInfoDict` 只有
    user_info/group_info/additional_config，`is_notify` 属于 `MessageDict` 顶层
    （`:48-58`，写入 `:440`、读回 `:546-548`）；`additional_config` 里的路由键由
    `route_key_factory.py:28-39` 定义（`platform_io_account_id` / `platform_io_scope`），
    入站消息由 `supervisor.py:1496-1501` 写入。
    """

    message = merged_forward_message(is_notify=True)["message"]

    assert message["is_notify"] is True, "is_notify 是 message 顶层键"
    assert "is_notify" not in message["message_info"], "message_info 不含 is_notify"
    assert set(message["message_info"]) == {"user_info", "group_info", "additional_config"}

    additional_config = message["message_info"]["additional_config"]
    assert set(additional_config) <= set(HOST_ACCOUNT_ID_KEYS) | set(HOST_SCOPE_KEYS)
    assert HOST_ACCOUNT_ID_KEYS[0] in additional_config, "账号必须能被宿主别名读到"
    assert HOST_SCOPE_KEYS[0] in additional_config, "作用域必须能被宿主别名读到"


def test_自动入口账号别名与宿主路由键工厂一致() -> None:
    """生产的账号候选键不得偏离宿主 `RouteKeyFactory.ACCOUNT_ID_KEYS`。"""

    assert tuple(ACCOUNT_ID_KEYS) == HOST_ACCOUNT_ID_KEYS


def test_配置字段替身保留声明元数据与默认值() -> None:
    """`Field()` 必须保留默认值与宿主 Schema 生成器读取的元数据，而不是丢弃后只回默认值。

    对照证据（tasks 1.3）：真实 SDK 的 `Field` 是 pydantic `Field`，宿主 Schema
    生成器只读 `json_schema_extra` 里的封闭键集合（label / hint / x-widget /
    rows / max_length / order 等），`max_length` 另有 pydantic 约束语义。
    """

    class Section(fake_maibot_sdk.PluginConfigBase):
        modes: list[Literal["text", "image", "forward"]] = fake_maibot_sdk.Field(
            default_factory=lambda: ["forward"],
            description="候选类型。",
            json_schema_extra={"label": "候选类型", "hint": "默认仅合并转发", "order": 1},
        )
        note: str = fake_maibot_sdk.Field(
            default="",
            max_length=8,
            json_schema_extra={"x-widget": "textarea", "rows": 4, "max_length": 8},
        )

    assert Section.modes.description == "候选类型。"
    assert Section.modes.json_schema_extra == {
        "label": "候选类型",
        "hint": "默认仅合并转发",
        "order": 1,
    }
    assert Section.note.max_length == 8
    assert Section.note.json_schema_extra == {"x-widget": "textarea", "rows": 4, "max_length": 8}
    assert Section().model_dump() == {"modes": ["forward"], "note": ""}


def test_配置枚举列表逐项校验且拒绝未知值() -> None:
    """`list[Literal[...]]` 逐项校验：空列表与合法组合通过，未知值明确报错而非放宽成全选。"""

    class Section(fake_maibot_sdk.PluginConfigBase):
        modes: list[Literal["text", "image", "forward"]] = fake_maibot_sdk.Field(
            default_factory=lambda: ["forward"]
        )

    assert Section(modes=[]).modes == [], "空列表是合法的候选集合"
    assert Section(modes=["text"]).modes == ["text"]
    assert Section(modes=["text", "forward"]).modes == ["text", "forward"]
    assert Section().modes == ["forward"], "缺省回落默认工厂"

    for invalid in (["video"], ["text", "video"], ["TEXT"], [""], "text", None):
        with pytest.raises(ValueError):
            Section(modes=invalid)


def test_配置字符串长度上限按字符校验且不改写内容() -> None:
    """字符串长度按 `Field(max_length=…)` 强制；合法内容（含换行）不得被压平或截断。"""

    class Section(fake_maibot_sdk.PluginConfigBase):
        short: str = fake_maibot_sdk.Field(default="", max_length=5)
        note: str = fake_maibot_sdk.Field(default="")

    assert Section(short="12345").short == "12345"
    with pytest.raises(ValueError):
        Section(short="123456")
    with pytest.raises(ValueError):
        Section(short=None)

    value = "  第一行\n\t第二行  "
    assert Section(note=value).note == value


def test_配置实例间默认值相互隔离() -> None:
    """默认工厂每次实例化都应产出独立副本，实例之间的默认值不得互相污染。"""

    class Section(fake_maibot_sdk.PluginConfigBase):
        items: list[str] = fake_maibot_sdk.Field(default_factory=list)

    first, second = Section(), Section()
    first.items.append("x")
    assert second.items == []
    assert Section().items == []


class _DemoInner(fake_maibot_sdk.PluginConfigBase):
    """替身自测用的嵌套 section（模块级，供 `get_type_hints` 解析注解）。"""

    enabled: bool = fake_maibot_sdk.Field(default=False)
    names: list[str] = fake_maibot_sdk.Field(default_factory=list)


class _DemoOuter(fake_maibot_sdk.PluginConfigBase):
    """替身自测用的外层配置（含嵌套 section 与字符串版本字段）。"""

    inner: _DemoInner = fake_maibot_sdk.Field(default_factory=_DemoInner)
    version: str = fake_maibot_sdk.Field(default="1.0.0")


def test_配置嵌套覆盖与未知键忽略不退化() -> None:
    """嵌套 section 的 dict 覆盖要转成对应实例，未知键按 `extra="ignore"` 忽略。

    两个演示模型放在模块级：`get_type_hints` 只解析模块全局命名空间里的注解，
    与真实 pydantic 对模块级配置模型的解析口径一致。

    类型不符的值必须报错。这里用 `"maybe"` 而非 `"yes"`：真实 pydantic 的宽松
    模式会把 `"yes"` 强转为 `True`，替身有意只接受精确 bool（见
    `fake_maibot_sdk._validate_config_value`），`"maybe"` 在两者都报错。
    """

    config = _DemoOuter(inner={"enabled": True, "unknown": 1}, unknown=2)
    assert isinstance(config.inner, _DemoInner)
    assert config.inner.enabled is True
    assert config.inner.names == []
    assert config.model_dump() == {
        "inner": {"enabled": True, "names": []},
        "version": "1.0.0",
    }
    with pytest.raises(ValueError):
        _DemoOuter(inner={"enabled": "maybe"})


def test_配置替身跳过ClassVar并保持默认配置导出() -> None:
    """`ClassVar` 注解（WebUI 标签）不算配置字段；`build_default_config()` 仍可用。"""

    class Section(fake_maibot_sdk.PluginConfigBase):
        __ui_label__: ClassVar[str] = "演示"
        value: int = fake_maibot_sdk.Field(default=1)

    assert Section().model_dump() == {"value": 1}

    class FakePlugin(fake_maibot_sdk.MaiBotPlugin):
        config_model = Section

    assert FakePlugin.build_default_config() == {"value": 1}
