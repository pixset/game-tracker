"""
main.py — точка входа FastAPI.

Запуск локально:   uvicorn main:app --reload --port 8000
Деплой на Railway:  uvicorn main:app --host 0.0.0.0 --port $PORT
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
from collector import collector_loop, verify_and_fetch_roblox, verify_and_fetch_steam

logging.basicConfig(level=logging.INFO)

COLLECT_INTERVAL_SECONDS = 60
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


class AddGameRequest(BaseModel):
    url: str   # полная ссылка на страницу игры


def _extract_steam_appid(url: str) -> str | None:
    """store.steampowered.com/app/730/... → '730'"""
    import re
    m = re.search(r'store\.steampowered\.com/app/(\d+)', url)
    return m.group(1) if m else None


def _extract_roblox_placeid(url: str) -> str | None:
    """roblox.com/games/2753915549/... → '2753915549'"""
    import re
    m = re.search(r'roblox\.com/games/(\d+)', url)
    return m.group(1) if m else None


class ConnectionManager:
    def __init__(self):
        self.active: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws)

    async def broadcast_leaderboard(self):
        if not self.active:
            return
        payload = db.get_leaderboard(None, 1000)
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


async def broadcast_loop():
    while True:
        await asyncio.sleep(5)
        await manager.broadcast_leaderboard()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    collector_task = asyncio.create_task(collector_loop(COLLECT_INTERVAL_SECONDS))
    broadcast_task = asyncio.create_task(broadcast_loop())
    yield
    collector_task.cancel()
    broadcast_task.cancel()


app = FastAPI(title="Game Tracker API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/top")
def top(source: str = "all", limit: int = 100):
    limit = max(1, min(limit, 1000))
    return db.get_leaderboard(source, limit)


@app.get("/api/games/{source}/{source_id}")
def game_stats(source: str, source_id: str):
    stats = db.get_game_stats(source, source_id)
    if not stats:
        raise HTTPException(status_code=404, detail="game not found")
    return stats


@app.get("/api/games/{source}/{source_id}/history")
def game_history(source: str, source_id: str, hours: int = 24):
    return db.get_game_history(source, source_id, hours)


@app.get("/api/compare")
async def compare(games: str, hours: int = 24):
    """
    games — список вида "steam:730,steam:570,roblox:920587237" (максимум 3).
    """
    pairs = []
    for token in games.split(","):
        token = token.strip()
        if not token or ":" not in token:
            continue
        source, source_id = token.split(":", 1)
        pairs.append((source.strip().lower(), source_id.strip()))
    pairs = pairs[:3]

    result = []
    for source, source_id in pairs:
        stats = db.get_game_stats(source, source_id)
        history = db.get_game_history(source, source_id, hours)
        if stats:
            result.append({**stats, "history": history})
        else:
            result.append({
                "source": source.upper(), "id": source_id,
                "name": f"{source}:{source_id}", "image": None,
                "current": None, "history": history,
            })
    return result


@app.post("/api/games/add")
async def add_game(body: AddGameRequest):
    url = body.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="Вставь ссылку на страницу игры в Steam или Roblox.")

    # Определяем платформу по URL
    steam_appid = _extract_steam_appid(url)
    roblox_placeid = _extract_roblox_placeid(url)

    if not steam_appid and not roblox_placeid:
        raise HTTPException(
            status_code=400,
            detail="Не удалось распознать ссылку. Поддерживается: "
                   "store.steampowered.com/app/... или roblox.com/games/...",
        )

    async with httpx.AsyncClient() as client:
        if steam_appid:
            result = await verify_and_fetch_steam(client, steam_appid)
            source = "steam"
        else:
            result = await verify_and_fetch_roblox(client, roblox_placeid)
            source = "roblox"

    if result is None:
        raise HTTPException(
            status_code=404,
            detail="Игра не найдена через публичный API. Убедись, что ссылка правильная и игра существует.",
        )

    resolved_id = result["id"]
    db.add_user_game(source, resolved_id, result["name"], result["image"])
    db.insert_snapshot(source, resolved_id, result["players"])
    await manager.broadcast_leaderboard()

    return {
        "source": source.upper(), "id": resolved_id,
        "name": result["name"], "image": result["image"], "current": result["players"],
    }


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        await websocket.send_json(db.get_leaderboard(None, 1000))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/")
    def index():
        return FileResponse(FRONTEND_DIR / "index.html")
