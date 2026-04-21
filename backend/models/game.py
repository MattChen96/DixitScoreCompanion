import time
import uuid
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from backend.models.game_phase import GamePhase


class Player(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    nickname: str
    score: int = 0
    card_played: Optional[int] = None
    vote: Optional[int] = None
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
    cards_on_table: list[int] = Field(default_factory=list)
    score_base_applied: bool = False
    score_bonus_applied: bool = False
