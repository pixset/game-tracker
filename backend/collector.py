"""
collector.py — опрос внешних API и запись снэпшотов онлайна.

ВАЖНО про источник "топ-1000 игр":
Steam не даёт официального документированного способа получить топ-N игр по
живому онлайну за один вызов. Есть недокументированный, но широко
используемый эндпоинт ISteamChartsService/GetGamesByConcurrentPlayers —
именно он питает страницу store.steampowered.com/charts/mostplayed. Он не
требует ключа и одним запросом отдаёт сразу рейтинг игр с текущим онлайном —
то есть НЕ нужно дёргать GetNumberOfCurrentPlayers по каждой игре отдельно.

Поскольку Valve не документирует точные имена полей, парсинг ниже сделан
защитно (берёт первое подходящее имя поля из нескольких вариантов) и тихо
пропускает записи, которые не удалось разобрать — при необходимости легко
скорректировать под реальный ответ, глядя в логи (логируется пример первой
записи при старте).

Roblox: публичного "топ-N всех игр" API больше не существует (легаси
games.roblox.com/v1/games/list официально сломан и не поддерживается — это
подтверждено в форуме разработчиков Roblox). Поэтому Roblox-игры трекаются
только из стартового набора + добавленные пользователями. Зато точечный
запрос онлайна по известному universeId (games.roblox.com/v1/games) и иконка
(thumbnails.roblox.com) работают надёжно.
"""
import asyncio
import logging
import os

import httpx

import db
from games_list import ROBLOX_GAMES, STEAM_GAMES

logger = logging.getLogger("collector")

STEAM_API_KEY = os.environ.get("STEAM_API_KEY")  # опционально, см. README

STEAM_PLAYERS_URL = "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
STEAM_CHART_URL = "https://api.steampowered.com/ISteamChartsService/GetGamesByConcurrentPlayers/v1/"
STEAM_APPLIST_URL = "https://api.steampowered.com/ISteamApps/GetAppList/v2/"
STEAM_HEADER_IMG = "https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg"

ROBLOX_URL = "https://games.roblox.com/v1/games"
ROBLOX_ICONS_URL = "https://thumbnails.roblox.com/v1/games/icons"

_app_names_loaded = False


def _with_key(params: dict) -> dict:
    if STEAM_API_KEY:
        params = {**params, "key": STEAM_API_KEY}
    return params


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ----------------------------------------------------------- Steam: чарт ----
async def fetch_steam_chart(client: httpx.AsyncClient, max_games: int = 1000) -> list[dict]:
    """Топ игр по текущему онлайну одним запросом. См. предупреждение в шапке файла."""
    try:
        r = await client.get(STEAM_CHART_URL, params=_with_key({}), timeout=15)
        r.raise_for_status()
        data = r.json()
        ranks = (
            data.get("response", {}).get("ranks")
            or data.get("response", {}).get("games")
            or []
        )
        if ranks:
            logger.info(f"steam chart: пример первой записи -> {ranks[0]}")

        out = []
        for item in ranks[:max_games]:
            appid = item.get("appid") or item.get("app_id")
            players = (
                item.get("concurrent_in_game")
                or item.get("concurrent_players")
                or item.get("current_players")
                or item.get("players")
            )
            if appid is None or players is None:
                continue
            out.append({"appid": str(appid), "players": int(players)})
        return out
    except Exception as e:
        logger.warning(f"steam chart failed (см. README про STEAM_API_KEY): {e}")
        return []


async def refresh_app_names(client: httpx.AsyncClient):
    """GetAppList — реальные названия для ВСЕХ appid одним запросом. Кэшируется в БД."""
    global _app_names_loaded
    try:
        r = await client.get(STEAM_APPLIST_URL, params=_with_key({}), timeout=30)
        r.raise_for_status()
        apps = r.json().get("applist", {}).get("apps", [])
        pairs = [(str(a["appid"]), a["name"]) for a in apps if a.get("name")]
        db.bulk_upsert_app_names(pairs)
        _app_names_loaded = True
        logger.info(f"app_names: загружено {len(pairs)} названий")
    except Exception as e:
        logger.warning(f"refresh_app_names failed: {e}")


async def fetch_steam_players(client: httpx.AsyncClient, appid: str) -> int | None:
    """Точечный запрос — используется только для pinned-игр, не попавших в чарт."""
    try:
        r = await client.get(STEAM_PLAYERS_URL, params=_with_key({"appid": appid}), timeout=10)
        r.raise_for_status()
        data = r.json().get("response", {})
        if data.get("result") == 1:
            return data.get("player_count")
    except Exception as e:
        logger.warning(f"steam appid={appid} failed: {e}")
    return None


async def verify_and_fetch_steam(client: httpx.AsyncClient, appid: str):
    """Для формы 'добавить игру': appid → {id, name, image, players} или None."""
    count = await fetch_steam_players(client, appid)
    if count is None:
        return None
    name = db.get_app_name(appid) or f"Steam App #{appid}"
    image = STEAM_HEADER_IMG.format(appid=appid)
    return {"id": appid, "name": name, "image": image, "players": count}


