import aiosqlite
import asyncio
from config import DB_PATH


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                points INTEGER DEFAULT 0,
                referral_points INTEGER DEFAULT 0,
                referred_by INTEGER,
                join_date TEXT DEFAULT (datetime('now')),
                is_banned INTEGER DEFAULT 0
            )
        """)
        # Migrate existing DBs — ignore if column already exists
        for col_sql in [
            "ALTER TABLE users ADD COLUMN referral_points INTEGER DEFAULT 0",
        ]:
            try:
                await db.execute(col_sql)
            except Exception:
                pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                telegram_id INTEGER PRIMARY KEY,
                state TEXT DEFAULT 'idle',
                phone TEXT,
                user_key TEXT,
                data_key TEXT,
                access_token TEXT,
                cookies_json TEXT,
                reg_cookies_json TEXT,
                rnd_name TEXT,
                rnd_city TEXT,
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        for col_sql in [
            "ALTER TABLE sessions ADD COLUMN reg_cookies_json TEXT",
        ]:
            try:
                await db.execute(col_sql)
            except Exception:
                pass
        await db.execute("""
            CREATE TABLE IF NOT EXISTS usage_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER,
                phone TEXT,
                success INTEGER DEFAULT 0,
                points_awarded INTEGER DEFAULT 0,
                used_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS broadcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message TEXT,
                sent_at TEXT DEFAULT (datetime('now')),
                sent_by INTEGER
            )
        """)
        await db.commit()


# ── User operations ───────────────────────────────────────────────────────────

async def get_user(telegram_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def create_user(telegram_id: int, username: str, first_name: str, referred_by: int = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR IGNORE INTO users (telegram_id, username, first_name, referred_by)
               VALUES (?, ?, ?, ?)""",
            (telegram_id, username, first_name, referred_by),
        )
        await db.commit()


async def update_points(telegram_id: int, delta: int):
    """Add or subtract regular points. Returns new total."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET points = MAX(0, points + ?) WHERE telegram_id = ?",
            (delta, telegram_id),
        )
        await db.commit()
        async with db.execute(
            "SELECT points FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def update_referral_points(telegram_id: int, delta: int):
    """Add or subtract referral points. Returns new referral_points total."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET referral_points = MAX(0, referral_points + ?) WHERE telegram_id = ?",
            (delta, telegram_id),
        )
        await db.commit()
        async with db.execute(
            "SELECT referral_points FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def get_referral_points(telegram_id: int) -> int:
    """Return current referral_points balance."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT referral_points FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def set_points(telegram_id: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET points = ? WHERE telegram_id = ?",
            (max(0, amount), telegram_id),
        )
        await db.commit()


async def add_points_all(delta: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET points = MAX(0, points + ?)", (delta,))
        await db.commit()


async def get_all_users():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users ORDER BY points DESC") as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_user_count():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            row = await cur.fetchone()
            return row[0]


async def get_banned_users():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE is_banned = 1") as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def set_ban(telegram_id: int, banned: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET is_banned = ? WHERE telegram_id = ?",
            (1 if banned else 0, telegram_id),
        )
        await db.commit()


async def get_referral_count(telegram_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE referred_by = ?", (telegram_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0]


# ── Session operations ────────────────────────────────────────────────────────

async def get_session(telegram_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM sessions WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def upsert_session(telegram_id: int, **fields):
    """Create or update a session row with the given fields."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Ensure row exists
        await db.execute(
            "INSERT OR IGNORE INTO sessions (telegram_id) VALUES (?)", (telegram_id,)
        )
        if fields:
            set_clause = ", ".join(f"{k} = ?" for k in fields)
            set_clause += ", updated_at = datetime('now')"
            values = list(fields.values()) + [telegram_id]
            await db.execute(
                f"UPDATE sessions SET {set_clause} WHERE telegram_id = ?", values
            )
        await db.commit()


async def clear_session(telegram_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE sessions SET
               state='idle', phone=NULL, user_key=NULL, data_key=NULL,
               access_token=NULL, cookies_json=NULL, reg_cookies_json=NULL,
               rnd_name=NULL, rnd_city=NULL, updated_at=datetime('now')
               WHERE telegram_id = ?""",
            (telegram_id,),
        )
        await db.commit()


# ── Usage log ─────────────────────────────────────────────────────────────────

async def log_usage(telegram_id: int, phone: str, success: bool, points_awarded: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO usage_log (telegram_id, phone, success, points_awarded)
               VALUES (?, ?, ?, ?)""",
            (telegram_id, phone, 1 if success else 0, points_awarded),
        )
        await db.commit()


async def get_usage_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM usage_log WHERE success=1") as cur:
            successful = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM usage_log") as cur:
            total = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(DISTINCT telegram_id) FROM usage_log") as cur:
            unique_users = (await cur.fetchone())[0]
        return {"total": total, "successful": successful, "unique_users": unique_users}
