"""Domain constants. Single source of truth for phase identifiers and card bounds."""

from backend.models.game_phase import GamePhase

GAME_PHASES: tuple[str, ...] = tuple(p.value for p in GamePhase)
GAME_PHASES_SET: frozenset[str] = frozenset(GAME_PHASES)

# Classic Dixit deck numbering (1..84). Used to reject out-of-range submissions.
MIN_CARD_NUMBER: int = 1
MAX_CARD_NUMBER: int = 84
