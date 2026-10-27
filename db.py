"""Postgres-слой бота.

Внешняя база (Neon) периодически закрывает простаивающие соединения
(`AdminShutdown`, `ConnectionDoesNotExist` и т.п.). Поэтому:

* пул проверяет соединение перед выдачей (`check=check_connection`);
* простаивающие соединения пересоздаются (`max_idle`);
* каждая операция при обрыве соединения повторяется (см. `_retry`).
"""

import functools
import logging
import random
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Dict, List, Optional

import psycopg
from psycopg_pool import ConnectionPool

from config import DATABASE_URL

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL не задан. Бот работает только с внешней Postgres — "
        "укажите строку подключения в переменных окружения."
    )

log = logging.getLogger(__name__)

# Ошибки, при которых соединение мертво, а запрос имеет смысл повторить.
TRANSIENT_ERRORS = (psycopg.OperationalError, psycopg.InterfaceError)

RETRY_ATTEMPTS = 3

_pool: "ConnectionPool | None" = None


class _Proxy:
    """Переводит ? на %s, чтобы SQL оставался компактным."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql: str, params=()):
        return self._conn.execute(sql.replace("?", "%s"), params)


@contextmanager
def _conn():
    if _pool is None:
        raise RuntimeError("База не инициализирована: вызовите db.init()")
    with _pool.connection() as conn:
        yield _Proxy(conn)


def _retry(func):
    """Повторяет операцию, если соединение с базой оборвалось."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        delay = 0.3
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                return func(*args, **kwargs)
            except TRANSIENT_ERRORS as e:
                if attempt == RETRY_ATTEMPTS:
                    log.error("%s: обрыв соединения, попытки исчерпаны: %s", func.__name__, e)
                    raise
                log.warning(
                    "%s: обрыв соединения (%s), повтор %d/%d",
                    func.__name__, type(e).__name__, attempt, RETRY_ATTEMPTS - 1,
                )
                time.sleep(delay + random.uniform(0, 0.2))
                delay *= 2

    return wrapper


