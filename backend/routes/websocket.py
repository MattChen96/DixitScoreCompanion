"""
WebSocket game rooms.

Protocol (matches ``.docs/API_SPECS.md``):

- Client connects to ``/ws/{game_id}``. The server sends an initial
  ``game_state`` event containing the full game.
- Each server message carries ``{event, ...}`` where most events include
  ``game`` (sanitized — no ``recovery_token``). The backend is the single
  source of truth: clients must replace their local state from ``game`` on
  every message that carries one.
- Client -> server events: ``join_room``, ``submit_card``, ``submit_vote``,
  ``update_vote``, ``confirm_narrator``, ``reconnect``, ``ping``.
- Server -> client events: ``game_state``, ``player_joined``, ``phase_changed``,
  ``card_submitted``, ``vote_submitted``, ``scores_updated``, ``game_error``
  (room-wide gameplay error, e.g. duplicate cards),
  ``player_reconnected``, ``player_disconnected``, ``pong`` (per-socket, no
  ``game``), and ``{"event": "error", "detail": str}`` for per-socket errors.
"""

import asyncio
import json
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.models.constants import MAX_CARD_NUMBER, MIN_CARD_NUMBER
from backend.models.game import Game
from backend.services import game_service

router = APIRouter()

# Active sockets per game room. An entry is only created when a socket joins.
connections: dict[str, set[WebSocket]] = defaultdict(set)

# Per-player socket registry. A single player may temporarily hold multiple
# sockets (e.g. an orphaned tab still in TCP close-wait while a new one
# reconnects). Sockets are reaped on disconnect.
player_sockets: dict[str, dict[str, set[WebSocket]]] = defaultdict(
    lambda: defaultdict(set)
)


def _attach_player_socket(game_id: str, player_id: str, ws: WebSocket) -> None:
    player_sockets[game_id][player_id].add(ws)


def _detach_socket(game_id: str, ws: WebSocket) -> None:
    room = player_sockets.get(game_id)
    if not room:
        return
    empty: list[str] = []
    for pid, sockets in room.items():
        sockets.discard(ws)
        if not sockets:
            empty.append(pid)
    for pid in empty:
        room.pop(pid, None)
    if not room:
        player_sockets.pop(game_id, None)


def game_wire(game: Game) -> dict[str, Any]:
    """Public, sanitized JSON projection of ``Game``.

    Strips ``recovery_token`` from every player so it never leaks in
    broadcasts. Augments the model dump with two computed fields that
    let the frontend remain rule-agnostic:

    * ``available_actions`` — list of action names currently valid for
      this game state (computed by ``game_service.available_actions``).
    * ``card_range`` — ``{min, max}`` for card number inputs, taken from
      the domain constants so the frontend has no hardcoded bounds.
    """
    data = game.model_dump(
        mode="json",
        exclude={"players": {"__all__": {"recovery_token"}}},
    )
    data["cards_on_table"] = sorted(data["cards_on_table"])
    data["available_actions"] = game_service.available_actions(game)
    data["card_range"] = {"min": MIN_CARD_NUMBER, "max": MAX_CARD_NUMBER}
    return data


async def _send_error(ws: WebSocket, detail: str) -> None:
    await ws.send_text(json.dumps({"event": "error", "detail": detail}))


async def _reject_recovery(ws: WebSocket) -> None:
    """Send the error frame and close with 1008.

    A tiny asyncio.sleep(0) between send and close ensures the message is
    fully flushed on every ASGI transport (uvicorn, TestClient, hypercorn)
    before the channel tears down; without it the memory-stream transport
    used by Starlette's TestClient can drop the buffered frame on close.
    """
    await _send_error(ws, "recovery_failed")
    await asyncio.sleep(0)
    await ws.close(code=1008)


async def _broadcast(game_id: str, payload: str) -> None:
    room = connections.get(game_id)
    if not room:
        return
    dead: set[WebSocket] = set()
    for ws in list(room):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.add(ws)
    if dead:
        room -= dead


async def notify_game_room(game: Game, event: str) -> None:
    """Broadcast ``{event, game}`` to every socket in ``game.id``."""
    payload = json.dumps({"event": event, "game": game_wire(game)})
    await _broadcast(game.id, payload)


async def notify_game_error(game: Game, error: str, message: str) -> None:
    """Broadcast a room-wide gameplay error.

    Schema: ``{event: "game_error", error, message, game}``. ``game`` carries
    the post-recovery state (e.g. the reset round) so clients can re-render
    immediately without an extra round-trip.
    """
    payload = json.dumps(
        {
            "event": "game_error",
            "error": error,
            "message": message,
            "game": game_wire(game),
        }
    )
    await _broadcast(game.id, payload)


