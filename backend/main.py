import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from backend.routes import game as game_router
from backend.routes import websocket as websocket_router
from backend.services.heartbeat import heartbeat_loop

_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Background liveness scan: marks silent players disconnected and
    # broadcasts player_disconnected. Started here so a restart of the app
    # also restarts the scan.
    task = asyncio.create_task(heartbeat_loop(), name="heartbeat-loop")
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


app = FastAPI(title="Dixit Score Companion", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(game_router.router)
app.include_router(websocket_router.router)


@app.get("/")
def serve_index():
    return FileResponse(_FRONTEND_DIR / "index.html")


@app.get("/app.js")
def serve_app_js():
    return FileResponse(
        _FRONTEND_DIR / "app.js",
        media_type="application/javascript; charset=utf-8",
    )


@app.get("/health")
def health():
    return {"status": "ok"}
