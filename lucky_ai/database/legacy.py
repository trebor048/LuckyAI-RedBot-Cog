import time
import asyncio
import aiosqlite
import logging
from typing import List, Optional

from ..utils import generate_content_hash

log = logging.getLogger("red.LuckyAICog.db")


class MessageDB:
    def __init__(self, db_path):
        self.db_path = db_path
        self._conn = None
        self._lock = asyncio.Lock()

    async def initialize(self):
        async with self._lock:
            self._conn = await aiosqlite.connect(self.db_path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA journal_mode = WAL")
            await self._conn.execute("PRAGMA synchronous = NORMAL")
            await self._conn.execute("PRAGMA cache_size = -32000")
            await self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    author_id TEXT NOT NULL,
                    author_tag TEXT,
                    content TEXT NOT NULL,
                    content_hash TEXT,
                    timestamp INTEGER NOT NULL,
                    channel_id TEXT NOT NULL,
                    guild_id TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS user_opt_outs (
                    user_id TEXT,
                    guild_id TEXT,
                    opted_out INTEGER DEFAULT 0,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, guild_id)
                );

                CREATE TABLE IF NOT EXISTS guild_blacklist (
                    guild_id TEXT,
                    user_id TEXT,
                    reason TEXT,
                    added_by TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS guild_sync_status (
                    guild_id TEXT,
                    channel_id TEXT,
                    last_message_id TEXT,
                    last_sync_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, channel_id)
                );

                CREATE TABLE IF NOT EXISTS guild_sync_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id TEXT NOT NULL,
                    channel_id TEXT,
                    operation TEXT NOT NULL,
                    message_count INTEGER DEFAULT 0,
                    duration_ms INTEGER,
                    error TEXT,
                    triggered_by TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS hot_takes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    generated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    roast_text TEXT NOT NULL,
                    trigger_message_count INTEGER,
                    model_used TEXT,
                    latency_ms INTEGER
                );

                CREATE TABLE IF NOT EXISTS command_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    command TEXT NOT NULL,
                    timestamp INTEGER NOT NULL,
                    latency_ms INTEGER,
                    success INTEGER DEFAULT 1
                );

                CREATE INDEX IF NOT EXISTS idx_messages_author_id ON messages(author_id);
                CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_messages_author_timestamp ON messages(author_id, timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_message_channel ON messages(channel_id, timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_user_opt_outs_guild ON user_opt_outs(guild_id, opted_out);
                CREATE INDEX IF NOT EXISTS idx_guild_blacklist_guild ON guild_blacklist(guild_id);

                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    roast_count INTEGER DEFAULT 0,
                    last_active TEXT
                );

                CREATE TABLE IF NOT EXISTS hot_take_state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_command_usage_guild ON command_usage(guild_id, timestamp);
            """)
            log.info("Message database initialized at %s", self.db_path)

    async def close(self):
        async with self._lock:
            if self._conn:
                await self._conn.close()
                self._conn = None

    async def _ensure_open(self):
        async with self._lock:
            if self._conn is None:
                await self.initialize()

    async def save_message(self, msg):
        await self._ensure_open()
        if not msg or not msg.get("id"):
            return
        author = msg.get("author") or {}
        channel = msg.get("channel") or {}
        if not author.get("id") or not channel.get("id"):
            return
        guild_id = msg.get("guild_id")
        if not guild_id and isinstance(msg.get("guild"), dict):
            guild_id = msg["guild"].get("id")
        async with self._lock:
            await self._conn.execute(
                """INSERT OR REPLACE INTO messages
                   (id, author_id, author_tag, content, content_hash, timestamp, channel_id, guild_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    msg["id"],
                    author.get("id"),
                    author.get("tag") or author.get("name") or str(author.get("id", "")),
                    msg.get("content") or "",
                    generate_content_hash(msg.get("content")),
                    msg.get("timestamp") if msg.get("timestamp") is not None else int(time.time() * 1000),
                    channel.get("id"),
                    guild_id,
                ),
            )
            await self._conn.commit()

    async def save_message_batch(self, messages):
        await self._ensure_open()
        if not messages:
            return
        async with self._lock:
            for msg in messages:
                author = msg.get("author", {})
                channel = msg.get("channel", {})
                guild_id = msg.get("guild_id")
                if not guild_id and isinstance(msg.get("guild"), dict):
                    guild_id = msg["guild"].get("id")
                await self._conn.execute(
                    """INSERT OR REPLACE INTO messages
                       (id, author_id, author_tag, content, content_hash, timestamp, channel_id, guild_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        msg["id"],
                        author.get("id"),
                        author.get("tag") or author.get("name") or str(author.get("id", "")),
                        msg.get("content") or "",
                        generate_content_hash(msg.get("content")),
                        msg.get("timestamp") if msg.get("timestamp") is not None else int(time.time() * 1000),
                        channel.get("id"),
                        guild_id,
                    ),
                )
            await self._conn.commit()

    async def delete_message(self, message_id):
        await self._ensure_open()
        async with self._lock:
            await self._conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
            await self._conn.commit()

    async def get_messages(self, user_id, limit=200, mode="random", guild_id=None):
        await self._ensure_open()
        if guild_id:
            opted = await self.get_user_opt_out(user_id, guild_id)
            if opted:
                return []

        limit = min(limit, 2000)
        async with self._lock:
            if mode == "recent":
                if guild_id:
                    cursor = await self._conn.execute(
                        """SELECT id, author_id, author_tag, content, timestamp, channel_id, guild_id
                           FROM messages WHERE author_id = ? AND guild_id = ? ORDER BY timestamp DESC LIMIT ?""",
                        (user_id, guild_id, limit),
                    )
                else:
                    cursor = await self._conn.execute(
                        """SELECT id, author_id, author_tag, content, timestamp, channel_id, guild_id
                           FROM messages WHERE author_id = ? ORDER BY timestamp DESC LIMIT ?""",
                        (user_id, limit),
                    )
            else:
                if guild_id:
                    count_row = await self._conn.execute(
                        "SELECT COUNT(*) as cnt FROM messages WHERE author_id = ? AND guild_id = ?", (user_id, guild_id)
                    )
                else:
                    count_row = await self._conn.execute(
                        "SELECT COUNT(*) as cnt FROM messages WHERE author_id = ?", (user_id,)
                    )
                row = await count_row.fetchone()
                total = row[0] if row else 0
                if total == 0:
                    return []
                if total <= limit:
                    if guild_id:
                        cursor = await self._conn.execute(
                            """SELECT id, author_id, author_tag, content, timestamp, channel_id, guild_id
                               FROM messages WHERE author_id = ? AND guild_id = ? ORDER BY RANDOM() LIMIT ?""",
                            (user_id, guild_id, total),
                        )
                    else:
                        cursor = await self._conn.execute(
                            """SELECT id, author_id, author_tag, content, timestamp, channel_id, guild_id
                               FROM messages WHERE author_id = ? ORDER BY RANDOM() LIMIT ?""",
                            (user_id, total),
                        )
                else:
                    if guild_id:
                        cursor = await self._conn.execute(
                            """SELECT id, author_id, author_tag, content, timestamp, channel_id, guild_id
                               FROM messages WHERE author_id = ? AND guild_id = ? ORDER BY RANDOM() LIMIT ?""",
                            (user_id, guild_id, limit),
                        )
                    else:
                        cursor = await self._conn.execute(
                            """SELECT id, author_id, author_tag, content, timestamp, channel_id, guild_id
                               FROM messages WHERE author_id = ? ORDER BY RANDOM() LIMIT ?""",
                            (user_id, limit),
                        )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_channel_messages(self, channel_id, limit=200):
        await self._ensure_open()
        limit = min(limit, 1000)
        async with self._lock:
            cursor = await self._conn.execute(
                """SELECT id, author_id, author_tag, content, timestamp, channel_id, guild_id
                   FROM messages WHERE channel_id = ? ORDER BY timestamp DESC LIMIT ?""",
                (channel_id, limit),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_user_opt_out(self, user_id, guild_id):
        await self._ensure_open()
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT opted_out FROM user_opt_outs WHERE user_id = ? AND guild_id = ?",
                (user_id, guild_id),
            )
            row = await cursor.fetchone()
            return bool(row and row[0])

    async def set_user_opt_out(self, user_id, guild_id, opted_out):
        await self._ensure_open()
        async with self._lock:
            await self._conn.execute(
                """INSERT OR REPLACE INTO user_opt_outs (user_id, guild_id, opted_out, updated_at)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP)""",
                (user_id, guild_id, 1 if opted_out else 0),
            )
            await self._conn.commit()

    async def get_opt_out_user_ids(self, guild_id):
        await self._ensure_open()
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT user_id FROM user_opt_outs WHERE guild_id = ? AND opted_out = 1",
                (guild_id,),
            )
            rows = await cursor.fetchall()
            return [r[0] for r in rows]

    async def add_to_blacklist(self, guild_id, user_id, added_by, reason=None):
        await self._ensure_open()
        async with self._lock:
            await self._conn.execute(
                """INSERT OR REPLACE INTO guild_blacklist (guild_id, user_id, reason, added_by)
                   VALUES (?, ?, ?, ?)""",
                (guild_id, user_id, reason, added_by),
            )
            await self._conn.commit()

    async def remove_from_blacklist(self, guild_id, user_id):
        await self._ensure_open()
        async with self._lock:
            await self._conn.execute(
                "DELETE FROM guild_blacklist WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            await self._conn.commit()

    async def is_blacklisted(self, guild_id, user_id):
        await self._ensure_open()
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT 1 FROM guild_blacklist WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            return bool(await cursor.fetchone())

    async def get_blacklist(self, guild_id):
        await self._ensure_open()
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT user_id, reason, added_by, created_at FROM guild_blacklist WHERE guild_id = ?",
                (guild_id,),
            )
            return [dict(r) for r in await cursor.fetchall()]

    async def get_sync_channels(self, guild_id):
        await self._ensure_open()
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT channel_id FROM guild_sync_status WHERE guild_id = ?", (guild_id,)
            )
            return [r[0] for r in await cursor.fetchall()]

    async def is_channel_sync_enabled(self, guild_id, channel_id):
        await self._ensure_open()
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT 1 FROM guild_sync_status WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id),
            )
            return bool(await cursor.fetchone())

    async def update_sync_status(self, guild_id, channel_id, last_message_id=None):
        await self._ensure_open()
        async with self._lock:
            await self._conn.execute(
                """INSERT OR REPLACE INTO guild_sync_status (guild_id, channel_id, last_message_id, last_sync_time)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP)""",
                (guild_id, channel_id, last_message_id),
            )
            await self._conn.commit()

    async def delete_sync_status(self, guild_id, channel_id):
        await self._ensure_open()
        async with self._lock:
            await self._conn.execute(
                "DELETE FROM guild_sync_status WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id),
            )
            await self._conn.commit()

    async def delete_channel_messages(self, guild_id, channel_id):
        await self._ensure_open()
        async with self._lock:
            cursor = await self._conn.execute(
                "DELETE FROM messages WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id),
            )
            await self._conn.commit()
            return cursor.rowcount

    async def log_sync_operation(self, guild_id, channel_id, operation, message_count=0, duration=0, error=None, triggered_by="system"):
        await self._ensure_open()
        async with self._lock:
            await self._conn.execute(
                """INSERT INTO guild_sync_logs (guild_id, channel_id, operation, message_count, duration_ms, error, triggered_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (guild_id, channel_id, operation, message_count, duration, error, triggered_by),
            )
            await self._conn.commit()

    async def log_hot_take(self, guild_id, channel_id, roast_text, message_count, model, latency_ms):
        await self._ensure_open()
        async with self._lock:
            await self._conn.execute(
                """INSERT INTO hot_takes (guild_id, channel_id, roast_text, trigger_message_count, model_used, latency_ms)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (guild_id, channel_id, roast_text, message_count, model, latency_ms),
            )
            await self._conn.commit()

    async def log_command_usage(self, guild_id, user_id, command, success=True):
        await self._ensure_open()
        async with self._lock:
            await self._conn.execute(
                """INSERT INTO command_usage (guild_id, user_id, command, timestamp, success)
                   VALUES (?, ?, ?, ?, ?)""",
                (guild_id, user_id, command, int(time.time() * 1000), 1 if success else 0),
            )
            await self._conn.commit()

    async def get_command_stats(self, guild_id, days=7):
        await self._ensure_open()
        since = int(time.time() * 1000) - (days * 86400 * 1000)
        async with self._lock:
            cursor = await self._conn.execute(
                """SELECT command, COUNT(*) as cnt FROM command_usage
                   WHERE guild_id = ? AND timestamp > ?
                   GROUP BY command ORDER BY cnt DESC""",
                (guild_id, since),
            )
            return [dict(r) for r in await cursor.fetchall()]

    async def update_roast_count(self, user_id):
        await self._ensure_open()
        async with self._lock:
            await self._conn.execute(
                """INSERT INTO users (id, roast_count, last_active) VALUES (?, 1, datetime('now'))
                   ON CONFLICT(id) DO UPDATE SET roast_count = roast_count + 1""",
                (user_id,),
            )
            await self._conn.commit()

    async def get_leaderboard(self, limit=10):
        await self._ensure_open()
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT id, roast_count FROM users ORDER BY roast_count DESC LIMIT ?", (limit,)
            )
            return [dict(r) for r in await cursor.fetchall()]

    async def save_hot_take_enabled(self, enabled):
        await self._ensure_open()
        async with self._lock:
            await self._conn.execute(
                "INSERT OR REPLACE INTO hot_take_state (key, value) VALUES ('enabled', ?)",
                ("1" if enabled else "0",),
            )
            await self._conn.commit()

    async def get_hot_take_enabled(self):
        await self._ensure_open()
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT value FROM hot_take_state WHERE key = 'enabled'", ()
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return row[0] == "1"

    async def get_database_stats(self):
        await self._ensure_open()
        async with self._lock:
            msg_c = await self._conn.execute("SELECT COUNT(*) as cnt FROM messages")
            guild_c = await self._conn.execute("SELECT COUNT(DISTINCT guild_id) as cnt FROM messages")
            user_c = await self._conn.execute("SELECT COUNT(DISTINCT author_id) as cnt FROM messages")
            users_c = await self._conn.execute("SELECT COUNT(*) as cnt FROM users")
            users_row = await users_c.fetchone()
            msg_row = await msg_c.fetchone()
            guild_row = await guild_c.fetchone()
            author_row = await user_c.fetchone()
            return {
                "total_messages": msg_row[0] if msg_row else 0,
                "total_guilds": guild_row[0] if guild_row else 0,
                "total_users": (author_row[0] if author_row else 0) or (users_row[0] if users_row else 0),
            }

    # ============== MISSING METHODS ==============

    async def get_sync_logs(self, guild_id: str) -> List[dict]:
        """Get recent sync operation logs."""
        await self._ensure_open()
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT * FROM guild_sync_logs WHERE guild_id = ? ORDER BY id DESC LIMIT 50",
                (guild_id,),
            )
            return [dict(r) for r in await cursor.fetchall()]

    async def get_recent_hot_takes(self, guild_id: str, limit: int = 10) -> List[dict]:
        """Get recent hot takes for a guild."""
        await self._ensure_open()
        async with self._lock:
            cursor = await self._conn.execute(
                """SELECT id, channel_id, generated_at, roast_text, trigger_message_count, model_used, latency_ms
                   FROM hot_takes WHERE guild_id = ? ORDER BY generated_at DESC LIMIT ?""",
                (guild_id, min(limit, 100)),
            )
            return [dict(r) for r in await cursor.fetchall()]

    async def is_duplicate_content(self, user_id: str, content: str) -> bool:
        """Check if message content already exists (for duplicate detection)."""
        await self._ensure_open()
        hash_val = generate_content_hash(content)
        async with self._lock:
            cursor = await self._conn.execute(
                "SELECT 1 FROM messages WHERE author_id = ? AND content_hash = ? LIMIT 1",
                (user_id, hash_val),
            )
            return bool(await cursor.fetchone())

    async def update_last_active(self, user_id: str) -> None:
        """Update user's last active timestamp."""
        await self._ensure_open()
        async with self._lock:
            await self._conn.execute(
                """INSERT INTO users (id, last_active) VALUES (?, datetime('now'))
                   ON CONFLICT(id) DO UPDATE SET last_active = excluded.last_active""",
                (user_id,),
            )
            await self._conn.commit()
