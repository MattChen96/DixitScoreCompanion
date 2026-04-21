"""
Dixit round scoring (SCORE_BASE / SCORE_BONUS). Deterministic; server is source of truth.
"""

from backend.models.game import Game, Player
from backend.models.game_phase import GamePhase


def _players_sorted(game: Game) -> list[Player]:
    return sorted(game.players, key=lambda p: p.id)


def _active_sorted(game: Game) -> list[Player]:
    """Players that are currently connected, sorted by id for determinism."""
    return sorted((p for p in game.players if p.connected), key=lambda p: p.id)


def _validate_unique_cards_played(game: Game) -> None:
    seen: set[int] = set()
    for p in _players_sorted(game):
        c = p.card_played
        if c is None:
            continue
        if c in seen:
            raise ValueError(
                "Cannot score this round: two or more players played the same card number; "
                "votes cannot be attributed uniquely."
            )
        seen.add(c)


def _validate_round_complete(game: Game) -> None:
    if not game.narrator_id:
        raise ValueError("Cannot score: narrator is not set.")

    narrator = next((p for p in game.players if p.id == game.narrator_id), None)
    if narrator is None:
        raise ValueError("Cannot score: narrator_id is not a player in this game.")

    # Stall policy: the narrator must always have a card on the table, even if
    # they are currently disconnected. This keeps "narrator missing before
    # playing" from silently scoring a bogus round.
    if narrator.card_played is None:
        raise ValueError(
            f"Cannot score: narrator {narrator.id!r} has not submitted a card yet."
        )

    # Only currently-active players gate progress — a player who dropped
    # before playing/voting must NOT block scoring. The stall window is
    # enforced upstream by the heartbeat task.
    for p in _active_sorted(game):
        if p.card_played is None:
            raise ValueError(
                f"Cannot score: every active player must have submitted a card; missing for player {p.id!r}."
            )

    for p in _active_sorted(game):
        if p.id != game.narrator_id and p.vote is None:
            raise ValueError(
                f"Cannot score: every active non-narrator must have voted; missing vote for player {p.id!r}."
            )

    _validate_unique_cards_played(game)


def apply_score_base(game: Game) -> None:
    if game.phase != GamePhase.SCORE_BASE:
        raise ValueError(
            f"Base scores can only be applied in SCORE_BASE phase; current phase is {game.phase.value}."
        )
    if game.score_base_applied:
        raise ValueError("Base scores already applied for this round.")

    _validate_round_complete(game)

    narrator = next(p for p in game.players if p.id == game.narrator_id)
    narrator_card = narrator.card_played
    assert narrator_card is not None

    voters = [p for p in _players_sorted(game) if p.id != game.narrator_id]
    correct = [p for p in voters if p.vote == narrator_card]
    n_voters = len(voters)
    n_correct = len(correct)

    if n_correct == 0 or n_correct == n_voters:
        for p in _players_sorted(game):
            if p.id != game.narrator_id:
                p.score += 2
    else:
        narrator.score += 3
        for p in sorted(correct, key=lambda x: x.id):
            p.score += 3

    game.score_base_applied = True


def apply_score_bonus(game: Game) -> None:
    if game.phase != GamePhase.SCORE_BONUS:
        raise ValueError(
            f"Bonus scores can only be applied in SCORE_BONUS phase; current phase is {game.phase.value}."
        )
    if not game.score_base_applied:
        raise ValueError("Cannot apply bonus before base scores have been applied.")
    if game.score_bonus_applied:
        raise ValueError("Bonus scores already applied for this round.")

    _validate_round_complete(game)

    voters = [p for p in _players_sorted(game) if p.id != game.narrator_id]

    for owner in _players_sorted(game):
        votes_on_card = sum(1 for v in voters if v.vote == owner.card_played)
        owner.score += votes_on_card

    game.score_bonus_applied = True


def reset_round_after_next(game: Game) -> None:
    """Clear per-round fields after NEXT_ROUND -> SELECT_NARRATOR."""
    for p in game.players:
        p.card_played = None
        p.vote = None
    game.cards_on_table.clear()
    game.score_base_applied = False
    game.score_bonus_applied = False
