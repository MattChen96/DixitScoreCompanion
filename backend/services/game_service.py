"""
Game service layer.

The only place that mutates game state. Validates inputs against the current
phase, delegates phase transitions to :mod:`backend.services.state_machine`,
and scoring to the rules engine (:mod:`backend.rules.rules_loader`).
"""

import base64
import io
import os
import time
import uuid
from typing import Optional

import qrcode

from backend import store
from backend.models.constants import MAX_CARD_NUMBER, MIN_CARD_NUMBER
from backend.models.game import Game, Player
from backend.models.game_phase import GamePhase, SubmissionStep
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
    "Two or more players selected the same card. Please re-enter your cards."
)


class DuplicateCardError(Exception):
    """Raised when a card declaration collides with a card already declared.

    The service has already reset the declarations (cleared every ``card_played``
    and ``cards_on_table``) before raising. ``self.game`` is the post-reset state.
    The phase remains TURN_SUBMISSION with submission_step=declaration.
    """

    def __init__(self, game: "Game") -> None:
        super().__init__(DUPLICATE_CARDS_MESSAGE)
        self.game = game
        self.error = DUPLICATE_CARDS_ERROR


def _reset_declarations(game: Game) -> None:
    """Invalidate declarations: clear card_played and cards_on_table.
    
    The phase stays TURN_SUBMISSION with submission_step=declaration.
    Does NOT clear votes (they shouldn't exist at this point anyway).
    """
    for p in game.players:
        p.card_played = None
    game.cards_on_table.clear()


def _reset_round_after_next(game: Game) -> None:
    """Clear per-round fields on NEXT_ROUND → TURN_SUBMISSION.

    This is round lifecycle, not scoring — it runs after both scoring
    passes have already been applied and prepares a fresh round. Keeping
    it here (rather than in the rules engine) avoids widening the
    engine's surface area for logic that isn't rule-dependent.

    Note: narrator_id is NOT cleared here; it is rotated from
    narrator_queue in next_phase after this function returns.
    """
    for p in game.players:
        p.card_played = None
        p.votes.clear()
    game.cards_on_table.clear()
    game.score_base_applied = False
    game.score_bonus_applied = False
    game.last_base_delta.clear()
    game.last_bonus_delta.clear()
    game.submission_step = None


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


def _generate_qr_code(game_id: str) -> str:
    """Return a base64 PNG data URI for a QR code linking to the game lobby.

    The URL encoded in the QR is ``https://{domain}/join/{game_id}`` where
    ``domain`` is read from the ``DIXIT_APP_DOMAIN`` environment variable
    (defaults to ``"localhost"`` for local development).

    The returned string starts with ``data:image/png;base64,`` and can be
    used directly as an ``<img src="...">`` attribute.
    """
    domain = os.environ.get("DIXIT_APP_DOMAIN", "localhost")
    url = f"https://{domain}/join/{game_id}"
    img = qrcode.make(url)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def create_game(ruleset: str = "standard", votes_per_player: int = 1) -> Game:
    # Fail fast on an unknown ruleset name so we never create a game
    # that would later fail at start or scoring time. The resulting
    # engine is cached for subsequent _engine_for(game) calls.
    _engine_cache.setdefault(ruleset, load_rules(ruleset))
    if votes_per_player not in (1, 2):
        raise ValueError("votes_per_player must be 1 or 2")

    game_id = uuid.uuid4().hex[:8].upper()
    game = Game(id=game_id, ruleset=ruleset, votes_per_player=votes_per_player)
    game.qr_code = _generate_qr_code(game_id)
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

    transition_phase(game, requester_id, GamePhase.NARRATOR_ORDERING)
    return game


def set_narrator_queue(game_id: str, requester_id: str, narrator_ids: list[str]) -> Game:
    """Set the narrator rotation order for the whole game (NARRATOR_ORDERING phase).

    Validates that narrator_ids contains all players exactly once, sets the
    narrator_queue, and auto-transitions the game to TURN_SUBMISSION with
    the first player as narrator.
    """
    game = _require_game(game_id)
    if requester_id != game.host_id:
        raise ValueError("Only the host can set the narrator order")
    _require_phase(game, GamePhase.NARRATOR_ORDERING, "Setting narrator queue")

    player_ids = {p.id for p in game.players}
    if len(narrator_ids) != len(game.players):
        raise ValueError(
            f"narrator_ids must include all {len(game.players)} players"
        )
    if len(narrator_ids) != len(set(narrator_ids)):
        raise ValueError("narrator_ids must not contain duplicates")
    if set(narrator_ids) != player_ids:
        raise ValueError("narrator_ids must contain exactly the IDs of all current players")

    game.narrator_queue = list(narrator_ids)
    game.narrator_queue_index = 0
    game.narrator_id = narrator_ids[0]
    # Auto-transition to TURN_SUBMISSION
    transition_phase(game, requester_id, GamePhase.TURN_SUBMISSION)
    game.submission_step = SubmissionStep.DECLARATION
    store.set_game(game_id, game)
    return game


