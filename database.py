import sqlite3
import threading
from pathlib import Path

DB_PATH = Path("data/guardian.db")

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute("PRAGMA temp_store=MEMORY")
        _conn.execute("PRAGMA cache_size=-2000")
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS container_configs (
                name TEXT PRIMARY KEY,
                priority INTEGER DEFAULT 5,
                suspended INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY,
                cpu_threshold REAL DEFAULT 80,
                ram_threshold REAL DEFAULT 80,
                check_interval INTEGER DEFAULT 10,
                cpu_high_streak INTEGER DEFAULT 3
            );
            """
        )
        cols = {row[1] for row in _conn.execute("PRAGMA table_info(settings)")}
        if "cpu_high_streak" not in cols:
            _conn.execute(
                "ALTER TABLE settings ADD COLUMN cpu_high_streak INTEGER DEFAULT 3"
            )
        _conn.commit()
    return _conn


def load_settings():
    with _lock:
        row = _connect().execute(
            """
            SELECT cpu_threshold, ram_threshold, check_interval, cpu_high_streak
            FROM settings WHERE id=1
            """
        ).fetchone()
        if not row:
            return None
        streak = row["cpu_high_streak"]
        if streak is None:
            streak = 3
        return {
            "cpu_threshold": row["cpu_threshold"],
            "ram_threshold": row["ram_threshold"],
            "check_interval": row["check_interval"],
            "cpu_high_streak": int(streak),
        }


def load_containers():
    with _lock:
        rows = _connect().execute(
            "SELECT name, priority, suspended FROM container_configs"
        ).fetchall()
        return [
            {
                "name": r["name"],
                "priority": int(r["priority"]),
                "suspended": bool(r["suspended"]),
            }
            for r in rows
        ]


def upsert_container(name, priority=None, suspended=None):
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT priority, suspended FROM container_configs WHERE name=?",
            (name,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO container_configs(name, priority, suspended) VALUES (?, ?, ?)",
                (
                    name,
                    5 if priority is None else int(priority),
                    0 if suspended is None else int(bool(suspended)),
                ),
            )
        else:
            conn.execute(
                "UPDATE container_configs SET priority=?, suspended=? WHERE name=?",
                (
                    row["priority"] if priority is None else int(priority),
                    row["suspended"] if suspended is None else int(bool(suspended)),
                    name,
                ),
            )
        conn.commit()


def upsert_settings(cpu_threshold, ram_threshold, check_interval, cpu_high_streak=3):
    with _lock:
        conn = _connect()
        conn.execute(
            """
            INSERT INTO settings(id, cpu_threshold, ram_threshold, check_interval, cpu_high_streak)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                cpu_threshold=excluded.cpu_threshold,
                ram_threshold=excluded.ram_threshold,
                check_interval=excluded.check_interval,
                cpu_high_streak=excluded.cpu_high_streak
            """,
            (cpu_threshold, ram_threshold, check_interval, int(cpu_high_streak)),
        )
        conn.commit()
