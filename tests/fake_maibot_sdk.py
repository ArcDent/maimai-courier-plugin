"""Fake `maibot_sdk`：本机不安装真实 SDK 时供测试导入的替身。

`conftest.py` 在导入插件前把本模块装进 `sys.modules["maibot_sdk"]`，使生产代码的
`import maibot_sdk` 命中替身。组件声明与装饰器元数据形态对齐
maibot-plugin-sdk@4d8e577d409fc7886e4a9f19dd94bea829081e4f：

- `collect_components` 遍历实例属性、产出 `{"name", "type", "metadata"}`，
  `type` 取 `ComponentType` 的枚举值（`TOOL` / `HOOK_HANDLER`）；
- `Tool` 的额外关键字（如 `chat_scope`）原样进入 metadata；
- `HookHandler` 的 `hook/mode/order/timeout_ms/error_policy` 进入 metadata，
  枚举字段按真实 SDK 的 pydantic 口径精确校验（装饰函数时执行）；
- `ctx` 是只读 property、经 `_set_context()` 注入，`config` 同为只读 property。
"""

from __future__ import annotations

import copy
import enum
from pathlib import Path
from typing import Any, ClassVar, Literal, get_args, get_origin, get_type_hints

CONFIG_RELOAD_SCOPE_SELF = "self"
"""插件自身配置热重载的 scope 值（对齐真实 SDK）。"""

SDK_ROOT_EXPORTS = frozenset(
    {
        "MaiBotPlugin",
        "API",
        "Action",
        "Command",
        "Tool",
        "EventHandler",
        "HookHandler",
        "HomeCard",
        "LLMProvider",
        "LLMProviderBase",
        "MessageGateway",
        "WorkflowStep",
        "PluginConfigBase",
        "Field",
        "PluginContext",
        "PluginPaths",
        "SendResult",
        "CONFIG_RELOAD_SCOPE_SELF",
        "ON_BOT_CONFIG_RELOAD",
        "ON_MODEL_CONFIG_RELOAD",
    }
)
"""真实 SDK 根 `__all__`（maibot-plugin-sdk@4d8e577d409fc7886e4a9f19dd94bea829081e4f）。

生产代码只能从这里导入：`HookMode` / `HookOrder` / `ErrorPolicy` 只存在于
`maibot_sdk.types`，不在根导出面内，插件用字符串字面量声明 hook 模式。
"""


class _FakeFieldInfo:
    """字段声明替身（对齐真实 SDK 里 pydantic `Field()` 返回的 `FieldInfo` 常用面）。

    只保留本仓库配置声明与测试实际消费的只读信息：默认值／默认工厂、
    `description`、`json_schema_extra`（宿主 Schema 生成器读取的封闭元数据键集合）
    与 pydantic 字符串约束 `max_length`；不复制 pydantic 的完整 `FieldInfo`。
    `max_length` 是唯一的约束面：只写 `json_schema_extra["max_length"]` 不会拦下
    超长输入，真实宿主同样由 pydantic 约束。
    """

    __slots__ = ("default", "default_factory", "description", "json_schema_extra", "max_length")

    def __init__(
        self,
        default: Any = None,
        *,
        default_factory: Any = None,
        description: str = "",
        json_schema_extra: Any = None,
        max_length: int | None = None,
    ) -> None:
        self.default = default
        self.default_factory = default_factory
        self.description = description
        self.json_schema_extra = dict(json_schema_extra or {})
        self.max_length = max_length

    def get_default(self) -> Any:
        """取本次实例化应使用的默认值：默认工厂每次调用都产出新对象。"""

        if self.default_factory is not None:
            return self.default_factory()
        return copy.deepcopy(self.default)