# ---------------------------------------------------------------- Roblox ----
async def _lookup_roblox(client: httpx.AsyncClient, universe_id: str):
    playing_map = await fetch_roblox_batch(client, [universe_id])
    info = playing_map.get(universe_id)
    if not info:
        return None
    icons = await fetch_roblox_icons(client, [universe_id])
    name = info.get("name") or f"Roblox Game #{universe_id}"
    return {"id": universe_id, "name": name, "image": icons.get(universe_id), "players": info["playing"]}


async def resolve_roblox_universe_id(client: httpx.AsyncClient, place_id: str):
    """Конвертация placeId (из ссылки на игру) в universeId, нужный для опроса онлайна."""
    try:
        r = await client.get(
            f"https://apis.roblox.com/universes/v1/places/{place_id}/universe", timeout=10
        )
        r.raise_for_status()
        universe_id = r.json().get("universeId")
        return str(universe_id) if universe_id else None
    except Exception as e:
        logger.warning(f"roblox universe resolve failed for place={place_id}: {e}")
        return None


# ---------------------------------------------------------------- Roblox ----
async def fetch_roblox_batch(client: httpx.AsyncClient, universe_ids: list[str]) -> dict:
    if not universe_ids:
        return {}
    try:
        r = await client.get(
            ROBLOX_URL, params={"universeIds": ",".join(universe_ids)}, timeout=10
        )
        r.raise_for_status()
        result = {}
        for item in r.json().get("data", []):
            result[str(item["id"])] = {"playing": item.get("playing", 0), "name": item.get("name")}
        return result
    except Exception as e:
        logger.warning(f"roblox batch failed: {e}")
        return {}


async def fetch_roblox_icons(client: httpx.AsyncClient, universe_ids: list[str]) -> dict:
    if not universe_ids:
        return {}
    try:
        r = await client.get(
            ROBLOX_ICONS_URL,
            params={"universeIds": ",".join(universe_ids), "size": "150x150", "format": "Png"},
            timeout=10,
        )
        r.raise_for_status()
        result = {}
        for item in r.json().get("data", []):
            if item.get("state") == "Completed" and item.get("imageUrl"):
                result[str(item["targetId"])] = item["imageUrl"]
        return result
    except Exception as e:
        logger.warning(f"roblox icons failed: {e}")
        return {}


async def verify_and_fetch_roblox(client: httpx.AsyncClient, source_id: str):
    """
    Для формы 'добавить игру'. Пользователь может вставить как universeId,
    так и placeId (он в URL игры) — пробуем оба варианта, возвращаем
    {id, name, image, players} с ПРАВИЛЬНЫМ (universeId) id для сохранения,
    либо None, если игра не нашлась ни одним способом.
    """
    result = await _lookup_roblox(client, source_id)
    if result:
        return result

    universe_id = await resolve_roblox_universe_id(client, source_id)
    if universe_id:
        return await _lookup_roblox(client, universe_id)
    return None


# --------------------------------------------------------------- основной цикл ----
async def collect_once():
    async with httpx.AsyncClient() as client:
        # --- 1. Steam: топ по чарту одним запросом ---
        chart = await fetch_steam_chart(client)
        chart_appids = set()
        for entry in chart:
            appid, players = entry["appid"], entry["players"]
            chart_appids.add(appid)
            name = db.get_app_name(appid) or f"Steam App #{appid}"
            db.upsert_game("steam", appid, name, STEAM_HEADER_IMG.format(appid=appid))
            db.insert_snapshot("steam", appid, players)

        # --- 2. Steam: pinned-игры, которые чарт не покрыл (нишевые / добавленные) ---
        pinned_steam = db.get_pinned_games("steam")
        missing = [g for g in pinned_steam if g["source_id"] not in chart_appids]
        sem = asyncio.Semaphore(10)

        async def one_steam(g):
            async with sem:
                count = await fetch_steam_players(client, g["source_id"])
                if count is not None:
                    db.insert_snapshot("steam", g["source_id"], count)

        if missing:
            await asyncio.gather(*(one_steam(g) for g in missing))

        # --- 3. Roblox: все трекаемые игры (стартовый набор + добавленные) ---
        roblox_games = db.get_all_games("roblox")
        ids = [g["source_id"] for g in roblox_games]
        names_lookup = {g["source_id"]: g["name"] for g in roblox_games}

        for chunk in chunked(ids, 50):
            playing_map = await fetch_roblox_batch(client, chunk)
            icons_map = await fetch_roblox_icons(client, chunk)
            for uid, info in playing_map.items():
                name = info.get("name") or names_lookup.get(uid) or f"Roblox Game #{uid}"
                db.upsert_game("roblox", uid, name, icons_map.get(uid))
                db.insert_snapshot("roblox", uid, info["playing"])

    logger.info(f"collect_once: steam_chart={len(chart_appids)} roblox={len(ids)}")


async def collector_loop(interval_seconds: int = 60):
    db.init_db()
    db.seed_initial_games(STEAM_GAMES, ROBLOX_GAMES)

    cycles = 0
    async with httpx.AsyncClient() as client:
        await refresh_app_names(client)  # один раз при старте

    while True:
        try:
            await collect_once()
        except Exception as e:
            logger.exception(f"collector loop error: {e}")

        cycles += 1
        # обновляем кэш названий Steam-игр раз в сутки (примерно)
        if cycles % max(1, (24 * 3600) // interval_seconds) == 0:
            async with httpx.AsyncClient() as client:
                await refresh_app_names(client)

        await asyncio.sleep(interval_seconds)