def init():
    """Создаёт пул и таблицы. Повторный вызов безопасен."""
    global _pool
    if _pool is not None:
        return

    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    conn_kwargs = {"connect_timeout": 10, "keepalives": 1, "keepalives_idle": 30}
    if "-pooler" in url:
        # PgBouncer в transaction-режиме не дружит с серверными prepared statements
        conn_kwargs["prepare_threshold"] = None

    _pool = ConnectionPool(
        url,
        min_size=1,
        max_size=5,
        open=True,
        kwargs=conn_kwargs,
        # мёртвое соединение не выдаётся из пула
        check=ConnectionPool.check_connection,
        # простаивающие соединения пересоздаются, пока Neon их не прибил
        max_idle=90,
        max_lifetime=1800,
        timeout=15,
        reconnect_timeout=60,
    )

    with _pool.connection() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                   user_id BIGINT PRIMARY KEY,
                   mode TEXT NOT NULL DEFAULT 'idle',
                   q_index INTEGER,
                   updated_at TEXT,
                   demo_used INTEGER NOT NULL DEFAULT 0,
                   paid_until BIGINT,
                   granted INTEGER NOT NULL DEFAULT 0,
                   banned INTEGER NOT NULL DEFAULT 0
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS history (
                   id BIGSERIAL PRIMARY KEY,
                   user_id BIGINT NOT NULL,
                   role TEXT NOT NULL,
                   content TEXT NOT NULL,
                   ts TEXT NOT NULL
               )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hist_user ON history(user_id)")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS answers (
                   id BIGSERIAL PRIMARY KEY,
                   user_id BIGINT NOT NULL,
                   username TEXT,
                   first_name TEXT,
                   q_index INTEGER,
                   question TEXT,
                   score INTEGER,
                   ts TEXT NOT NULL
               )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_answers_user ON answers(user_id)")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS payments (
                   id BIGSERIAL PRIMARY KEY,
                   user_id BIGINT NOT NULL,
                   charge_id TEXT,
                   amount INTEGER,
                   currency TEXT,
                   is_recurring INTEGER,
                   until_ts BIGINT,
                   ts TEXT NOT NULL
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS settings (
                   key TEXT PRIMARY KEY,
                   value TEXT
               )"""
        )
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'users'"
        ).fetchall()
        cols = {r[0] for r in rows}
        if "demo_used" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN demo_used INTEGER NOT NULL DEFAULT 0")
        if "paid_until" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN paid_until BIGINT")
        if "granted" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN granted INTEGER NOT NULL DEFAULT 0")
        if "banned" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN banned INTEGER NOT NULL DEFAULT 0")


@_retry
def get_user(user_id: int) -> Dict:
    with _conn() as c:
        row = c.execute(
            "SELECT user_id, mode, q_index FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row:
            return {"user_id": row[0], "mode": row[1], "q_index": row[2]}
        return {"user_id": user_id, "mode": "idle", "q_index": None}


@_retry
def set_user(user_id: int, mode: Optional[str] = None, q_index: Optional[int] = None):
    cur = get_user(user_id)
    mode = cur["mode"] if mode is None else mode
    q_index = cur["q_index"] if q_index is None else q_index
    with _conn() as c:
        c.execute(
            """INSERT INTO users(user_id, mode, q_index, updated_at)
               VALUES(?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 mode=excluded.mode, q_index=excluded.q_index, updated_at=excluded.updated_at""",
            (user_id, mode, q_index, datetime.utcnow().isoformat()),
        )


@_retry
def add_history(user_id: int, role: str, content: str):
    with _conn() as c:
        c.execute(
            "INSERT INTO history(user_id, role, content, ts) VALUES(?,?,?,?)",
            (user_id, role, content, datetime.utcnow().isoformat()),
        )


@_retry
def get_history(user_id: int, limit: int = 8) -> List[Dict[str, str]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT role, content FROM history WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


@_retry
def clear_history(user_id: int):
    with _conn() as c:
        c.execute("DELETE FROM history WHERE user_id=?", (user_id,))


@_retry
def reset_user(user_id: int):
    with _conn() as c:
        c.execute(
            "UPDATE users SET mode='idle', q_index=NULL, updated_at=? WHERE user_id=?",
            (datetime.utcnow().isoformat(), user_id),
        )


@_retry
def record_answer(
    user_id: int,
    username: str | None,
    first_name: str | None,
    q_index: int,
    question: str,
    score: int | None,
):
    with _conn() as c:
        c.execute(
            """INSERT INTO answers(user_id, username, first_name, q_index, question, score, ts)
               VALUES(?,?,?,?,?,?,?)""",
            (
                user_id,
                username,
                first_name,
                q_index,
                question,
                score,
                datetime.utcnow().isoformat(),
            ),
        )


@_retry
def stats(limit: int = 100) -> List[Dict]:
    with _conn() as c:
        rows = c.execute(
            """SELECT user_id,
                      MAX(username) AS username,
                      MAX(first_name) AS first_name,
                      COUNT(*) AS cnt,
                      AVG(score) AS avg_score
               FROM answers
               GROUP BY user_id
               ORDER BY cnt DESC, avg_score DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [
        {
            "user_id": r[0],
            "username": r[1],
            "first_name": r[2],
            "count": r[3],
            "avg": r[4],
        }
        for r in rows
    ]


@_retry
def stats_totals() -> Dict:
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*), AVG(score), COUNT(DISTINCT user_id) FROM answers"
        ).fetchone()
    return {"count": row[0] or 0, "avg": row[1], "users": row[2] or 0}


@_retry
def count_users() -> int:
    with _conn() as c:
        row = c.execute("SELECT COUNT(*) FROM users").fetchone()
    return row[0] or 0


@_retry
def count_answers() -> int:
    with _conn() as c:
        row = c.execute("SELECT COUNT(*) FROM answers").fetchone()
    return row[0] or 0


@_retry
def columns(table: str = "users") -> List[str]:
    """Колонки таблицы — для диагностики схемы."""
    with _conn() as c:
        rows = c.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name=?",
            (table,),
        ).fetchall()
    return [r[0] for r in rows]


# ---------- платный доступ ----------