def next_phase(game_id: str, requester_id: str) -> Game:
    game = _require_game(game_id)

    old_phase = game.phase

    # TURN_SUBMISSION can only advance to REVEAL_VOTES when in voting sub-step
    # and all active non-narrator players have voted.
    if old_phase == GamePhase.TURN_SUBMISSION:
        if game.submission_step != SubmissionStep.VOTING:
            raise ValueError(
                "Cannot advance to reveal: voting phase has not started yet"
            )
        live_voters = [
            p for p in active_players(game) if p.id != game.narrator_id
        ]
        if not live_voters or not all(len(p.votes) >= 1 for p in live_voters):
            raise ValueError(
                "Cannot advance to reveal: not all players have voted"
            )

    advance_phase_by_host(game, requester_id)
    new_phase = game.phase

    # Clear submission_step when leaving TURN_SUBMISSION
    if old_phase == GamePhase.TURN_SUBMISSION and new_phase == GamePhase.REVEAL_VOTES:
        game.submission_step = None

    if new_phase in (GamePhase.SCORING,):
        _engine_for(game).calculate_scores(game)
    elif old_phase == GamePhase.NEXT_ROUND and new_phase == GamePhase.TURN_SUBMISSION:
        _reset_round_after_next(game)
        # Rotate narrator from the queue
        if game.narrator_queue:
            game.narrator_queue_index = (
                game.narrator_queue_index + 1
            ) % len(game.narrator_queue)
            game.narrator_id = game.narrator_queue[game.narrator_queue_index]
        game.submission_step = SubmissionStep.DECLARATION

    return game


def _all_active_declared(game: Game) -> bool:
    """Check if all active players have declared a card."""
    live = active_players(game)
    return bool(live) and all(p.card_played is not None for p in live)


def _try_advance_to_voting(game: Game) -> bool:
    """Attempt to auto-advance from declaration to voting sub-step.
    
    Returns True if advancement occurred, False otherwise.
    Raises DuplicateCardError if duplicate cards are detected.
    """
    if game.phase != GamePhase.TURN_SUBMISSION:
        return False
    if game.submission_step != SubmissionStep.DECLARATION:
        return False
    if not _all_active_declared(game):
        return False
    
    # All players declared - validate no duplicates
    if _has_duplicate_cards(game):
        _reset_declarations(game)
        raise DuplicateCardError(game)
    
    # Success - auto-advance to voting
    game.submission_step = SubmissionStep.VOTING
    return True


def submit_card(game_id: str, player_id: str, card_number: int) -> Game:
    """Declare which card the player has played (TURN_SUBMISSION declaration step).
    
    When all active players have declared, validates that all cards are unique.
    If duplicates exist, resets all declarations and raises DuplicateCardError.
    If validation succeeds, auto-advances to the voting sub-step.
    """
    _require_card_number(card_number)
    game = _require_game(game_id)
    
    # Must be in TURN_SUBMISSION phase with declaration sub-step
    if game.phase != GamePhase.TURN_SUBMISSION:
        raise ValueError(
            f"Declaring a card is only allowed in TURN_SUBMISSION phase; "
            f"current phase is {game.phase.value}."
        )
    if game.submission_step != SubmissionStep.DECLARATION:
        raise ValueError(
            "Declaring a card is only allowed in the declaration step; "
            "currently in voting step."
        )

    player = _require_player(game, player_id)
    if player.card_played is not None:
        raise ValueError("This player has already declared a card this round")

    player.card_played = card_number
    game.cards_on_table.append(card_number)
    
    # Try to auto-advance to voting (will validate uniqueness)
    _try_advance_to_voting(game)
    
    return game


def submit_vote(game_id: str, player_id: str, card_number: int) -> Game:
    """Add a single vote to the player's vote list.

    Can be called up to ``Game.votes_per_player`` times per player per round.
    Votes remain editable until the host advances the phase.
    """
    _require_card_number(card_number)
    game = _require_game(game_id)
    
    # Must be in TURN_SUBMISSION phase with voting sub-step
    if game.phase != GamePhase.TURN_SUBMISSION:
        raise ValueError(
            f"Voting is only allowed in TURN_SUBMISSION phase; "
            f"current phase is {game.phase.value}."
        )
    if game.submission_step != SubmissionStep.VOTING:
        raise ValueError(
            "Voting is only allowed in the voting step; "
            "currently in declaration step."
        )

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
    previously submitted votes atomically. Votes stay editable until the host
    advances the phase.
    """
    for card_number in card_numbers:
        _require_card_number(card_number)
    game = _require_game(game_id)
    
    # Must be in TURN_SUBMISSION phase with voting sub-step
    if game.phase != GamePhase.TURN_SUBMISSION:
        raise ValueError(
            f"Updating votes is only allowed in TURN_SUBMISSION phase; "
            f"current phase is {game.phase.value}."
        )
    if game.submission_step != SubmissionStep.VOTING:
        raise ValueError(
            "Updating votes is only allowed in the voting step; "
            "currently in declaration step."
        )

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
    * ``"start_game"``        — LOBBY, ≥3 players present
    * ``"set_narrator_queue"`` — NARRATOR_ORDERING phase, host only
    * ``"submit_card"``       — TURN_SUBMISSION phase, declaration sub-step
    * ``"submit_vote"``       — TURN_SUBMISSION phase, voting sub-step
    * ``"next_phase"``        — host may advance; all round conditions satisfied
    """
    actions: list[str] = []
    phase = game.phase

    if phase == GamePhase.LOBBY:
        if len(game.players) >= 3:
            actions.append("start_game")

    elif phase == GamePhase.NARRATOR_ORDERING:
        actions.append("set_narrator_queue")

    elif phase == GamePhase.TURN_SUBMISSION:
        if game.submission_step == SubmissionStep.DECLARATION:
            actions.append("submit_card")
            # No next_phase in declaration - auto-advances when all cards validated
        elif game.submission_step == SubmissionStep.VOTING:
            actions.append("submit_vote")
            actions.append("update_vote")
            live_voters = [
                p for p in active_players(game) if p.id != game.narrator_id
            ]
            if live_voters and all(len(p.votes) >= 1 for p in live_voters):
                actions.append("next_phase")

    elif phase in (
        GamePhase.REVEAL_VOTES,
        GamePhase.REVEAL_NARRATOR,
        GamePhase.SCORING,
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
