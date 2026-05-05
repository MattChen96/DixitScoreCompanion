"""Odyssey ruleset — multi-vote scoring variant.

Players may vote for 1 or 2 cards per round.  Voting with a single card
and guessing correctly rewards 4 points instead of the standard 3.

All other scoring (fail conditions, narrator points, vote bonus) follows
the same rules as :class:`backend.rules.standard_dixit.StandardDixitRules`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from backend.rules.standard_dixit import StandardDixitRules

if TYPE_CHECKING:
    from backend.models.game import Game, Player

_CONFIG_PATH = Path(__file__).parent / "config" / "odyssey.json"


@dataclass(frozen=True)
class _OdysseyConfig:
    correct_guess_points: int
    correct_guess_single_vote_points: int
    narrator_points: int
    fail_all_points: int
    fail_others_points: int
    vote_bonus: int

    @classmethod
    def load(cls, path: Path = _CONFIG_PATH) -> "_OdysseyConfig":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            correct_guess_points=int(data["correct_guess_points"]),
            correct_guess_single_vote_points=int(data["correct_guess_single_vote_points"]),
            narrator_points=int(data["narrator_points"]),
            fail_all_points=int(data["fail_all_points"]),
            fail_others_points=int(data["fail_others_points"]),
            vote_bonus=int(data["vote_bonus"]),
        )


class OdysseyRules(StandardDixitRules):
    """Odyssey scoring rules.

    Identical to :class:`~backend.rules.standard_dixit.StandardDixitRules`
    except that a player who voted with a **single card** and guessed
    correctly receives ``correct_guess_single_vote_points`` (4) instead of
    the standard ``correct_guess_points`` (3).
    """

    def __init__(self, config_path: Path = _CONFIG_PATH) -> None:
        self._cfg = _OdysseyConfig.load(config_path)  # type: ignore[assignment]

    # ------------------------------------------------------------------
    # Override: distinguish single-vote vs multi-vote on correct guesses
    # ------------------------------------------------------------------

    def _apply_score_base(self, game: "Game") -> None:
        if game.score_base_applied:
            raise ValueError("Base scores already applied for this round.")

        self._validate_round_complete(game)

        narrator = next(p for p in game.players if p.id == game.narrator_id)
        narrator_card = narrator.card_played
        assert narrator_card is not None

        voters = [p for p in self._players_sorted(game) if p.id != game.narrator_id]
        correct: list["Player"] = [p for p in voters if narrator_card in p.votes]
        n_voters = len(voters)
        n_correct = len(correct)

        before = {p.id: p.score for p in game.players}

        if n_correct == 0 or n_correct == n_voters:
            # Fail condition: same as standard rules.
            narrator.score += self._cfg.fail_all_points
            for p in self._players_sorted(game):
                if p.id != game.narrator_id:
                    p.score += self._cfg.fail_others_points
        else:
            narrator.score += self._cfg.narrator_points
            for p in sorted(correct, key=lambda x: x.id):
                if len(p.votes) == 1:
                    # Single-vote risk: bonus reward.
                    p.score += self._cfg.correct_guess_single_vote_points
                else:
                    # Two-vote play: standard reward.
                    p.score += self._cfg.correct_guess_points

        game.last_base_delta = {p.id: p.score - before[p.id] for p in game.players}
        game.score_base_applied = True