@_retry
def access(user_id: int) -> Dict:
    with _conn() as c:
        row = c.execute(
            "SELECT demo_used, paid_until, granted FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()
    if row:
        return {"demo_used": row[0] or 0, "paid_until": row[1], "granted": row[2] or 0}
    return {"demo_used": 0, "paid_until": None, "granted": 0}


@_retry
def is_paid(user_id: int) -> bool:
    a = access(user_id)
    return bool(a["granted"]) or (a["paid_until"] is not None and a["paid_until"] > time.time())


@_retry
def is_banned(user_id: int) -> bool:
    with _conn() as c:
        row = c.execute("SELECT banned FROM users WHERE user_id=?", (user_id,)).fetchone()
    return bool(row and row[0])


@_retry
def set_banned(user_id: int, banned: bool):
    now = datetime.utcnow().isoformat()
    with _conn() as c:
        c.execute(
            """INSERT INTO users(user_id, mode, banned, updated_at) VALUES(?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET banned=excluded.banned, updated_at=excluded.updated_at""",
            (user_id, "idle", 1 if banned else 0, now),
        )


@_retry
def inc_demo(user_id: int) -> int:
    """Один запрос с RETURNING — атомарно и без лишнего round-trip."""
    now = datetime.utcnow().isoformat()
    with _conn() as c:
        row = c.execute(
            """INSERT INTO users(user_id, mode, demo_used, updated_at) VALUES(?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 demo_used=users.demo_used+1, updated_at=excluded.updated_at
               RETURNING demo_used""",
            (user_id, "idle", 1, now),
        ).fetchone()
    return row[0]


@_retry
def set_paid(user_id: int, until_ts: int):
    now = datetime.utcnow().isoformat()
    with _conn() as c:
        c.execute(
            """INSERT INTO users(user_id, mode, paid_until, updated_at) VALUES(?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET paid_until=excluded.paid_until, updated_at=excluded.updated_at""",
            (user_id, "idle", int(until_ts), now),
        )


@_retry
def grant(user_id: int, days: Optional[int] = None):
    """Админ выдаёт доступ: без days — навсегда (granted), с days — на срок."""
    now = datetime.utcnow().isoformat()
    if days:
        until = int(time.time()) + days * 86400
        with _conn() as c:
            c.execute(
                """INSERT INTO users(user_id, mode, paid_until, updated_at) VALUES(?,?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET paid_until=excluded.paid_until, updated_at=excluded.updated_at""",
                (user_id, "idle", until, now),
            )
    else:
        with _conn() as c:
            c.execute(
                """INSERT INTO users(user_id, mode, granted, updated_at) VALUES(?,?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET granted=1, updated_at=excluded.updated_at""",
                (user_id, "idle", 1, now),
            )


@_retry
def record_payment(
    user_id: int, charge_id: str | None, amount: int, currency: str,
    is_recurring: bool, until_ts: int,
):
    """Пишет платёж один раз: повторный вызов с тем же charge_id игнорируется."""
    with _conn() as c:
        if charge_id:
            exists = c.execute(
                "SELECT 1 FROM payments WHERE charge_id=? LIMIT 1", (charge_id,)
            ).fetchone()
            if exists:
                return
        c.execute(
            """INSERT INTO payments(user_id, charge_id, amount, currency, is_recurring, until_ts, ts)
               VALUES(?,?,?,?,?,?,?)""",
            (
                user_id,
                charge_id,
                amount,
                currency,
                1 if is_recurring else 0,
                int(until_ts),
                datetime.utcnow().isoformat(),
            ),
        )


# ---------- настройки ----------

@_retry
def get_setting(key: str, default: str = "") -> str:
    with _conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row and row[0] is not None else default


@_retry
def set_setting(key: str, value: str):
    with _conn() as c:
        c.execute(
            "INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


@_retry
def users_overview(limit: int = 200) -> List[Dict]:
    """Список пользователей: доступ, демо, число ответов и средний балл."""
    with _conn() as c:
        rows = c.execute(
            """WITH ids AS (
                   SELECT user_id FROM users
                   UNION
                   SELECT user_id FROM answers
               )
               SELECT ids.user_id,
                      MAX(u.demo_used) AS demo_used,
                      MAX(u.paid_until) AS paid_until,
                      MAX(u.granted) AS granted,
                      COUNT(a.id) AS cnt,
                      AVG(a.score) AS avg_score,
                      MAX(a.username) AS username,
                      MAX(a.first_name) AS first_name
               FROM ids
               LEFT JOIN users u ON u.user_id = ids.user_id
               LEFT JOIN answers a ON a.user_id = ids.user_id
               GROUP BY ids.user_id
               ORDER BY cnt DESC, ids.user_id
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [
        {
            "user_id": r[0],
            "demo_used": r[1] or 0,
            "paid_until": r[2],
            "granted": r[3] or 0,
            "count": r[4] or 0,
            "avg": r[5],
            "username": r[6],
            "first_name": r[7],
        }
        for r in rows
    ]
