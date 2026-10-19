import sqlite3
from datetime import datetime
from typing import List, Dict, Optional

from config import DB_PATH


def _conn():
    return sqlite3.connect(DB_PATH)


def init():
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS users (
                   user_id INTEGER PRIMARY KEY,
                   mode TEXT NOT NULL DEFAULT 'idle',
                   q_index INTEGER,
                   updated_at TEXT
               )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS history (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   user_id INTEGER NOT NULL,
                   role TEXT NOT NULL,
                   content TEXT NOT NULL,
                   ts TEXT NOT NULL
               )"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_hist_user ON history(user_id)")
        c.execute(
            """CREATE TABLE IF NOT EXISTS answers (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   user_id INTEGER NOT NULL,
                   username TEXT,
                   first_name TEXT,
                   q_index INTEGER,
                   question TEXT,
                   score INTEGER,
                   ts TEXT NOT NULL
               )"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_answers_user ON answers(user_id)")


def get_user(user_id: int) -> Dict:
    with _conn() as c:
        row = c.execute(
            "SELECT user_id, mode, q_index FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row:
            return {"user_id": row[0], "mode": row[1], "q_index": row[2]}
        return {"user_id": user_id, "mode": "idle", "q_index": None}


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


def add_history(user_id: int, role: str, content: str):
    with _conn() as c:
        c.execute(
            "INSERT INTO history(user_id, role, content, ts) VALUES(?,?,?,?)",
            (user_id, role, content, datetime.utcnow().isoformat()),
        )


def get_history(user_id: int, limit: int = 8) -> List[Dict[str, str]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT role, content FROM history WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


def clear_history(user_id: int):
    with _conn() as c:
        c.execute("DELETE FROM history WHERE user_id=?", (user_id,))


def reset_user(user_id: int):
    with _conn() as c:
        c.execute(
            "UPDATE users SET mode='idle', q_index=NULL, updated_at=? WHERE user_id=?",
            (datetime.utcnow().isoformat(), user_id),
        )


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


def stats(limit: int = 100) -> List[Dict]:
    with _conn() as c:
        rows = c.execute(
            """SELECT user_id, username, first_name,
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


def stats_totals() -> Dict:
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*), AVG(score), COUNT(DISTINCT user_id) FROM answers"
        ).fetchone()
    return {"count": row[0] or 0, "avg": row[1], "users": row[2] or 0}

