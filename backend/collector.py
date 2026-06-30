"""
collector.py — опрос внешних API и запись снэпшотов онлайна.

Steam: ISteamChartsService/GetGamesByConcurrentPlayers — одним запросом топ-1000
игр с текущим онлайном (недокументированный, но публичный эндпоинт Steam Charts).
Если он временно недоступен — fallback на точечные запросы по pinned-играм.
Имена всех игр: GetAppList (основной) → SteamSpy (резерв) → store API (подчистка
по одному для игр с "Steam App #XXXXXX" названием).

Roblox: placeId из URL страницы игры → резолвим в universeId один раз при старте
через apis.roblox.com/universes/v1/places/<placeId>/universe. Только стартовый
список + добавленные пользователями (публичного топ-N API у Roblox нет).
"""
import asyncio
import logging
import os

import httpx

import db
from games_list import ROBLOX_GAMES, STEAM_GAMES

logger = logging.getLogger("collector")

STEAM_API_KEY = os.environ.get("STEAM_API_KEY")

STEAM_PLAYERS_URL = "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
STEAM_CHART_URL   = "https://api.steampowered.com/ISteamChartsService/GetGamesByConcurrentPlayers/v1/"
STEAM_APPLIST_URL = "https://api.steampowered.com/ISteamApps/GetAppList/v2/"
STEAM_APPLIST_FB  = "https://api.steampowered.com/IStoreService/GetAppList/v1/"
STEAM_STORE_URL   = "https://store.steampowered.com/api/appdetails"
STEAMSPY_URL      = "https://steamspy.com/api.php"
STEAM_HEADER_IMG  = "https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg"

ROBLOX_URL        = "https://games.roblox.com/v1/games"
ROBLOX_ICONS_URL  = "https://thumbnails.roblox.com/v1/games/icons"
ROBLOX_RESOLVE    = "https://apis.roblox.com/universes/v1/places/{place_id}/universe"

_app_names_loaded = False


def _with_key(params: dict) -> dict:
    if STEAM_API_KEY:
        params = {**params, "key": STEAM_API_KEY}
    return params


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ══════════════════════════════════════════════════════════════════
# STEAM: имена игр
# ══════════════════════════════════════════════════════════════════

async def refresh_app_names_steamapi(client: httpx.AsyncClient) -> bool:
    """GetAppList — все Steam-игры и их имена одним запросом."""
    for url in (STEAM_APPLIST_URL, STEAM_APPLIST_FB):
        try:
            r = await client.get(url, params=_with_key({}), timeout=30)
            r.raise_for_status()
            apps = r.json().get("applist", {}).get("apps", [])
            pairs = [(str(a["appid"]), a["name"]) for a in apps if a.get("name")]
            if not pairs:
                continue
            db.bulk_upsert_app_names(pairs)
            logger.info(f"app_names via {url}: {len(pairs)} имён")
            return True
        except Exception as e:
            logger.warning(f"GetAppList failed ({url}): {e}")
    return False


async def refresh_app_names_steamspy(client: httpx.AsyncClient) -> bool:
    """
    SteamSpy /api.php?request=all — публичный API без ключа, отдаёт
    ВСЕ ~75 000 игр с именами одним запросом. Хороший резерв при падении GetAppList.
    """
    try:
        r = await client.get(STEAMSPY_URL, params={"request": "all"}, timeout=60)
        r.raise_for_status()
        data = r.json()
        pairs = [(str(k), v["name"]) for k, v in data.items() if v.get("name")]
        if not pairs:
            return False
        db.bulk_upsert_app_names(pairs)
        logger.info(f"app_names via SteamSpy: {len(pairs)} имён")
        return True
    except Exception as e:
        logger.warning(f"SteamSpy failed: {e}")
        return False


async def refresh_app_names(client: httpx.AsyncClient) -> bool:
    global _app_names_loaded
    ok = await refresh_app_names_steamapi(client)
    if not ok:
        ok = await refresh_app_names_steamspy(client)
    _app_names_loaded = ok
    return ok


