"""
Game service layer.

The only place that mutates game state. Validates inputs against the current
phase, delegates phase transitions to :mod:`backend.services.state_machine`,
and scoring to the rules engine (:mod:`backend.rules.rules_loader`).
"""

import time
import uuid
from typing import Optional

from backend import store
from backend.models.constants import MAX_CARD_NUMBER, MIN_CARD_NUMBER
from backend.models.game import Game, Player
from backend.models.game_phase import GamePhase
from backend.rules.rules_loader import load_rules
from backend.services.state_machine import advance_phase_by_host, transition_phase

# All scoring lives in the rules engine now. We hold a single instance for
# the whole process because StandardDixitRules is stateless; per-game rule
# selection would be a future extension.
_rules_engine = load_rules("standard")

# WebSocket "game_error" payload used when two players play the same card.
DUPLICATE_CARDS_ERROR = "duplicate_cards"
DUPLICATE_CARDS_MESSAGE = (
    "Two or more players selected the same card. The round has been reset."
)


class DuplicateCardError(Exception):
    """Raised when a PLAY_CARDS submission collides with a card already played.

    The service has already reset the round (cleared every ``card_played`` and
    ``cards_on_table``) before raising. ``self.game`` is the post-reset state.
    """

    def __init__(self, game: "Game") -> None:
        super().__init__(DUPLICATE_CARDS_MESSAGE)
        self.game = game
        self.error = DUPLICATE_CARDS_ERROR


def _reset_play_cards_round(game: Game) -> None:
    """Invalidate a PLAY_CARDS round: clear submissions, keep phase = PLAY_CARDS."""
    for p in game.players:
        p.card_played = None
        p.vote = None
    game.cards_on_table.clear()


def _reset_round_after_next(game: Game) -> None:
    """Clear per-round fields on NEXT_ROUND → SELECT_NARRATOR.

    This is round lifecycle, not scoring — it runs after both scoring
    passes have already been applied and prepares a fresh round. Keeping
    it here (rather than in the rules engine) avoids widening the
    engine's surface area for logic that isn't rule-dependent.
    """
    for p in game.players:
        p.card_played = None
        p.vote = None
    game.cards_on_table.clear()
    game.score_base_applied = False
    game.score_bonus_applied = False


def _has_duplicate_cards(game: Game) -> bool:
    cards = [p.card_played for p in game.players if p.card_played is not None]
    return len(cards) != len(set(cards))


def _require_game(game_id: str) -> Game:
    game = store.get_game(game_id)
    if game is None:
        raise ValueError("Game not found")
    return game


def _require_player(game: Game, player_id: str) -> Player:
    for p in game.players:
        if p.id == player_id:
            return p
    raise ValueError("Player not found")


def _require_phase(game: Game, expected: GamePhase, action: str) -> None:
    if game.phase != expected:
        raise ValueError(
            f"{action} is only allowed in {expected.value} phase; "
            f"current phase is {game.phase.value}."
        )


def _require_card_number(card_number: int) -> None:
    if card_number < MIN_CARD_NUMBER or card_number > MAX_CARD_NUMBER:
        raise ValueError(
            f"card_number must be between {MIN_CARD_NUMBER} and {MAX_CARD_NUMBER}, "
            f"got {card_number}."
        )


def create_game(ruleset: str = "standard") -> Game:
    game_id = uuid.uuid4().hex[:8].upper()
    game = Game(id=game_id, ruleset=ruleset)
    store.set_game(game_id, game)
    return game


def get_game(game_id: str) -> Optional[Game]:
    return store.get_game(game_id)


def join_game(game_id: str, nickname: str) -> tuple[Game, Player]:
    game = _require_game(game_id)
    if game.phase != GamePhase.LOBBY:
        raise ValueError("Game has already started")

    player_id = uuid.uuid4().hex[:8]
    player = Player(id=player_id, nickname=nickname)

    if not game.players:
        game.host_id = player_id

    game.players.append(player)
    return game, player


def start_game(game_id: str, requester_id: str) -> Game:
    game = _require_game(game_id)
    if requester_id != game.host_id:
        raise ValueError("Only the host can start the game")
    if game.phase != GamePhase.LOBBY:
        raise ValueError("Game has already started")
    if len(game.players) < 3:
        raise ValueError("At least 3 players required")

    transition_phase(game, requester_id, GamePhase.SELECT_NARRATOR)
    return game


