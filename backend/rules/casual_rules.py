"""Casual Dixit ruleset.

Lower stakes and more forgiving scoring — good for new players.

Point values come from ``config/casual.json``:

* ``correct_guess_points`` 2  — smaller reward for correct guesses
* ``narrator_points``      2  — smaller reward for the narrator
* ``fail_all_points``      1  — narrator still earns a consolation point on failure
* ``fail_others_points``   2  — non-narrators score standard 2 pts
* ``vote_bonus``           1  — standard +1 per vote received

All scoring logic is inherited from
:class:`~backend.rules.standard_dixit.StandardDixitRules`; only the config
path differs.
"""

from __future__ import annotations

from pathlib import Path

from backend.rules.standard_dixit import StandardDixitRules

_CONFIG_PATH = Path(__file__).parent / "config" / "casual.json"


class CasualRules(StandardDixitRules):
    """Dixit variant with lower stakes and more forgiving scoring."""

    def __init__(self) -> None:
        super().__init__(config_path=_CONFIG_PATH)