async def fetch_store_name(client: httpx.AsyncClient, appid: str) -> str | None:
    """
    Точечный запрос к Steam Store API — только для игр с неизвестным именем.
    Rate-limit жёсткий (~1 req/сек на IP), поэтому вызываем редко и с задержкой.
    """
    try:
        r = await client.get(
            STEAM_STORE_URL, params={"appids": appid, "filters": "basic"}, timeout=10
        )
        r.raise_for_status()
        d = r.json().get(appid, {})
        if d.get("success") and d.get("data", {}).get("name"):
            return d["data"]["name"]
    except Exception as e:
        logger.debug(f"store name fetch failed appid={appid}: {e}")
    return None


# ══════════════════════════════════════════════════════════════════
# STEAM: чарт и точечные запросы
# ══════════════════════════════════════════════════════════════════

async def fetch_steam_chart(client: httpx.AsyncClient, max_games: int = 1000) -> list[dict]:
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
            logger.info(f"steam chart: первая запись → {ranks[0]}")
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
        logger.warning(f"steam chart failed: {e}")
        return []


async def fetch_steam_players(client: httpx.AsyncClient, appid: str) -> int | None:
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
    """Для формы добавления: проверяет appid, возвращает dict или None."""
    count = await fetch_steam_players(client, appid)
    if count is None:
        return None
    name = db.get_app_name(appid)
    if not name:
        name = await fetch_store_name(client, appid)
    name = name or f"Steam App #{appid}"
    if name != db.get_app_name(appid):
        db.bulk_upsert_app_names([(appid, name)])
    return {"id": appid, "name": name, "image": STEAM_HEADER_IMG.format(appid=appid), "players": count}


# ══════════════════════════════════════════════════════════════════
# ROBLOX
# ══════════════════════════════════════════════════════════════════

async def resolve_roblox_universe_id(client: httpx.AsyncClient, place_id: str) -> str | None:
    """placeId (из URL игры) → universeId."""
    try:
        r = await client.get(ROBLOX_RESOLVE.format(place_id=place_id), timeout=10)
        r.raise_for_status()
        uid = r.json().get("universeId")
        return str(uid) if uid else None
    except Exception as e:
        logger.warning(f"roblox resolve place={place_id}: {e}")
        return None


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


async def _lookup_roblox(client: httpx.AsyncClient, universe_id: str):
    playing_map = await fetch_roblox_batch(client, [universe_id])
    info = playing_map.get(universe_id)
    if not info:
        return None
    icons = await fetch_roblox_icons(client, [universe_id])
    name = info.get("name") or f"Roblox Game #{universe_id}"
    return {"id": universe_id, "name": name, "image": icons.get(universe_id), "players": info["playing"]}


async def verify_and_fetch_roblox(client: httpx.AsyncClient, place_id: str):
    """
    Для формы добавления: принимает placeId (из URL roblox.com/games/<placeId>),
    резолвит в universeId, проверяет существование, возвращает dict или None.
    """
    universe_id = await resolve_roblox_universe_id(client, place_id)
    if not universe_id:
        return None
    return await _lookup_roblox(client, universe_id)


# ══════════════════════════════════════════════════════════════════
# Сид Roblox-игр: placeId → universeId при старте
# ══════════════════════════════════════════════════════════════════

async def resolve_and_seed_roblox(client: httpx.AsyncClient):
    """
    Конвертирует placeId из games_list в universeId и сохраняет в БД.
    Вызывается один раз при старте. Если резолвинг не прошёл (API недоступен) —
    просто пропускает ту игру, следующий перезапуск попробует снова.
    """
    sem = asyncio.Semaphore(5)  # не спамить Roblox

    async def resolve_one(g):
        async with sem:
            uid = await resolve_roblox_universe_id(client, g["place_id"])
            if uid:
                db.upsert_game("roblox", uid, g["name"], pinned=True, added_by="system")
                logger.info(f"roblox seeded: {g['name']} place={g['place_id']} → universe={uid}")
            else:
                logger.warning(f"roblox seed failed: {g['name']} place={g['place_id']}")

    already = {g["source_id"] for g in db.get_all_games("roblox")}
    # только игры, которых ещё нет (проверяем по имени — universeId ещё не знаем)
    # → резолвим все, upsert не навредит если уже есть
    await asyncio.gather(*(resolve_one(g) for g in ROBLOX_GAMES))