def select_narrator(game_id: str, requester_id: str, narrator_id: str) -> Game:
    game = _require_game(game_id)
    if requester_id != game.host_id:
        raise ValueError("Only the host can select the narrator")
    _require_phase(game, GamePhase.SELECT_NARRATOR, "Selecting the narrator")

    if not any(p.id == narrator_id for p in game.players):
        raise ValueError("Narrator is not a player in this game")

    game.narrator_id = narrator_id
    transition_phase(game, requester_id, GamePhase.PLAY_CARDS)
    return game


def next_phase(game_id: str, requester_id: str) -> Game:
    game = _require_game(game_id)

    old_phase = game.phase

    # Defensive: never advance out of PLAY_CARDS while duplicate cards exist.
    # In normal operation submit_card already prevents this, but the guard keeps
    # the invariant local to the transition that would otherwise expose it.
    if old_phase == GamePhase.PLAY_CARDS and _has_duplicate_cards(game):
        _reset_play_cards_round(game)
        raise DuplicateCardError(game)

    advance_phase_by_host(game, requester_id)
    new_phase = game.phase

    if new_phase in (GamePhase.SCORE_BASE, GamePhase.SCORE_BONUS):
        _rules_engine.calculate_scores(game)
    elif old_phase == GamePhase.NEXT_ROUND and new_phase == GamePhase.SELECT_NARRATOR:
        _reset_round_after_next(game)

    return game


def submit_card(game_id: str, player_id: str, card_number: int) -> Game:
    _require_card_number(card_number)
    game = _require_game(game_id)
    _require_phase(game, GamePhase.PLAY_CARDS, "Submitting a card")

    player = _require_player(game, player_id)
    if player.card_played is not None:
        raise ValueError("This player has already submitted a card this round")

    # Each player must select a UNIQUE card. If this submission collides with
    # one already on the table, invalidate the whole round (clear every
    # card_played and cards_on_table) and signal the duplicate to the caller,
    # which is responsible for broadcasting the "game_error" event.
    if card_number in game.cards_on_table:
        _reset_play_cards_round(game)
        raise DuplicateCardError(game)

    player.card_played = card_number
    game.cards_on_table.append(card_number)
    return game


def submit_vote(game_id: str, player_id: str, card_number: int) -> Game:
    _require_card_number(card_number)
    game = _require_game(game_id)
    _require_phase(game, GamePhase.VOTE, "Voting")

    if not game.cards_on_table:
        raise ValueError("There are no cards on the table to vote for yet")

    player = _require_player(game, player_id)
    if player.id == game.narrator_id:
        raise ValueError("Narrator cannot vote")
    if player.vote is not None:
        raise ValueError("This player has already voted this round")
    if card_number not in game.cards_on_table:
        raise ValueError(
            "Vote must be for a card that is on the table "
            "(one of the submitted card numbers)."
        )

    player.vote = card_number
    return game


# ---------------------------------------------------------------------------
# Reconnect / liveness
# ---------------------------------------------------------------------------


def active_players(game: Game) -> list[Player]:
    """Players that are currently considered live (heartbeat fresh)."""
    return [p for p in game.players if p.connected]


def touch_player(game: Game, player_id: str) -> Player:
    """Mark a player as alive: connected=True and last_seen=now.

    Raises ``ValueError`` if the player is not in the game. Idempotent.
    """
    player = _require_player(game, player_id)
    player.connected = True
    player.last_seen = time.time()
    return player


def reconnect_player(game_id: str, player_id: str, recovery_token: str) -> tuple[Game, Player]:
    """Validate a recovery_token and bring a player back online.

    Returns the game + player on success. Raises ``ValueError`` with detail
    ``"recovery_failed"`` for any mismatch (unknown game, unknown player,
    wrong token) so the WS layer can answer with a single uniform error.
    """
    game = store.get_game(game_id)
    if game is None:
        raise ValueError("recovery_failed")
    player = next((p for p in game.players if p.id == player_id), None)
    if player is None or player.recovery_token != recovery_token:
        raise ValueError("recovery_failed")
    player.connected = True
    player.last_seen = time.time()
    return game, player
