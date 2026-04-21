"""Stub for the standard Dixit ruleset.

This module will eventually wrap the existing scoring and validation logic
from ``backend.services.scoring`` and ``backend.services.game_service`` so
that all game-specific behaviour is concentrated in one place.

For now it is a no-op implementation that satisfies the ``RulesEngine``
interface without touching any live game state, fulfilling the contract
required by ``rules_loader.load_rules("dixit")``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from backend.rules.base_rules import RulesEngine

if TYPE_CHECKING:
    from backend.models.game import Game


class StandardDixitRules(RulesEngine):
    """Standard Dixit scoring and move validation — stub implementation.

    All four methods are intentional no-ops. Real logic will be wired in a
    subsequent task; this class exists only to make ``load_rules("dixit")``
    return a valid, importable object without breaking anything.
    """

    def calculate_scores(self, game: "Game") -> None:
        pass

    def validate_move(self, game: "Game", move: dict[str, Any]) -> None:
        pass

    def on_phase_start(self, game: "Game") -> None:
        pass

    def on_phase_end(self, game: "Game") -> None:
        pass
