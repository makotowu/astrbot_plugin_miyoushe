"""米游社 BBS API 客户端 —— 只读接口，无需登录"""

from typing import Any, Dict, List, Optional

import httpx
from astrbot.api import logger

# ========== API 地址 ==========
API_GET_NEWS_LIST = "https://bbs-api-static.miyoushe.com/painter/wapi/getNewsList"
API_GET_POST_FULL = "https://bbs-api.miyoushe.com/post/wapi/getPostFull"
API_SEARCH_POSTS = "https://bbs-api.miyoushe.com/post/wapi/searchPosts"

# ========== 游戏活动公告 API（独立于 BBS） ==========
API_GS_ACTIVITY = (
    "https://hk4e-api.mihoyo.com/common/hk4e_cn/announcement/api/getAnnList"
    "?game=hk4e&game_biz=hk4e_cn&lang=zh-cn&bundle_id=hk4e_cn"
    "&platform=pc&region=cn_gf01&level=55&uid=100000000"
)
API_SR_ACTIVITY = (
    "https://hkrpg-api.mihoyo.com/common/hkrpg_cn/announcement/api/getAnnList"
    "?game=hkrpg&game_biz=hkrpg_cn&lang=zh-cn&auth_appid=announcement"
    "&authkey_ver=1&bundle_id=hkrpg_cn&channel_id=1&level=65"
    "&platform=pc&region=prod_gf_cn&sdk_presentation_style=fullscreen"
    "&sdk_screen_transparent=true&sign_type=2&uid=100000000"
)


def _build_headers() -> Dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.miyoushe.com/",
    }


class MhyClient:
    """与米游社公开 API 交互（只读，无需 Cookie）"""

    def __init__(self, proxy: Optional[str] = None):
        self._proxy = proxy
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            kwargs: Dict[str, Any] = {"timeout": 15}
            if self._proxy:
                kwargs["proxies"] = {"all://": self._proxy}
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ==================== 资讯列表 ====================

    async def get_news_list(
        self,
        gids: int,
        news_type: int,
        page_size: int = 10,
    ) -> Optional[List[Dict[str, Any]]]:
        """获取官方资讯列表（公告/活动/资讯）

        Args:
            gids: 游戏ID (1=崩坏三, 2=原神, 3=崩坏二, 4=未定, 6=崩铁, 8=绝区零)
            news_type: 1=公告, 2=活动, 3=资讯
            page_size: 返回条数
        """
        params = {
            "gids": str(gids),
            "type": str(news_type),
            "page_size": str(page_size),
        }
        try:
            client = await self._get_client()
            resp = await client.get(
                API_GET_NEWS_LIST, params=params, headers=_build_headers()
            )
            data = resp.json()
            if data.get("retcode") != 0:
                logger.warning(
                    f"获取资讯列表失败 (gids={gids}, type={news_type}): "
                    f"{data.get('message')}"
                )
                return None
            return data.get("data", {}).get("list", [])
        except Exception as e:
            logger.warning(f"获取资讯列表异常 (gids={gids}, type={news_type}): {e}")
            return None

    # ==================== 文章详情 ====================

    async def get_post_full(self, post_id: str) -> Optional[Dict[str, Any]]:
        """获取文章详情"""
        params = {"post_id": post_id}
        try:
            client = await self._get_client()
            resp = await client.get(
                API_GET_POST_FULL, params=params, headers=_build_headers()
            )
            data = resp.json()
            if data.get("retcode") != 0:
                logger.warning(
                    f"获取文章详情失败 (post_id={post_id}): {data.get('message')}"
                )
                return None
            return data.get("data", {}).get("post", {})
        except Exception as e:
            logger.warning(f"获取文章详情异常 (post_id={post_id}): {e}")
            return None

    # ==================== 帖子搜索 ====================

    async def search_posts(
        self, keyword: str, gids: int = 2, page_size: int = 20
    ) -> Optional[List[Dict[str, Any]]]:
        """搜索米游社帖子"""
        params = {
            "gids": str(gids),
            "size": str(page_size),
            "keyword": keyword,
        }
        try:
            client = await self._get_client()
            resp = await client.get(
                API_SEARCH_POSTS, params=params, headers=_build_headers()
            )
            data = resp.json()
            if data.get("retcode") != 0:
                logger.warning(f"搜索帖子失败: {data.get('message')}")
                return None
            return data.get("data", {}).get("posts", [])
        except Exception as e:
            logger.warning(f"搜索帖子异常 (keyword={keyword}): {e}")
            return None

    # ==================== 活动到期查询 ====================

    async def get_activity_list(self, game_key: str) -> List[Dict[str, Any]]:
        """获取游戏服务器公告中的活动列表（用于到期预警）

        Args:
            game_key: 'gs' 或 'sr'
        Returns:
            [{title, end_time, banner}, ...]  其中 end_time 是 Unix 时间戳（秒）
        """
        if game_key == "gs":
            api = API_GS_ACTIVITY
        elif game_key == "sr":
            api = API_SR_ACTIVITY
        else:
            return []

        try:
            client = await self._get_client()
            resp = await client.get(api, headers=_build_headers())
            data = resp.json()

            result: List[Dict[str, Any]] = []

            if game_key == "gs":
                # 原神: data.list[1].list 是活动列表
                sections = data.get("data", {}).get("list", [])
                if len(sections) < 2:
                    return result
                for item in sections[1].get("list", []):
                    tag = item.get("tag_label", "")
                    title = item.get("title", "")
                    if (
                        "活动" in tag
                        and "传说任务" not in title
                        and "游戏公告" not in title
                    ):
                        end_str = item.get("end_time", "")
                        result.append(
                            {
                                "title": item.get("subtitle", title),
                                "end_time": self._parse_time(end_str),
                                "banner": item.get("banner", ""),
                            }
                        )
            elif game_key == "sr":
                # 崩铁: data.pic_list[0].type_list[0].list
                pic_list = data.get("data", {}).get("pic_list", [])
                if not pic_list:
                    return result
                type_list = pic_list[0].get("type_list", [])
                if not type_list:
                    return result
                for item in type_list[0].get("list", []):
                    if item.get("title"):
                        end_str = item.get("end_time", "")
                        result.append(
                            {
                                "title": item.get("title", ""),
                                "end_time": self._parse_time(end_str),
                                "banner": item.get("img", ""),
                            }
                        )

            return result
        except Exception as e:
            logger.warning(f"获取活动列表异常 (game={game_key}): {e}")
            return []

    @staticmethod
    def _parse_time(time_str: str) -> float:
        """解析时间字符串为 Unix 时间戳"""
        import time as _time

        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return _time.mktime(_time.strptime(time_str, fmt))
            except ValueError:
                continue
        return 0
