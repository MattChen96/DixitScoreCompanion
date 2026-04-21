"""Abstract base class for a Dixit rules engine.

Every concrete ruleset must subclass ``RulesEngine`` and override the four
hook methods below. The methods receive the current ``Game`` object (and,
where relevant, a ``move`` dict), so a concrete implementation has full
read access to game state.

No method here may mutate the game directly — that responsibility stays in
``backend.services.game_service`` and ``backend.services.scoring``. These
hooks are intended as extension points for alternative rulesets (e.g. a
simplified variant or a house-rules flavour), not as a replacement for the
existing service layer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.models.game import Game


class RulesEngine(ABC):
    """Interface that every ruleset must implement."""

    @abstractmethod
    def calculate_scores(self, game: "Game") -> None:
        """Compute and apply scores for the current round.

        Called after all players have voted and the host has advanced to
        SCORE_BASE / SCORE_BONUS. The implementation is responsible for
        updating ``Player.score`` fields on the game's player list.

        Args:
            game: The current, fully-populated game state.
        """

    @abstractmethod
    def validate_move(self, game: "Game", move: dict[str, Any]) -> None:
        """Validate an incoming player action before it is applied.

        Raise ``ValueError`` with a descriptive message if the move is
        illegal; return normally if it is accepted.

        Args:
            game: Current game state (read-only intent).
            move: A dict describing the action, e.g.
                  ``{"type": "submit_card", "player_id": "...", "card_number": 7}``.
        """

    @abstractmethod
    def on_phase_start(self, game: "Game") -> None:
        """Hook called immediately after the game enters a new phase.

        Use this to perform any per-phase initialisation (e.g. shuffling
        cards, setting timers) that the ruleset requires.

        Args:
            game: Game state with ``game.phase`` already set to the new phase.
        """

    @abstractmethod
    def on_phase_end(self, game: "Game") -> None:
        """Hook called immediately before the game leaves a phase.

        Use this to perform any per-phase teardown or validation before the
        transition is committed.

        Args:
            game: Game state with ``game.phase`` still set to the current phase.
        """
