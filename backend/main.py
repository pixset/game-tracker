"""
main.py — точка входа FastAPI.

Запуск локально:   uvicorn main:app --reload --port 8000
Деплой на Railway:  Procfile/start command: uvicorn main:app --host 0.0.0.0 --port $PORT
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import db
from collector import collector_loop

logging.basicConfig(level=logging.INFO)

COLLECT_INTERVAL_SECONDS = 60
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


class ConnectionManager:
    def __init__(self):
        self.active: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws)

    async def broadcast_top10(self):
        if not self.active:
            return
        payload = db.get_top10()
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
    """Каждые несколько секунд проталкивает текущий топ-10 всем подключённым клиентам."""
    while True:
        await asyncio.sleep(5)
        await manager.broadcast_top10()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    collector_task = asyncio.create_task(collector_loop(COLLECT_INTERVAL_SECONDS))
    broadcast_task = asyncio.create_task(broadcast_loop())
    yield
    collector_task.cancel()
    broadcast_task.cancel()


app = FastAPI(title="Pulse.GG API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/top10")
def top10():
    return db.get_top10()


@app.get("/api/games/{source}/{source_id}")
def game_stats(source: str, source_id: str):
    stats = db.get_game_stats(source, source_id)
    if not stats:
        return {"error": "game not found"}
    return stats


@app.get("/api/games/{source}/{source_id}/history")
def game_history(source: str, source_id: str, hours: int = 24):
    return db.get_game_history(source, source_id, hours)


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        # сразу шлём текущий снэпшот при подключении
        await websocket.send_json(db.get_top10())
        while True:
            await websocket.receive_text()  # держим соединение живым (ping/pong от клиента)
    except WebSocketDisconnect:
        manager.disconnect(websocket)


# --- Отдаём статический фронтенд той же службой (удобно для Railway: один процесс) ---
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/")
    def index():
        return FileResponse(FRONTEND_DIR / "index.html")
