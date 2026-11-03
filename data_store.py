"""推送数据持久化 —— push_data.json 管理

除了读写 JSON，本模块还负责把旧版本插件写入的「裸 session_id」迁移成 AstrBot
主动发送所需的 unified_msg_origin（三段式：platform_id:MessageType:session_id）。
"""

import json
import os
import re
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from astrbot.api import logger
from astrbot.api.star import StarTools

# AstrBot 的 unified_msg_origin 由 MessageSession.__str__ 生成：
#   f"{platform_id}:{message_type.value}:{session_id}"
# 群消息段固定为 MessageType.GROUP_MESSAGE.value（astrbot/core/platform/message_type.py:
# GROUP_MESSAGE = "GroupMessage"）。这里优先取枚举，取不到时退回已核实的字面量。
try:  # pragma: no cover - 仅为跨版本兜底
    from astrbot.core.platform.message_type import MessageType as _MessageType

    _GROUP_MESSAGE = _MessageType.GROUP_MESSAGE.value
except Exception:  # pragma: no cover
    _GROUP_MESSAGE = "GroupMessage"

# 旧版订阅里的两类裸 id：
#   - qq_official 的群 group_openid：32 位大写/小写 hex（线上实例全部为大写）
#   - aiocqhttp(OneBot v11) 的群号：纯数字
_HEX32_RE = re.compile(r"^[0-9A-Fa-f]{32}$")
_DIGITS_RE = re.compile(r"^\d+$")

# (platform_id, platform_name)，platform_name 即平台类型，如 qq_official / aiocqhttp
PlatformInfo = Tuple[str, str]