def Field(
    default: Any = None,
    *,
    default_factory: Any = None,
    description: str = "",
    json_schema_extra: Any = None,
    max_length: int | None = None,
    **metadata: Any,
) -> Any:
    """字段声明替身（对齐真实 SDK 里 pydantic `Field` 的常用参数）。

    `json_schema_extra` 承载宿主 Schema 生成器读取的 UI 元数据（label / hint /
    x-widget / rows / order 等），`max_length` 承载 pydantic 字符串长度约束。
    未识别的关键字参数会被忽略：UI 元数据必须写进 `json_schema_extra`，宿主
    Schema 生成器只读该处，替身不发明宽松别名。`default_factory` 延迟到实例化时
    才求值，`PluginConfigBase` 据此保证每次实例化都拿到独立副本。

    差异说明：真实 pydantic v2 会把未识别的关键字参数并入 `json_schema_extra`
    （并给出弃用警告），替身不复制这一行为、一律忽略；因此用未识别关键字声明的
    元数据在替身里不生效，测试不得依赖这种写法。
    """

    del metadata
    return _FakeFieldInfo(
        default,
        default_factory=default_factory,
        description=description,
        json_schema_extra=json_schema_extra,
        max_length=max_length,
    )


class PluginConfigBase:
    """配置模型替身：按类注解逐字段取默认值，支持嵌套 section 与构造期校验。

    `model_dump()` 产出与真实 SDK（pydantic `model_dump(mode="python")`）同形的
    嵌套字典；构造时把 dict 覆盖值转成对应 section 实例，缺省字段回落默认值。
    `ClassVar` 注解（如 WebUI 用的 `__ui_label__`）按类属性处理，不算配置字段。

    校验口径对齐真实 SDK 的 pydantic 配置模型：`Literal` 与 `list[Literal]` 精确
    匹配枚举成员（未知值明确报错，不解释成全选）、字符串按 `Field(max_length=…)`
    限长且不改写内容、未知键按 `extra="ignore"` 忽略、嵌套 section 递归构造。
    校验失败抛 `ValueError`（真实 pydantic 的 `ValidationError` 是 `ValueError`
    子类）。替身不复制完整 pydantic，只覆盖本仓库配置声明实际使用的注解形态；
    有意不实现 pydantic 的宽松输入归一（如 `"yes"` → `True`、`"5"` → `5`、tuple
    收作 `list`），差异只可能误拒、不会误放。
    """

    def __init__(self, **overrides: Any) -> None:
        for key, annotation in _config_fields(type(self)).items():
            field = getattr(type(self), key, None)
            raw = overrides[key] if key in overrides else _field_default(field)
            setattr(
                self,
                key,
                _validate_config_value(
                    annotation, raw, key, max_length=_field_max_length(field)
                ),
            )

    def model_dump(self) -> dict[str, Any]:
        """导出嵌套字典（对齐真实 SDK 的 `model_dump(mode="python")`）。"""

        dumped: dict[str, Any] = {}
        for key in _config_field_names(type(self)):
            value = getattr(self, key)
            dumped[key] = value.model_dump() if isinstance(value, PluginConfigBase) else value
        return dumped


def _resolved_annotations(cls: type) -> dict[str, Any]:
    """已解析的类注解；`get_type_hints` 失败时回退原始注解（含字符串形态）。"""

    raw_annotations = dict(getattr(cls, "__annotations__", {}))
    try:
        resolved_annotations = get_type_hints(cls)
    except Exception:
        resolved_annotations = {}
    return {
        name: resolved_annotations.get(name, annotation)
        for name, annotation in raw_annotations.items()
    }


def _config_fields(cls: type) -> dict[str, Any]:
    """配置字段名 → 注解：跳过 `ClassVar` 注解（对齐 pydantic 把 ClassVar 当类属性）。"""

    return {
        name: annotation
        for name, annotation in _resolved_annotations(cls).items()
        if get_origin(annotation) is not ClassVar
    }


def _config_field_names(cls: type) -> list[str]:
    """配置字段名（保持声明顺序）。"""

    return list(_config_fields(cls))


def _field_default(field: Any) -> Any:
    """取字段声明的默认值：`Field()` 声明走其默认工厂，裸默认值直接深拷贝。"""

    if isinstance(field, _FakeFieldInfo):
        return field.get_default()
    return copy.deepcopy(field)


def _field_max_length(field: Any) -> int | None:
    """取字段声明的字符串长度上限（仅 `Field(max_length=…)` 参与校验）。"""

    return field.max_length if isinstance(field, _FakeFieldInfo) else None


