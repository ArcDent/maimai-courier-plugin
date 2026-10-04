"""第6组公开插件装配：真实直连客户端与账本，只有平台动作是合成替身。"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

from delivery_ledger import LedgerUnavailableError
from tests.snowluma_client_support import ScriptedOneBot
from tests.support import (FakeClock, SOURCE_ACCOUNT_ID, SOURCE_GROUP_ID, TARGET_GROUP_ID,
                           enabled_config, make_plugin, prepare_source, ledger_key, open_test_ledger)

DEFAULT_RESOURCE_ID = "res-card-synthetic"
DEFAULT_MEDIA_URL = "https://cdn.example.invalid/media.png"


def media_url_for(ordinal):
    return DEFAULT_MEDIA_URL if ordinal == 1 else DEFAULT_MEDIA_URL.replace(".png", f"-{ordinal}.png")


def config_with(base_plugin, **overrides):
    config = deepcopy(base_plugin.config.model_dump())
    for section, values in overrides.items():
        config.setdefault(section, {}).update(values)
    return config


def onebot_segments(raw_message, *, resource_id=DEFAULT_RESOURCE_ID, media_url=None):
    """冻结的两种输入格式映射；预期输出仍由测试写独立字面量。"""
    result = []
    ordinal = 0
    for segment in raw_message:
        kind, data = segment.get("type"), segment.get("data")
        if kind == "text":
            result.append({"type": "text", "data": {"text": data}})
        elif kind == "image":
            ordinal += 1
            result.append({"type": "image", "data": {"url": media_url or media_url_for(ordinal)}})
        elif kind == "at":
            fields = {"qq": data.get("target_user_id")}
            if data.get("target_user_cardname"):
                fields["card"] = data["target_user_cardname"]
            elif data.get("target_user_nickname"):
                fields["nickname"] = data["target_user_nickname"]
            result.append({"type": "at", "data": fields})
        elif kind == "forward" or (kind == "dict" and data.get("type") == "forward"):
            result.append({"type": "forward", "data": {"id": resource_id}})
        else:
            result.append(deepcopy(segment))
    return result


class Gates:
    """测试控制公开动作的等待位置，生产不认识这些标签。"""
    def __init__(self):
        self.events = {}
        self.arrivals = {}

    def pause(self, stage):
        self.events[stage] = asyncio.Event()

    def release(self, stage):
        self.events[stage].set()

    async def arrive(self, stage):
        self.arrivals[stage] = self.arrivals.get(stage, 0) + 1
        if stage in self.events:
            await self.events[stage].wait()


class DirectOneBot(ScriptedOneBot):
    """按固定动作协议响应；完整记录实际提交，断言不依赖客户端内部状态。"""
    def __init__(self, ctx, targets):
        super().__init__()
        self.ctx = ctx
        self.account = SOURCE_ACCOUNT_ID
        self.groups = [{"group_id": value} for value in targets]
        self.sources = {}
        self.receipts = {}
        self.gates = Gates()
        self.before_wire = None
        self.send_response = None
        self.next_replies = {}

    def fail_next(self, action, response):
        self.next_replies[action] = response

    @property
    def messages(self):
        return self.sources

    async def action(self, name, value=None, *, expected_generation=None):
        from maimai_courier_plugin.snowluma_transport import OneBotUnavailable
        self.calls.append((name, value, expected_generation))
        if self.closed or (expected_generation is not None and expected_generation != self.connection_generation):
            raise OneBotUnavailable("connection_changed", submitted=False)
        await self.gates.arrive(name)
        if name in self.next_replies:
            response = self.next_replies.pop(name)
            if isinstance(response, BaseException):
                raise response
            return response
        if self.on_action:
            result = await self.on_action(name, value)
            if result is not None:
                return result
        if name == "get_login_info":
            data = {"user_id": self.account}
        elif name == "get_group_list":
            data = self.groups
        elif name == "get_msg":
            if value in self.receipts:
                data = self.receipts[value]
            elif value in self.sources:
                data = self.sources[value]
            else:
                host = next((msg for (_, mid), msg in self.ctx.message.store.items() if mid == value), None)
                data = None if host is None else {
                    "message_id": value, "message_type": "group", "group_id": SOURCE_GROUP_ID,
                    "user_id": "90001", "message": onebot_segments(host["raw_message"]),
                }
        else:
            if self.send_response is not None:
                return deepcopy(self.send_response)
            data = {"message_id": f"target-{len(self.sent)}"}
        return {"status": "ok", "retcode": 0, "data": deepcopy(data)}

    async def send_msg(self, *, target_group_id, message, expected_generation, before_submit, stage=""):
        from maimai_courier_plugin.snowluma_transport import OneBotUnavailable
        await self.gates.arrive(f"{stage}_queue")
        if self.before_wire:
            await self.before_wire(stage)
        if self.closed or expected_generation != self.connection_generation or not before_submit():
            raise OneBotUnavailable("not_submitted", submitted=False)
        self.sent.append({"action": "send_msg", "params": {"group_id": target_group_id, "message": deepcopy(list(message))}, "stage": stage})
        message_id = f"target-{len(self.sent)}"
        self.receipts[message_id] = {"message_id": message_id, "message_type": "group",
                                     "group_id": target_group_id, "user_id": self.account, "message_seq": len(self.sent)}
        await self.gates.arrive("platform_commit")
        return await self.action("send_msg", expected_generation=expected_generation)

    def count(self, action):
        return sum(name == action for name, _, _ in self.calls)

    def routing_observation(self):
        source_ids = [value for name, value, _ in self.calls if name == "get_msg" and value not in self.receipts]
        return {"source_message_id": source_ids[-1], "source_group_id": SOURCE_GROUP_ID,
                "target_group_id": self.sent[0]["params"]["group_id"]}

    @property
    def send_count(self):
        return len(self.sent)

    @property
    def send_calls(self):
        return [SimpleNamespace(target=call["params"]["group_id"], detail=call["params"]) for call in self.sent]


class FaultLedger:
    """公开存储依赖注入；断言仍读真实账本，不复制投递事实。"""
    def __init__(self, ledger):
        self.ledger = ledger
        self.faults = set()

    def fail_next(self, operation):
        self.faults.add(operation)

    def __getattr__(self, name):
        method = getattr(self.ledger, name)
        def call(*args, **kwargs):
            if name in self.faults:
                self.faults.remove(name)
                raise LedgerUnavailableError("模拟存储失败")
            return method(*args, **kwargs)
        return call


class DirectFixture:
    def __init__(self, path, *, raw_message=None, target_groups=(TARGET_GROUP_ID,), source_groups=(SOURCE_GROUP_ID,), clock=None,
                 message_types=("forward",), config_overrides=None, onebot_raw_message=None):
        self.data_dir = path
        path.mkdir(parents=True, exist_ok=True)
        self.clock = clock or FakeClock()
        self.ledger = open_test_ledger(path, clock=self.clock)
        self.storage = FaultLedger(self.ledger)
        config = enabled_config(source_groups=source_groups, target_groups=target_groups, message_types=message_types)
        config["snowluma"] = {"websocket_url": "ws://127.0.0.1:8765/onebot", "token_env_var": "SYNTHETIC_TOKEN"}
        for section, values in (config_overrides or {}).items():
            config.setdefault(section, {}).update(values)
        self.plugin, self.ctx = make_plugin(path, config=config, clock=self.clock,
            transport_factory=lambda _: self.onebot, environ={"SYNTHETIC_TOKEN": "synthetic-only"},
            ledger_opener=lambda _: self.storage)
        prepare_source(self.ctx, raw_message=raw_message)
        self.onebot = DirectOneBot(self.ctx, target_groups)
        if onebot_raw_message is not None:
            store_source_message(self.onebot, message_id="m-source-1", message=onebot_raw_message)

    def record(self, *, message_id="m-source-1", target_group_id=TARGET_GROUP_ID):
        try:
            record = self.ledger.get(ledger_key(message_id=message_id, target_group_id=target_group_id))
        except LedgerUnavailableError:
            reopened = open_test_ledger(self.data_dir, clock=self.clock)
            try:
                record = reopened.get(ledger_key(message_id=message_id, target_group_id=target_group_id))
            finally:
                reopened.close()
        return None if record is None else asdict(record)

    def sent_segments(self):
        return [call["params"]["message"] for call in self.onebot.sent]

    def sent_texts(self):
        return [segments[0]["data"]["text"] for segments in self.sent_segments()
                if len(segments) == 1 and segments[0]["type"] == "text"]

    def old_send_calls(self):
        return self.ctx.send.calls

    def reopen(self):
        self.close()
        self.__init__(self.data_dir, clock=self.clock)

    def close(self):
        asyncio.run(self.plugin.on_unload())
        self.ledger.close()


def make_direct_plugin(path, **kwargs):
    return DirectFixture(path, **kwargs)


def store_source_message(onebot, *, message_id, group_id=SOURCE_GROUP_ID, message=None, user_id="90001", **kwargs):
    stored = {"message_id": message_id, "message_type": "group", "group_id": group_id,
              "user_id": user_id, "message": message or []}
    stored.update(kwargs)
    onebot.sources[message_id] = stored
    return stored


def delivery_rows(ledger, *, message_id="m-source-1"):
    record = ledger.get(ledger_key(message_id=message_id))
    return [] if record is None else [(record.key.source_message_id, record.status, record.target_message_id)]


def comment_sleeps(clock):
    return [seconds for seconds in clock.sleeps if seconds == 0.5]


async def deny_before_wire(stage):
    from maimai_courier_plugin.snowluma_transport import OneBotUnavailable
    raise OneBotUnavailable("not_submitted", submitted=False)