class PushDataStore:
    """管理米游社推送配置与状态的 JSON 文件读写"""

    def __init__(
        self,
        platforms: Optional[Sequence[PlatformInfo]] = None,
        resolver: Optional[Callable[[str], Optional[str]]] = None,
    ) -> None:
        """构造并加载 push_data.json，同时尝试迁移旧版裸 session_id。

        Args:
            platforms: 已加载平台的 (platform_id, platform_name) 列表，由
                Main.__init__ 从 context.platform_manager.platform_insts 收集。
            resolver: 可选的旧 id 解析回调，接收裸 id 返回 unified_msg_origin；
                返回 None 表示交给内置规则继续尝试。与 platforms 二选一或同时提供。
        """
        self._platforms: List[PlatformInfo] = [
            (str(pid), str(pname)) for pid, pname in (platforms or []) if pid and pname
        ]
        self._resolver = resolver

        self._data_dir = os.path.join(
            StarTools.get_data_dir(plugin_name="astrbot_plugin_miyoushe"),
        )
        os.makedirs(self._data_dir, exist_ok=True)
        self._file = os.path.join(self._data_dir, "push_data.json")
        self._data = self._load()
        self.migrate_legacy_sessions()

    # ==================== 属性访问 ====================

    @property
    def push_groups(self) -> Dict[str, List[str]]:
        """{config_key: [unified_msg_origin, ...]}"""
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

    # ==================== 旧数据迁移 ====================

    def has_legacy_sessions(self) -> bool:
        """是否还存在未迁移的裸 session_id（不含 ':'）。

        用于 AstrBot 启动后平台适配器就绪时补做一次迁移。
        """
        for section_name in ("pushGroups", "activityPush"):
            section = self._data.get(section_name)
            if not isinstance(section, dict):
                continue
            for values in section.values():
                if self._has_legacy_value(values):
                    return True
        rotations = self._data.get("activityRotations")
        if isinstance(rotations, dict):
            for rot in rotations.values():
                if isinstance(rot, dict) and self._has_legacy_value(
                    rot.get("remaining")
                ):
                    return True
        return False

    @staticmethod
    def _has_legacy_value(values: object) -> bool:
        if not isinstance(values, list):
            return False
        return any(isinstance(v, str) and ":" not in v for v in values)

    def migrate_legacy_sessions(
        self, platforms: Optional[Sequence[PlatformInfo]] = None
    ) -> bool:
        """把旧版裸 session_id 迁移成 unified_msg_origin。

        规则（幂等：已含 ':' 的一律不动）：
          - 32 位 hex（qq_official 的 group_openid）→
            ``{qq_official 平台 id}:GroupMessage:{raw}``
          - 纯数字（aiocqhttp 群号）→
            ``{aiocqhttp 平台 id}:GroupMessage:{raw}``
          - 无法识别 → 记 warning 并保留原值

        同时清理同一列表内的重复项；pushGroups / activityPush / activityRotations
        以外的 key（banWordOverride 等）原样保留。仅在有变更时写回文件。

        Args:
            platforms: 可选的平台信息覆盖。AstrBot 启动时插件先于平台适配器
                加载，构造时可能拿不到平台列表，调用方可稍后（如首个推送轮次）
                再次传入平台信息重试迁移。

        Returns:
            是否发生了变更（有迁移或去重）
        """
        if platforms:
            self._platforms = [
                (str(pid), str(pname)) for pid, pname in platforms if pid and pname
            ]
        if not self._platforms and self._resolver is None:
            logger.info("[push_data] 未获取到平台信息，跳过旧订阅 session 迁移")
            return False

        changed = False
        migrated_refs: List[str] = []

        for section_name in ("pushGroups", "activityPush"):
            section = self._data.get(section_name)
            if not isinstance(section, dict):
                continue
            for key, values in list(section.items()):
                new_values, migrated_values = self._migrate_list(values)
                if new_values != values:
                    section[key] = new_values
                    changed = True
                migrated_refs.extend(migrated_values)

        # 活动轮询队列里的 session_id 同样需要迁移，否则发送时仍会抛 ValueError
        rotations = self._data.get("activityRotations")
        if isinstance(rotations, dict):
            for rot in rotations.values():
                if not isinstance(rot, dict):
                    continue
                values = rot.get("remaining")
                if not isinstance(values, list):
                    continue
                new_values, migrated_values = self._migrate_list(values)
                if new_values != values:
                    rot["remaining"] = new_values
                    changed = True
                migrated_refs.extend(migrated_values)

        if migrated_refs:
            # 逐项迁移明细见 DEBUG，INFO 只留一条汇总，避免线上刷屏
            logger.info(
                "[push_data] 旧版订阅会话迁移完成，"
                f"共迁移 {len(set(migrated_refs))} 项"
                f"（列表项引用 {len(migrated_refs)} 处）"
            )
        if changed:
            self.save()
        return changed

    def _migrate_list(self, values: object) -> Tuple[list, List[str]]:
        """迁移一个 session_id 列表，返回 (新列表, 迁移后的值列表)。"""
        if not isinstance(values, list):
            return values, []
        out: List[object] = []
        migrated_values: List[str] = []
        for raw in values:
            if isinstance(raw, str):
                new_value: object = self._migrate_session_id(raw)
                if new_value != raw and isinstance(new_value, str):
                    migrated_values.append(new_value)
            else:
                logger.warning(f"[push_data] 订阅项不是字符串，已保留: {raw!r}")
                new_value = raw
            if new_value not in out:  # 去重，避免迁移后重复推送
                out.append(new_value)
        return out, migrated_values

    def _migrate_session_id(self, raw: str) -> str:
        """把单个裸 id 转成 unified_msg_origin；无法识别时原样返回。"""
        raw = raw.strip()
        if not raw:
            return raw
        # 已经是三段式（或至少含冒号）→ 视为已迁移，不动
        if ":" in raw:
            if len(raw.split(":")) < 3:
                logger.warning(
                    f"[push_data] 订阅 id 含 ':' 但不是三段式，按原样保留: {raw!r}"
                )
            return raw

        # 1) 外部注入的解析回调优先
        if self._resolver is not None:
            try:
                resolved = self._resolver(raw)
            except Exception as e:
                logger.warning(f"[push_data] 平台解析回调异常 ({raw!r}): {e}")
                resolved = None
            if resolved:
                logger.debug(f"[push_data] 迁移旧订阅 {raw!r} → {resolved!r}")
                return resolved

        # 2) 内置规则：32 位 hex → qq_official；纯数字 → aiocqhttp
        if _HEX32_RE.match(raw):
            pid = self._find_platform("qq_official")
            if pid:
                resolved = f"{pid}:{_GROUP_MESSAGE}:{raw}"
                logger.debug(f"[push_data] 迁移旧订阅 {raw!r} → {resolved!r}")
                return resolved
            logger.warning(
                f"[push_data] 未找到 qq_official 平台，无法迁移旧订阅 {raw!r}，保留原值"
            )
            return raw

        if _DIGITS_RE.match(raw):
            pid = self._find_platform("aiocqhttp")
            if pid:
                resolved = f"{pid}:{_GROUP_MESSAGE}:{raw}"
                logger.debug(f"[push_data] 迁移旧订阅 {raw!r} → {resolved!r}")
                return resolved
            logger.warning(
                f"[push_data] 未找到 aiocqhttp 平台，无法迁移旧订阅 {raw!r}，保留原值"
            )
            return raw

        logger.warning(f"[push_data] 无法识别的旧订阅 id，按原样保留: {raw!r}")
        return raw

    def _find_platform(self, platform_name: str) -> Optional[str]:
        """按平台类型取第一个 platform_inst 的 id。

        qq_official 额外兼容 webhook 变体（qq_official_webhook），两者 session_id
        语义相同，都是 group_openid。
        """
        for pid, pname in self._platforms:
            if pname == platform_name:
                return pid
        if platform_name == "qq_official":
            for pid, pname in self._platforms:
                if pname.startswith("qq_official"):
                    return pid
        return None

    # ==================== 活动预警轮询队列 ====================

    def get_activity_rotation(self, game_key: str) -> Optional[List[str]]:
        """获取活动预警轮询剩余队列。

        Returns:
            None 表示没有当日记录（需要从完整订阅列表重新开始）；
            空列表表示当日已轮询完所有群（不应重置重发）。
        """
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
        remaining = rot.get("remaining")
        if not isinstance(remaining, list):
            return None
        return remaining

    def set_activity_rotation(
        self,
        game_key: str,
        remaining: List[str],
        date: Optional[str] = None,
    ) -> None:
        """设置活动预警轮询剩余队列。

        Args:
            game_key: "gs" / "sr"
            remaining: 当日剩余待通知的 unified_msg_origin 列表
            date: 队列所属日期（"YYYY-MM-DD"），默认取当前 lastActivityDate；
                调用方必须在写入前把 lastActivityDate 更新为今天，否则跨天读取
                时日期不匹配会导致轮询每天被重置。
        """
        date = date or self.last_activity_date
        if not date:
            logger.warning(
                f"[push_data] lastActivityDate 为空，无法记录活动预警轮询队列 ({game_key})"
            )
            return
        key = f"activityRotation_{game_key}"
        self._data.setdefault("activityRotations", {})[key] = {
            "date": date,
            "remaining": remaining,
        }

    def remove_session(self, session_id: str) -> int:
        """从所有推送订阅与活动轮询队列中移除某个会话，返回移除的处数。

        用于会话被平台判为永久失效（如 QQ 群已注销，code 11255）时清理数据，
        避免后续每轮继续往死群重试。调用方需自行调用 save() 写回。
        """
        removed = 0
        for section_name in ("pushGroups", "activityPush"):
            section = self._data.get(section_name)
            if not isinstance(section, dict):
                continue
            for key, values in list(section.items()):
                if isinstance(values, list) and session_id in values:
                    section[key] = [v for v in values if v != session_id]
                    removed += 1
        rotations = self._data.get("activityRotations")
        if isinstance(rotations, dict):
            for rot in rotations.values():
                if not isinstance(rot, dict):
                    continue
                values = rot.get("remaining")
                if isinstance(values, list) and session_id in values:
                    rot["remaining"] = [v for v in values if v != session_id]
                    removed += 1
        return removed

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
