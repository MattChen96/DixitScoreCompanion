import time
import uuid
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from backend.models.game_phase import GamePhase, SubmissionStep


class Player(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    nickname: str
    score: int = 0
    card_played: Optional[int] = None
    # Multi-vote support: list of card numbers voted this round (0, 1, or 2
    # entries depending on Game.votes_per_player). Empty for the narrator.
    # Supersedes the old single `vote` field; cleared on round reset.
    votes: list[int] = Field(default_factory=list)
    # --- reconnect / heartbeat ---
    # Persistent client identifier. Returned to the joining client only at
    # /join_game time and stored in localStorage. Server strips this from every
    # broadcast / REST response so it never leaks to other players.
    recovery_token: str = Field(default_factory=lambda: uuid.uuid4().hex)
    # Runtime liveness flag. Updated by ping/pong, any player action, and the
    # heartbeat scan task in backend.services.heartbeat.
    connected: bool = True
    last_seen: float = Field(default_factory=time.time)


class Game(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    players: list[Player] = Field(default_factory=list)
    host_id: Optional[str] = None
    narrator_id: Optional[str] = None
    phase: GamePhase = GamePhase.LOBBY
    # Sub-step within TURN_SUBMISSION phase. When phase != TURN_SUBMISSION,
    # this field is None. When entering TURN_SUBMISSION, it starts as
    # "declaration" and auto-advances to "voting" when all cards are validated.
    submission_step: Optional[SubmissionStep] = None
    cards_on_table: list[int] = Field(default_factory=list)
    score_base_applied: bool = False
    score_bonus_applied: bool = False
    # Per-player point deltas for the current round, keyed by player id.
    # Populated by the rules engine when SCORE_BASE / SCORE_BONUS is applied.
    # Cleared on round reset so the previous round's deltas never bleed into
    # the next round's display. An empty dict means scoring hasn't run yet.
    last_base_delta: dict[str, int] = Field(default_factory=dict)
    last_bonus_delta: dict[str, int] = Field(default_factory=dict)
    # Rules engine selector. Selects which RulesEngine implementation
    # to use for scoring and move validation. Defaults to "standard"
    # (canonical Dixit rules).
    ruleset: str = "standard"
    # Maximum votes each non-narrator player may cast per round (1 or 2).
    # Defaults to 1 (classic Dixit). Exposed in every broadcast so the
    # frontend can render the vote grid correctly without hardcoding.
    votes_per_player: int = 1
    # Queue of narrator player IDs for the game, set once during NARRATOR_ORDERING.
    # After /set_narrator_queue, this is locked for the whole game.
    narrator_queue: list[str] = Field(default_factory=list)
    # 0-based index into narrator_queue for the current round.
    # Increments (modulo queue length) on each NEXT_ROUND → TURN_SUBMISSION transition.
    narrator_queue_index: int = 0
    # Deprecated: kept for backward compatibility only. No longer set or checked.
    narrator_confirmed: bool = False
    # QR code as a base64 PNG data URI (data:image/png;base64,...).
    # Generated once at game creation. Stripped by game_wire so it is never
    # included in WebSocket broadcasts or any REST response other than
    # POST /create_game.
    qr_code: Optional[str] = None
    # Dynamic card range: updated whenever a player joins/leaves the lobby.
    # min is always 1; max equals the current number of players.
    card_range: dict[str, int] = Field(default_factory=lambda: {"min": 1, "max": 84})
