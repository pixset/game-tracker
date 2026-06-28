"""
collector.py — опрос внешних API и запись снэпшотов онлайна.

Steam: GetNumberOfCurrentPlayers — официальный публичный эндпоинт,
не требует ключа API.
Roblox: games.roblox.com/v1/games — официальный публичный эндпоинт каталога.
"""
import asyncio
import logging

import httpx

import db
from games_list import ROBLOX_GAMES, STEAM_GAMES

logger = logging.getLogger("collector")

STEAM_URL = "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
STEAM_HEADER_IMG = "https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg"
ROBLOX_URL = "https://games.roblox.com/v1/games"
ROBLOX_ICONS_URL = "https://thumbnails.roblox.com/v1/games/icons"


async def fetch_steam(client: httpx.AsyncClient, appid: str) -> int | None:
    try:
        r = await client.get(STEAM_URL, params={"appid": appid}, timeout=10)
        r.raise_for_status()
        data = r.json().get("response", {})
        if data.get("result") == 1:
            return data.get("player_count")
    except Exception as e:
        logger.warning(f"steam appid={appid} failed: {e}")
    return None


async def fetch_roblox_batch(client: httpx.AsyncClient, universe_ids: list[str]) -> dict:
    """Roblox поддерживает запрос сразу нескольких universeIds за один вызов."""
    if not universe_ids:
        return {}
    try:
        r = await client.get(
            ROBLOX_URL, params={"universeIds": ",".join(universe_ids)}, timeout=10
        )
        r.raise_for_status()
        result = {}
        for item in r.json().get("data", []):
            result[str(item["id"])] = item.get("playing", 0)
        return result
    except Exception as e:
        logger.warning(f"roblox batch failed: {e}")
        return {}


async def fetch_roblox_icons(client: httpx.AsyncClient, universe_ids: list[str]) -> dict:
    """Иконки игр Roblox — отдельный публичный эндпоинт thumbnails.roblox.com."""
    if not universe_ids:
        return {}
    try:
        r = await client.get(
            ROBLOX_ICONS_URL,
            params={
                "universeIds": ",".join(universe_ids),
                "size": "150x150",
                "format": "Png",
            },
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


async def collect_once():
    """Один проход сбора данных по всем играм из списка."""
    async with httpx.AsyncClient() as client:
        # --- Steam: запросы параллельно, но с лимитом одновременных соединений ---
        sem = asyncio.Semaphore(10)

        async def one_steam(game):
            async with sem:
                count = await fetch_steam(client, game["appid"])
                if count is not None:
                    db.upsert_game(
                        "steam", game["appid"], game["name"],
                        STEAM_HEADER_IMG.format(appid=game["appid"]),
                    )
                    db.insert_snapshot("steam", game["appid"], count)

        await asyncio.gather(*(one_steam(g) for g in STEAM_GAMES))

        # --- Roblox: батч-запрос на онлайн + батч-запрос на иконки ---
        universe_ids = [g["universe_id"] for g in ROBLOX_GAMES]
        playing_map = await fetch_roblox_batch(client, universe_ids)
        icons_map = await fetch_roblox_icons(client, universe_ids)
        for g in ROBLOX_GAMES:
            count = playing_map.get(g["universe_id"])
            if count is not None:
                db.upsert_game(
                    "roblox", g["universe_id"], g["name"],
                    icons_map.get(g["universe_id"]),
                )
                db.insert_snapshot("roblox", g["universe_id"], count)

    logger.info("collect_once: done")


async def collector_loop(interval_seconds: int = 60):
    """Бесконечный цикл сбора, запускается фоновой задачей при старте сервера."""
    db.init_db()
    while True:
        try:
            await collect_once()
        except Exception as e:
            logger.exception(f"collector loop error: {e}")
        await asyncio.sleep(interval_seconds)
