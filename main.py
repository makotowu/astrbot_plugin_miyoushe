"""米游社资讯推送插件 —— 主入口"""

import asyncio
import html
import json
import re
import time
from typing import Dict, List, Optional, Tuple

from astrbot.api import AstrBotConfig, logger
from astrbot.api.all import *  # noqa: F403
from astrbot.api.event import AstrMessageEvent, MessageChain, MessageEventResult
from astrbot.api.event.filter import (
    PermissionType,
    command,
    permission_type,
    regex,
)
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star

from .ai_summary import PostSummarizer, extract_fallback_summary
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

# 兼容不同 AstrBot 版本：新版类名 MessageSession，旧版为 MessageSesion（两者互为
# 别名）。都导入失败时只跳过 scene 预热，不影响插件加载。
try:  # pragma: no cover - 跨版本兜底
    from astrbot.core.platform.message_session import (
        MessageSession as _MessageSession,
    )
except Exception:  # pragma: no cover
    try:
        from astrbot.core.platform.message_session import (
            MessageSesion as _MessageSession,
        )
    except Exception:  # pragma: no cover
        _MessageSession = None  # type: ignore[assignment]

try:  # pragma: no cover - 跨版本兜底
    from astrbot.core.platform.message_type import MessageType as _MessageType
except Exception:  # pragma: no cover
    _MessageType = None  # type: ignore[assignment]

# 群消息段的枚举值（容器 v4.28.0 确认 MessageType.GROUP_MESSAGE = "GroupMessage"）
_GROUP_MESSAGE_TYPE = (
    _MessageType.GROUP_MESSAGE.value if _MessageType is not None else "GroupMessage"
)

