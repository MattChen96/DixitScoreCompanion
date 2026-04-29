from enum import Enum


class GamePhase(str, Enum):
    """Phases from .docs/GAME_FLOW.md (definition order = play order)."""

    LOBBY = "LOBBY"
    SELECT_NARRATOR = "SELECT_NARRATOR"
    # TURN_SUBMISSION is a unified phase replacing the old PLAY_CARDS + VOTE.
    # It has two internal sub-steps tracked by Game.submission_step:
    # - "declaration": players declare which card they played
    # - "voting": players vote for the narrator's card
    # The sub-step auto-advances when declaration validation succeeds.
    TURN_SUBMISSION = "TURN_SUBMISSION"
    REVEAL_VOTES = "REVEAL_VOTES"
    REVEAL_NARRATOR = "REVEAL_NARRATOR"
    SCORE_BASE = "SCORE_BASE"
    SCORE_BONUS = "SCORE_BONUS"
    LEADERBOARD = "LEADERBOARD"
    NEXT_ROUND = "NEXT_ROUND"


class SubmissionStep(str, Enum):
    """Sub-steps within the TURN_SUBMISSION phase."""

    DECLARATION = "declaration"
    VOTING = "voting"
