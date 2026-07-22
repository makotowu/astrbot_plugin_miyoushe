"""米游社资讯图片渲染器 —— 复用 Yunzai 原版 HTML/CSS 模板"""

import asyncio
import base64
import html as html_mod
import io
import json
import os
import re
import time as _time
from collections import OrderedDict
from typing import Dict, List, Optional

import qrcode as qrcode_lib
from astrbot.api import logger

from .constants import GAME_URL_KEY

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")


def _read_file(*parts: str) -> bytes:
    path = os.path.join(ASSETS_DIR, *parts)
    with open(path, "rb") as f:
        return f.read()


def _read_text(*parts: str) -> str:
    path = os.path.join(ASSETS_DIR, *parts)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _b64(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def _inline_template(html_path: str, css_path: str) -> str:
    """加载 HTML 模板并将外部 CSS 内联为 <style> 标签，
    CSS 中引用的字体/图片同时转为 base64 data URI。
    同时将 <img src="{{pluResPath}}..."> 也转为 base64。"""
    html = _read_text(html_path)
    css = _read_text(css_path)

    # CSS 内联字体/图片
    css_dir = os.path.dirname(os.path.join(ASSETS_DIR, css_path))

    def _inline_css_url(match):
        url = match.group(1)
        resolved = os.path.normpath(os.path.join(css_dir, url))
        if not os.path.exists(resolved):
            return match.group(0)
        ext = os.path.splitext(url)[1].lower()
        mime_map = {
            ".woff2": "font/woff2",
            ".woff": "font/woff",
            ".ttf": "font/ttf",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".svg": "image/svg+xml",
        }
        mime = mime_map.get(ext, "application/octet-stream")
        data = _read_file(os.path.relpath(resolved, ASSETS_DIR).replace("\\", "/"))
        return f'url("{_b64(data, mime)}")'

    css = re.sub(r"""url\(["']?([^"')]+)["']?\)""", _inline_css_url, css)

    # 替换 <link> 为 <style>
    html = re.sub(
        r'<link[^>]*href=["\']([^"\']+\.css)["\'][^>]*>',
        f"<style>\n{css}\n</style>",
        html,
        count=1,
    )

    # 内联 <img src="{{pluResPath}}..."> 为 base64
    def _inline_img(match):
        img_path = match.group(1)  # html/mysNews/mys.png
        resolved = os.path.normpath(os.path.join(ASSETS_DIR, img_path))
        if os.path.exists(resolved):
            data = _read_file(img_path.replace("\\", "/"))
            return f'src="{_b64(data, "image/png")}"'
        return match.group(0)

    html = re.sub(
        r'src=["\']\{\{pluResPath\}\}([^"\'\\]+)["\']',
        _inline_img,
        html,
    )

    # 清理 pluResPath（不再需要）
    html = html.replace("{{pluResPath}}", "")

    return html


# 启动时加载并内联模板
_TMPL_DETAIL = _inline_template(
    "html/mysNews/mysNews.html",
    "html/mysNews/mysNews.css",
)
_TMPL_LIST = _inline_template(
    "html/mysNews-list/mysNews-list.html",
    "html/mysNews-list/mysNews-list.css",
)


def _generate_qrcode_base64(url: str) -> str:
    """生成二维码的 base64 data URI"""
    img = qrcode_lib.make(url, border=1, box_size=5)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    b64 = base64.b64encode(buf.read()).decode()
    return f"data:image/png;base64,{b64}"


class Renderer:
    """负责获取文章详情并渲染为图片（复用 Yunzai 原版模板）"""

    def __init__(self, star_instance, mhy_client):
        self.star = star_instance
        self.mhy_client = mhy_client
        self._emoticon_cache: Dict[int, Dict[str, str]] = {}
        self._render_cache: "OrderedDict[str, List[str]]" = OrderedDict()
        self._render_cache_max = 32
        self._article_meta: Dict[str, dict] = {}

    # ==================== 公开接口 ====================

    async def render_news_detail(
        self, post_id: str, gid: int, ultra: bool = True
    ) -> List[str]:
        cache_key = f"{post_id}:{'u' if ultra else 'n'}"
        cached = self._render_cache.get(cache_key)
        if cached:
            return cached

        post = await self.mhy_client.get_post_full(post_id)
        if not post:
            return []

        data, content = await self._process_post(post, gid)

        # 缓存文章元数据（封面图、描述），供兜底发送使用
        post_data = post.get("post", {})
        images = post_data.get("images", [])
        raw_content = post_data.get("content", "")
        desc = re.sub(r"<[^>]+>", "", raw_content)[:120].strip()
        self._article_meta[post_id] = {
            "cover_url": images[0] if images else "",
            "description": desc,
        }

        game_key = GAME_URL_KEY.get(gid, "ys")
        qr_url = f"https://bbs.mihoyo.com/{game_key}/article/{post_id}"
        qrcode_b64 = _generate_qrcode_base64(qr_url)

        params = {
            "data": data,
            "dataConent": content,
            "qrcodeImg": qrcode_b64,
        }
        img_path = await self._do_render(_TMPL_DETAIL, params, ultra=ultra)
        if img_path:
            self._cache_set(cache_key, [img_path])
            return [img_path]
        return []

    def get_article_meta(self, post_id: str) -> dict:
        """获取已缓存的文章元数据（cover_url, description）"""
        return self._article_meta.get(post_id, {})

    async def render_news_list(
        self, news_list: list, game: str, type_name: str
    ) -> List[str]:
        data = []
        for item in news_list:
            copied = json.loads(json.dumps(item))
            post = copied.get("post") or {}
            ts = post.get("created_at", 0)
            if ts:
                post["created_at"] = _time.strftime(
                    "%Y-%m-%d %H:%M", _time.localtime(ts)
                )
            data.append(copied)

        params = {
            "game": game,
            "typeName": type_name,
            "data": data,
        }
        img = await self._do_render(_TMPL_LIST, params)
        return [img] if img else []

    def _cache_set(self, post_id: str, img_paths: List[str]) -> None:
        if len(self._render_cache) >= self._render_cache_max:
            self._render_cache.popitem(last=False)
        self._render_cache[post_id] = img_paths

    # ==================== 数据处理 ====================

    async def _process_post(self, post: dict, gid: int):
        post_data = post.get("post", {})
        user = post.get("user", {})
        stat = post.get("stat", {})
        forum = post.get("forum", {})
        topics = post.get("topics") or []

        content = post_data.get("content", "")

        try:
            content_json = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            content_json = None

        if isinstance(content_json, dict) and content_json.get("imgs"):
            parts = []
            for url in content_json["imgs"]:
                cdn_url = url + (
                    "?x-oss-process=image//resize,s_600/quality,q_80/"
                    "auto-orient,0/interlace,1/format,png"
                )
                parts.append(f'<div class="ql-image-box"><img src="{cdn_url}"></div>')
            content = "\n".join(parts)
        else:
            for img_url in post_data.get("images", []):
                cdn_url = img_url + (
                    "?x-oss-process=image//resize,s_600/quality,q_80/"
                    "auto-orient,0/interlace,1/format,jpg"
                )
                content = content.replace(img_url, cdn_url)
            content = await self._replace_emoticons(content, gid)
            content = html_mod.unescape(content)

        for key in ("view_num", "like_num", "reply_num", "bookmark_num"):
            val = stat.get(key, 0)
            if isinstance(val, (int, float)) and val > 10000:
                stat[key] = f"{val / 10000:.2f}万"
            else:
                stat[key] = str(val)

        created_at = post_data.get("created_at", 0)
        created_time = (
            _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(created_at))
            if created_at > 0
            else ""
        )

        data = {
            "user": {
                "avatar_url": user.get("avatar_url", ""),
                "nickname": user.get("nickname", ""),
                "level_exp": {"level": user.get("level_exp", {}).get("level", 1)},
                "certification": {
                    "type": user.get("certification", {}).get("type", 0),
                    "label": user.get("certification", {}).get("label", ""),
                },
            },
            "post": {
                "subject": post_data.get("subject", ""),
                "post_id": str(post_data.get("post_id", "")),
                "images": post_data.get("images", []),
                "created_at": created_at,
                "created_time": created_time,
            },
            "stat": stat,
            "forum": {"name": forum.get("name", "")},
            "topics": [{"name": t.get("name", "")} for t in topics if t.get("name")],
        }

        return data, content

    # ==================== 表情替换 ====================

    async def _replace_emoticons(self, content: str, gid: int) -> str:
        if gid not in self._emoticon_cache:
            self._emoticon_cache[gid] = await self._fetch_emoticons(gid)

        emoticons = self._emoticon_cache[gid]
        if not emoticons:
            return content

        def _replace(match):
            name = match.group(1)
            if name in emoticons:
                return f'<img class="emoticon-image" src="{emoticons[name]}"/>'
            return ""

        return re.sub(r"_\(([^)]*)\)", _replace, content)

    @staticmethod
    async def _fetch_emoticons(gid: int) -> Dict[str, str]:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://bbs-api.miyoushe.com/misc/api/emoticon_set",
                    params={"gids": str(gid)},
                    headers={
                        "Referer": "https://www.miyoushe.com/",
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
                        ),
                    },
                )
                data = resp.json()
                if data.get("retcode") != 0:
                    return {}
                result = {}
                for group in data.get("data", {}).get("list", []):
                    for item in group.get("list", []):
                        if item.get("icon") and item.get("name"):
                            result[item["name"]] = item["icon"]
                return result
        except Exception as e:
            logger.warning(f"获取米游社表情包失败 (gid={gid}): {e}")
            return {}

    # ==================== 渲染执行 ====================

    async def _do_render(
        self, tmpl: str, params: dict, ultra: bool = True
    ) -> Optional[str]:
        options = {
            "full_page": True,
            "type": "jpeg",
            "quality": 50,
            "scale": "device",
        }
        if ultra:
            options["device_scale_factor_level"] = "ultra"

        for attempt in range(1, 4):
            try:
                result = await self.star.html_render(
                    tmpl=tmpl,
                    data=params,
                    return_url=False,
                    options=options,
                )
                if result and os.path.exists(result) and os.path.getsize(result) > 4096:
                    return result
                logger.warning(
                    f"渲染结果异常 (尝试 {attempt}): "
                    f"size={os.path.getsize(result) if result else 'None'}"
                )
            except Exception as e:
                logger.error(f"渲染失败 (尝试 {attempt}): {e}")

            if attempt < 3:
                await asyncio.sleep(2)

        return None