_MIYOUSHE_URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?(?:miyoushe|bbs\.mihoyo)\.com/(\w+)(?:[^#]*?)(?:#/)?article/(\d+)"
)

# 活动到期预警的数据来源只支持原神(gs)/崩铁(sr) 的服务器公告 API
_ACTIVITY_GAMES: Dict[str, int] = {"gs": 2, "sr": 6}
# GAME_CONFIG_KEY 反查：config key → gid（用于给不支持的 key 打日志）
_CONFIG_KEY_TO_GID: Dict[str, int] = {v: k for k, v in GAME_CONFIG_KEY.items()}

# 搜单单条结果展示「文章自带摘要」的截断字数（搜索不调 AI）
_SEARCH_ITEM_SUMMARY_CHARS = 300

# 主动发送结果：(是否成功, 是否永久失效, 最后一次异常)。
# 并发发送时不能用实例级状态记录错误，结果必须通过返回值传递。
_SendOutcome = Tuple[bool, bool, Optional[BaseException]]

# 单个会话发送结果：(是否全部成功, 是否永久失效, 最后一次异常, 已成功投递的 post_id)
_SessionOutcome = Tuple[bool, bool, Optional[BaseException], List[str]]

# 平台判定「会话永久失效」的异常特征（QQ 官方群已注销 code 11255 / 40011028），
# 命中后立即摘除订阅、不再降级重试，避免死群拖垮整轮推送。
_PERMANENT_SESSION_MARKERS = (
    "11255",
    "40011028",
    "群已注销",
    "用户/群已注销",
    "请求的资源不存在",
)


class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.cfg = config
        self.context = context

        proxy = (config.get("proxy", "") or "").strip() or None
        self.mhy_client = MhyClient(proxy=proxy)
        # 链接解析 / 公告详情 / 搜索改为 AI 总结（需配置支持视觉的模型）
        self.summarizer = PostSummarizer(self.context, config, self.mhy_client)
        # 旧 push_data.json 里存的是裸 session_id，需要平台信息才能迁移成
        # unified_msg_origin（platform_id:MessageType:session_id）
        self.push_data = PushDataStore(platforms=self._collect_platform_infos())
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
        """执行一轮推送（每条公告一条富文本卡片消息）"""
        window = int(self.cfg.get("push_window", 7200))
        max_num = int(self.cfg.get("max_push_per_round", 2))
        summary_len = max(0, int(self.cfg.get("push_summary_len", 100)))
        now = int(time.time())

        self.dedup.prune()

        # AstrBot 启动顺序是「先加载插件、后初始化平台适配器」，插件构造时可能
        # 拿不到平台信息；首个推送轮次再尝试一次旧订阅迁移（幂等，无变更不写盘）。
        if self.push_data.has_legacy_sessions():
            self.push_data.migrate_legacy_sessions(
                platforms=self._collect_platform_infos()
            )

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
                    if ban_re and title and ban_re.search(title):
                        logger.info(f"[推送过滤] {game_name}{type_name}: {title}")
                        continue

                    # 先按「会话 + post_id」去重：某个平台失败时不会被其他平台
                    # 的成功掩盖；若没有任何会话需要这条，则跳过渲染（省开销）
                    targets = [
                        session_id
                        for session_id in groups
                        if not self.dedup.is_sent(
                            self._dedup_key(session_id, post_id)
                        )
                    ]
                    if not targets:
                        continue

                    label = f"【{game_name}{type_name}推送】\n{title}"
                    link = f"https://www.miyoushe.com/{url_key}/article/{post_id}"
                    # 富文本卡片：文字正文 + 封面（不再做整篇长图/T2I 渲染）
                    try:
                        text, rich_chain = await self._build_rich_chain(
                            post_id, label, link, summary_len, created_at
                        )
                    except Exception as e:  # 单条构造失败不拖垮整轮
                        logger.warning(
                            f"[推送] 构造富文本卡片失败，退回纯文字: "
                            f"post={post_id} err={e}"
                        )
                        text = self._format_push_text(label, "", created_at, link)
                        rich_chain = MessageChain(chain=[Plain(text)])

                    entry = {
                        "post_id": post_id,
                        "gid": gid,
                        "label": label,
                        "link": link,
                        "text": text,
                        "chain": rich_chain,
                    }
                    for session_id in targets:
                        pending.setdefault(session_id, []).append(entry)

                    pushed_count += 1
                    await asyncio.sleep(1)

        # 并发发送：单个会话失败/永久失效不阻塞其他会话；会话内部逐条发送
        # （每条公告一条富文本卡片消息），成功一条就标记一条去重。
        sessions = list(pending.keys())
        results = await asyncio.gather(
            *[self._send_entries_merged(sid, pending[sid]) for sid in sessions],
            return_exceptions=True,
        )
        for session_id, result in zip(sessions, results):
            entries = pending[session_id]
            if isinstance(result, BaseException):
                # 兜底：_send_entries_merged 内部已尽量捕获，这里防止单会话
                # 未捕获异常中断整轮；不写去重，下一轮重试
                logger.warning(
                    f"[定时推送] {session_id} 条数={len(entries)} 发送异常，下轮重试",
                    exc_info=result,
                )
                continue
            # (ok, permanent, exc, delivered_post_ids)：只标记真正投递成功的 post
            for post_id in result[3]:
                self.dedup.mark_sent(self._dedup_key(session_id, post_id))

        self.push_data.save()

    async def _send_chain(
        self, session_id: str, chain: MessageChain
    ) -> _SendOutcome:
        """主动发送一条消息链，返回 (是否成功, 是否永久失效, 最后一次异常)。

        并发发送时不能用实例级状态记录错误，结果一律通过返回值传递。

        - session 不合法时 context.send_message 会抛 ValueError（旧版裸 id 即
          因此失败）；
        - 找不到匹配平台时返回 False（不算永久失效）；
        - 会话永久失效（群/用户已注销，QQ 官方 code 11255）时立即摘除订阅并返回
          permanent=True，调用方不再做降级重试，避免长时间空转；
        - 平台适配器内部静默跳过时返回值仍是 True，无法从插件侧区分（上游限制，
          故发送前做 scene 预热，见 `_prime_proactive_scenes`）。
        """
        try:
            ret = await self.context.send_message(session_id, chain)
        except Exception as e:
            reason = self._permanent_session_error_reason(e)
            if reason:
                # 永久失效：摘除订阅 + 快速失败（不降级、不写去重）
                self._remove_invalid_session(session_id, reason)
                return False, True, e
            logger.warning(f"主动推送发送异常 ({session_id}): {e}", exc_info=True)
            return False, False, e
        if ret is False:
            exc = RuntimeError("context.send_message 返回 False（未找到匹配平台）")
            logger.warning(f"主动推送未找到匹配平台 ({session_id})")
            return False, False, exc
        return True, False, None

    @staticmethod
    def _permanent_session_error_reason(exc: BaseException) -> Optional[str]:
        """判断异常是否表示会话永久失效，命中则返回命中的特征文本。

        沿 `__cause__` / `__context__` 链路查找，兼容 tenacity/botpy 的异常包装。
        """
        texts: List[str] = []
        seen: set = set()
        cur: Optional[BaseException] = exc
        while isinstance(cur, BaseException) and id(cur) not in seen:
            seen.add(id(cur))
            texts.append(str(cur))
            texts.append(repr(cur))
            cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
        blob = "\n".join(texts)
        for marker in _PERMANENT_SESSION_MARKERS:
            if marker in blob:
                return marker
        return None

    def _remove_invalid_session(self, session_id: str, reason: str) -> None:
        """把永久失效会话从推送订阅/活动轮询队列移除并写回文件。"""
        removed = 0
        try:
            removed = self.push_data.remove_session(session_id)
            if removed:
                self.push_data.save()
        except Exception:  # pragma: no cover - 清理失败不影响主流程
            logger.exception(f"[定时推送] 移除失效会话订阅失败: {session_id}")
        logger.warning(
            f"[定时推送] 会话已永久失效，已从推送订阅移除: {session_id} "
            f"(原因: {reason}, 移除 {removed} 处)"
        )

    @staticmethod
    def _finalize_proactive_chain(chain: MessageChain) -> MessageChain:
        """主动推送链统一关闭 t2i 与 markdown。

        qq_official 平台未配置 use_markdown 时适配器默认 True，纯文本会被当作
        msg_type=2 的主动 markdown 发送（QQ 侧对主动 markdown 有限制风险），
        这里显式关闭。旧版本没有 use_markdown_ 字段时赋值也无副作用。
        """
        chain.use_t2i(False)
        chain.use_markdown_ = False
        return chain

    @staticmethod
    def _parse_push_session(umo: str) -> Optional[Tuple[str, str, str]]:
        """解析 unified_msg_origin → (platform_id, message_type, session_id)。"""
        if _MessageSession is not None:
            try:
                session = _MessageSession.from_str(umo)
            except Exception:
                return None
            message_type = getattr(session, "message_type", None)
            return (
                str(getattr(session, "platform_name", "") or ""),
                str(getattr(message_type, "value", "") or ""),
                str(getattr(session, "session_id", "") or ""),
            )
        # 兜底：手工切三段（仅在 MessageSession 导入失败的异常环境使用）
        parts = umo.split(":", 2)
        if len(parts) != 3 or not all(parts):
            return None
        return parts[0], parts[1], parts[2]

    async def _prime_proactive_scenes(
        self, session_id: str
    ) -> Optional[Tuple[str, str, str]]:
        """预热 qq_official 群 scene 缓存，让重启后未活跃的群也能主动推送。

        qq_official 适配器的 `send_by_session` 在缺 msg_id 时，仅当
        ``_session_scene[session_id] == "group"`` 才允许主动发送；该 dict 是
        内存态，只在收到群消息时写入，容器重启即清空（容器内源码
        qqofficial_platform_adapter.py:383-400）。这里调用适配器公开方法
        `remember_session_scene` 预热，不直接改私有 dict；任何异常都不影响发送。

        Returns:
            解析出的 (platform_id, message_type, session_id)；解析失败返回 None。
        """
        parsed = self._parse_push_session(session_id)
        if not parsed:
            logger.debug(f"[定时推送] 无法解析会话，跳过 scene 预热: {session_id!r}")
            return None
        platform_id, message_type, raw_session_id = parsed
        if message_type != _GROUP_MESSAGE_TYPE:
            # 私聊等场景无需预热（qq_official 好友消息缺 msg_id 时走另一分支）
            return parsed

        insts = (
            getattr(
                getattr(self.context, "platform_manager", None),
                "platform_insts",
                None,
            )
            or []
        )
        for inst in insts:
            try:
                meta = inst.meta()
            except Exception:
                continue
            if str(getattr(meta, "id", "") or "") != platform_id:
                continue
            remember = getattr(inst, "remember_session_scene", None)
            if not callable(remember):
                logger.debug(
                    f"[定时推送] 平台 {platform_id} 无 remember_session_scene，"
                    "跳过 scene 预热"
                )
                return parsed
            try:
                remember(raw_session_id, "group")
                logger.debug(
                    f"[定时推送] 已预热 scene: platform={platform_id} "
                    f"type={message_type} sid={raw_session_id}"
                )
            except Exception:  # pragma: no cover - 预热失败不影响发送
                logger.debug(
                    f"[定时推送] scene 预热失败: platform={platform_id} "
                    f"sid={raw_session_id}",
                    exc_info=True,
                )
            return parsed
        logger.debug(f"[定时推送] 未找到平台实例，跳过 scene 预热: {platform_id}")
        return parsed

    async def _send_entry(
        self, session_id: str, entry: dict
    ) -> _SendOutcome:
        """发送一条公告：富文本卡片（文字+封面）失败时降级为纯文字链。"""
        rich_chain = MessageChain(chain=list(entry["chain"].chain))
        ok, permanent, exc = await self._send_chain(
            session_id, self._finalize_proactive_chain(rich_chain)
        )
        if ok or permanent:
            return ok, permanent, exc
        logger.warning(
            f"[定时推送] {session_id} post_id={entry['post_id']} "
            f"富文本卡片发送失败，降级纯文字重试: {exc}"
        )
        text_chain = self._finalize_proactive_chain(
            MessageChain(chain=[Plain(entry["text"])])
        )
        return await self._send_chain(session_id, text_chain)

    def _log_entries_result(
        self, session_id: str, total: int, outcome: _SessionOutcome
    ) -> None:
        """按 session 输出可观测的发送结果日志（部署后便于 grep 验证）。"""
        ok, permanent, exc, delivered = outcome
        if ok:
            logger.info(f"[定时推送] {session_id} 条数={total} 发送成功")
            return
        if permanent:
            logger.warning(
                f"[定时推送] {session_id} 条数={total} 发送失败"
                "（会话永久失效，已移除订阅，不再重试）"
            )
            return
        if delivered:
            logger.warning(
                f"[定时推送] {session_id} 条数={total} 部分成功"
                f"（{len(delivered)}/{total}），失败条下轮重试"
            )
            return
        msg = f"[定时推送] {session_id} 条数={total} 发送失败，下轮重试"
        if isinstance(exc, BaseException):
            logger.warning(f"{msg}：{exc}", exc_info=exc)
        else:
            logger.warning(msg)

    async def _send_entries_merged(
        self, session_id: str, entries: List[dict]
    ) -> _SessionOutcome:
        """逐条发送同一 session 的待推送公告（每条一条富文本卡片消息）。

        每条独立降级「富文本卡片 → 纯文字」，某条失败不影响同会话其他条；
        永久失效会话立即停止并摘除订阅。返回
        (是否全部成功, 是否永久失效, 最后一次异常, 已成功投递的 post_id 列表)。
        """
        # 发送前预热 qq_official 的群 scene 内存缓存，否则重启后未活跃的群
        # 会被适配器静默 skip（只打 WARN，send_message 仍返回 True）
        parsed = await self._prime_proactive_scenes(session_id)
        logger.debug(f"[定时推送] {session_id} 条数={len(entries)} scene={parsed}")

        delivered: List[str] = []
        last_exc: Optional[BaseException] = None
        for entry in entries:
            ok, permanent, exc = await self._send_entry(session_id, entry)
            if ok:
                delivered.append(entry["post_id"])
                continue
            last_exc = exc
            if permanent:
                # 永久失效：会话已摘除，立即停止本会话剩余条目的发送
                self._log_entries_result(
                    session_id, len(entries), (False, True, exc, delivered)
                )
                return False, True, exc, delivered

        all_ok = len(delivered) == len(entries)
        outcome: _SessionOutcome = (all_ok, False, last_exc, delivered)
        self._log_entries_result(session_id, len(entries), outcome)
        return outcome

    async def _build_rich_chain(
        self,
        post_id: str,
        label: str,
        link: str,
        summary_len: int = 100,
        fallback_created_at: int = 0,
    ) -> Tuple[str, MessageChain]:
        """构造一条富文本推送卡片：文字正文 +（可选）封面图。

        推送路径只通过 `get_post_full` 取结构化字段，不做图片渲染。
        Returns:
            (文字内容, MessageChain)。文字内容同时用于纯文字降级链。
        """
        wrapper = await self.mhy_client.get_post_full(post_id)
        wrapper = wrapper if isinstance(wrapper, dict) else {}
        post = wrapper.get("post")
        post = post if isinstance(post, dict) else {}

        summary = self._extract_push_summary(wrapper, post, summary_len)
        created_ts = self._extract_created_ts(post, wrapper, fallback_created_at)
        text = self._format_push_text(label, summary, created_ts, link)
        cover_url = self._extract_cover_url(wrapper, post)

        parts: list = [Plain(text)]
        if cover_url:
            try:
                parts.append(Image.fromURL(cover_url))
            except Exception as e:  # 封面坏掉不能让整条推送失败
                logger.warning(
                    f"[推送] 封面加载失败，仅发纯文字: post={post_id} "
                    f"url={cover_url} err={e}"
                )
        return text, MessageChain(chain=parts)

    def _extract_push_summary(
        self, wrapper: dict, post: dict, summary_len: int
    ) -> str:
        """按 structured_content → content → meta_content 顺序提取摘要。"""
        if summary_len <= 0:
            return ""

        raw = ""
        structured = post.get("structured_content")
        if isinstance(structured, str) and structured.strip():
            try:
                blocks = json.loads(structured)
            except Exception:
                blocks = None
            if isinstance(blocks, list):
                chunks: List[str] = []
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    insert = block.get("insert")
                    if isinstance(insert, str):
                        chunks.append(insert)
                    elif isinstance(insert, dict):
                        insert_text = insert.get("text")
                        if isinstance(insert_text, str):
                            chunks.append(insert_text)
                    # 忽略 image / 其他非文本 insert
                raw = "".join(chunks)

        if not raw:
            content = post.get("content")
            if isinstance(content, str) and content.strip():
                raw = content

        if not raw:
            meta_content = post.get("meta_content")
            if isinstance(meta_content, str) and meta_content.strip():
                raw = meta_content

        summary = self._clean_summary_text(raw)
        if not summary:
            return ""
        if len(summary) > summary_len:
            summary = summary[:summary_len].rstrip() + "…"
        return summary

    @staticmethod
    def _clean_summary_text(text: str) -> str:
        """去 HTML 标签/实体、压缩空白与连续空行，供摘要使用。"""
        if not isinstance(text, str):
            return ""
        cleaned = re.sub(r"<[^>]+>", "", text)
        cleaned = html.unescape(cleaned)
        cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
        cleaned = re.sub(r"[ \t\u3000]+", " ", cleaned)
        cleaned = re.sub(r"[ \t]*\n[ \t]*", "\n", cleaned)  # 去行首尾空白
        cleaned = re.sub(r"\n\s*\n+", "\n", cleaned)
        return cleaned.strip()

    @staticmethod
    def _extract_created_ts(post: dict, wrapper: dict, fallback: int = 0) -> int:
        """取发布时间（unix 秒）；毫秒时间戳自动降级为秒。"""
        for candidate in (post.get("created_at"), wrapper.get("created_at"), fallback):
            try:
                created = int(candidate or 0)
            except (TypeError, ValueError):
                continue
            if created > 10**12:  # 毫秒
                created //= 1000
            if created > 0:
                return created
        return 0

    @staticmethod
    def _extract_cover_url(wrapper: dict, post: dict) -> str:
        """按 cover.crop.url → cover.url → post.cover → images[0] 取封面 URL。

        只接受 http(s)，避免把相对路径/空值传给 Image.fromURL。
        """
        candidates: List[object] = []
        cover = wrapper.get("cover")
        if isinstance(cover, dict):
            crop = cover.get("crop")
            if isinstance(crop, dict):
                candidates.append(crop.get("url"))
            candidates.append(cover.get("url"))
        elif isinstance(cover, str):
            candidates.append(cover)
        post_cover = post.get("cover")
        if isinstance(post_cover, str):
            candidates.append(post_cover)
        elif isinstance(post_cover, dict):
            candidates.append(post_cover.get("url"))
        images = post.get("images")
        if isinstance(images, list) and images:
            first = images[0]
            if isinstance(first, str):
                candidates.append(first)
            elif isinstance(first, dict):
                candidates.append(first.get("url"))
        for url in candidates:
            if (
                isinstance(url, str)
                and url.strip().lower().startswith(("http://", "https://"))
            ):
                return url.strip()
        return ""

    @staticmethod
    def _format_push_text(
        label: str, summary: str, created_ts: int, link: str
    ) -> str:
        """富文本卡片的文字模板：标题、摘要、发布时间、链接，无连续空行。"""
        blocks: List[str] = [label.strip()]
        body: List[str] = []
        if summary:
            body.append(summary)
        if created_ts > 0:
            body.append(
                "🕒 " + time.strftime("%Y-%m-%d %H:%M", time.localtime(created_ts))
            )
        body.append(f"🔗 {link}")
        blocks.append("\n".join(body))
        text = "\n\n".join(block for block in blocks if block)
        return re.sub(r"\n{3,}", "\n\n", text).strip()

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
        # 先把 lastActivityDate 置为今天，保证下面 set_activity_rotation 写入的
        # date 是今天。原实现把它放在最后，写入队列时用的还是旧日期，第二天
        # get_activity_rotation 比较日期不匹配 → 轮询每天重置（原 bug）。
        self.push_data.last_activity_date = today

        # 遍历实际开启了活动预警的游戏（activityPush 的 key），不再写死 gs/sr；
        # 活动到期 API 只支持原神(gs)/崩铁(sr)，其他游戏跳过并记日志。
        for game_key in list(self.push_data.activity_push_groups.keys()):
            gid = _ACTIVITY_GAMES.get(game_key)
            if gid is None:
                cfg_gid = _CONFIG_KEY_TO_GID.get(game_key)
                game_name = GAME_MAP.get(cfg_gid, game_key) if cfg_gid else game_key
                logger.info(
                    f"[活动预警] {game_name}({game_key}) 没有可用的活动到期 API"
                    "（仅支持原神/崩铁），跳过"
                )
                continue

            groups = self.push_data.activity_push_groups.get(game_key, [])
            if not groups:
                continue

            # 轮询队列：每天只通知一个群。
            # None = 没有当日记录（从头开始）；[] = 当日已轮询完（不能重置重发）
            remaining = self.push_data.get_activity_rotation(game_key)
            if remaining is None:
                remaining = list(groups)
            if not remaining:
                logger.info(f"[活动预警轮询] {GAME_MAP[gid]} 今日已轮询完所有群")
                continue

            target_session = remaining.pop(0)
            logger.info(
                f"[活动预警轮询] {GAME_MAP[gid]} → {target_session} "
                f"(剩余 {len(remaining)} 群)"
            )

            activities = await self.mhy_client.get_activity_list(game_key)
            if not activities:
                logger.warning(f"[活动预警] {GAME_MAP[gid]} 未获取到活动列表，跳过")
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
                # 与资讯推送一致：先预热 qq_official 群 scene，再发主动消息，
                # 并显式关闭 markdown；结果日志带 session 便于排查
                await self._prime_proactive_scenes(target_session)
                chain = self._finalize_proactive_chain(
                    MessageChain(chain=[Plain("\n".join(msg_parts))])
                )
                ok, permanent, exc = await self._send_chain(target_session, chain)
                if ok:
                    logger.info(f"[活动预警] {target_session} 条数=1 发送成功")
                elif permanent:
                    logger.warning(
                        f"[活动预警] {target_session} 条数=1 发送失败"
                        "（会话永久失效，已移除订阅，不再重试）"
                    )
                else:
                    msg = f"[活动预警] {target_session} 条数=1 发送失败"
                    if isinstance(exc, BaseException):
                        logger.warning(f"{msg}：{exc}", exc_info=exc)
                    else:
                        logger.warning(msg)

            # 保存剩余队列（此时 lastActivityDate 已是今天）
            self.push_data.set_activity_rotation(game_key, remaining)

        self.push_data.save()

    # ================================================================
    #  AI 总结（链接解析 / 公告详情 / 搜索共用）
    # ================================================================

    async def _ai_summary_reply(
        self,
        prefix: str,
        post_id: str,
        link: str,
        event: Optional[AstrMessageEvent] = None,
        fallback_title: str = "",
        fallback_created: int = 0,
    ) -> str:
        """生成「标题 + AI 总结 + 发布时间 + 链接」文字回复。

        AI 失败时 summarizer 已回退文章自带摘要 + 提示，这里不向用户抛异常。
        """
        umo = self._event_umo(event) if event is not None else ""
        result = await self.summarizer.summarize(post_id, umo=umo or None)
        title = result.title or fallback_title or f"文章 {post_id}"
        created = result.created_at or fallback_created
        summary = result.text or "（暂无摘要）"

        blocks: List[str] = [f"【{prefix}·AI总结】\n{title}", summary]
        tail: List[str] = []
        if created > 0:
            tail.append(
                "🕒 " + time.strftime("%Y-%m-%d %H:%M", time.localtime(created))
            )
        tail.append(f"🔗 {link}")
        blocks.append("\n".join(tail))
        text = "\n\n".join(block for block in blocks if block)
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    # ================================================================
    #  命令: 查询资讯
    # ================================================================

    @regex(rf"^#?({GAME_REGEX})(公告|资讯|活动)$")
    async def query_news_detail(self, event: AstrMessageEvent):
        """#原神公告 — 最新一条公告/资讯的 AI 总结"""
        gid = Main._require_game(event)
        if gid is None:
            return
        news_type = Main._parse_type(event.message_str)
        async for result in self._send_news_detail(event, gid, news_type, index=0):
            yield result

    @regex(rf"^#?({GAME_REGEX})(公告|资讯|活动)(\d+)$")
    async def query_news_index(self, event: AstrMessageEvent):
        """#原神公告3 — 按序号（AI 总结）"""
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
        """#原神公告列表 — 文字列表（标题 + 时间 + 链接，不调 AI）"""
        gid = Main._require_game(event)
        if gid is None:
            return
        news_type = Main._parse_type(event.message_str)

        game_name = GAME_MAP[gid]
        type_name = TYPE_MAP[news_type]
        url_key = GAME_URL_KEY.get(gid, "ys")
        yield event.plain_result(f"正在获取 {game_name}{type_name}列表...")

        news_list = await self.mhy_client.get_news_list(
            gid,
            news_type,
            page_size=5,
        )
        if not news_list:
            yield event.plain_result(f"暂无 {game_name}{type_name}。")
            return

        lines = [f"【{game_name}{type_name}】最新:"]
        for i, item in enumerate(news_list):
            post = item.get("post") or {}
            title = post.get("subject", "无标题")
            post_id = str(post.get("post_id", ""))
            created = int(post.get("created_at", 0) or 0)
            time_str = (
                time.strftime("%m-%d %H:%M", time.localtime(created))
                if created
                else ""
            )
            link = (
                f"https://www.miyoushe.com/{url_key}/article/{post_id}"
                if post_id
                else ""
            )
            lines.append(f"{i + 1}. {title}")
            extra = " · ".join(part for part in (time_str, link) if part)
            if extra:
                lines.append(f"    {extra}")
        yield event.plain_result("\n".join(lines))

    async def _send_news_detail(
        self, event: AstrMessageEvent, gid: int, news_type: int, index: int
    ):
        game_name = GAME_MAP[gid]
        type_name = TYPE_MAP[news_type]
        url_key = GAME_URL_KEY.get(gid, "ys")

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
        created = int(post.get("created_at", 0) or 0)
        link = f"https://www.miyoushe.com/{url_key}/article/{post_id}"
        if not post_id:
            yield event.plain_result(f"【{game_name}{type_name}】\n{title}\n{link}")
            return

        yield event.plain_result("正在生成 AI 总结…")
        yield event.plain_result(
            await self._ai_summary_reply(
                f"{game_name}{type_name}", post_id, link, event, title, created
            )
        )

    # ================================================================
    #  命令: 搜索帖子（支持翻页）
    # ================================================================

    @regex(r"^#(米游社|mys)(\s+.+)$")
    async def search_posts(self, event: AstrMessageEvent):
        """#米游社 关键词      → 纯文字列表（前 10 条，不调 AI）
        #米游社 关键词 3   → 第 3 条纯文字（标题/时间/摘要/链接，不调 AI）"""
        msg = event.message_str
        m = re.search(r"^(?:#米游社|#mys)\s+(.+)", msg)
        if not m:
            yield event.plain_result("请输入搜索关键词，如 #米游社 七七")
            return

        rest = m.group(1).strip()
        # 解析尾部的数字作为序号（1 起）
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

        url_key = GAME_URL_KEY.get(2, "ys")

        if not page_match:
            # 列表模式：纯文字列表，默认展示前 10 条（不调 AI）
            max_list = 10
            lines: List[str] = [f"【米游社搜索】{keyword}"]
            for i, item in enumerate(posts[:max_list]):
                post = item.get("post") or {}
                title = post.get("subject", "无标题")
                post_id = str(post.get("post_id", ""))
                created = int(post.get("created_at", 0) or 0)
                link = (
                    f"https://www.miyoushe.com/{url_key}/article/{post_id}"
                    if post_id
                    else ""
                )
                lines.append(f"{i + 1}. {title}")
                meta: List[str] = []
                if created:
                    meta.append(
                        "🕒 "
                        + time.strftime("%Y-%m-%d %H:%M", time.localtime(created))
                    )
                if link:
                    meta.append(f"🔗 {link}")
                if meta:
                    lines.append("   " + "   ".join(meta))
            lines.append(
                f"共找到 {len(posts)} 条，发送 #米游社 {keyword} 序号 查看单条"
            )
            yield event.plain_result("\n".join(lines))
            return

        if page >= len(posts):
            yield event.plain_result(f"搜索结果只有 {len(posts)} 条，序号超出范围。")
            return

        # 单条模式：纯文字（标题 + 时间 + 作者/板块 + 文章自带摘要 + 链接），不调 AI
        item = posts[page]
        post = item.get("post") or {}
        post_id = str(post.get("post_id", ""))
        title = post.get("subject", "无标题")
        created = int(post.get("created_at", 0) or 0)
        link = f"https://www.miyoushe.com/{url_key}/article/{post_id}"
        if not post_id:
            yield event.plain_result(f"【米游社搜索】\n{title}\n{link}")
            return

        wrapper: dict = {}
        try:
            fetched = await self.mhy_client.get_post_full(post_id)
            wrapper = fetched if isinstance(fetched, dict) else {}
        except Exception as e:
            logger.warning(f"[搜索] 获取文章详情失败: post={post_id} err={e}")
        full_post = wrapper.get("post")
        full_post = full_post if isinstance(full_post, dict) else post

        full_created = int(full_post.get("created_at", 0) or 0)
        if full_created > 10**12:  # 毫秒
            full_created //= 1000
        if full_created > 0:
            created = full_created

        summary = extract_fallback_summary(
            wrapper, full_post, _SEARCH_ITEM_SUMMARY_CHARS
        )
        author = ""
        user = wrapper.get("user")
        if isinstance(user, dict):
            author = str(user.get("nickname") or "").strip()
        forum = ""
        forum_obj = wrapper.get("forum")
        if isinstance(forum_obj, dict):
            forum = str(forum_obj.get("forum_name") or "").strip()

        blocks: List[str] = [f"【米游社搜索】\n{title}"]
        meta_lines: List[str] = []
        if created:
            meta_lines.append(
                "🕒 " + time.strftime("%Y-%m-%d %H:%M", time.localtime(created))
            )
        if forum:
            meta_lines.append(f"📂 {forum}")
        if author:
            meta_lines.append(f"👤 {author}")
        meta_lines.append(f"🔗 {link}")
        blocks.append("\n".join(meta_lines))
        if summary:
            blocks.append(summary)
        text = "\n\n".join(block for block in blocks if block)
        yield event.plain_result(re.sub(r"\n{3,}", "\n\n", text).strip())

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
        link = f"https://www.miyoushe.com/{game_short}/article/{post_id}"

        yield event.plain_result("正在生成 AI 总结…")
        yield event.plain_result(
            await self._ai_summary_reply("米游社", post_id, link, event)
        )

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
        session_id = self._event_umo(event)
        if not session_id:
            yield event.plain_result("无法获取会话标识，推送订阅未保存。")
            return
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
        session_id = self._event_umo(event)
        if not session_id:
            yield event.plain_result("无法获取会话标识，推送订阅未保存。")
            return
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
            "查询 (需指定游戏名，AI 总结):\n"
            "· #原神公告   · #崩铁资讯\n"
            "· #绝区零活动 · #原神公告3\n"
            "· #原神公告列表 · #崩铁资讯列表（文字列表，不调 AI）\n"
            "· #米游社 关键词（纯文字列表，前10条）\n"
            "· #米游社 关键词 3（单条纯文字，不调 AI）\n"
            "· 发送文章链接 → AI 总结\n\n"
            "推送设置 (群管理员):\n"
            "· #开启/关闭原神公告推送\n"
            "· #开启/关闭崩铁资讯推送\n"
            "· #开启/关闭原神活动预警\n\n"
            "手动推送 (Bot管理员):\n"
            "· #推送原神公告\n"
            "· #推送崩铁资讯\n\n"
            "定时每5分钟检查一次，推送为「文字+封面」富文本卡片；\n"
            "链接解析/公告详情使用 AI 总结（已移除图片渲染，需配置视觉模型）；\n"
            "#米游社 搜索为纯文字列表，不调 AI。"
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

    @staticmethod
    def _event_umo(event: AstrMessageEvent) -> str:
        """推送订阅统一存 unified_msg_origin（platform_id:MessageType:session_id）。

        AstrBot v4 的 event.get_session_id() 只返回裸 session_id（qq_official 下
        就是 group_openid），用它订阅后 context.send_message 会抛
        “不合法的 session 字符串: not enough values to unpack (expected 3, got 1)”。
        """
        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            logger.error("event 缺少 unified_msg_origin，无法保存推送订阅")
        return umo

    @staticmethod
    def _dedup_key(session_id: str, post_id: str) -> str:
        """去重键按「会话 + post_id」维度。

        这样某个会话发送失败时不会因为其他会话发送成功而被标记已发送，
        失败会话下一轮仍可重试，成功会话也不会收到重复推送。
        """
        return f"{session_id}|{post_id}"

    def _collect_platform_infos(self) -> List[Tuple[str, str]]:
        """收集已加载平台的 (platform_id, platform_name)，供旧订阅数据迁移使用。

        platform_manager 缺失或结构变化时返回空列表（不影响插件主体功能）。
        """
        infos: List[Tuple[str, str]] = []
        try:
            manager = getattr(self.context, "platform_manager", None)
            insts = getattr(manager, "platform_insts", None) or []
            for inst in insts:
                try:
                    meta = inst.meta()
                except Exception:
                    continue
                pid = getattr(meta, "id", "") or ""
                pname = getattr(meta, "name", "") or ""
                if pid and pname:
                    infos.append((str(pid), str(pname)))
        except Exception as e:
            logger.warning(f"获取平台列表失败，跳过旧订阅 session 迁移: {e}")
        return infos

    def _get_ban_re(self, config_key: str) -> Optional[re.Pattern]:
        words = self.push_data.get_ban_word_override(
            config_key
        ) or DEFAULT_BAN_WORDS.get(config_key, "")
        if not words:
            return None
        return re.compile(words)
