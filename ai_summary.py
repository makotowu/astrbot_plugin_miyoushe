"""AI 文章总结 —— 用支持视觉的 LLM 总结米游社文章（替代已移除的 T2I 渲染）

命令侧（链接解析 / 公告详情 / 帖子搜索）调用 `PostSummarizer.summarize()`：
它只读 `MhyClient.get_post_full()` 的数据，把标题、发布时间、正文（必要时配图）交给
LLM 生成中文总结；任何失败都不抛给用户，而是回退为文章自带摘要 + 一行提示。
"""

import asyncio
import html
import json
import re
import time
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from astrbot.api import logger

# 带图调用失败（模型不支持图片输入）后的固定提示，仅在视觉降级时追加
VISION_FALLBACK_HINT = (
    "（当前模型不支持图片输入，已仅按文字总结；"
    "请在 WebUI 选择支持视觉的模型以总结配图）"
)

# 其它失败（无 provider / 超时 / 总结为空 / 上游异常）的统一提示
SUMMARY_FALLBACK_HINT = "（AI 总结暂不可用，以下为文章自带摘要）"

# 未开启 AI 总结时的提示
DISABLED_HINT = "（AI 总结已关闭，以下为文章自带摘要）"

# 单次总结整体超时（秒），含「带图失败 → 纯文本重试」在内的整个流程
_TIMEOUT_SECS = 90.0

# AI 失败时回退展示的文章自带摘要最大字数
_FALLBACK_SUMMARY_CHARS = 500

_SYSTEM_PROMPT = (
    "你是米游社资讯总结助手。只输出最终总结，不要输出思考过程。"
    "用中文输出 3-6 条要点，每条一行，保留活动时间/奖励/卡池/版本等关键数字与日期；"
    "不要编造或补充原文没有的信息。"
)


class _TextChatFailed(Exception):
    """视觉降级后的纯文本调用仍失败（用于和普通异常区分日志路径）。"""


def _err_text(exc: object) -> str:
    """异常文本截断（日志用，避免超长堆栈信息刷屏）。"""
    return str(exc)[:200]


@dataclass
class SummaryResult:
    """一次 AI 总结的最终结果。"""

    text: str = ""
    """最终展示文本：AI 总结正文，或「文章自带摘要 + 提示」。"""
    used_images: int = 0
    """实际传给模型的配图数量（0 = 纯文本调用）。"""
    vision_fallback: bool = False
    """是否发生了「带图调用失败 → 纯文本重试」的视觉降级。"""
    error: str = ""
    """失败原因（空字符串表示成功）。"""
    title: str = ""
    """文章标题（供命令模板使用，失败时可能为空）。"""
    created_at: int = 0
    """发布时间 unix 秒（供命令模板使用，失败时为 0）。"""


def clean_text(text: object) -> str:
    """去 HTML 标签/实体、压缩空白与连续空行。"""
    if not isinstance(text, str):
        return ""
    cleaned = re.sub(r"<[^>]+>", "", text)
    cleaned = html.unescape(cleaned)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"[ \t\u3000]+", " ", cleaned)
    cleaned = re.sub(r"[ \t]*\n[ \t]*", "\n", cleaned)
    cleaned = re.sub(r"\n\s*\n+", "\n", cleaned)
    return cleaned.strip()


def extract_created_ts(post: dict, wrapper: Optional[dict] = None) -> int:
    """取发布时间（unix 秒）；毫秒时间戳自动降级为秒。"""
    wrapper = wrapper or {}
    for candidate in (post.get("created_at"), wrapper.get("created_at")):
        try:
            created = int(candidate or 0)
        except (TypeError, ValueError):
            continue
        if created > 10**12:  # 毫秒
            created //= 1000
        if created > 0:
            return created
    return 0


def extract_body(wrapper: dict, post: dict) -> str:
    """提取文章正文全文（不做字数截断）：structured_content → content(HTML) → meta_content。"""
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
                # image 等非文本 insert 忽略
            raw = "".join(chunks)

    if not raw:
        content = post.get("content")
        if isinstance(content, str) and content.strip():
            raw = content

    if not raw:
        meta_content = post.get("meta_content")
        if isinstance(meta_content, str) and meta_content.strip():
            raw = meta_content

    body = clean_text(raw)
    return body


def extract_fallback_summary(
    wrapper: dict, post: dict, limit: int = _FALLBACK_SUMMARY_CHARS
) -> str:
    """AI 失败时回退展示的文章自带摘要（meta_content → content → 正文）。"""
    summary = ""
    meta_content = post.get("meta_content")
    if isinstance(meta_content, str) and meta_content.strip():
        summary = clean_text(meta_content)
    if not summary:
        content = post.get("content")
        if isinstance(content, str) and content.strip():
            summary = clean_text(content)
    if not summary:
        summary = extract_body(wrapper, post)
    if limit > 0 and len(summary) > limit:
        summary = summary[:limit].rstrip() + "…"
    return summary