def _describe_choices(choices: tuple[Any, ...]) -> str:
    """拼出 pydantic 风格的候选措辞（如 `'text', 'image' or 'forward'`）。"""

    rendered = [repr(choice) for choice in choices]
    if len(rendered) == 1:
        return rendered[0]
    return ", ".join(rendered[:-1]) + f" or {rendered[-1]}"


def _validate_config_value(
    annotation: Any,
    value: Any,
    field_name: str,
    *,
    max_length: int | None = None,
) -> Any:
    """按注解校验配置值，返回可安全落进实例的值（列表产出新对象，隔离调用方）。

    只覆盖本仓库配置声明实际使用的形态：`Literal`、`list[X]`、嵌套
    `PluginConfigBase`、`str`（含 `max_length`）、`bool` 与 `int`；其余注解不设
    约束（替身不做 pydantic 的完整类型系统）。报错路径与真实 SDK 一致（抛
    `ValueError`），有意不实现 pydantic 的宽松输入归一（如 `"yes"` → `True`、
    `"5"` → `5`、tuple 收作 `list`）：差异只可能误拒、不会误放。
    """

    if annotation is None:
        return value
    origin = get_origin(annotation)
    if origin is Literal:
        choices = get_args(annotation)
        if value not in choices:
            raise ValueError(f"{field_name}: Input should be {_describe_choices(choices)}")
        return value
    if origin is list:
        if not isinstance(value, list):
            raise ValueError(f"{field_name}: Input should be a valid list")
        (item_annotation,) = get_args(annotation)
        return [_validate_config_value(item_annotation, item, field_name) for item in value]
    if isinstance(annotation, type) and issubclass(annotation, PluginConfigBase):
        if isinstance(value, annotation):
            return value
        if isinstance(value, dict):
            return annotation(**value)
        raise ValueError(f"{field_name}: Input should be a valid {annotation.__name__}")
    if annotation is str:
        if not isinstance(value, str):
            raise ValueError(f"{field_name}: Input should be a valid string")
        if max_length is not None and len(value) > max_length:
            raise ValueError(f"{field_name}: String should have at most {max_length} characters")
        return value
    if annotation is bool:
        if not isinstance(value, bool):
            raise ValueError(f"{field_name}: Input should be a valid boolean")
        return value
    if annotation is int:
        # bool 是 int 的子类，显式拒绝，避免 True 被当成 1 存进整型字段。
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{field_name}: Input should be a valid int")
        return value
    return value


class _HookMode(str, enum.Enum):
    """Hook 处理模式（对齐真实 SDK `maibot_sdk.types.HookMode`，不在根导出面）。"""

    BLOCKING = "blocking"
    OBSERVE = "observe"


class _HookOrder(str, enum.Enum):
    """Hook 顺序槽位（对齐真实 SDK `maibot_sdk.types.HookOrder`）。"""

    EARLY = "early"
    NORMAL = "normal"
    LATE = "late"


class _ErrorPolicy(str, enum.Enum):
    """Hook 异常处理策略（对齐真实 SDK `maibot_sdk.types.ErrorPolicy`）。"""

    ABORT = "abort"
    SKIP = "skip"
    LOG = "log"


def _enum_member(enum_type: type[enum.Enum], raw_value: Any, field_name: str) -> enum.Enum:
    """取枚举成员（对齐真实 SDK 的 pydantic 枚举字段口径）。

    只接受该枚举的成员或精确的值字符串（如 `"observe"`）；大写、带空白、空串、
    `None` 与未知值一律抛 `ValueError`。对照证据：pydantic 对 `HookMode` 这类
    `str, Enum` 字段接受成员与精确值字符串，拒绝其余写法。
    """

    if isinstance(raw_value, enum_type):
        return raw_value
    if isinstance(raw_value, str):
        try:
            return enum_type(raw_value)
        except ValueError:
            pass
    raise ValueError(f"{field_name} 不合法: {raw_value!r}")


