from enum import Enum


class GamePhase(str, Enum):
    """Phases from .docs/GAME_FLOW.md (definition order = play order)."""

    LOBBY = "LOBBY"
    NARRATOR_ORDERING = "NARRATOR_ORDERING"
    # TURN_SUBMISSION is a unified phase replacing the old PLAY_CARDS + VOTE.
    # It has two internal sub-steps tracked by Game.submission_step:
    # - "declaration": players declare which card they played
    # - "voting": players vote for the narrator's card
    # The sub-step auto-advances when declaration validation succeeds.
    TURN_SUBMISSION = "TURN_SUBMISSION"
    REVEAL_VOTES = "REVEAL_VOTES"
    REVEAL_NARRATOR = "REVEAL_NARRATOR"
    # SCORING is a unified phase that applies both base and bonus scoring and displays
    # them together. The progressive reveal (base first, then bonus) happens in the UI
    # via the Game.last_base_delta and Game.last_bonus_delta fields which are populated
    # when entering SCORING. From the player perspective, one flow: see both scorings in one view.
    SCORING = "SCORING"
    LEADERBOARD = "LEADERBOARD"
    NEXT_ROUND = "NEXT_ROUND"


class SubmissionStep(str, Enum):
    """Sub-steps within the TURN_SUBMISSION phase."""

    DECLARATION = "declaration"
    VOTING = "voting"
