from enum import Enum


class GamePhase(str, Enum):
    """Phases from .docs/GAME_FLOW.md (definition order = play order)."""

    LOBBY = "LOBBY"
    SELECT_NARRATOR = "SELECT_NARRATOR"
    PLAY_CARDS = "PLAY_CARDS"
    VOTE = "VOTE"
    REVEAL_VOTES = "REVEAL_VOTES"
    REVEAL_NARRATOR = "REVEAL_NARRATOR"
    SCORE_BASE = "SCORE_BASE"
    SCORE_BONUS = "SCORE_BONUS"
    LEADERBOARD = "LEADERBOARD"
    NEXT_ROUND = "NEXT_ROUND"