class _FakeComponentInfo:
    """组件声明元数据（对齐真实 SDK `ComponentInfo` 子类共同字段）。"""

    __slots__ = ("metadata", "name", "type")

    def __init__(self, name: str, component_type: str, metadata: dict[str, Any]) -> None:
        self.name = str(name)
        self.type = component_type
        self.metadata = dict(metadata)


def _attach_component(func: Any, name: str, component_type: str, metadata: dict[str, Any]) -> Any:
    """把组件声明附着到函数（真实 SDK 用 `_COMPONENT_INFO_ATTR` 标记）。"""

    func._component_info = _FakeComponentInfo(name, component_type, metadata)
    return func


class MaiBotPlugin:
    """SDK 插件基类替身：只实现本插件消费的公开面。

    `ctx` 与 `config` 都是**只读 property**：上下文一律经 `_set_context()` 注入
    （Runner 的同款入口），配置经 `set_plugin_config()` 注入，与真实 SDK 一致。
    """

    config_model: Any = None

    def __init__(self) -> None:
        self._ctx: Any = None
        self._plugin_config_instance: Any = None

    @property
    def ctx(self) -> Any:
        """插件运行时上下文（只读）；尚未注入时与真实 SDK 同样抛 `RuntimeError`。"""

        if self._ctx is None:
            raise RuntimeError("插件上下文尚未初始化，确保在 Runner 环境中运行")
        return self._ctx

    def _set_context(self, ctx: Any) -> None:
        """注入上下文对象（对齐真实 SDK 的 Runner 注入入口）。"""

        self._ctx = ctx

    @property
    def config(self) -> Any:
        """强类型配置实例（只读）；尚未注入时与真实 SDK 同样抛 `RuntimeError`。"""

        if self._plugin_config_instance is None:
            raise RuntimeError("当前插件配置尚未完成注入")
        return self._plugin_config_instance

    @classmethod
    def build_default_config(cls) -> dict[str, Any]:
        """按配置模型导出默认配置（对齐真实 SDK 的同名 classmethod）。"""

        model = cls.config_model
        return model().model_dump() if model is not None else {}

    def set_plugin_config(self, config: dict[str, Any]) -> None:
        """注入配置（对齐 Runner 的注入方式）：缺省字段回落模型默认值。"""

        model = type(self).config_model
        if model is None:
            return
        self._plugin_config_instance = model(**dict(config or {}))

    def get_components(self) -> list[dict[str, Any]]:
        """收集组件声明（对齐真实 SDK `collect_components` 的产出形态）。

        与真实 SDK 一致，取属性失败时跳过：未注入的 `ctx` / `config` 是只读
        property，读取即抛 `RuntimeError`，不得让组件收集因此中断。
        """

        components: list[dict[str, Any]] = []
        for attr_name in dir(self):
            try:
                attr = getattr(self, attr_name)
            except Exception:
                continue
            info = getattr(attr, "_component_info", None)
            if not callable(attr) or not isinstance(info, _FakeComponentInfo):
                continue
            components.append(
                {
                    "name": info.name,
                    "type": info.type,
                    "metadata": {**info.metadata, "handler_name": attr_name},
                }
            )
        return components


