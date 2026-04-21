"""End-to-end smoke test for the reconnect / heartbeat system.

Mirrors the seven scenarios from the player-reconnect plan. Uses FastAPI's
in-process TestClient (HTTP + WebSocket) so the only dependency is the app
itself; no network, no real sleeping on HEARTBEAT_TIMEOUT_S.

Run with:  .venv/bin/python -m scripts.smoke_reconnect
Exits non-zero on first failure.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

from fastapi.testclient import TestClient

from backend import store
from backend.main import app
from backend.models.game_phase import GamePhase
from backend.services import game_service, heartbeat, scoring
from backend.routes import websocket as ws_routes


PASS = "\033[32mOK\033[0m"
FAIL = "\033[31mFAIL\033[0m"


def _check(label: str, cond: bool, detail: str = "") -> None:
    tag = PASS if cond else FAIL
    print(f"  [{tag}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        raise SystemExit(1)


def _recv_n(ws, n: int) -> list[dict[str, Any]]:
    """Blocking read of exactly N frames. Starlette's TestClient websocket has
    no non-blocking receive; every test knows how many server pushes to expect.
    """
    msgs: list[dict[str, Any]] = []
    for _ in range(n):
        msgs.append(ws.receive_json())
    return msgs


def _reset_store() -> None:
    store.games.clear()


def _create_game(client: TestClient) -> str:
    r = client.post("/create_game")
    r.raise_for_status()
    return r.json()["game_id"]


def _join(client: TestClient, game_id: str, nickname: str) -> dict[str, Any]:
    r = client.post("/join_game", json={"game_id": game_id, "nickname": nickname})
    r.raise_for_status()
    body = r.json()
    _check(
        f"join_game({nickname}) returns recovery_token",
        isinstance(body.get("recovery_token"), str) and len(body["recovery_token"]) > 0,
    )
    _check(
        f"join_game({nickname}) 'game' is sanitized",
        all("recovery_token" not in p for p in body["game"]["players"]),
    )
    return body


def scenario_1_full_round(client: TestClient) -> None:
    print("\n[1] Baseline: 3 players, complete round, scoring works")
    _reset_store()
    gid = _create_game(client)
    a = _join(client, gid, "A")
    b = _join(client, gid, "B")
    c = _join(client, gid, "C")
    host_id = a["player_id"]

    client.post("/start_game", json={"game_id": gid, "player_id": host_id}).raise_for_status()
    client.post(
        "/select_narrator",
        json={"game_id": gid, "player_id": host_id, "narrator_id": host_id},
    ).raise_for_status()

    for who, card in [(a, 10), (b, 20), (c, 30)]:
        r = client.post(
            "/submit_card",
            json={"game_id": gid, "player_id": who["player_id"], "card_number": card},
        )
        r.raise_for_status()

    client.post("/next_phase", json={"game_id": gid, "player_id": host_id}).raise_for_status()  # -> VOTE

    for who in [b, c]:
        client.post(
            "/submit_vote",
            json={"game_id": gid, "player_id": who["player_id"], "card_number": 10},
        ).raise_for_status()

    for _ in range(3):  # VOTE -> REVEAL_VOTES -> REVEAL_NARRATOR -> SCORE_BASE
        client.post("/next_phase", json={"game_id": gid, "player_id": host_id}).raise_for_status()

    game = store.get_game(gid)
    assert game is not None
    _check("phase == SCORE_BASE", game.phase == GamePhase.SCORE_BASE)
    narrator = next(p for p in game.players if p.id == host_id)
    _check(
        "narrator 0 when all voters correct (both voted narrator's card)",
        narrator.score == 0,
        f"narrator.score={narrator.score}",
    )
    others = [p for p in game.players if p.id != host_id]
    _check(
        "every non-narrator got +2 (all-correct rule)",
        all(p.score == 2 for p in others),
        f"scores={[p.score for p in others]}",
    )


def scenario_2_disconnect_mid_play(client: TestClient) -> None:
    print("\n[2] Player B drops mid-PLAY_CARDS (before playing) -> heartbeat flips connected=False")
    _reset_store()
    gid = _create_game(client)
    a = _join(client, gid, "A")
    b = _join(client, gid, "B")
    c = _join(client, gid, "C")
    host_id = a["player_id"]

    client.post("/start_game", json={"game_id": gid, "player_id": host_id}).raise_for_status()
    client.post(
        "/select_narrator",
        json={"game_id": gid, "player_id": host_id, "narrator_id": host_id},
    ).raise_for_status()

    client.post(
        "/submit_card",
        json={"game_id": gid, "player_id": a["player_id"], "card_number": 11},
    ).raise_for_status()
    client.post(
        "/submit_card",
        json={"game_id": gid, "player_id": c["player_id"], "card_number": 33},
    ).raise_for_status()

    game = store.get_game(gid)
    assert game is not None
    # Force B's last_seen into the past to simulate the 45s timeout.
    b_player = next(p for p in game.players if p.id == b["player_id"])
    b_player.last_seen = time.time() - (heartbeat.HEARTBEAT_TIMEOUT_S + 5)

    asyncio.run(heartbeat._scan_once())

    _check(
        "B.connected flipped to False by heartbeat",
        b_player.connected is False,
    )
    active = game_service.active_players(game)
    _check(
        "active_players excludes B",
        b["player_id"] not in [p.id for p in active] and len(active) == 2,
    )


def scenario_3_reconnect_restores_state(client: TestClient) -> None:
    print("\n[3] Reconnect via WS restores exact pre-disconnect state")
    _reset_store()
    gid = _create_game(client)
    a = _join(client, gid, "A")
    b = _join(client, gid, "B")
    c = _join(client, gid, "C")
    host_id = a["player_id"]

    client.post("/start_game", json={"game_id": gid, "player_id": host_id}).raise_for_status()
    client.post(
        "/select_narrator",
        json={"game_id": gid, "player_id": host_id, "narrator_id": host_id},
    ).raise_for_status()
    client.post(
        "/submit_card",
        json={"game_id": gid, "player_id": a["player_id"], "card_number": 7},
    ).raise_for_status()
    client.post(
        "/submit_card",
        json={"game_id": gid, "player_id": c["player_id"], "card_number": 8},
    ).raise_for_status()

    # B drops
    game = store.get_game(gid)
    assert game is not None
    b_player = next(p for p in game.players if p.id == b["player_id"])
    b_player.connected = False
    b_player.last_seen = time.time() - 100

    # B reconnects
    with client.websocket_connect(f"/ws/{gid}") as ws:
        initial = _recv_n(ws, 1)  # game_state from websocket_endpoint()
        _check(
            "initial game_state sent on WS accept",
            initial[0].get("event") == "game_state",
            f"got={initial[0].get('event')}",
        )
        ws.send_json(
            {
                "event": "reconnect",
                "data": {
                    "player_id": b["player_id"],
                    "recovery_token": b["recovery_token"],
                },
            }
        )
        # reconnect triggers: 1) per-socket game_state, 2) broadcast player_reconnected
        msgs = _recv_n(ws, 2)
        events = [m.get("event") for m in msgs]
        _check(
            "received game_state on reconnect",
            "game_state" in events,
            f"events={events}",
        )
        _check(
            "received player_reconnected broadcast",
            "player_reconnected" in events,
            f"events={events}",
        )
        snapshot = next(m for m in msgs if m.get("event") == "game_state")
        b_in_snap = next(p for p in snapshot["game"]["players"] if p["id"] == b["player_id"])
        _check("B.connected == true after reconnect", b_in_snap["connected"] is True)
        _check("B.card_played preserved (still None)", b_in_snap["card_played"] is None)
        _check(
            "A's card preserved after B reconnect",
            next(
                p for p in snapshot["game"]["players"] if p["id"] == a["player_id"]
            )["card_played"]
            == 7,
        )
        _check(
            "recovery_token stripped from broadcast",
            all("recovery_token" not in p for p in snapshot["game"]["players"]),
        )


def scenario_4_narrator_played_then_dropped(client: TestClient) -> None:
    print("\n[4] Narrator plays, then drops -> scoring still advances")
    _reset_store()
    gid = _create_game(client)
    a = _join(client, gid, "A")
    b = _join(client, gid, "B")
    c = _join(client, gid, "C")
    host_id = a["player_id"]

    client.post("/start_game", json={"game_id": gid, "player_id": host_id}).raise_for_status()
    client.post(
        "/select_narrator",
        json={"game_id": gid, "player_id": host_id, "narrator_id": b["player_id"]},
    ).raise_for_status()
    for who, card in [(a, 41), (b, 42), (c, 43)]:
        client.post(
            "/submit_card",
            json={"game_id": gid, "player_id": who["player_id"], "card_number": card},
        ).raise_for_status()
    client.post("/next_phase", json={"game_id": gid, "player_id": host_id}).raise_for_status()  # VOTE
    for who in [a, c]:
        client.post(
            "/submit_vote",
            json={"game_id": gid, "player_id": who["player_id"], "card_number": 42},
        ).raise_for_status()

    # Narrator (B) drops after playing
    game = store.get_game(gid)
    assert game is not None
    b_player = next(p for p in game.players if p.id == b["player_id"])
    b_player.connected = False

    for _ in range(3):  # VOTE -> REVEAL_VOTES -> REVEAL_NARRATOR -> SCORE_BASE
        client.post("/next_phase", json={"game_id": gid, "player_id": host_id}).raise_for_status()

    _check("phase == SCORE_BASE (narrator drop after play is fine)", game.phase == GamePhase.SCORE_BASE)
    b_player = next(p for p in game.players if p.id == b["player_id"])
    _check("narrator (disconnected) still scored 0 (all voted correct)", b_player.score == 0)


def scenario_5_narrator_never_played_stall(client: TestClient) -> None:
    print("\n[5] Narrator disconnected before playing -> scoring blocked (stall)")
    _reset_store()
    gid = _create_game(client)
    a = _join(client, gid, "A")
    b = _join(client, gid, "B")
    c = _join(client, gid, "C")
    host_id = a["player_id"]

    client.post("/start_game", json={"game_id": gid, "player_id": host_id}).raise_for_status()
    client.post(
        "/select_narrator",
        json={"game_id": gid, "player_id": host_id, "narrator_id": b["player_id"]},
    ).raise_for_status()
    # A and C play; B (narrator) does not
    client.post(
        "/submit_card",
        json={"game_id": gid, "player_id": a["player_id"], "card_number": 11},
    ).raise_for_status()
    client.post(
        "/submit_card",
        json={"game_id": gid, "player_id": c["player_id"], "card_number": 22},
    ).raise_for_status()

    # B drops
    game = store.get_game(gid)
    assert game is not None
    b_player = next(p for p in game.players if p.id == b["player_id"])
    b_player.connected = False

    raised = False
    try:
        scoring._validate_round_complete(game)
    except ValueError as exc:
        raised = True
        _check(
            "validation error mentions narrator",
            "narrator" in str(exc).lower(),
            f"msg={exc}",
        )
    _check("scoring validation raised for missing narrator card", raised)


def scenario_6_duplicate_while_disconnected(client: TestClient) -> None:
    print("\n[6] Duplicate-card reset still works while a player is disconnected")
    _reset_store()
    gid = _create_game(client)
    a = _join(client, gid, "A")
    b = _join(client, gid, "B")
    c = _join(client, gid, "C")
    host_id = a["player_id"]

    client.post("/start_game", json={"game_id": gid, "player_id": host_id}).raise_for_status()
    client.post(
        "/select_narrator",
        json={"game_id": gid, "player_id": host_id, "narrator_id": host_id},
    ).raise_for_status()
    client.post(
        "/submit_card",
        json={"game_id": gid, "player_id": a["player_id"], "card_number": 50},
    ).raise_for_status()

    # C drops
    game = store.get_game(gid)
    assert game is not None
    c_player = next(p for p in game.players if p.id == c["player_id"])
    c_player.connected = False

    # B plays the same card A played -> duplicate
    resp = client.post(
        "/submit_card",
        json={"game_id": gid, "player_id": b["player_id"], "card_number": 50},
    )
    resp.raise_for_status()
    body = resp.json()
    _check("phase still PLAY_CARDS after duplicate reset", body["game"]["phase"] == "PLAY_CARDS")
    _check(
        "every card_played cleared after duplicate reset",
        all(p["card_played"] is None for p in body["game"]["players"]),
    )
    _check(
        "C still in game but disconnected after reset",
        any(p["id"] == c["player_id"] and p["connected"] is False for p in body["game"]["players"]),
    )


def scenario_7_invalid_token(client: TestClient) -> None:
    print("\n[7] Invalid recovery_token -> error + close")
    _reset_store()
    gid = _create_game(client)
    a = _join(client, gid, "A")
    _join(client, gid, "B")
    _join(client, gid, "C")

    # The server closes with 1008 after sending recovery_failed. Starlette's
    # TestClient raises WebSocketDisconnect on the next receive AND on context
    # teardown if the channel is already closed, so we drive the socket
    # manually here instead of with `with`.
    from starlette.websockets import WebSocketDisconnect as _StarletteDisconnect

    ws_ctx = client.websocket_connect(f"/ws/{gid}")
    ws = ws_ctx.__enter__()
    try:
        _recv_n(ws, 1)  # initial game_state
        ws.send_json(
            {
                "event": "reconnect",
                "data": {"player_id": a["player_id"], "recovery_token": uuid.uuid4().hex},
            }
        )
        err = ws.receive_json()
        _check(
            "server sent recovery_failed",
            err.get("event") == "error" and err.get("detail") == "recovery_failed",
            f"got={err}",
        )
        closed = False
        try:
            ws.receive_json()
        except Exception as exc:  # WebSocketDisconnect or anyio.ClosedResourceError
            closed = isinstance(exc, _StarletteDisconnect) or "Closed" in type(exc).__name__
        _check("server closed the socket", closed)
    finally:
        # Swallow the teardown error: the server already closed, so the
        # disconnect send inside starlette's TestClient raises.
        try:
            ws_ctx.__exit__(None, None, None)
        except Exception:
            pass


def main() -> None:
    print("=" * 60)
    print("Dixit reconnect/heartbeat smoke test")
    print("=" * 60)
    with TestClient(app) as client:
        scenario_1_full_round(client)
        scenario_2_disconnect_mid_play(client)
        scenario_3_reconnect_restores_state(client)
        scenario_4_narrator_played_then_dropped(client)
        scenario_5_narrator_never_played_stall(client)
        scenario_6_duplicate_while_disconnected(client)
        scenario_7_invalid_token(client)
    print("\nAll scenarios passed.")


if __name__ == "__main__":
    main()
