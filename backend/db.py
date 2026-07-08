"""
db.py — слой хранения данных.

Хранилище: Supabase (PostgreSQL).
Раньше был SQLite-файл на Railway Volume — он слетал при каждом редеплое и
жил только внутри одного контейнера. Теперь всё пишется в общую базу Supabase,
поэтому данные переживают редеплои и доступны откуда угодно.

Подключение берётся из переменной окружения DATABASE_URL (или SUPABASE_DB_URL):
это строка вида
    postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres
(Supabase → Project Settings → Database → Connection string → "Transaction"/"Session" pooler).

Ключевые таблицы:
- games.pinned   — игры, которые трекаются ВСЕГДА (стартовый набор + добавленные
  пользователями), даже если выпали из топ-чарта Steam.
- games.added_by — 'system' (обнаружена автоматически / стартовый набор) или
  'user' (добавлена через форму на сайте) — это и есть "сохранено для всех".
- app_names      — кэш «appid → реальное название» из Steam GetAppList.
"""
import os
import time
from contextlib import contextmanager

import psycopg2
from psycopg2 import pool as pg_pool
from psycopg2.extras import RealDictCursor, execute_values

DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")

_POOL: pg_pool.ThreadedConnectionPool | None = None


def _ensure_dsn() -> str:
    if not DATABASE_URL:
        raise RuntimeError(
            "Не задана переменная окружения DATABASE_URL (строка подключения к Supabase Postgres). "
            "Возьми её в Supabase → Project Settings → Database → Connection string и пропиши "
            "в переменных окружения Railway."
        )
    # Supabase требует TLS — добавляем sslmode=require, если он не указан явно.
    dsn = DATABASE_URL
    if "sslmode=" not in dsn:
        sep = "&" if "?" in dsn else "?"
        dsn = f"{dsn}{sep}sslmode=require"
    return dsn


def _get_pool() -> pg_pool.ThreadedConnectionPool:
    global _POOL
    if _POOL is None:
        _POOL = pg_pool.ThreadedConnectionPool(1, 8, dsn=_ensure_dsn())
    return _POOL


def _q(sql: str) -> str:
    """SQLite использовал плейсхолдеры '?', psycopg2 — '%s'. Конвертируем."""
    return sql.replace("?", "%s")


class _Conn:
    """
    Тонкая обёртка, повторяющая привычный по sqlite интерфейс
    conn.execute(sql, params).fetchone()/fetchall(), чтобы остальной код
    (включая collector.py) не пришлось переписывать целиком.
    """

    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql: str, params=()):
        cur = self.raw.cursor(cursor_factory=RealDictCursor)
        cur.execute(_q(sql), params)
        return cur

    def executescript(self, sql: str):
        cur = self.raw.cursor()
        cur.execute(sql)
        return cur

    def cursor(self):
        return self.raw.cursor()


@contextmanager
def get_conn():
    pool = _get_pool()
    raw = pool.getconn()
    try:
        yield _Conn(raw)
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        pool.putconn(raw)


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
                ts          BIGINT NOT NULL,
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
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (source, source_id) DO NOTHING""",
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


def add_admin_game(source, source_id, name, image_url=None):
    """
    Добавление игры из админ-панели. В отличие от add_user_game принудительно
    ставит added_by='system' (даже если игра уже была добавлена игроком) —
    чтобы на карточке не висела плашка «добавлено игроком».
    """
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT 1 FROM games WHERE source=? AND source_id=?", (source, source_id)
        ).fetchone()
        if existing is None:
            conn.execute(
                """INSERT INTO games (source, source_id, name, image_url, pinned, added_by)
                   VALUES (?, ?, ?, ?, 1, 'system')
                   ON CONFLICT (source, source_id) DO NOTHING""",
                (source, source_id, name, image_url),
            )
        else:
            conn.execute(
                """UPDATE games SET name=?, image_url=COALESCE(?, image_url),
                   pinned=1, added_by='system' WHERE source=? AND source_id=?""",
                (name, image_url, source, source_id),
            )


def delete_game(source, source_id):
    """Полное удаление игры из трекинга вместе со всей её историей."""
    with get_conn() as conn:
        conn.execute("DELETE FROM snapshots WHERE source=? AND source_id=?", (source, source_id))
        conn.execute("DELETE FROM games WHERE source=? AND source_id=?", (source, source_id))


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


def seed_initial_games(steam_seed: list, roblox_seed: list, minecraft_seed: list | None = None):
    for g in steam_seed:
        upsert_game("steam", g["appid"], g["name"], pinned=True, added_by="system")
    for g in roblox_seed:
        upsert_game("roblox", g["universe_id"], g["name"], pinned=True, added_by="system")
    for g in (minecraft_seed or []):
        upsert_game("minecraft", g["address"], g["name"], pinned=True, added_by="system")


# ------------------------------------------------------------- app names ----
def get_app_name(appid: str):
    with get_conn() as conn:
        row = conn.execute("SELECT name FROM app_names WHERE appid=?", (appid,)).fetchone()
        return row["name"] if row else None


def bulk_upsert_app_names(pairs: list[tuple[str, str]]):
    if not pairs:
        return
    with get_conn() as conn:
        cur = conn.cursor()
        # GetAppList отдаёт ~150 000 имён — заливаем пачками, чтобы не упереться
        # в лимиты одного запроса по сети.
        for i in range(0, len(pairs), 2000):
            batch = pairs[i:i + 2000]
            execute_values(
                cur,
                "INSERT INTO app_names (appid, name) VALUES %s "
                "ON CONFLICT (appid) DO UPDATE SET name = EXCLUDED.name",
                batch,
                page_size=2000,
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
    """
    Последний снэпшот по каждой игре, отсортированный по онлайну.

    Раньше дельта считалась коррелированным подзапросом на КАЖДУЮ строку
    результата (SELECT ... WHERE s2.ts < s.ts ORDER BY ... LIMIT 1) — при
    росте истории снэпшотов и количества игр это лишняя нагрузка на Postgres
    на каждый вызов (а вызывается он раз в 5 секунд для рассылки по WS).
    Теперь дельта считается один раз оконной функцией LAG() по каждой игре.
    """
    params: list = []
    where_clause = ""
    if source and source.lower() != "all":
        where_clause = "WHERE g.source = ?"
        params.append(source.lower())

    with get_conn() as conn:
        rows = conn.execute(
            f"""
            WITH ranked AS (
                SELECT source, source_id, players, ts,
                       LAG(players) OVER (
                           PARTITION BY source, source_id ORDER BY ts
                       ) AS prev_players,
                       ROW_NUMBER() OVER (
                           PARTITION BY source, source_id ORDER BY ts DESC
                       ) AS rn
                FROM snapshots
            )
            SELECT g.source, g.source_id, g.name, g.image_url, g.added_by,
                   r.players, r.ts, r.prev_players
            FROM games g
            JOIN ranked r ON r.source = g.source AND r.source_id = g.source_id AND r.rn = 1
            {where_clause}
            ORDER BY r.players DESC
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
            "avg_24h": int(round(agg_24h["avg_24h"])) if agg_24h["avg_24h"] else None,
            "peak_all_time": agg["peak_all_time"],
            "avg_all_time": int(round(agg["avg_all_time"])) if agg["avg_all_time"] else None,
            "samples": agg["samples"],
        }