async def _handle_client_action(
    ws: WebSocket, game_id: str, event: str, data: dict[str, Any]
) -> None:
    if event == "join_room":
        game = game_service.get_game(game_id)
        if game is None:
            await _send_error(ws, "Game not found")
            return
        await notify_game_room(game, "game_state")
        return

    if event == "reconnect":
        player_id = data.get("player_id")
        token = data.get("recovery_token")
        if not isinstance(player_id, str) or not isinstance(token, str):
            await _reject_recovery(ws)
            return
        try:
            game, _player = game_service.reconnect_player(game_id, player_id, token)
        except ValueError:
            await _reject_recovery(ws)
            return
        _attach_player_socket(game_id, player_id, ws)
        # Full snapshot to the reconnecting socket, then tell the room.
        await ws.send_text(
            json.dumps({"event": "game_state", "game": game_wire(game)})
        )
        await notify_game_room(game, "player_reconnected")
        return

    if event == "ping":
        player_id = data.get("player_id")
        if isinstance(player_id, str):
            game = game_service.get_game(game_id)
            if game is not None:
                try:
                    game_service.touch_player(game, player_id)
                    _attach_player_socket(game_id, player_id, ws)
                except ValueError:
                    # Unknown player — ignore the ping; the client will either
                    # reconnect with a valid token or fall back to the join
                    # screen.
                    pass
        await ws.send_text(json.dumps({"event": "pong"}))
        return

    if event in ("submit_card", "submit_vote"):
        try:
            player_id = data["player_id"]
            card_number = int(data["card_number"])
        except (KeyError, TypeError, ValueError):
            await _send_error(ws, "Missing or invalid player_id / card_number")
            return

        # Any live action is also a heartbeat.
        game = game_service.get_game(game_id)
        if game is not None:
            try:
                game_service.touch_player(game, player_id)
                _attach_player_socket(game_id, player_id, ws)
            except ValueError:
                # touch failures are non-fatal; the real action call below will
                # surface the canonical error.
                pass

        try:
            if event == "submit_card":
                game = game_service.submit_card(game_id, player_id, card_number)
                await notify_game_room(game, "card_submitted")
            else:
                game = game_service.submit_vote(game_id, player_id, card_number)
                await notify_game_room(game, "vote_submitted")
        except game_service.DuplicateCardError as exc:
            await notify_game_error(exc.game, exc.error, str(exc))
        except ValueError as exc:
            await _send_error(ws, str(exc))
        return

    if event == "update_vote":
        try:
            player_id = data["player_id"]
            card_numbers = [int(c) for c in data["card_numbers"]]
        except (KeyError, TypeError, ValueError):
            await _send_error(ws, "Missing or invalid player_id / card_numbers")
            return

        game = game_service.get_game(game_id)
        if game is not None:
            try:
                game_service.touch_player(game, player_id)
                _attach_player_socket(game_id, player_id, ws)
            except ValueError:
                pass

        try:
            game = game_service.update_vote(game_id, player_id, card_numbers)
            await notify_game_room(game, "vote_submitted")
        except ValueError as exc:
            await _send_error(ws, str(exc))
        return

    if event == "confirm_narrator":
        player_id = data.get("player_id") if isinstance(data, dict) else None
        if not isinstance(player_id, str):
            await _send_error(ws, "Missing or invalid player_id")
            return
        game = game_service.get_game(game_id)
        if game is not None:
            try:
                game_service.touch_player(game, player_id)
                _attach_player_socket(game_id, player_id, ws)
            except ValueError:
                pass
        try:
            game = game_service.confirm_narrator(game_id, player_id)
            await notify_game_room(game, "narrator_confirmed")
        except ValueError as exc:
            await _send_error(ws, str(exc))
        return

    await _send_error(ws, f"Unknown event: {event!r}")


@router.websocket("/ws/{game_id}")
async def websocket_endpoint(websocket: WebSocket, game_id: str) -> None:
    game = game_service.get_game(game_id)
    if game is None:
        await websocket.accept()
        await _send_error(websocket, "Game not found")
        await websocket.close(code=1008)
        return

    await websocket.accept()
    connections[game_id].add(websocket)

    try:
        await notify_game_room(game, "game_state")

        async for raw in websocket.iter_text():
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                await _send_error(websocket, "Invalid JSON")
                continue

            event = message.get("event")
            data = message.get("data")
            if not isinstance(data, dict):
                data = {}

            await _handle_client_action(websocket, game_id, event, data)

    except WebSocketDisconnect:
        pass
    finally:
        room = connections.get(game_id)
        if room is not None:
            room.discard(websocket)
            if not room:
                connections.pop(game_id, None)
        _detach_socket(game_id, websocket)
