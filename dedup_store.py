"""去重存储 —— SQLite 实现，更可靠的持久化去重"""

import os
import sqlite3
import threading
import time
from typing import Optional

from astrbot.api.star import StarTools


class DedupStore:
    """基于 SQLite 的已推送去重存储

    - 自动清理超过 TTL 的旧记录
    - 线程安全（连接级锁）
    """

    def __init__(self, ttl_secs: int = 36000) -> None:
        self._ttl = ttl_secs
        self._lock = threading.Lock()
        data_dir = os.path.join(
            StarTools.get_data_dir(plugin_name="astrbot_plugin_miyoushe"),
        )
        os.makedirs(data_dir, exist_ok=True)
        self._path = os.path.join(data_dir, "dedup.db")
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            conn = sqlite3.connect(self._path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS sent_posts ("
                "post_id TEXT PRIMARY KEY, ts INTEGER NOT NULL)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sent_ts ON sent_posts(ts)")
            conn.commit()
            conn.close()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._path, check_same_thread=False)
        return self._conn

    def is_sent(self, post_id: str) -> bool:
        with self._lock:
            conn = self._get_conn()
            cur = conn.execute("SELECT 1 FROM sent_posts WHERE post_id=?", (post_id,))
            return cur.fetchone() is not None

    def mark_sent(self, post_id: str) -> None:
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "INSERT OR IGNORE INTO sent_posts (post_id, ts) VALUES (?, ?)",
                (post_id, int(time.time())),
            )
            conn.commit()

    def prune(self) -> None:
        cutoff = int(time.time()) - self._ttl
        with self._lock:
            conn = self._get_conn()
            conn.execute("DELETE FROM sent_posts WHERE ts < ?", (cutoff,))
            conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None
