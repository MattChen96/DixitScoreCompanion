"""High-risk Dixit ruleset.

Higher rewards for success, penalties for failure.

Point values come from ``config/high_risk.json``:

* ``correct_guess_points`` 5  — big reward for guessing the narrator's card
* ``narrator_points``      5  — narrator rewarded for a well-crafted clue
* ``fail_all_points``     -2  — narrator penalised for clue too obvious or too cryptic
* ``fail_others_points``   3  — non-narrators benefit when the narrator fails
* ``vote_bonus``           2  — double bonus per vote received on a played card

All scoring logic is inherited from
:class:`~backend.rules.standard_dixit.StandardDixitRules`; only the config
path differs.
"""

from __future__ import annotations

from pathlib import Path

from backend.rules.standard_dixit import StandardDixitRules

_CONFIG_PATH = Path(__file__).parent / "config" / "high_risk.json"


class HighRiskRules(StandardDixitRules):
    """Dixit variant with amplified rewards and narrator penalties."""

    def __init__(self) -> None:
        super().__init__(config_path=_CONFIG_PATH)