# ══════════════════════════════════════════════════════════════════
# Фоновое подтягивание имён для "Steam App #XXXXXX"
# ══════════════════════════════════════════════════════════════════

async def patch_unknown_names(client: httpx.AsyncClient, batch_size: int = 5):
    """
    Берёт до batch_size игр с именем "Steam App #..." и запрашивает
    настоящее имя через Steam Store API. Вызывается раз в несколько циклов.
    Rate-limit Store API ~200 req/5min, поэтому batch маленький.
    """
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT source_id FROM games
               WHERE source='steam' AND name LIKE 'Steam App #%'
               LIMIT ?""",
            (batch_size,),
        ).fetchall()

    for row in rows:
        appid = row["source_id"]
        name = await fetch_store_name(client, appid)
        if name:
            db.upsert_game("steam", appid, name, STEAM_HEADER_IMG.format(appid=appid))
            db.bulk_upsert_app_names([(appid, name)])
            logger.info(f"patched name: {appid} → {name}")
        await asyncio.sleep(1.5)  # уважаем rate-limit Steam Store


# ══════════════════════════════════════════════════════════════════
# Основной цикл
# ══════════════════════════════════════════════════════════════════

async def collect_once():
    async with httpx.AsyncClient() as client:
        # 1. Steam чарт — топ до 1000 игр одним запросом
        chart = await fetch_steam_chart(client)
        chart_appids = set()
        for entry in chart:
            appid, players = entry["appid"], entry["players"]
            chart_appids.add(appid)
            name = db.get_app_name(appid) or f"Steam App #{appid}"
            db.upsert_game("steam", appid, name, STEAM_HEADER_IMG.format(appid=appid))
            db.insert_snapshot("steam", appid, players)

        # 2. Pinned Steam-игры, не попавшие в чарт
        missing = [g for g in db.get_pinned_games("steam") if g["source_id"] not in chart_appids]
        sem = asyncio.Semaphore(10)

        async def one_steam(g):
            async with sem:
                count = await fetch_steam_players(client, g["source_id"])
                if count is not None:
                    db.insert_snapshot("steam", g["source_id"], count)

        if missing:
            await asyncio.gather(*(one_steam(g) for g in missing))

        # 3. Roblox — все трекаемые игры батчами по 50
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

    total_roblox = len([g for g in db.get_all_games("roblox")])
    logger.info(f"collect_once: steam={len(chart_appids)} roblox={total_roblox}")


async def collector_loop(interval_seconds: int = 60):
    db.init_db()

    # Немедленно подсеиваем известные имена Steam-игр из стартового списка
    db.seed_initial_games(STEAM_GAMES, [])  # Steam сидим сразу
    db.bulk_upsert_app_names([(g["appid"], g["name"]) for g in STEAM_GAMES])

    # Roblox: резолвим placeId → universeId при старте
    async with httpx.AsyncClient() as client:
        await resolve_and_seed_roblox(client)

    # Загружаем полный кэш имён Steam
    names_loaded = False
    async with httpx.AsyncClient() as client:
        names_loaded = await refresh_app_names(client)

    cycles = 0
    while True:
        try:
            await collect_once()
        except Exception as e:
            logger.exception(f"collector loop error: {e}")

        cycles += 1
        daily_cycles = max(1, (24 * 3600) // interval_seconds)

        # Подтягиваем имена для "Steam App #..." каждые 5 циклов
        if cycles % 5 == 0:
            async with httpx.AsyncClient() as client:
                await patch_unknown_names(client)

        # Обновляем полный кэш имён: каждый цикл, пока не загрузилось; потом раз в сутки
        if not names_loaded or cycles % daily_cycles == 0:
            async with httpx.AsyncClient() as client:
                names_loaded = await refresh_app_names(client)

        await asyncio.sleep(interval_seconds)
