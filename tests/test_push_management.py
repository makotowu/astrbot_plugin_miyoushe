"""Regression tests using the real AstrBot SDK; no bot/API credentials required."""

import asyncio
import importlib
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain
from astrbot.api.platform import AstrBotMessage, MessageMember, PlatformMetadata
from astrbot.api.star import StarTools
from astrbot.core.platform.message_type import MessageType
from astrbot.core.star.filter.regex import RegexFilter
from astrbot.core.star.star_handler import star_handlers_registry

ROOT = Path(os.environ.get("MIYOUSHE_TEST_TARGET", Path(__file__).resolve().parents[1]))
package = types.ModuleType("miyoushe_under_test")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
mod = importlib.import_module(package.__name__ + ".main")
Main = mod.Main


def event(
    text="",
    *,
    group="10001",
    user="20001",
    bot_admin=False,
    raw_role="admin",
    sender_role=None,
    platform="aiocqhttp",
    isolated=False,
):
    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
    message.group_id = group
    message.sender = MessageMember(user_id=user, nickname="tester")
    if sender_role is not None:
        message.sender.role = sender_role
    message.message = [Plain(text)]
    message.message_str = text
    message.self_id = "bot"
    message.message_id = "test-message"
    message.raw_message = {"sender": {"role": raw_role}} if raw_role is not None else {}
    session = f"{user}_{group}" if isolated and group else group or user
    ev = AstrMessageEvent(
        text, message, PlatformMetadata(platform, "", "test-bot"), session
    )
    ev.role = "admin" if bot_admin else "member"
    return ev


def matches(handler, ev):
    name = f"{Main.__module__}_{handler}"
    md = star_handlers_registry.get_handler_by_full_name(name)
    return bool(
        md
        and any(
            isinstance(f, RegexFilter) and f.filter(ev, {}) for f in md.event_filters
        )
    )


async def replies(generator):
    return [
        "".join(c.text for c in result.chain if isinstance(c, Plain))
        async for result in generator
    ]


class PushManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.cleanup_temp)
        self.data_patch = patch.object(
            StarTools, "get_data_dir", return_value=Path(self.temp.name)
        )
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)
        self.plugin = Main.__new__(Main)
        self.plugin.cfg = {"push_interval": 300}
        self.plugin.context = types.SimpleNamespace(
            send_message=AsyncMock(return_value=True),
            platform_manager=types.SimpleNamespace(platform_insts=[]),
        )
        self.plugin.push_data = mod.PushDataStore()
        self.plugin.dedup = mod.DedupStore()
        self.addCleanup(self.plugin.dedup.close)
        self.plugin.mhy_client = types.SimpleNamespace(
            get_news_list=AsyncMock(return_value=[]),
            get_activity_list=AsyncMock(return_value=[]),
        )

    def cleanup_temp(self):
        # Windows may defer SQLite/AV file deletion briefly after close().
        for attempt in range(5):
            try:
                self.temp.cleanup()
                return
            except OSError:
                if attempt == 4:
                    raise
                time.sleep(0.05)

    async def test_bot_admin_has_priority_over_member_role(self):
        self.assertTrue(
            await self.plugin._is_group_admin(
                event(bot_admin=True, sender_role="member")
            )
        )

    async def test_raw_onebot_member_is_not_admin(self):
        self.assertFalse(await self.plugin._is_group_admin(event(raw_role="member")))

    async def test_raw_onebot_admin_can_enable_in_group(self):
        ev = event("#开启原神公告推送")
        self.assertTrue(matches("toggle_push", ev))
        output = await replies(self.plugin.toggle_push(ev))
        self.assertIn("已开启", output[0])
        self.assertEqual(
            self.plugin.push_data.push_groups["gs_announce"],
            ["test-bot:GroupMessage:10001"],
        )
        self.assertEqual(
            mod.PushDataStore().push_groups, self.plugin.push_data.push_groups
        )

    async def test_private_subscription_is_explicitly_private(self):
        ev = event("#开启原神公告推送", group="")
        output = await replies(self.plugin.toggle_push(ev))
        self.assertIn("私聊", output[0])
        self.assertEqual(
            self.plugin.push_data.push_groups["gs_announce"],
            ["test-bot:FriendMessage:20001"],
        )

    async def test_documented_manual_push_command_matches(self):
        self.assertTrue(matches("manual_push", event("#推送原神公告")))

    async def test_legacy_manual_push_command_matches(self):
        self.assertTrue(matches("manual_push", event("#原神推送公告")))

    async def test_documented_game_aliases_are_supported(self):
        for alias, gid in [("星铁", 6), ("崩3", 1), ("崩2", 3)]:
            self.assertTrue(matches("toggle_push", event(f"#开启{alias}公告推送")))
            self.assertEqual(Main._parse_game(alias), gid)

    async def test_natural_language_tool_is_registered(self):
        from astrbot.core.provider.register import llm_tools

        tool = next((t for t in llm_tools.func_list if t.name == "miyoushe_push"), None)
        self.assertIsNotNone(tool)
        self.assertEqual(
            set(tool.parameters["properties"]), {"action", "game", "news_type"}
        )

    async def test_official_admin_can_enable_without_private_chat(self):
        ev = event(
            "请开启原神公告推送", platform="qq_official", raw_role=None, bot_admin=True
        )
        result = await self.plugin.miyoushe_push_tool(ev, "开启", "原神", "公告")
        self.assertIn("已开启", result)
        self.assertIn("当前群聊", result)
        self.assertEqual(
            mod.PushDataStore().push_groups["gs_announce"],
            ["test-bot:GroupMessage:10001"],
        )

    async def test_official_unknown_role_must_not_gain_permission(self):
        ev = event(platform="qq_official", raw_role=None)
        result = await self.plugin._manage_push(ev, "开启")
        self.assertIn("暂无权限", result)
        self.assertIn("无需私信", result)
        self.assertEqual(self.plugin.push_data.push_groups, {})

    async def test_generic_command_and_help_match(self):
        for text in [
            "#开启米游社推送",
            "/开启米游社推送",
            "开启米游社推送",
            "#关闭米游社推送",
            "#米游社推送状态",
            "#米游社推送测试",
        ]:
            self.assertTrue(matches("manage_push_command", event(text)), text)
        self.assertTrue(matches("help_cmd", event("#米游社推送")))
        output = await replies(
            self.plugin.manage_push_command(event("#开启米游社推送"))
        )
        self.assertIn("全部游戏", output[0])
        self.assertEqual(len(self.plugin.push_data.push_groups), 12)

    async def test_subscription_saved_before_first_reply(self):
        gen = self.plugin.toggle_push(event("#开启原神公告推送"))
        await anext(gen)
        self.assertEqual(
            mod.PushDataStore().push_groups["gs_announce"],
            ["test-bot:GroupMessage:10001"],
        )
        await gen.aclose()

    async def test_duplicate_enable_and_disable_only_current_session(self):
        ev = event()
        other = "test-bot:GroupMessage:10002"
        self.plugin.push_data.push_groups["gs_announce"] = [other]
        for _ in range(2):
            await self.plugin._manage_push(ev, "开启", "原神", "公告")
        self.assertEqual(
            self.plugin.push_data.push_groups["gs_announce"],
            [other, ev.unified_msg_origin],
        )
        await self.plugin._manage_push(ev, "关闭", "原神", "公告")
        self.assertEqual(mod.PushDataStore().push_groups["gs_announce"], [other])

    async def test_private_does_not_enable_group_subscription(self):
        await self.plugin._manage_push(event(group=""), "开启", "原神", "公告")
        output = await self.plugin._manage_push(event(), "状态")
        self.assertIn("尚未开启", output)

    async def test_qq_isolated_sessions_normalize_to_same_group(self):
        for user in ["20001", "20002"]:
            await self.plugin._manage_push(
                event(user=user, isolated=True), "开启", "原神", "公告"
            )
        self.assertEqual(
            self.plugin.push_data.push_groups["gs_announce"],
            ["test-bot:GroupMessage:10001"],
        )

    async def test_nonqq_topic_umo_is_preserved(self):
        ev = event(platform="telegram")
        ev.unified_msg_origin = "telegram-bot:GroupMessage:-10001:topic:9"
        self.assertEqual(Main._event_umo(ev), ev.unified_msg_origin)

    async def test_telegram_group_admin_from_group_metadata(self):
        ev = event(platform="telegram", raw_role=None)
        ev.message_obj.group.group_admins = ["20001"]
        self.assertTrue(await self.plugin._is_group_admin(ev))
        ev.message_obj.group.group_admins = []
        self.assertFalse(await self.plugin._is_group_admin(ev))

    async def test_missing_role_falls_back_to_onebot_member_api(self):
        ev = event(raw_role=None)
        ev.bot = types.SimpleNamespace(
            get_group_member_info=AsyncMock(return_value={"role": "admin"})
        )
        self.assertTrue(await self.plugin._is_group_admin(ev))
        ev.bot.get_group_member_info.assert_awaited_once_with(
            group_id=10001, user_id=20001, no_cache=True
        )

    async def test_save_failure_restores_memory_and_existing_file(self):
        await self.plugin._manage_push(event(), "开启", "原神", "公告")
        before = Path(self.plugin.push_data._file).read_bytes()
        with patch.object(
            mod.PushDataStore.save.__globals__["os"],
            "replace",
            side_effect=PermissionError("test write denied"),
        ):
            output = await self.plugin._manage_push(event(), "关闭", "原神", "公告")
        self.assertIn("未生效", output)
        self.assertEqual(
            self.plugin.push_data.push_groups["gs_announce"],
            ["test-bot:GroupMessage:10001"],
        )
        self.assertEqual(Path(self.plugin.push_data._file).read_bytes(), before)
        self.assertEqual(list(Path(self.temp.name).glob("push_data_*.tmp")), [])

    async def test_status_is_per_current_session(self):
        await self.plugin._manage_push(event(), "开启", "崩铁", "资讯")
        self.assertIn(
            "崩坏：星穹铁道资讯", await self.plugin._manage_push(event(), "状态")
        )
        self.assertIn(
            "尚未开启", await self.plugin._manage_push(event(group="10002"), "状态")
        )

    async def test_test_message_primes_official_scene_and_routes_to_group(self):
        remember = unittest.mock.Mock()
        self.plugin.context.platform_manager.platform_insts = [
            types.SimpleNamespace(
                meta=lambda: types.SimpleNamespace(id="test-bot", name="qq_official"),
                remember_session_scene=remember,
            )
        ]
        ev = event(platform="qq_official", raw_role=None, bot_admin=True, isolated=True)
        result = await self.plugin.miyoushe_push_tool(ev, "测试")
        remember.assert_called_once_with("10001", "group")
        args = self.plugin.context.send_message.await_args.args
        self.assertEqual(args[0], "test-bot:GroupMessage:10001")
        self.assertFalse(args[1].use_markdown_)
        self.assertIn("发送调用已完成", result)
        self.assertEqual(self.plugin.push_data.push_groups, {})

    async def test_test_send_failures_do_not_report_success(self):
        for value in [False, RuntimeError("QQ active quota exhausted")]:
            self.plugin.context.send_message.side_effect = (
                value if isinstance(value, Exception) else None
            )
            self.plugin.context.send_message.return_value = value
            output = await self.plugin._manage_push(event(), "测试")
            self.assertIn("失败", output)
            self.assertNotIn("发送调用已完成", output)

    async def test_invalid_tool_arguments_do_not_write(self):
        for args in [
            ("跳过权限", "全部", "全部"),
            ("开启", "不存在", "公告"),
            ("开启", "原神", "活动"),
        ]:
            await self.plugin._manage_push(event(), *args)
        self.assertEqual(self.plugin.push_data.push_groups, {})

    async def test_unsupported_activity_is_not_saved(self):
        output = await replies(
            self.plugin.toggle_activity_push(event("#开启绝区零活动预警"))
        )
        self.assertIn("仅支持", output[0])
        self.assertEqual(self.plugin.push_data.activity_push_groups, {})

    async def test_manual_push_ignores_window_and_only_targets_current_chat(self):
        self.plugin.mhy_client.get_news_list.return_value = [
            {"post": {"post_id": "old-post", "subject": "old article", "created_at": 1}}
        ]
        self.plugin._build_rich_chain = AsyncMock(
            return_value=("article", mod.MessageChain(chain=[Plain("article")]))
        )
        self.plugin.push_data.push_groups["gs_announce"] = [
            "test-bot:GroupMessage:another-group"
        ]
        output = await replies(
            self.plugin.manual_push(event("#推送原神公告", bot_admin=True))
        )
        self.plugin.mhy_client.get_news_list.assert_awaited_once_with(
            gids=2, news_type=1, page_size=1
        )
        self.assertIn("发送调用已完成", output[-1])
        self.assertEqual(
            self.plugin.context.send_message.await_args.args[0],
            "test-bot:GroupMessage:10001",
        )
        self.assertFalse(
            self.plugin.dedup.is_sent("test-bot:GroupMessage:10001|old-post")
        )

    async def test_scheduled_push_after_enable_delivers_and_deduplicates(self):
        await self.plugin._manage_push(event(), "开启", "原神", "公告")
        self.plugin._check_activity_expiry = AsyncMock()
        self.plugin.mhy_client.get_news_list.return_value = [
            {
                "post": {
                    "post_id": "new-post",
                    "subject": "new article",
                    "created_at": int(time.time()),
                }
            }
        ]
        self.plugin._build_rich_chain = AsyncMock(
            return_value=("article", mod.MessageChain(chain=[Plain("article")]))
        )
        with patch.object(mod.asyncio, "sleep", new=AsyncMock()):
            await self.plugin._do_push_round()
            await self.plugin._do_push_round()
        self.assertEqual(self.plugin.context.send_message.await_count, 1)
        self.assertEqual(
            self.plugin.context.send_message.await_args.args[0],
            "test-bot:GroupMessage:10001",
        )
        self.assertTrue(
            self.plugin.dedup.is_sent("test-bot:GroupMessage:10001|new-post")
        )

    async def test_full_constructor_starts_and_stops_cron(self):
        plugin = Main(self.plugin.context, {})
        self.assertIsNotNone(plugin._cron_task)
        task = plugin._cron_task
        await plugin.terminate()
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.done())

    async def test_real_official_adapter_posts_to_group_not_private(self):
        # Use the real SDK context + official adapter, replacing only Tencent HTTP
        # and framework telemetry. An empty scene cache simulates a bot restart.
        from astrbot.api.star import Context
        from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import (
            QQOfficialPlatformAdapter,
        )
        from astrbot.core.utils.metrics import Metric

        adapter = QQOfficialPlatformAdapter.__new__(QQOfficialPlatformAdapter)
        adapter.config = {"id": "test-bot"}
        adapter._session_last_message_id = {}
        adapter._session_scene = {}
        adapter._allow_group_proactive_send = True
        adapter.use_markdown_default = True
        api = types.SimpleNamespace(
            post_group_message=AsyncMock(return_value={"id": "mock-tencent-response"}),
            post_c2c_message=AsyncMock(),
            post_message=AsyncMock(),
        )
        adapter.client = types.SimpleNamespace(api=api)
        context = Context.__new__(Context)
        context.platform_manager = types.SimpleNamespace(platform_insts=[adapter])
        context.get_config = lambda **kwargs: {}
        self.plugin.context = context
        with patch.object(Metric, "upload", new=AsyncMock()):
            output = await self.plugin._manage_push(
                event(platform="qq_official", raw_role=None, bot_admin=True), "测试"
            )
            await asyncio.sleep(0)
        self.assertIn("发送调用已完成", output)
        api.post_group_message.assert_awaited_once()
        payload = api.post_group_message.await_args.kwargs
        self.assertEqual(payload["group_openid"], "10001")
        self.assertIn("米游社主动推送测试", payload["content"])
        self.assertNotIn("markdown", payload)
        self.assertNotIn("msg_id", payload)
        api.post_c2c_message.assert_not_awaited()
        api.post_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