def extract_image_urls(wrapper: dict, post: dict) -> List[str]:
    """取配图 URL（不做数量截断）：cover.url 优先，其次 post.images、wrapper.image_list。

    只保留 http(s)，按出现顺序去重；文章有多少张图就返回多少张。
    """
    urls: List[str] = []

    def _add(value: object) -> None:
        if not isinstance(value, str):
            return
        url = value.strip()
        if not url.lower().startswith(("http://", "https://")):
            return
        if url not in urls:
            urls.append(url)

    cover = wrapper.get("cover")
    if isinstance(cover, dict):
        _add(cover.get("url"))
    for item in post.get("images") or []:
        if isinstance(item, dict):
            _add(item.get("url"))
        else:
            _add(item)
    for item in wrapper.get("image_list") or []:
        if isinstance(item, dict):
            _add(item.get("url"))
        else:
            _add(item)
    return urls


class PostSummarizer:
    """用 LLM 总结米游社文章；失败自动回退文章自带摘要，绝不向命令层抛异常。"""

    def __init__(self, context: Any, cfg: Any, mhy_client: Any = None) -> None:
        self.context = context
        self.cfg = cfg
        self.mhy_client = mhy_client

    async def summarize(self, post_id: str, umo: Optional[str] = None) -> SummaryResult:
        """总结一篇文章。

        Args:
            post_id: 米游社文章 id。
            umo: unified_msg_origin，用于取「当前会话默认模型」；可为 None。
        """
        if self.mhy_client is None:
            return SummaryResult(text=SUMMARY_FALLBACK_HINT, error="no_mhy_client")

        try:
            wrapper = await self.mhy_client.get_post_full(post_id)
        except Exception as e:
            logger.warning(f"[AI总结] 获取文章失败: post={post_id} err={e}")
            wrapper = None
        if not isinstance(wrapper, dict):
            return SummaryResult(text=SUMMARY_FALLBACK_HINT, error="fetch_failed")

        post = wrapper.get("post")
        post = post if isinstance(post, dict) else {}
        title = str(post.get("subject") or "").strip()
        created_at = extract_created_ts(post, wrapper)
        result = SummaryResult(title=title, created_at=created_at)

        if not self._enabled():
            result.text = self._fallback_text(wrapper, post, "disabled")
            result.error = "disabled"
            return result

        # 正文不设上限：提取到的全文直接交给模型（用户要求“无上限”）
        body = extract_body(wrapper, post)
        if not body:
            logger.warning(f"[AI总结] 文章正文为空: post={post_id}")
            result.text = self._fallback_text(wrapper, post, "empty_article")
            result.error = "empty_article"
            return result

        provider, provider_error = self._get_provider(umo)
        if provider is None:
            logger.warning(
                f"[AI总结] 无可用 LLM Provider: post={post_id} reason={provider_error}"
            )
            result.text = self._fallback_text(wrapper, post, "no_provider")
            result.error = "no_provider"
            return result

        provider_label = self._provider_label(provider)
        # 配图不设上限：文章里的图全部传给视觉模型（用户要求“无上限”）
        image_urls = extract_image_urls(wrapper, post)
        prompt = self._build_prompt(title, created_at, body)

        try:
            text, used_images, vision_fallback = await asyncio.wait_for(
                self._chat(provider, prompt, image_urls, post_id, provider_label),
                timeout=_TIMEOUT_SECS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"[AI总结] 超时(timeout, {_TIMEOUT_SECS:g}s): post={post_id} "
                f"provider={provider_label}"
            )
            result.text = self._fallback_text(wrapper, post, "timeout")
            result.error = "timeout"
            return result
        except _TextChatFailed as e:
            logger.warning(
                f"[AI总结] 纯文本总结失败(text_failed): post={post_id} "
                f"provider={provider_label} err={_err_text(e)}"
            )
            result.text = self._fallback_text(wrapper, post, "text_failed")
            result.error = "text_failed"
            return result
        except Exception as e:
            logger.warning(
                f"[AI总结] 调用失败: post={post_id} provider={provider_label} "
                f"err={_err_text(e)}"
            )
            result.text = self._fallback_text(wrapper, post, "llm_error")
            result.error = str(e)[:200] or "llm_error"
            return result

        # 推理模型可能只产出 reasoning_content 而 completion_text 为空：
        # 这种情况不能拿思考过程当总结，按失败回退（见 FIX_NOTES §7 风险）。
        text = (text or "").strip()
        if not text:
            logger.warning(
                f"[AI总结] 模型返回空总结(empty_summary): post={post_id} "
                f"provider={provider_label} used_images={used_images}"
            )
            result.text = self._fallback_text(wrapper, post, "empty_summary")
            result.error = "empty_summary"
            return result

        result.text = clean_text(text)
        if not result.text:
            logger.warning(
                f"[AI总结] 模型返回空总结(empty_summary): post={post_id} "
                f"provider={provider_label}"
            )
            result.text = self._fallback_text(wrapper, post, "empty_summary")
            result.error = "empty_summary"
            return result

        result.used_images = used_images
        result.vision_fallback = vision_fallback
        if vision_fallback:
            result.text = f"{result.text}\n\n{VISION_FALLBACK_HINT}"
        return result

    # ==================== 内部实现 ====================

    def _enabled(self) -> bool:
        try:
            return bool(self.cfg.get("ai_summary_enable", True))
        except Exception:
            return True

    def _get_provider(self, umo: Optional[str]) -> Tuple[Any, str]:
        """按配置选择 Provider；返回 (provider 或 None, 失败原因)。"""
        provider_id = str(self.cfg.get("ai_summary_provider_id", "") or "").strip()
        try:
            if provider_id:
                provider = self.context.get_provider_by_id(provider_id)
                if provider is None:
                    return None, f"provider_id_not_found:{provider_id}"
                return provider, ""
            provider = self.context.get_using_provider(umo)
            if provider is None:
                return None, "no_default_provider"
            return provider, ""
        except Exception as e:
            logger.warning(f"[AI总结] 获取 Provider 异常: {e}")
            return None, f"provider_error:{e}"

    async def _chat(
        self,
        provider: Any,
        prompt: str,
        image_urls: List[str],
        post_id: str,
        provider_label: str,
    ) -> Tuple[str, int, bool]:
        """先带图调用，带图失败则降级纯文本重试。

        Returns:
            (模型输出文本, 实际使用图片数, 是否发生视觉降级)

        Raises:
            _TextChatFailed: 视觉降级后的纯文本调用仍失败（与普通异常区分日志）。
        """
        if image_urls:
            try:
                resp = await provider.text_chat(
                    prompt=prompt,
                    image_urls=list(image_urls),
                    system_prompt=_SYSTEM_PROMPT,
                )
                return self._response_text(resp), len(image_urls), False
            except Exception as e:
                # 模型不支持图片输入时上游会抛异常 → 退化为纯文本总结
                logger.warning(
                    f"[AI总结] 带图调用失败，降级纯文本(vision_fallback): "
                    f"post={post_id} provider={provider_label} "
                    f"images={len(image_urls)} err={_err_text(e)}"
                )

        try:
            resp = await provider.text_chat(
                prompt=prompt,
                image_urls=None,
                system_prompt=_SYSTEM_PROMPT,
            )
        except Exception as e:
            raise _TextChatFailed(_err_text(e)) from e
        return self._response_text(resp), 0, bool(image_urls)

    @staticmethod
    def _response_text(resp: Any) -> str:
        text = getattr(resp, "completion_text", None)
        return str(text) if text is not None else ""

    @staticmethod
    def _provider_label(provider: Any) -> str:
        """用于日志的 provider 标识（id/model），取不到时用 ?。"""
        provider_id = ""
        try:
            provider_config = getattr(provider, "provider_config", None)
            if isinstance(provider_config, dict):
                provider_id = str(provider_config.get("id") or "")
        except Exception:
            provider_id = ""
        model_name = str(getattr(provider, "model_name", "") or "")
        return f"id={provider_id or '?'}/model={model_name or '?'}"

    @staticmethod
    def _build_prompt(title: str, created_at: int, body: str) -> str:
        lines: List[str] = ["请总结以下米游社文章：", ""]
        if title:
            lines.append(f"标题：{title}")
        if created_at > 0:
            lines.append(
                "发布时间："
                + time.strftime("%Y-%m-%d %H:%M", time.localtime(created_at))
            )
        lines.extend(["", "正文：", body])
        return "\n".join(lines)

    def _fallback_text(self, wrapper: dict, post: dict, reason: str) -> str:
        summary = extract_fallback_summary(wrapper, post, _FALLBACK_SUMMARY_CHARS)
        hint = DISABLED_HINT if reason == "disabled" else SUMMARY_FALLBACK_HINT
        if summary:
            return f"{summary}\n\n{hint}"
        return hint
