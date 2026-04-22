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
from backend.rules.base_rules import RulesEngine
from backend.rules.rules_loader import load_rules
from backend.services.state_machine import advance_phase_by_host, transition_phase

# Per-ruleset engine cache. Rulesets are stateless (they only read point
# values from their JSON config), so a single instance per name is safe
# and avoids re-reading the config on every next_phase call.
_engine_cache: dict[str, RulesEngine] = {}


def _engine_for(game: Game) -> RulesEngine:
    """Return the rules engine for a game, loading on first use.

    Uses ``Game.ruleset`` — set at creation and immutable thereafter —
    so a game started with ``"high_risk"`` scores using ``HighRiskRules``
    and never gets silently downgraded to standard rules.
    """
    engine = _engine_cache.get(game.ruleset)
    if engine is None:
        engine = load_rules(game.ruleset)
        _engine_cache[game.ruleset] = engine
    return engine

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
        p.votes.clear()
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
        p.votes.clear()
    game.cards_on_table.clear()
    game.score_base_applied = False
    game.score_bonus_applied = False
    game.votes_locked = False


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


def create_game(ruleset: str = "standard", votes_per_player: int = 1) -> Game:
    # Fail fast on an unknown ruleset name so we never create a game
    # that would later fail at start or scoring time. The resulting
    # engine is cached for subsequent _engine_for(game) calls.
    _engine_cache.setdefault(ruleset, load_rules(ruleset))
    if votes_per_player not in (1, 2):
        raise ValueError("votes_per_player must be 1 or 2")

    game_id = uuid.uuid4().hex[:8].upper()
    game = Game(id=game_id, ruleset=ruleset, votes_per_player=votes_per_player)
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

    # Load the rules engine for this game at start time. create_game
    # already validated the name, but pre-warming here binds the chosen
    # ruleset to the active session and makes subsequent scoring a
    # straight cache hit.
    _engine_for(game)

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
        _engine_for(game).calculate_scores(game)
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
    """Add a single vote to the player's vote list.

    Can be called up to ``Game.votes_per_player`` times per player per round.
    Votes are rejected once the host has locked them (``Game.votes_locked``).
    """
    _require_card_number(card_number)
    game = _require_game(game_id)
    _require_phase(game, GamePhase.VOTE, "Voting")

    if game.votes_locked:
        raise ValueError("Votes are locked; no further votes can be submitted")
    if not game.cards_on_table:
        raise ValueError("There are no cards on the table to vote for yet")

    player = _require_player(game, player_id)
    if player.id == game.narrator_id:
        raise ValueError("Narrator cannot vote")
    if len(player.votes) >= game.votes_per_player:
        raise ValueError(
            f"This player has already cast {game.votes_per_player} vote(s) this round"
        )
    if card_number in player.votes:
        raise ValueError("You cannot vote for the same card twice")
    if card_number not in game.cards_on_table:
        raise ValueError(
            "Vote must be for a card that is on the table "
            "(one of the submitted card numbers)."
        )
    if card_number == player.card_played:
        raise ValueError("You cannot vote for your own card")

    player.votes.append(card_number)
    return game


def update_vote(game_id: str, player_id: str, card_numbers: list[int]) -> Game:
    """Replace a player's vote list entirely (for editing or multi-vote confirmation).

    Accepts 1 or 2 card numbers (up to ``Game.votes_per_player``). Replaces any
    previously submitted votes atomically. Rejected once votes are locked.
    """
    for card_number in card_numbers:
        _require_card_number(card_number)
    game = _require_game(game_id)
    _require_phase(game, GamePhase.VOTE, "Updating votes")

    if game.votes_locked:
        raise ValueError("Votes are locked; votes cannot be changed")
    if not game.cards_on_table:
        raise ValueError("There are no cards on the table to vote for yet")
    if not card_numbers:
        raise ValueError("At least one vote is required")
    if len(card_numbers) > game.votes_per_player:
        raise ValueError(
            f"Cannot cast more than {game.votes_per_player} vote(s) per player"
        )
    if len(card_numbers) != len(set(card_numbers)):
        raise ValueError("You cannot vote for the same card twice")

    player = _require_player(game, player_id)
    if player.id == game.narrator_id:
        raise ValueError("Narrator cannot vote")
    for card_number in card_numbers:
        if card_number not in game.cards_on_table:
            raise ValueError(
                "Vote must be for a card that is on the table "
                "(one of the submitted card numbers)."
            )
        if card_number == player.card_played:
            raise ValueError("You cannot vote for your own card")

    player.votes = list(card_numbers)
    return game


def lock_votes(game_id: str, requester_id: str) -> Game:
    """Lock all votes so no further submissions or edits are possible (host only).

    Once locked, the host may advance from VOTE to REVEAL_VOTES.
    The lock is cleared automatically on round reset.
    """
    game = _require_game(game_id)
    _require_phase(game, GamePhase.VOTE, "Locking votes")
    if requester_id != game.host_id:
        raise ValueError("Only the host can lock votes")
    if game.votes_locked:
        raise ValueError("Votes are already locked")
    game.votes_locked = True
    return game


# ---------------------------------------------------------------------------
# Available actions (sent to clients on every broadcast)
# ---------------------------------------------------------------------------


def available_actions(game: Game) -> list[str]:
    """Return the game-level actions currently available.

    This is broadcast inside every ``game_wire`` payload so the frontend
    never needs to replicate phase-transition conditions, active-player
    counts, or ruleset constants. The list is game-level (not per-player);
    the frontend layers identity checks (is the current player the host?)
    on top where needed.

    Action names map 1-to-1 to API call names:
    * ``"start_game"``      — LOBBY, ≥3 players present
    * ``"select_narrator"`` — SELECT_NARRATOR phase active
    * ``"submit_card"``     — PLAY_CARDS phase active
    * ``"submit_vote"``     — VOTE phase active
    * ``"next_phase"``      — host may advance; all round conditions satisfied
    """
    actions: list[str] = []
    phase = game.phase

    if phase == GamePhase.LOBBY:
        if len(game.players) >= 3:
            actions.append("start_game")

    elif phase == GamePhase.SELECT_NARRATOR:
        actions.append("select_narrator")

    elif phase == GamePhase.PLAY_CARDS:
        actions.append("submit_card")
        live = active_players(game)
        if live and all(p.card_played is not None for p in live):
            actions.append("next_phase")

    elif phase == GamePhase.VOTE:
        if not game.votes_locked:
            # Players may submit or update their votes while unlocked.
            actions.append("submit_vote")
            actions.append("update_vote")
            # Host can lock votes at any point while they are unlocked.
            actions.append("lock_votes")
        else:
            # Once locked, the host may advance to REVEAL_VOTES.
            actions.append("next_phase")

    elif phase in (
        GamePhase.REVEAL_VOTES,
        GamePhase.REVEAL_NARRATOR,
        GamePhase.SCORE_BASE,
        GamePhase.SCORE_BONUS,
        GamePhase.NEXT_ROUND,
        GamePhase.LEADERBOARD,
    ):
        actions.append("next_phase")

    return actions


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
