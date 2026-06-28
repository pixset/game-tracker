"""
db.py — слой хранения данных.

SQLite для старта (Railway Volume даёт persistent disk).
При росте нагрузки меняется на Postgres почти без переписывания кода —
запросы написаны на простом SQL без специфичных для SQLite функций,
кроме strftime (есть аналог в Postgres).
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
                source      TEXT NOT NULL,        -- 'steam' | 'roblox'
                source_id   TEXT NOT NULL,        -- appid / universeId
                name        TEXT NOT NULL,
                image_url   TEXT,
                PRIMARY KEY (source, source_id)
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                source      TEXT NOT NULL,
                source_id   TEXT NOT NULL,
                ts          INTEGER NOT NULL,     -- unix timestamp
                players     INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_snap_game_ts
                ON snapshots (source, source_id, ts);
            """
        )


def upsert_game(source: str, source_id: str, name: str, image_url: str | None = None):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO games (source, source_id, name, image_url)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source, source_id) DO UPDATE SET
                name = excluded.name,
                image_url = COALESCE(excluded.image_url, games.image_url)
            """,
            (source, source_id, name, image_url),
        )


def insert_snapshot(source: str, source_id: str, players: int, ts: int | None = None):
    ts = ts or int(time.time())
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO snapshots (source, source_id, ts, players) VALUES (?, ?, ?, ?)",
            (source, source_id, ts, players),
        )


def get_top10():
    """Последний снэпшот по каждой игре, отсортированный по онлайну."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT g.source, g.source_id, g.name, g.image_url,
                   s.players, s.ts,
                   (SELECT players FROM snapshots s2
                    WHERE s2.source = g.source AND s2.source_id = g.source_id
                      AND s2.ts < s.ts
                    ORDER BY s2.ts DESC LIMIT 1) AS prev_players
            FROM games g
            JOIN snapshots s ON s.source = g.source AND s.source_id = g.source_id
            WHERE s.ts = (
                SELECT MAX(ts) FROM snapshots s3
                WHERE s3.source = g.source AND s3.source_id = g.source_id
            )
            ORDER BY s.players DESC
            LIMIT 10
            """
        ).fetchall()
        return [
            {
                "id": r["source_id"],
                "source": r["source"].upper(),
                "name": r["name"],
                "image": r["image_url"],
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
            SELECT MAX(players) AS peak_all_time,
                   AVG(players) AS avg_all_time,
                   COUNT(*) AS samples
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
            "current": latest["players"] if latest else None,
            "last_update": latest["ts"] if latest else None,
            "peak_24h": agg_24h["peak_24h"],
            "avg_24h": round(agg_24h["avg_24h"]) if agg_24h["avg_24h"] else None,
            "peak_all_time": agg["peak_all_time"],
            "avg_all_time": round(agg["avg_all_time"]) if agg["avg_all_time"] else None,
            "samples": agg["samples"],
        }
