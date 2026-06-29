"""
db.py — слой хранения данных.

Ключевые изменения по сравнению с первой версией:
- games.pinned — игры, которые трекаются ВСЕГДА (стартовый набор + добавленные
  пользователями), даже если они выпали из топ-чарта Steam.
- games.added_by — 'system' (обнаружена автоматически / стартовый набор) или
  'user' (добавлена через форму на сайте) — это и есть "сохранено для всех":
  как только игра попадает в эту таблицу на сервере, её видят все посетители.
- app_names — кэш «appid → реальное название» из Steam GetAppList, чтобы не
  дёргать дорогой store-API на каждую игру из чарта.
"""
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "tracker.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS games (
                source      TEXT NOT NULL,
                source_id   TEXT NOT NULL,
                name        TEXT NOT NULL,
                image_url   TEXT,
                pinned      INTEGER NOT NULL DEFAULT 0,
                added_by    TEXT NOT NULL DEFAULT 'system',
                PRIMARY KEY (source, source_id)
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                source      TEXT NOT NULL,
                source_id   TEXT NOT NULL,
                ts          INTEGER NOT NULL,
                players     INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_snap_game_ts
                ON snapshots (source, source_id, ts);

            CREATE TABLE IF NOT EXISTS app_names (
                appid TEXT PRIMARY KEY,
                name  TEXT NOT NULL
            );
            """
        )


# ---------------------------------------------------------------- games ----
def upsert_game(source, source_id, name, image_url=None, pinned=None, added_by=None):
    """
    pinned=None  -> не трогать существующее значение (или 0 при первой вставке)
    pinned=True  -> закрепить (стартовый набор / добавлено пользователем)
    """
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT pinned FROM games WHERE source=? AND source_id=?",
            (source, source_id),
        ).fetchone()
        if existing is None:
            conn.execute(
                """INSERT INTO games (source, source_id, name, image_url, pinned, added_by)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (source, source_id, name, image_url, 1 if pinned else 0, added_by or "system"),
            )
        elif pinned is not None:
            conn.execute(
                """UPDATE games SET name=?, image_url=COALESCE(?, image_url), pinned=?
                   WHERE source=? AND source_id=?""",
                (name, image_url, 1 if pinned else 0, source, source_id),
            )
        else:
            conn.execute(
                """UPDATE games SET name=?, image_url=COALESCE(?, image_url)
                   WHERE source=? AND source_id=?""",
                (name, image_url, source, source_id),
            )


def add_user_game(source, source_id, name, image_url=None):
    """Добавление игры через форму на сайте — закрепляется навсегда и видна всем."""
    upsert_game(source, source_id, name, image_url, pinned=True, added_by="user")


def game_exists(source, source_id) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM games WHERE source=? AND source_id=?", (source, source_id)
        ).fetchone()
        return row is not None


def get_all_games(source: str):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT source_id, name, image_url, pinned, added_by FROM games WHERE source=?",
            (source,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_pinned_games(source: str):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT source_id, name FROM games WHERE source=? AND pinned=1",
            (source,),
        ).fetchall()
        return [dict(r) for r in rows]


def seed_initial_games(steam_seed: list, roblox_seed: list):
    for g in steam_seed:
        upsert_game("steam", g["appid"], g["name"], pinned=True, added_by="system")
    for g in roblox_seed:
        upsert_game("roblox", g["universe_id"], g["name"], pinned=True, added_by="system")


# ------------------------------------------------------------- app names ----
def get_app_name(appid: str):
    with get_conn() as conn:
        row = conn.execute("SELECT name FROM app_names WHERE appid=?", (appid,)).fetchone()
        return row["name"] if row else None


def bulk_upsert_app_names(pairs: list[tuple[str, str]]):
    if not pairs:
        return
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO app_names (appid, name) VALUES (?, ?) "
            "ON CONFLICT(appid) DO UPDATE SET name=excluded.name",
            pairs,
        )


# ------------------------------------------------------------- snapshots ----
def insert_snapshot(source: str, source_id: str, players: int, ts: int | None = None):
    ts = ts or int(time.time())
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO snapshots (source, source_id, ts, players) VALUES (?, ?, ?, ?)",
            (source, source_id, ts, players),
        )


def get_leaderboard(source: str | None = None, limit: int = 100):
    """Последний снэпшот по каждой игре, отсортированный по онлайну."""
    params: list = []
    where_clause = ""
    if source and source.lower() != "all":
        where_clause = "WHERE g.source = ?"
        params.append(source.lower())
    ts_join = "AND" if where_clause else "WHERE"

    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT g.source, g.source_id, g.name, g.image_url, g.added_by,
                   s.players, s.ts,
                   (SELECT players FROM snapshots s2
                    WHERE s2.source = g.source AND s2.source_id = g.source_id
                      AND s2.ts < s.ts
                    ORDER BY s2.ts DESC LIMIT 1) AS prev_players
            FROM games g
            JOIN snapshots s ON s.source = g.source AND s.source_id = g.source_id
            {where_clause}
            {ts_join} s.ts = (
                SELECT MAX(ts) FROM snapshots s3
                WHERE s3.source = g.source AND s3.source_id = g.source_id
            )
            ORDER BY s.players DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        return [
            {
                "id": r["source_id"],
                "source": r["source"].upper(),
                "name": r["name"],
                "image": r["image_url"],
                "added_by": r["added_by"],
                "current": r["players"],
                "delta": (r["players"] - r["prev_players"]) if r["prev_players"] is not None else 0,
                "ts": r["ts"],
            }
            for r in rows
        ]


def get_game_history(source: str, source_id: str, hours: int = 24):
    cutoff = int(time.time()) - hours * 3600
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT ts, players FROM snapshots
            WHERE source = ? AND source_id = ? AND ts >= ?
            ORDER BY ts ASC
            """,
            (source, source_id, cutoff),
        ).fetchall()
        return [{"ts": r["ts"], "players": r["players"]} for r in rows]


def get_game_stats(source: str, source_id: str):
    with get_conn() as conn:
        game = conn.execute(
            "SELECT * FROM games WHERE source = ? AND source_id = ?", (source, source_id)
        ).fetchone()
        if not game:
            return None

        agg = conn.execute(
            """
            SELECT MAX(players) AS peak_all_time, AVG(players) AS avg_all_time, COUNT(*) AS samples
            FROM snapshots WHERE source = ? AND source_id = ?
            """,
            (source, source_id),
        ).fetchone()

        cutoff_24h = int(time.time()) - 24 * 3600
        agg_24h = conn.execute(
            """
            SELECT MAX(players) AS peak_24h, AVG(players) AS avg_24h
            FROM snapshots WHERE source = ? AND source_id = ? AND ts >= ?
            """,
            (source, source_id, cutoff_24h),
        ).fetchone()

        latest = conn.execute(
            """
            SELECT players, ts FROM snapshots
            WHERE source = ? AND source_id = ?
            ORDER BY ts DESC LIMIT 1
            """,
            (source, source_id),
        ).fetchone()

        return {
            "source": source.upper(),
            "id": source_id,
            "name": game["name"],
            "image": game["image_url"],
            "added_by": game["added_by"],
            "current": latest["players"] if latest else None,
            "last_update": latest["ts"] if latest else None,
            "peak_24h": agg_24h["peak_24h"],
            "avg_24h": round(agg_24h["avg_24h"]) if agg_24h["avg_24h"] else None,
            "peak_all_time": agg["peak_all_time"],
            "avg_all_time": round(agg["avg_all_time"]) if agg["avg_all_time"] else None,
            "samples": agg["samples"],
        }
