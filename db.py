"""Локальная база бота: пользователи, оплаты, рефералы и настройки."""
from __future__ import annotations

import json
import time

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id INTEGER PRIMARY KEY,
    username TEXT,
    uuid TEXT,
    email TEXT,
    sub_id TEXT,
    trial_used INTEGER NOT NULL DEFAULT 0,
    notified_expiry INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    charge_id TEXT PRIMARY KEY,
    tg_id INTEGER NOT NULL,
    plan TEXT NOT NULL,
    amount INTEGER NOT NULL,
    currency TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Колонки, добавленные после первой версии — докатываем на существующую базу
MIGRATIONS = {
    "referrer_id": "ALTER TABLE users ADD COLUMN referrer_id INTEGER",
    "ref_bonus_days": "ALTER TABLE users ADD COLUMN ref_bonus_days INTEGER NOT NULL DEFAULT 0",
}

_db: aiosqlite.Connection | None = None


async def init(path: str) -> None:
    global _db
    _db = await aiosqlite.connect(path)
    _db.row_factory = aiosqlite.Row
    await _db.executescript(SCHEMA)
    async with _db.execute("PRAGMA table_info(users)") as cur:
        columns = {row["name"] for row in await cur.fetchall()}
    for column, sql in MIGRATIONS.items():
        if column not in columns:
            await _db.execute(sql)
    await _db.commit()


async def _one(sql: str, params=()):
    async with _db.execute(sql, params) as cur:
        return await cur.fetchone()


async def _all(sql: str, params=()):
    async with _db.execute(sql, params) as cur:
        return await cur.fetchall()


# ---------- пользователи ----------

async def touch_user(tg_id: int, username: str | None) -> bool:
    """Создаёт или обновляет пользователя. True, если пользователь новый."""
    exists = await _one("SELECT 1 FROM users WHERE tg_id = ?", (tg_id,))
    if exists:
        await _db.execute("UPDATE users SET username = ? WHERE tg_id = ?", (username, tg_id))
    else:
        await _db.execute(
            "INSERT INTO users (tg_id, username, created_at) VALUES (?, ?, ?)",
            (tg_id, username, int(time.time())),
        )
    await _db.commit()
    return not exists


async def get_user(tg_id: int):
    return await _one("SELECT * FROM users WHERE tg_id = ?", (tg_id,))


async def set_client(tg_id: int, uuid_: str, email: str, sub_id: str) -> None:
    await _db.execute(
        "INSERT INTO users (tg_id, uuid, email, sub_id, created_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(tg_id) DO UPDATE SET uuid = excluded.uuid, email = excluded.email, sub_id = excluded.sub_id",
        (tg_id, uuid_, email, sub_id, int(time.time())),
    )
    await _db.commit()


async def set_trial_used(tg_id: int) -> None:
    await _db.execute("UPDATE users SET trial_used = 1 WHERE tg_id = ?", (tg_id,))
    await _db.commit()


async def set_notified(tg_id: int, expiry_ms: int) -> None:
    await _db.execute("UPDATE users SET notified_expiry = ? WHERE tg_id = ?", (expiry_ms, tg_id))
    await _db.commit()


async def users_with_clients():
    return await _all("SELECT * FROM users WHERE uuid IS NOT NULL")


async def all_user_ids() -> list[int]:
    return [row["tg_id"] for row in await _all("SELECT tg_id FROM users")]


async def count_users() -> int:
    return (await _one("SELECT COUNT(*) FROM users"))[0]


async def list_users(offset: int, limit: int):
    return await _all("SELECT * FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset))


async def find_users(query: str):
    query = query.strip().lstrip("@")
    if query.isdigit():
        return await _all("SELECT * FROM users WHERE tg_id = ?", (int(query),))
    return await _all("SELECT * FROM users WHERE username LIKE ? LIMIT 10", (f"%{query}%",))


# ---------- рефералы ----------

async def set_referrer(tg_id: int, referrer_id: int) -> None:
    await _db.execute(
        "UPDATE users SET referrer_id = ? WHERE tg_id = ? AND referrer_id IS NULL",
        (referrer_id, tg_id),
    )
    await _db.commit()


async def add_ref_bonus(tg_id: int, days: int) -> None:
    await _db.execute("UPDATE users SET ref_bonus_days = ref_bonus_days + ? WHERE tg_id = ?", (days, tg_id))
    await _db.commit()


async def referral_stats(tg_id: int) -> dict:
    invited = (await _one("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (tg_id,)))[0]
    paid = (await _one(
        "SELECT COUNT(DISTINCT u.tg_id) FROM users u JOIN payments p ON p.tg_id = u.tg_id WHERE u.referrer_id = ?",
        (tg_id,),
    ))[0]
    user = await get_user(tg_id)
    return {"invited": invited, "paid": paid, "bonus_days": user["ref_bonus_days"] if user else 0}


# ---------- оплаты ----------

async def add_payment(charge_id: str, tg_id: int, plan: str, amount: int, currency: str) -> bool:
    """False, если такой платёж уже был обработан."""
    try:
        await _db.execute(
            "INSERT INTO payments VALUES (?, ?, ?, ?, ?, ?)",
            (charge_id, tg_id, plan, amount, currency, int(time.time())),
        )
    except aiosqlite.IntegrityError:
        return False
    await _db.commit()
    return True


async def payments_count(tg_id: int) -> int:
    return (await _one("SELECT COUNT(*) FROM payments WHERE tg_id = ?", (tg_id,)))[0]


async def user_payments(tg_id: int):
    return await _all(
        "SELECT currency, COUNT(*) AS cnt, SUM(amount) AS total FROM payments WHERE tg_id = ? GROUP BY currency",
        (tg_id,),
    )


async def stats() -> dict:
    now = int(time.time())
    day_start = now - now % 86400  # полночь по UTC
    users = await _one(
        "SELECT COUNT(*) AS total, "
        "SUM(created_at >= ?) AS today, SUM(created_at >= ?) AS week, "
        "SUM(trial_used) AS trials, COUNT(referrer_id) AS referred FROM users",
        (day_start, now - 7 * 86400),
    )
    revenue = {}
    for label, since in (("today", day_start), ("month", now - 30 * 86400), ("all", 0)):
        revenue[label] = await _all(
            "SELECT currency, COUNT(*) AS cnt, SUM(amount) AS total FROM payments WHERE created_at >= ? GROUP BY currency",
            (since,),
        )
    return {"users": users, "revenue": revenue}


# ---------- настройки ----------

async def get_setting(key: str, default=None):
    row = await _one("SELECT value FROM settings WHERE key = ?", (key,))
    return json.loads(row["value"]) if row else default


async def set_setting(key: str, value) -> None:
    await _db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )
    await _db.commit()
