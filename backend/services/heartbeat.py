"""Background liveness scan.

Clients send ``{event: "ping"}`` every ~15s and every game action also
refreshes ``Player.last_seen``. This task wakes periodically and flips any
player that has gone silent to ``connected = False``, broadcasting a
``player_disconnected`` event so the UI can re-render.

The task is started from :mod:`backend.main`'s ``lifespan`` handler and
cancelled on shutdown. No direct socket traffic happens here — every outbound
message goes through :mod:`backend.routes.websocket` broadcast helpers.
"""

from __future__ import annotations

import asyncio
import logging
import time

from backend import store
from backend.models.game import Game

logger = logging.getLogger(__name__)

# How often the scan runs. Fast enough that a dropped socket is surfaced
# within a few seconds of the timeout expiring; slow enough that the work is
# negligible for any realistic number of rooms.
HEARTBEAT_SCAN_S = 10

# A player is declared disconnected after this many seconds of silence. Must
# comfortably exceed the client ping cadence (15s) so ordinary network jitter
# doesn't trip it.
HEARTBEAT_TIMEOUT_S = 45


def _expire_stale_players(game: Game, now: float) -> bool:
    """Mark silent players as disconnected. Returns True if anything changed."""
    changed = False
    for player in game.players:
        if player.connected and now - player.last_seen > HEARTBEAT_TIMEOUT_S:
            player.connected = False
            changed = True
    return changed


async def _scan_once() -> None:
    # Imported lazily to avoid a module-import cycle between the route module
    # and the service layer at app startup.
    from backend.routes import websocket as ws_routes

    now = time.time()
    # Snapshot to be safe against concurrent mutations of the store.
    for game in list(store.games.values()):
        if _expire_stale_players(game, now):
            try:
                await ws_routes.notify_game_room(game, "player_disconnected")
            except Exception:  # pragma: no cover - best-effort broadcast
                logger.exception("heartbeat: broadcast failed for game %s", game.id)


async def heartbeat_loop() -> None:
    """Run until cancelled. Swallows and logs per-iteration errors."""
    try:
        while True:
            try:
                await _scan_once()
            except Exception:  # pragma: no cover - never let the loop die
                logger.exception("heartbeat scan raised; continuing")
            await asyncio.sleep(HEARTBEAT_SCAN_S)
    except asyncio.CancelledError:
        raise