class FakeLogger:
    """logging.Logger 接口形态的替身：记录 `(level, 文本)` 供断言。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def _log(self, level: str, message: str, *args: Any, **kwargs: Any) -> None:
        self.records.append((level, message % args if args else str(message)))

    def info(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._log("info", message, *args, **kwargs)

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._log("warning", message, *args, **kwargs)

    def error(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._log("error", message, *args, **kwargs)

    def debug(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._log("debug", message, *args, **kwargs)

    def texts(self, level: str) -> list[str]:
        return [message for lvl, message in self.records if lvl == level]


class FakePluginPaths:
    """`ctx.paths` 替身（对齐真实 SDK `PluginPaths.data_dir/runtime_dir`）。

    默认按宿主的分配方式把 `data_dir` 建成真实可写目录（MaiBot
    `src/plugin_runtime/runner/plugin_paths.py:57` 的
    `data_dir.mkdir(parents=True, exist_ok=True)`），供插件在其下建库/写文件。
    真实宿主的两个目录分属**独立根**（`:52-58`：`data/plugins/<id>` 与
    `temp/plugins/<id>`），二者都不是对方的子目录，且运行时目录同样会被创建；
    替身据此把 `runtime_dir` 放在 `data_dir` 的兄弟位置，不得嵌进数据目录。
    存储故障注入：`storage_fault=True` 时替身把 `data_dir` 路径物化成**被普通文件
    占据**的不可用路径——真实 IO（建账本、写文件）会得到 `OSError`，与磁盘或目录
    不可用同形；故障以真实文件系统状态表达，不伪造 SDK 不存在的存储接口。
    """

    def __init__(self, data_dir: Any, *, storage_fault: bool = False) -> None:
        data_dir = Path(data_dir)
        if storage_fault:
            data_dir.parent.mkdir(parents=True, exist_ok=True)
            data_dir.write_text("storage fault injected", encoding="utf-8")
        else:
            data_dir.mkdir(parents=True, exist_ok=True)
        runtime_dir = data_dir.parent / f"{data_dir.name}-runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = data_dir
        self.runtime_dir = runtime_dir


class FakeSendCapability:
    """`ctx.send` 替身：记录每次调用的能力名与参数，不做真实发送。

    对齐真实 SDK 的 `send.text` / `send.image` / `send.hybrid` / `send.forward` 代理
    签名（含 `return_details` 与透传 kwargs）；`return_details=True` 时返回**经 SDK
    归一后**的详细回执同形字典 `{"sent", "message_id"}`——真实 SDK 的
    `_normalize_capability_result` 只保留这两个键，`success` 与 `error` 都不会出现
    在插件侧（`context.py:241-294`）。
    控制面：`*_result` 覆盖该能力强制的返回值，`*_results` 队列按调用顺序逐个覆盖
    （源消息与评论同为 `send.text` 时需要分别注入）、`raise_on_*` 让对应能力抛异常、
    `hold`（需在事件循环内赋值）让调用悬停在途，供取消与顺序测试。
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.text_result: Any = None
        self.image_result: Any = None
        self.hybrid_result: Any = None
        self.forward_result: Any = None
        self.text_results: list[Any] = []
        self.image_results: list[Any] = []
        self.hybrid_results: list[Any] = []
        self.forward_results: list[Any] = []
        self.raise_on_text: BaseException | None = None
        self.raise_on_image: BaseException | None = None
        self.raise_on_hybrid: BaseException | None = None
        self.raise_on_forward: BaseException | None = None
        self.hold: Any = None

    async def _hold_if_configured(self) -> None:
        if self.hold is not None:
            await self.hold.wait()

    def _receipt(self, queue: list[Any], override: Any, kind: str) -> Any:
        """给出本次调用的回执：队列优先，其次单值覆盖，最后成功默认回执。"""

        if queue:
            return queue.pop(0)
        if override is not None:
            return override
        return {"sent": True, "message_id": f"{kind}-{len(self.calls)}"}

    def _record(self, capability: str, payload: dict[str, Any]) -> None:
        """登记一次调用（含能力名与替身补录键），供用例断言发送参数。"""

        self.calls.append({"capability": capability, **payload})

    async def text(
        self,
        text: str,
        stream_id: str,
        *,
        return_details: bool = False,
        **kwargs: Any,
    ) -> Any:
        self._record(
            "send.text",
            {"text": text, "stream_id": stream_id, "return_details": return_details, **kwargs},
        )
        await self._hold_if_configured()
        if self.raise_on_text is not None:
            raise self.raise_on_text
        if return_details:
            return self._receipt(self.text_results, self.text_result, "text")
        return True

    async def image(
        self,
        image_data: str,
        stream_id: str,
        *,
        return_details: bool = False,
        **kwargs: Any,
    ) -> Any:
        self._record(
            "send.image",
            {
                "image_base64": image_data,
                "stream_id": stream_id,
                "return_details": return_details,
                **kwargs,
            },
        )
        await self._hold_if_configured()
        if self.raise_on_image is not None:
            raise self.raise_on_image
        if return_details:
            return self._receipt(self.image_results, self.image_result, "image")
        return True

    async def hybrid(
        self,
        segments: list[dict[str, Any]],
        stream_id: str,
        *,
        return_details: bool = False,
        **kwargs: Any,
    ) -> Any:
        self._record(
            "send.hybrid",
            {
                "segments": segments,
                "stream_id": stream_id,
                "return_details": return_details,
                **kwargs,
            },
        )
        await self._hold_if_configured()
        if self.raise_on_hybrid is not None:
            raise self.raise_on_hybrid
        if return_details:
            return self._receipt(self.hybrid_results, self.hybrid_result, "hybrid")
        return True

    async def forward(
        self,
        messages: list[dict[str, Any]],
        stream_id: str,
        *,
        return_details: bool = False,
        **kwargs: Any,
    ) -> Any:
        self._record(
            "send.forward",
            {
                "messages": messages,
                "stream_id": stream_id,
                "return_details": return_details,
                **kwargs,
            },
        )
        await self._hold_if_configured()
        if self.raise_on_forward is not None:
            raise self.raise_on_forward
        if return_details:
            return self._receipt(self.forward_results, self.forward_result, "forward")
        return True


