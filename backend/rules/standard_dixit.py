"""Standard Dixit ruleset.

Owns all scoring logic for the classic Dixit rules. Point values are
loaded from ``backend/rules/config/standard.json`` at import time so
they can be adjusted without touching Python code.

Entry point is :meth:`StandardDixitRules.calculate_scores`, which
dispatches by the current phase:

* ``SCORE_BASE``  → base points (correct guesses of the narrator's card)
* ``SCORE_BONUS`` → +1 per vote received on each player's card

Both are guarded by idempotency flags on ``Game`` and run only after
:meth:`_validate_round_complete` succeeds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from backend.models.game_phase import GamePhase
from backend.rules.base_rules import RulesEngine

if TYPE_CHECKING:
    from backend.models.game import Game, Player

_CONFIG_PATH = Path(__file__).parent / "config" / "standard.json"


@dataclass(frozen=True)
class _RuleConfig:
    correct_guess_points: int
    narrator_points: int
    fail_all_points: int
    fail_others_points: int
    vote_bonus: int

    @classmethod
    def load(cls, path: Path = _CONFIG_PATH) -> "_RuleConfig":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            correct_guess_points=int(data["correct_guess_points"]),
            narrator_points=int(data["narrator_points"]),
            fail_all_points=int(data["fail_all_points"]),
            fail_others_points=int(data["fail_others_points"]),
            vote_bonus=int(data["vote_bonus"]),
        )


class StandardDixitRules(RulesEngine):
    """Standard Dixit scoring rules.

    Point values are read from a JSON config file once per instance.

    Subclasses override the ruleset by passing a different ``config_path``
    to ``super().__init__()``. Tests can inject a pre-built ``_RuleConfig``
    directly to skip file I/O.
    """

    def __init__(
        self,
        config_path: Path = _CONFIG_PATH,
        config: _RuleConfig | None = None,
    ) -> None:
        self._cfg = config if config is not None else _RuleConfig.load(config_path)

    # ------------------------------------------------------------------
    # RulesEngine interface
    # ------------------------------------------------------------------

    def calculate_scores(self, game: "Game") -> None:
        """Apply the round's scores for the current phase.

        Caller (``game_service.next_phase``) invokes this only after the
        transition into SCORE_BASE or SCORE_BONUS has committed, so
        ``game.phase`` already reflects what we're about to score.
        """
        if game.phase == GamePhase.SCORE_BASE:
            self._apply_score_base(game)
        elif game.phase == GamePhase.SCORE_BONUS:
            self._apply_score_bonus(game)
        else:
            raise ValueError(
                "calculate_scores is only valid in SCORE_BASE or SCORE_BONUS "
                f"phase; current phase is {game.phase.value}."
            )

    def validate_move(self, game: "Game", move: dict[str, Any]) -> None:
        # Move validation is still centralised in game_service (phase gates,
        # duplicate-card detection, narrator rules). Wiring it through the
        # rules engine is a later task; keep this a no-op so existing
        # behaviour is unchanged.
        return None

    def on_phase_start(self, game: "Game") -> None:
        return None

    def on_phase_end(self, game: "Game") -> None:
        return None

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    @staticmethod
    def _players_sorted(game: "Game") -> list["Player"]:
        return sorted(game.players, key=lambda p: p.id)

    @staticmethod
    def _active_sorted(game: "Game") -> list["Player"]:
        """Currently-connected players, sorted by id for determinism."""
        return sorted((p for p in game.players if p.connected), key=lambda p: p.id)

    def _validate_unique_cards_played(self, game: "Game") -> None:
        seen: set[int] = set()
        for p in self._players_sorted(game):
            c = p.card_played
            if c is None:
                continue
            if c in seen:
                raise ValueError(
                    "Cannot score this round: two or more players played the same "
                    "card number; votes cannot be attributed uniquely."
                )
            seen.add(c)

    def _validate_round_complete(self, game: "Game") -> None:
        if not game.narrator_id:
            raise ValueError("Cannot score: narrator is not set.")

        narrator = next((p for p in game.players if p.id == game.narrator_id), None)
        if narrator is None:
            raise ValueError("Cannot score: narrator_id is not a player in this game.")

        # Stall policy: the narrator must always have a card on the table, even
        # if they are currently disconnected. This keeps "narrator missing
        # before playing" from silently scoring a bogus round.
        if narrator.card_played is None:
            raise ValueError(
                f"Cannot score: narrator {narrator.id!r} has not submitted a card yet."
            )

        # Only currently-active players gate progress — a player who dropped
        # before playing/voting must NOT block scoring. The stall window is
        # enforced upstream by the heartbeat task.
        for p in self._active_sorted(game):
            if p.card_played is None:
                raise ValueError(
                    f"Cannot score: every active player must have submitted a card; "
                    f"missing for player {p.id!r}."
                )

        for p in self._active_sorted(game):
            if p.id != game.narrator_id and not p.votes:
                raise ValueError(
                    f"Cannot score: every active non-narrator must have voted; "
                    f"missing vote for player {p.id!r}."
                )

        self._validate_unique_cards_played(game)

    def _apply_score_base(self, game: "Game") -> None:
        if game.phase != GamePhase.SCORE_BASE:
            raise ValueError(
                f"Base scores can only be applied in SCORE_BASE phase; "
                f"current phase is {game.phase.value}."
            )
        if game.score_base_applied:
            raise ValueError("Base scores already applied for this round.")

        self._validate_round_complete(game)

        narrator = next(p for p in game.players if p.id == game.narrator_id)
        narrator_card = narrator.card_played
        assert narrator_card is not None

        voters = [p for p in self._players_sorted(game) if p.id != game.narrator_id]
        correct = [p for p in voters if narrator_card in p.votes]
        n_voters = len(voters)
        n_correct = len(correct)

        before = {p.id: p.score for p in game.players}

        if n_correct == 0 or n_correct == n_voters:
            narrator.score += self._cfg.fail_all_points
            for p in self._players_sorted(game):
                if p.id != game.narrator_id:
                    p.score += self._cfg.fail_others_points
        else:
            narrator.score += self._cfg.narrator_points
            for p in sorted(correct, key=lambda x: x.id):
                p.score += self._cfg.correct_guess_points

        game.last_base_delta = {p.id: p.score - before[p.id] for p in game.players}
        game.score_base_applied = True

    def _apply_score_bonus(self, game: "Game") -> None:
        if game.phase != GamePhase.SCORE_BONUS:
            raise ValueError(
                f"Bonus scores can only be applied in SCORE_BONUS phase; "
                f"current phase is {game.phase.value}."
            )
        if not game.score_base_applied:
            raise ValueError("Cannot apply bonus before base scores have been applied.")
        if game.score_bonus_applied:
            raise ValueError("Bonus scores already applied for this round.")

        self._validate_round_complete(game)

        voters = [p for p in self._players_sorted(game) if p.id != game.narrator_id]

        before = {p.id: p.score for p in game.players}

        for owner in self._players_sorted(game):
            # Narrator is excluded from bonus scoring: they cannot receive points
            # for votes cast on their card (players vote FOR it as the "guess",
            # not to reward the narrator).
            if owner.id == game.narrator_id:
                continue
            if owner.card_played is None:
                continue
            votes_on_card = sum(
                1 for v in voters if owner.card_played in v.votes
            )
            owner.score += votes_on_card * self._cfg.vote_bonus

        game.last_bonus_delta = {p.id: p.score - before[p.id] for p in game.players}
        game.score_bonus_applied = True
