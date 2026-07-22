"""米游社资讯推送插件 —— 主入口"""

import asyncio
import re
import time
from typing import Dict, List, Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.all import *  # noqa: F403
from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.api.event.filter import (
    PermissionType,
    command,
    permission_type,
    regex,
)
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star

from .constants import (
    DEFAULT_BAN_WORDS,
    GAME_ALIAS,
    GAME_CONFIG_KEY,
    GAME_MAP,
    GAME_REGEX,
    GAME_URL_KEY,
    TYPE_ALIAS,
    TYPE_MAP,
)
from .data_store import PushDataStore
from .dedup_store import DedupStore
from .mhy_client import MhyClient
from .renderer import Renderer

_MIYOUSHE_URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?(?:miyoushe|bbs\.mihoyo)\.com/(\w+)(?:[^#]*?)(?:#/)?article/(\d+)"
)


class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.cfg = config
        self.context = context

        proxy = (config.get("proxy", "") or "").strip() or None
        self.mhy_client = MhyClient(proxy=proxy)
        self.renderer = Renderer(self, self.mhy_client)
        self.push_data = PushDataStore()
        self.dedup = DedupStore()

        self._cron_task: Optional[asyncio.Task] = None
        self._start_cron()

    # ================================================================
    #  生命周期
    # ================================================================

    async def terminate(self):
        if self._cron_task and not self._cron_task.done():
            self._cron_task.cancel()
        self.dedup.close()
        await self.mhy_client.close()

    # ================================================================
    #  定时轮询
    # ================================================================

    def _start_cron(self):
        if self._cron_task and not self._cron_task.done():
            self._cron_task.cancel()
        self._cron_task = asyncio.create_task(self._cron_loop())

    async def _cron_loop(self):
        await asyncio.sleep(10)
        while True:
            try:
                await self._do_push_round()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error(f"定时推送异常: {e}")
            interval = max(60, int(self.cfg.get("push_interval", 300)))
            await asyncio.sleep(interval)

    async def _do_push_round(self):
        """执行一轮推送（合并推送，避免刷屏）"""
        window = int(self.cfg.get("push_window", 7200))
        max_num = int(self.cfg.get("max_push_per_round", 2))
        rai = self.cfg.get("rai", True)
        now = int(time.time())

        self.dedup.prune()

        # 收集待推送通知 per-session，保留元数据以支持兜底
        pending: Dict[str, List[dict]] = {}

        # 活动到期预警（独立轮询，单独推送）
        await self._check_activity_expiry()

        # 公告 & 资讯推送
        for gid in [1, 2, 3, 4, 6, 8]:
            cfg_key = GAME_CONFIG_KEY[gid]
            url_key = GAME_URL_KEY[gid]

            for news_type in (1, 3):
                type_part = "announce" if news_type == 1 else "info"
                config_key = f"{cfg_key}_{type_part}"
                groups = self.push_data.push_groups.get(config_key, [])
                if not groups:
                    continue

                news_list = await self.mhy_client.get_news_list(
                    gids=gid,
                    news_type=news_type,
                    page_size=10,
                )
                if not news_list:
                    continue

                game_name = GAME_MAP[gid]
                type_name = TYPE_MAP[news_type]
                ban_re = self._get_ban_re(cfg_key)
                pushed_count = 0

                for item in news_list:
                    if pushed_count >= max_num:
                        break

                    post = item.get("post") or {}
                    post_id = str(post.get("post_id", ""))
                    if not post_id:
                        continue

                    created_at = int(post.get("created_at", 0))
                    title = post.get("subject", "")

                    if now - created_at > window:
                        continue
                    if self.dedup.is_sent(post_id):
                        continue
                    if ban_re and title and ban_re.search(title):
                        logger.info(f"[推送过滤] {game_name}{type_name}: {title}")
                        continue

                    label = f"【{game_name}{type_name}推送】\n{title}"
                    link = f"https://www.miyoushe.com/{url_key}/article/{post_id}"
                    chain = await self._build_push_chain(post_id, gid, label, link, rai)

                    entry = {
                        "chain": chain,
                        "post_id": post_id,
                        "gid": gid,
                        "label": label,
                        "link": link,
                    }
                    for session_id in groups:
                        pending.setdefault(session_id, []).append(entry)

                    self.dedup.mark_sent(post_id)
                    pushed_count += 1
                    await asyncio.sleep(1)

        # 统一发送（先 ultra 图片，失败则降级普通分辨率重试，再失败则 text 兜底）
        for session_id, entries in pending.items():
            # 合并所有条目的 primary chain
            all_parts = []
            for entry in entries:
                all_parts.extend(entry["chain"])
                all_parts.append(Plain(""))

            try:
                result = MessageEventResult(chain=all_parts)
                result.use_t2i(False)
                await self.context.send_message(session_id, result)
            except Exception as e:
                logger.warning(
                    f"推送 ultra 图片失败 ({session_id}): {e}，降级为普通分辨率重试"
                )
                # 用普通分辨率重新渲染每条，合并后重发
                retry_parts = []
                for entry in entries:
                    chain = await self._build_push_chain(
                        entry["post_id"],
                        entry["gid"],
                        entry["label"],
                        entry["link"],
                        True,
                        ultra=False,
                    )
                    retry_parts.extend(chain)
                    retry_parts.append(Plain(""))
                try:
                    result = MessageEventResult(chain=retry_parts)
                    result.use_t2i(False)
                    await self.context.send_message(session_id, result)
                except Exception as e2:
                    logger.warning(
                        f"普通分辨率也失败 ({session_id}): {e2}，尝试 text 兜底"
                    )
                    for entry in entries:
                        try:
                            fb = await self._build_fallback_chain(
                                entry["post_id"],
                                entry["gid"],
                                entry["label"],
                                entry["link"],
                            )
                            fb_result = MessageEventResult(chain=fb)
                            fb_result.use_t2i(False)
                            await self.context.send_message(session_id, fb_result)
                        except Exception:
                            pass  # 单条兜底也失败则跳过

        self.push_data.save()

    async def _build_push_chain(
        self,
        post_id: str,
        gid: int,
        label: str,
        link: str,
        rai: bool,
        ultra: bool = True,
    ) -> list:
        if not rai:
            return [Plain(f"{label}\n{link}")]

        img_paths = await self.renderer.render_news_detail(post_id, gid, ultra=ultra)
        if img_paths:
            return [Plain(label)] + [Image.fromFileSystem(p) for p in img_paths]
        return [Plain(f"{label}\n{link}")]

    async def _build_fallback_chain(
        self, post_id: str, gid: int, label: str, link: str
    ) -> list:
        """兜底消息链：标题 + 简介 + 封面图 + 链接"""
        meta = self.renderer.get_article_meta(post_id)
        cover_url = meta.get("cover_url", "")
        description = meta.get("description", "")

        parts = [Plain(f"{label}")]
        if description:
            parts.append(Plain(f"\n\n{description}"))
        parts.append(Plain(f"\n\n🔗 {link}"))
        if cover_url:
            try:
                parts.append(Image.fromURL(cover_url))
            except Exception:
                pass
        return parts

    async def _safe_send(
        self,
        event: AstrMessageEvent,
        img_paths: List[str],
        post_id: str,
        gid: int,
        label: str,
        link: str,
    ) -> None:
        """发送图片，失败时自动 ultra→normal→text 三级降级。"""
        from astrbot.api.event import MessageChain

        chain = MessageChain(
            chain=[Image.fromFileSystem(p) for p in img_paths]
        )
        try:
            await event.send(chain)
            return
        except Exception as e:
            logger.warning(f"ultra 图片发送失败 ({e})，降级普通分辨率重试")

        # 降级：普通分辨率
        normal = await self.renderer.render_news_detail(post_id, gid, ultra=False)
        if normal:
            try:
                chain2 = MessageChain(chain=[Image.fromFileSystem(p) for p in normal])
                await event.send(chain2)
                return
            except Exception as e2:
                logger.warning(f"普通分辨率也失败 ({e2})，text 兜底")

        # 兜底：text
        fb = await self._build_fallback_chain(post_id, gid, label, link)
        fb_chain = MessageChain(chain=fb)
        await event.send(fb_chain)

    # ================================================================
    #  活动到期预警（每日一个群，轮询）
    # ================================================================

    async def _check_activity_expiry(self):
        now = time.localtime()
        if now.tm_hour < 10:
            return

        today = time.strftime("%Y-%m-%d")
        if self.push_data.last_activity_date == today:
            return

        for game_key, gid in [("gs", 2), ("sr", 6)]:
            groups = self.push_data.activity_push_groups.get(game_key, [])
            if not groups:
                continue

            # 轮询队列：每天只通知一个群
            remaining = self.push_data.get_activity_rotation(game_key)
            if not remaining:
                remaining = list(groups)

            if not remaining:
                continue

            target_session = remaining.pop(0)
            logger.info(
                f"[活动预警轮询] {GAME_MAP[gid]} → {target_session} "
                f"(剩余 {len(remaining)} 群)"
            )

            activities = await self.mhy_client.get_activity_list(game_key)
            if not activities:
                continue

            game_name = GAME_MAP[gid]
            now_ts = time.time()
            msg_parts = [f"📢 {game_name}活动即将结束"]

            for act in activities:
                end_ts = act.get("end_time", 0)
                if not end_ts:
                    continue
                remaining_secs = end_ts - now_ts
                if remaining_secs <= 0 or remaining_secs > 86400:
                    continue

                h = int(remaining_secs // 3600)
                m = int((remaining_secs % 3600) // 60)
                end_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(end_ts))
                msg_parts.append(
                    f"\n\n• {act['title']}\n  剩余 {h}小时{m}分钟\n  结束 {end_str}"
                )

                await asyncio.sleep(2)

            if len(msg_parts) > 1:
                try:
                    result = MessageEventResult().message("\n".join(msg_parts))
                    await self.context.send_message(target_session, result)
                except Exception as e:
                    logger.warning(f"活动预警推送失败 ({target_session}): {e}")

            # 保存剩余队列
            self.push_data.set_activity_rotation(game_key, remaining)

        self.push_data.last_activity_date = today
        self.push_data.save()

    # ================================================================
    #  命令: 查询资讯
    # ================================================================

    @regex(rf"^#?({GAME_REGEX})(公告|资讯|活动)$")
    async def query_news_detail(self, event: AstrMessageEvent):
        """#原神公告 — 最新一条详情"""
        gid = Main._require_game(event)
        if gid is None:
            return
        news_type = Main._parse_type(event.message_str)
        async for result in self._send_news_detail(event, gid, news_type, index=0):
            yield result

    @regex(rf"^#?({GAME_REGEX})(公告|资讯|活动)(\d+)$")
    async def query_news_index(self, event: AstrMessageEvent):
        """#原神公告3 — 按序号"""
        gid = Main._require_game(event)
        if gid is None:
            return
        news_type = Main._parse_type(event.message_str)
        match = re.search(r"(\d+)$", event.message_str)
        if match:
            idx = int(match.group(1)) - 1
            async for result in self._send_news_detail(
                event, gid, news_type, index=idx
            ):
                yield result

    @regex(rf"^#?({GAME_REGEX})(公告|资讯|活动)列表$")
    async def query_news_list(self, event: AstrMessageEvent):
        """#原神公告列表 — 列表（5条，对齐云崽）"""
        gid = Main._require_game(event)
        if gid is None:
            return
        news_type = Main._parse_type(event.message_str)

        game_name = GAME_MAP[gid]
        type_name = TYPE_MAP[news_type]
        yield event.plain_result(f"正在获取 {game_name}{type_name}列表...")

        news_list = await self.mhy_client.get_news_list(
            gid,
            news_type,
            page_size=5,
        )
        if not news_list:
            yield event.plain_result(f"暂无 {game_name}{type_name}。")
            return

        img_paths = await self.renderer.render_news_list(
            news_list,
            game_name,
            type_name,
        )
        if img_paths:
            yield event.chain_result([Image.fromFileSystem(p) for p in img_paths])
        else:
            lines = [f"【{game_name}{type_name}】最新:"]
            for i, item in enumerate(news_list):
                p = item.get("post") or {}
                lines.append(f"{i + 1}. {p.get('subject', '无标题')}")
            yield event.plain_result("\n".join(lines))

    async def _send_news_detail(
        self, event: AstrMessageEvent, gid: int, news_type: int, index: int
    ):
        game_name = GAME_MAP[gid]
        type_name = TYPE_MAP[news_type]
        rai = self.cfg.get("rai", True)

        page_size = max(index + 1, 5) if index < 5 else 5
        news_list = await self.mhy_client.get_news_list(
            gid,
            news_type,
            page_size=page_size,
        )
        if not news_list or index >= len(news_list):
            yield event.plain_result(
                f"暂无 {game_name}{type_name} "
                + (f"第 {index + 1} 条。" if index > 0 else "。")
            )
            return

        item = news_list[index]
        post = item.get("post") or {}
        post_id = str(post.get("post_id", ""))
        title = post.get("subject", "无标题")
        url_key = GAME_URL_KEY.get(gid, "ys")
        link = f"https://www.miyoushe.com/{url_key}/article/{post_id}"

        if rai and post_id:
            yield event.plain_result(f"正在获取 {game_name}{type_name} 详情...")
            img_paths = await self.renderer.render_news_detail(post_id, gid)
            if img_paths:
                await self._safe_send(
                    event,
                    img_paths,
                    post_id,
                    gid,
                    f"【{game_name}{type_name}】{title}",
                    link,
                )
                return

        yield event.plain_result(f"【{game_name}{type_name}】\n{title}\n{link}")

    # ================================================================
    #  命令: 搜索帖子（支持翻页）
    # ================================================================

    @regex(r"^#(米游社|mys)(\s+.+)$")
    async def search_posts(self, event: AstrMessageEvent):
        """#米游社 关键词      → 取第 1 条
        #米游社 关键词 3   → 取第 3 条"""
        msg = event.message_str
        m = re.search(r"^(?:#米游社|#mys)\s+(.+)", msg)
        if not m:
            yield event.plain_result("请输入搜索关键词，如 #米游社 七七")
            return

        rest = m.group(1).strip()
        # 解析尾部的数字作为页码
        page_match = re.search(r"\s+(\d+)$", rest)
        if page_match:
            page = int(page_match.group(1)) - 1
            keyword = rest[: page_match.start()].strip()
        else:
            page = 0
            keyword = rest

        if not keyword:
            yield event.plain_result("请输入搜索关键词，如 #米游社 七七")
            return

        yield event.plain_result(f"正在搜索: {keyword}...")

        posts = await self.mhy_client.search_posts(
            keyword,
            gids=2,
            page_size=20,
        )
        if not posts:
            yield event.plain_result(f"未找到「{keyword}」相关帖子。")
            return

        if page >= len(posts):
            yield event.plain_result(f"搜索结果只有 {len(posts)} 条，序号超出范围。")
            return

        item = posts[page]
        post = item.get("post") or {}
        post_id = str(post.get("post_id", ""))
        title = post.get("subject", "无标题")
        link = f"https://www.miyoushe.com/ys/article/{post_id}"

        if post_id:
            img_paths = await self.renderer.render_news_detail(post_id, gid=2)
            if img_paths:
                await self._safe_send(
                    event,
                    img_paths,
                    post_id,
                    2,
                    f"【米游社搜索】{title}",
                    link,
                )
                return

        yield event.plain_result(f"【米游社搜索】\n{title}\n{link}")

    # ================================================================
    #  命令: URL 自动解析
    # ================================================================

    @regex(r"https?://(?:www\.|m\.)?(?:miyoushe|bbs\.mihoyo)\.com/\w+(?:[^#]*?)(?:#/)?article/\d+")
    async def parse_miyoushe_url(self, event: AstrMessageEvent):
        m = _MIYOUSHE_URL_RE.search(event.message_str)
        if not m:
            return

        game_short = m.group(1)
        post_id = m.group(2)
        _SHORT_TO_GID = {v: k for k, v in GAME_URL_KEY.items()}
        gid = _SHORT_TO_GID.get(game_short, 2)

        yield event.plain_result("正在解析米游社文章...")
        img_paths = await self.renderer.render_news_detail(post_id, gid)
        if img_paths:
            await self._safe_send(
                event,
                img_paths,
                post_id,
                gid,
                "米游社文章",
                f"https://www.miyoushe.com/{game_short}/article/{post_id}",
            )
        else:
            yield event.plain_result("解析失败，请稍后重试。")

    # ================================================================
    #  命令: 推送开关
    # ================================================================

    async def _is_group_admin(self, event: AstrMessageEvent) -> bool:
        """检查用户是否有管理推送的权限（群管理员 或 Bot 管理员）"""
        try:
            msg_obj = getattr(event, "message_obj", None)
            if not msg_obj:
                return True  # 非 aiocqhttp，放行
            sender = getattr(msg_obj, "sender", None)
            if sender:
                role = (
                    sender.get("role", "")
                    if isinstance(sender, dict)
                    else getattr(sender, "role", "")
                )
                if role:
                    return role in ("owner", "admin", "group_owner", "group_admin")
                # sender 无 role 字段 → API 调用（Bot 管理员）
                return True
        except Exception:
            pass
        # 非 aiocqhttp：放行
        return True

    @regex(
        rf"^#?(?:({GAME_REGEX})(开启|关闭)(公告|资讯)推送|(开启|关闭)({GAME_REGEX})(公告|资讯)推送)$"
    )
    async def toggle_push(self, event: AstrMessageEvent):
        if not await self._is_group_admin(event):
            yield event.plain_result("暂无权限，只有群管理员才能操作。")
            return
        gid = Main._require_game(event)
        if gid is None:
            return
        game_name = GAME_MAP[gid]
        cfg_key = GAME_CONFIG_KEY[gid]
        msg = event.message_str

        if "资讯" in msg:
            type_part, type_name = "info", "资讯"
        else:
            type_part, type_name = "announce", "公告"

        config_key = f"{cfg_key}_{type_part}"
        session_id = event.get_session_id()
        groups: list = self.push_data.push_groups.setdefault(config_key, [])

        if "开启" in msg:
            if session_id not in groups:
                groups.append(session_id)
                yield event.plain_result(
                    f"✅ {game_name}{type_name}推送已开启！"
                    f"最新{type_name}将自动推送至此。"
                )
            else:
                yield event.plain_result(f"{game_name}{type_name}推送已处于开启状态。")
        else:
            if session_id in groups:
                groups.remove(session_id)
                yield event.plain_result(f"✅ {game_name}{type_name}推送已关闭。")
            else:
                yield event.plain_result(f"{game_name}{type_name}推送已处于关闭状态。")

        self.push_data.save()

    @regex(
        rf"^#?(?:({GAME_REGEX})(开启|关闭)活动(?:到期)?预警(?:推送)?|(开启|关闭)({GAME_REGEX})活动(?:到期)?预警(?:推送)?)$"
    )
    async def toggle_activity_push(self, event: AstrMessageEvent):
        if not await self._is_group_admin(event):
            yield event.plain_result("暂无权限，只有群管理员才能操作。")
            return
        gid = Main._require_game(event)
        if gid is None:
            return
        msg = event.message_str
        cfg_key = GAME_CONFIG_KEY[gid]
        game_name = GAME_MAP[gid]
        session_id = event.get_session_id()
        groups: list = self.push_data.activity_push_groups.setdefault(cfg_key, [])

        if "开启" in msg:
            if session_id not in groups:
                groups.append(session_id)
                yield event.plain_result(f"✅ {game_name}活动到期预警已开启！")
            else:
                yield event.plain_result(f"{game_name}活动到期预警已处于开启状态。")
        else:
            if session_id in groups:
                groups.remove(session_id)
                yield event.plain_result(f"✅ {game_name}活动到期预警已关闭。")
            else:
                yield event.plain_result(f"{game_name}活动到期预警已处于关闭状态。")

        self.push_data.save()

    @regex(rf"^#?({GAME_REGEX})推送(公告|资讯)$")
    @permission_type(PermissionType.ADMIN)
    async def manual_push(self, event: AstrMessageEvent):
        gid = Main._require_game(event)
        if gid is None:
            return
        yield event.plain_result("正在执行推送检查...")
        await self._do_push_round()
        yield event.plain_result("推送检查完成。")

    @command("米游社推送")
    async def help_cmd(self, event: AstrMessageEvent):
        yield event.plain_result(
            "/米游社推送 - 帮助\n"
            "══════════════════\n\n"
            "查询 (需指定游戏名):\n"
            "· #原神公告   · #崩铁资讯\n"
            "· #绝区零活动 · #原神公告3\n"
            "· #原神公告列表 · #崩铁资讯列表\n"
            "· #米游社 关键词\n"
            "· #米游社 关键词 3\n"
            "· 发送文章链接自动解析\n\n"
            "推送设置 (群管理员):\n"
            "· #开启/关闭原神公告推送\n"
            "· #开启/关闭崩铁资讯推送\n"
            "· #开启/关闭原神活动预警\n\n"
            "手动推送 (Bot管理员):\n"
            "· #推送原神公告\n"
            "· #推送崩铁资讯\n\n"
            "定时每5分钟检查一次，默认以图片渲染。"
        )

    # ================================================================
    #  工具方法
    # ================================================================

    @staticmethod
    def _parse_game(msg: str) -> Optional[int]:
        for alias, gid in GAME_ALIAS.items():
            if alias in msg:
                return gid
        return None

    @staticmethod
    def _require_game(event: AstrMessageEvent) -> Optional[int]:
        msg = event.message_str
        for alias, gid in GAME_ALIAS.items():
            if alias in msg:
                return gid
        event.set_result(
            MessageEventResult().message(
                "请指定游戏名，如 #原神公告 、 #崩铁资讯\n"
                "支持: 原神 / 崩铁 / 绝区零 / 崩坏三 / 崩坏二 / 未定事件簿"
            )
        )
        return None

    @staticmethod
    def _parse_type(msg: str) -> int:
        for alias, tid in TYPE_ALIAS.items():
            if alias in msg:
                return tid
        return 1

    def _get_ban_re(self, config_key: str) -> Optional[re.Pattern]:
        words = self.push_data.get_ban_word_override(
            config_key
        ) or DEFAULT_BAN_WORDS.get(config_key, "")
        if not words:
            return None
        return re.compile(words)
