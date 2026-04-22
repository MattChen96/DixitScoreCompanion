"""REST endpoints (``.docs/API_SPECS.md``). All mutations flow through
:mod:`backend.services.game_service`; broadcasts use :mod:`backend.routes.websocket`.
"""

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from backend.models.constants import MAX_CARD_NUMBER, MIN_CARD_NUMBER
from backend.models.game import Game
from backend.models.game_phase import GamePhase
from backend.routes import websocket as ws_routes
from backend.services import game_service

router = APIRouter()

# ---------------------------------------------------------------------------
# Shared field types (single source of truth for request validation)
# ---------------------------------------------------------------------------

_ID_PATTERN = r"^[A-Fa-f0-9]{1,32}$"

GameIdField = Annotated[str, Field(min_length=1, max_length=32, pattern=_ID_PATTERN)]
PlayerIdField = Annotated[str, Field(min_length=1, max_length=32, pattern=_ID_PATTERN)]
NicknameField = Annotated[str, Field(min_length=1, max_length=40)]
CardNumberField = Annotated[int, Field(ge=MIN_CARD_NUMBER, le=MAX_CARD_NUMBER)]


class _Body(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


class JoinGameRequest(_Body):
    game_id: GameIdField
    nickname: NicknameField


class CreateGameRequest(_Body):
    ruleset: str = Field(default="standard", min_length=1, max_length=32)
    votes_per_player: int = Field(default=1, ge=1, le=2)


class StartGameRequest(_Body):
    game_id: GameIdField
    player_id: PlayerIdField


class NextPhaseRequest(_Body):
    game_id: GameIdField
    player_id: PlayerIdField


class SelectNarratorRequest(_Body):
    game_id: GameIdField
    player_id: PlayerIdField
    narrator_id: PlayerIdField


class SubmitCardRequest(_Body):
    game_id: GameIdField
    player_id: PlayerIdField
    card_number: CardNumberField


class SubmitVoteRequest(_Body):
    game_id: GameIdField
    player_id: PlayerIdField
    card_number: CardNumberField


class UpdateVoteRequest(_Body):
    game_id: GameIdField
    player_id: PlayerIdField
    card_numbers: list[CardNumberField] = Field(min_length=1, max_length=2)


class LockVotesRequest(_Body):
    game_id: GameIdField
    player_id: PlayerIdField


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCORE_LIKE_PHASES = frozenset(
    {GamePhase.SCORE_BASE, GamePhase.SCORE_BONUS, GamePhase.LEADERBOARD}
)


def _game_json(game: Game) -> dict[str, Any]:
    # Same sanitized projection used by every WebSocket broadcast.
    return ws_routes.game_wire(game)


def _http_from_value_error(exc: ValueError) -> HTTPException:
    msg = str(exc)
    status = 404 if msg == "Game not found" else 400
    return HTTPException(status_code=status, detail=msg)


def _next_phase_event(game: Game) -> str:
    return "scores_updated" if game.phase in _SCORE_LIKE_PHASES else "phase_changed"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/rulesets")
def list_rulesets() -> list[dict[str, str]]:
    from backend.rules.rules_loader import available_rulesets  # noqa: PLC0415

    return [{"name": name} for name in available_rulesets()]


@router.post("/create_game")
def create_game(body: CreateGameRequest = CreateGameRequest()) -> dict[str, Any]:
    try:
        game = game_service.create_game(
            ruleset=body.ruleset, votes_per_player=body.votes_per_player
        )
    except ValueError as exc:
        raise _http_from_value_error(exc) from exc
    return {"game_id": game.id}


@router.post("/join_game")
async def join_game(body: JoinGameRequest) -> dict[str, Any]:
    try:
        game, player = game_service.join_game(body.game_id, body.nickname)
    except ValueError as exc:
        raise _http_from_value_error(exc) from exc
    await ws_routes.notify_game_room(game, "player_joined")
    # recovery_token is returned ONLY here, to the joining client. Every other
    # response and every broadcast strips it (see _game_json / _game_wire).
    return {
        "player_id": player.id,
        "game_id": game.id,
        "recovery_token": player.recovery_token,
        "game": _game_json(game),
    }


@router.post("/start_game")
async def start_game(body: StartGameRequest) -> dict[str, Any]:
    try:
        game = game_service.start_game(body.game_id, body.player_id)
    except ValueError as exc:
        raise _http_from_value_error(exc) from exc
    await ws_routes.notify_game_room(game, "phase_changed")
    return {"game": _game_json(game)}


@router.post("/next_phase")
async def next_phase(body: NextPhaseRequest) -> dict[str, Any]:
    try:
        game = game_service.next_phase(body.game_id, body.player_id)
    except game_service.DuplicateCardError as exc:
        await ws_routes.notify_game_error(exc.game, exc.error, str(exc))
        return {"game": _game_json(exc.game)}
    except ValueError as exc:
        raise _http_from_value_error(exc) from exc
    await ws_routes.notify_game_room(game, _next_phase_event(game))
    return {"game": _game_json(game)}


@router.post("/select_narrator")
async def select_narrator(body: SelectNarratorRequest) -> dict[str, Any]:
    try:
        game = game_service.select_narrator(
            body.game_id, body.player_id, body.narrator_id
        )
    except ValueError as exc:
        raise _http_from_value_error(exc) from exc
    await ws_routes.notify_game_room(game, "phase_changed")
    return {"game": _game_json(game)}


@router.post("/submit_card")
async def submit_card(body: SubmitCardRequest) -> dict[str, Any]:
    try:
        game = game_service.submit_card(body.game_id, body.player_id, body.card_number)
    except game_service.DuplicateCardError as exc:
        await ws_routes.notify_game_error(exc.game, exc.error, str(exc))
        return {"game": _game_json(exc.game)}
    except ValueError as exc:
        raise _http_from_value_error(exc) from exc
    await ws_routes.notify_game_room(game, "card_submitted")
    return {"game": _game_json(game)}


@router.post("/submit_vote")
async def submit_vote(body: SubmitVoteRequest) -> dict[str, Any]:
    try:
        game = game_service.submit_vote(body.game_id, body.player_id, body.card_number)
    except ValueError as exc:
        raise _http_from_value_error(exc) from exc
    await ws_routes.notify_game_room(game, "vote_submitted")
    return {"game": _game_json(game)}


@router.post("/update_vote")
async def update_vote(body: UpdateVoteRequest) -> dict[str, Any]:
    try:
        game = game_service.update_vote(
            body.game_id, body.player_id, body.card_numbers
        )
    except ValueError as exc:
        raise _http_from_value_error(exc) from exc
    await ws_routes.notify_game_room(game, "vote_submitted")
    return {"game": _game_json(game)}


@router.post("/lock_votes")
async def lock_votes(body: LockVotesRequest) -> dict[str, Any]:
    try:
        game = game_service.lock_votes(body.game_id, body.player_id)
    except ValueError as exc:
        raise _http_from_value_error(exc) from exc
    await ws_routes.notify_game_room(game, "votes_locked")
    return {"game": _game_json(game)}
