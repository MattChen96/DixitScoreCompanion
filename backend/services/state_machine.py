"""
Strict host-only phase transitions (.docs/GAME_FLOW.md).

Only mutates ``game.phase``; no scoring or round reset.
"""

from backend.models.game import Game
from backend.models.game_phase import GamePhase

ALLOWED_TRANSITIONS: frozenset[tuple[GamePhase, GamePhase]] = frozenset(
    {
        (GamePhase.LOBBY, GamePhase.NARRATOR_ORDERING),
        (GamePhase.NARRATOR_ORDERING, GamePhase.TURN_SUBMISSION),
        (GamePhase.TURN_SUBMISSION, GamePhase.REVEAL_VOTES),
        (GamePhase.REVEAL_VOTES, GamePhase.REVEAL_NARRATOR),
        (GamePhase.REVEAL_NARRATOR, GamePhase.SCORING),
        (GamePhase.SCORING, GamePhase.LEADERBOARD),
        (GamePhase.LEADERBOARD, GamePhase.NEXT_ROUND),
        (GamePhase.NEXT_ROUND, GamePhase.TURN_SUBMISSION),
    }
)


def allowed_next_phases(current: GamePhase) -> frozenset[GamePhase]:
    return frozenset(dst for src, dst in ALLOWED_TRANSITIONS if src == current)


def transition_phase(game: Game, requester_id: str, to_phase: GamePhase) -> Game:
    if game.host_id is None:
        raise ValueError(
            "Cannot change phase: this game has no host yet (wait for a player to join)."
        )
    if requester_id != game.host_id:
        raise ValueError(
            "Only the host can change the game phase. "
            f"Requester {requester_id!r} is not the host."
        )

    current = game.phase
    if (current, to_phase) not in ALLOWED_TRANSITIONS:
        allowed = sorted(p.value for p in allowed_next_phases(current))
        if not allowed:
            raise ValueError(
                f"Invalid phase transition: cannot leave {current.value!r} "
                f"toward {to_phase.value!r}; there are no allowed next phases from here."
            )
        raise ValueError(
            f"Invalid phase transition: cannot go from {current.value!r} "
            f"to {to_phase.value!r}. "
            f"Allowed next phases from {current.value!r}: {allowed}."
        )

    game.phase = to_phase
    return game


# Phases where generic "next phase" is not allowed (use dedicated endpoints).
_NO_BLIND_ADVANCE: frozenset[GamePhase] = frozenset({GamePhase.LOBBY})


def advance_phase_by_host(game: Game, requester_id: str) -> Game:
    """
    Move to the single legal successor for the current phase (host-only).

    LOBBY cannot be advanced this way; use start_game instead.
    NARRATOR_ORDERING cannot be advanced this way; use set_narrator_queue instead.
    TURN_SUBMISSION can only advance to REVEAL_VOTES when all players
    have voted (voting sub-step complete).
    """
    current = game.phase
    if current in _NO_BLIND_ADVANCE:
        raise ValueError(
            "Cannot advance from LOBBY this way: use start_game (host) "
            "to move to NARRATOR_ORDERING."
        )

    nxt = allowed_next_phases(current)
    if len(nxt) != 1:
        raise ValueError(
            f"Internal error: expected exactly one successor from {current.value!r}, "
            f"got {sorted(p.value for p in nxt)}."
        )
    (only,) = nxt
    return transition_phase(game, requester_id, only)