class FakeChatCapability:
    """`ctx.chat` 替身：对齐**真实 SDK 归一后**的公共返回形态。

    证据：`maibot_sdk/context.py:34-…` 的 `_CAPABILITY_RESULT_KEYS` 把
    `chat.get_all_streams` 归一为 `streams` 列表、`chat.get_stream_by_group_id` 归一为
    `stream`；`chat.open_session` 不在归一表内，插件拿到宿主实现字典
    （`{"success", "created", "stream", **会话字段}`，`data.py:_cap_chat_open_session`）。
    宿主能力实现内部捕获的异常会返回该字典（`success=False`），未捕获的异常在
    Runner 侧表现为 `RuntimeError`（`runner_main.py:714-766`）；两类都要能被用例注入。

    控制面：
    - `streams`：`get_all_streams` 返回的会话字段列表（宿主内存会话表投影），按
      `platform` 过滤，`"all_platforms"` 返回全部（对齐 `data.py:_list_sessions`）；
    - `open_session` 先按路由键（platform+account+scope+group）在 `streams` 中找既有
      会话，找不到再创建，并用可读等价键派生 `session_id`（宿主是同一路由键的 md5，
      见 `src/common/utils/utils_session.py:8-42`）；
    - `open_session_result` / `open_session_stream` / `raise_on_open_session`：路由
      失败、账号或平台不匹配等用例的注入面；
    - `raise_on_get_all_streams`：模拟未捕获异常经 Runner 变成 `RuntimeError`；
    - `calls`：记录每次调用的能力名与参数，供路由断言。
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.streams: list[dict[str, Any]] = []
        self.open_session_result: Any = None
        self.open_session_stream: dict[str, Any] | None = None
        self.raise_on_open_session: BaseException | None = None
        self.raise_on_get_all_streams: BaseException | None = None

    @staticmethod
    def _route_key(platform: str, group_id: str, account_id: str, scope: str) -> str:
        """可读等价路由键：同一 `platform+account+scope+group` 对应同一会话。"""

        return "|".join(["session", platform, account_id, scope, group_id])

    @staticmethod
    def _serialize(stream: dict[str, Any]) -> dict[str, Any]:
        """补齐宿主流字段别名（`session_id` 与 `stream_id` 同值）。"""

        serialized = dict(stream)
        serialized.setdefault("stream_id", serialized.get("session_id", ""))
        return serialized

    async def get_all_streams(self, platform: str = "qq") -> Any:
        self.calls.append({"capability": "chat.get_all_streams", "platform": platform})
        if self.raise_on_get_all_streams is not None:
            raise self.raise_on_get_all_streams
        return [
            self._serialize(item)
            for item in self.streams
            if platform == "all_platforms" or str(item.get("platform") or "") == platform
        ]

    async def open_session(
        self,
        platform: str = "qq",
        chat_type: str = "private",
        *,
        user_id: str = "",
        group_id: str = "",
        account_id: str = "",
        scope: str = "",
        **kwargs: Any,
    ) -> Any:
        self.calls.append(
            {
                "capability": "chat.open_session",
                "platform": platform,
                "chat_type": chat_type,
                "user_id": user_id,
                "group_id": group_id,
                "account_id": account_id,
                "scope": scope,
                **kwargs,
            }
        )
        if self.raise_on_open_session is not None:
            raise self.raise_on_open_session
        if self.open_session_result is not None:
            return self.open_session_result
        if self.open_session_stream is not None:
            stream = self._serialize(self.open_session_stream)
            return {"success": True, "created": False, "stream": stream, **stream}

        for existing in self.streams:
            if (
                str(existing.get("platform") or "") == platform
                and str(existing.get("group_id") or "") == group_id
                and str(existing.get("account_id") or "") == account_id
                and str(existing.get("scope") or "") == scope
            ):
                stream = self._serialize(existing)
                return {"success": True, "created": False, "stream": stream, **stream}

        stream = self._serialize(
            {
                "session_id": self._route_key(platform, group_id, account_id, scope),
                "platform": platform,
                "group_id": group_id,
                "account_id": account_id,
                "scope": scope,
                "is_group_session": chat_type == "group",
                "chat_type": chat_type,
            }
        )
        return {"success": True, "created": True, "stream": stream, **stream}


class FakeMessageCapability:
    """`ctx.message` 替身：对齐**真实 SDK 归一后**的返回形态（`message.get_by_id`）。

    证据：`_CAPABILITY_RESULT_KEYS["message.get_by_id"] == "message"`，插件拿到的是
    单条消息字典或 `None`；宿主未捕获的异常经 Runner 变成 `RuntimeError`。

    控制面：
    - `store`：`{(chat_id, message_id): 消息字典}`，按宿主 `chat_id` 过滤语义返回；
      配置了 `store`（含空字典）时它是权威来源，未命中即返回 `None`；
    - `results` 按消息 ID 覆盖返回值、`default_result` 为兜底（供未配置 `store` 的用例）；
    - `sequences` 按消息 ID 预置「第 N 次调用返回什么」的队列（就绪轮询用：前几次
      None、随后就绪），优先级高于 `store`；
    - `raise_on_get_by_id`：模拟读取失败（`RuntimeError`）。
    """

    def __init__(
        self,
        sequences: dict[str, list[Any]] | None = None,
        store: dict[tuple[str, str], Any] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.results: dict[str, Any] = {}
        self.default_result: Any = None
        self.store = None if store is None else dict(store)
        self.raise_on_get_by_id: BaseException | None = None
        self.sequences: dict[str, list[Any]] = {
            str(message_id): list(values) for message_id, values in (sequences or {}).items()
        }

    async def get_by_id(
        self,
        message_id: str,
        *,
        chat_id: str = "",
        stream_id: str = "",
        include_binary_data: bool = False,
        **kwargs: Any,
    ) -> Any:
        self.calls.append(
            {
                "message_id": str(message_id),
                "chat_id": chat_id,
                "stream_id": stream_id,
                "include_binary_data": include_binary_data,
                **kwargs,
            }
        )
        if self.raise_on_get_by_id is not None:
            raise self.raise_on_get_by_id
        pending = self.sequences.get(str(message_id))
        if pending:
            return pending.pop(0)
        if self.store is not None:
            return self.store.get((str(chat_id), str(message_id)))
        return self.results.get(str(message_id), self.default_result)


class FakeMaisakaProactiveCapability:
    """`ctx.maisaka.proactive` 替身（对齐真实 SDK `MaisakaProactiveCapability.trigger`）。

    证据：SDK `maibot_sdk/capabilities/maisaka.py:56-88` 的签名
    `trigger(stream_id, intent, *, reason, priority, metadata, **kwargs)`；宿主
    `_cap_maisaka_proactive_trigger`（`src/plugin_runtime/capabilities/core.py:260-289`）
    成功时返回 `{"success": True, "stream_id", "task_id", "queued"}`、失败返回
    `{"success": False, "error"}`，且**不在 SDK 归一表内**（`context.py:34-87`
    没有 `maisaka.proactive.trigger`），插件拿到原始宿主字典。

    控制面：`result` 覆盖单次回执、`results` 队列按调用顺序覆盖、
    `raise_on_trigger` 让能力抛异常（模拟未捕获异常的 Runner 行为）。
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result: Any = None
        self.results: list[Any] = []
        self.raise_on_trigger: BaseException | None = None

    async def trigger(
        self,
        stream_id: str,
        intent: str,
        *,
        reason: str = "",
        priority: str = "",
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        self.calls.append(
            {
                "capability": "maisaka.proactive.trigger",
                "stream_id": stream_id,
                "intent": intent,
                "reason": reason,
                "priority": priority,
                "metadata": dict(metadata or {}),
                **kwargs,
            }
        )
        if self.raise_on_trigger is not None:
            raise self.raise_on_trigger
        if self.results:
            return self.results.pop(0)
        if self.result is not None:
            return self.result
        return {
            "success": True,
            "stream_id": stream_id,
            "task_id": f"task-{len(self.calls)}",
            "queued": True,
        }


class FakeMaisakaCapability:
    """`ctx.maisaka` 替身：只装配本插件消费的主动唤起面。"""

    def __init__(self) -> None:
        self.proactive = FakeMaisakaProactiveCapability()


class FakePluginContext:
    """`PluginContext` 替身：装配本插件消费的 capability 面。

    测试需要哪一面就补哪一面，不做投机装配。`storage_fault=True` 时把
    `paths.data_dir` 注入为不可用路径（见 `FakePluginPaths`）。
    """

    def __init__(self, data_dir: Any, *, storage_fault: bool = False) -> None:
        self.logger = FakeLogger()
        self.paths = FakePluginPaths(data_dir, storage_fault=storage_fault)
        self.send = FakeSendCapability()
        self.message = FakeMessageCapability()
        self.chat = FakeChatCapability()
        self.maisaka = FakeMaisakaCapability()


def Tool(
    name: str,
    description: str = "",
    brief_description: str = "",
    detailed_description: str = "",
    parameters: list[Any] | dict[str, Any] | None = None,
    **metadata: Any,
) -> Any:
    """假装饰器：附着 Tool 组件声明（对齐真实 SDK 的 Tool 元数据面）。"""

    def decorator(func: Any) -> Any:
        declared = {
            "description": description or brief_description,
            "brief_description": brief_description or description,
            "detailed_description": detailed_description,
            "parameters": parameters if isinstance(parameters, list) else [],
            "parameters_raw": parameters if isinstance(parameters, dict) else {},
            "invoke_method": "plugin.invoke_tool",
            **metadata,
        }
        return _attach_component(func, name, "TOOL", declared)

    return decorator


def HookHandler(
    hook: str,
    *,
    name: str = "",
    description: str = "",
    mode: Any = _HookMode.BLOCKING,
    order: Any = _HookOrder.NORMAL,
    timeout_ms: int = 0,
    error_policy: Any = _ErrorPolicy.SKIP,
    **metadata: Any,
) -> Any:
    """假装饰器：附着 HookHandler 组件声明并精确校验枚举值。

    对 mode/order/error_policy 做与真实 SDK 的 pydantic 枚举字段同口径的校验：
    只接受枚举成员或精确的小写值字符串（如 `"observe"`），大写、带空白、空串、
    `None` 与未知值一律抛 `ValueError`。校验在**装饰函数时**执行，与真实 SDK
    构造 `HookHandlerComponentInfo` 的时机一致。
    """

    def decorator(func: Any) -> Any:
        mode_value = _enum_member(_HookMode, mode, "HookMode").value
        order_value = _enum_member(_HookOrder, order, "HookOrder").value
        policy_value = _enum_member(_ErrorPolicy, error_policy, "ErrorPolicy").value
        declared = {
            "hook": str(hook),
            "description": description,
            "mode": mode_value,
            "order": order_value,
            "timeout_ms": int(timeout_ms),
            "error_policy": policy_value,
            **metadata,
        }
        return _attach_component(func, str(name or func.__name__), "HOOK_HANDLER", declared)

    return decorator
