"""推送数据持久化 —— push_data.json 管理"""

import json
import os
from typing import Dict, List, Optional

from astrbot.api import logger
from astrbot.api.star import StarTools


class PushDataStore:
    """管理米游社推送配置与状态的 JSON 文件读写"""

    def __init__(self) -> None:
        self._data_dir = os.path.join(
            StarTools.get_data_dir(plugin_name="astrbot_plugin_miyoushe"),
        )
        os.makedirs(self._data_dir, exist_ok=True)
        self._file = os.path.join(self._data_dir, "push_data.json")
        self._data = self._load()

    # ==================== 属性访问 ====================

    @property
    def push_groups(self) -> Dict[str, List[str]]:
        """{config_key: [session_id, ...]}"""
        return self._data["pushGroups"]

    @property
    def activity_push_groups(self) -> Dict[str, List[str]]:
        """{"gs": [...], "sr": [...]}"""
        return self._data["activityPush"]

    @property
    def last_activity_date(self) -> str:
        return self._data.get("lastActivityDate", "")

    @last_activity_date.setter
    def last_activity_date(self, value: str) -> None:
        self._data["lastActivityDate"] = value

    # ==================== 活动预警轮询队列 ====================

    def get_activity_rotation(self, game_key: str) -> Optional[List[str]]:
        """获取活动预警轮询剩余队列，当日已处理完返回 None"""
        date = self.last_activity_date
        if not date:
            return None
        key = f"activityRotation_{game_key}"
        rot = self._data.setdefault("activityRotations", {}).get(key)
        if not rot:
            return None
        # 检查是否是今天的数据
        if rot.get("date") != date:
            return None
        return rot.get("remaining")

    def set_activity_rotation(self, game_key: str, remaining: List[str]) -> None:
        """设置活动预警轮询剩余队列"""
        date = self.last_activity_date
        if not date:
            return
        key = f"activityRotation_{game_key}"
        self._data.setdefault("activityRotations", {})[key] = {
            "date": date,
            "remaining": remaining,
        }

    def get_ban_word_override(self, game_key: str) -> str:
        return self._data.get("banWordOverride", {}).get(game_key, "")

    # ==================== IO ====================

    def save(self) -> None:
        try:
            with open(self._file, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存推送数据失败: {e}")

    def _load(self) -> dict:
        defaults = {
            "pushGroups": {},
            "activityPush": {},
            "banWordOverride": {},
            "sentPostIds": {},
            "lastActivityDate": "",
            "activityRotations": {},
        }
        if os.path.exists(self._file):
            try:
                with open(self._file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                for k, v in defaults.items():
                    if k not in loaded:
                        loaded[k] = v
                return loaded
            except Exception as e:
                logger.warning(f"加载推送数据失败: {e}")
        return defaults
