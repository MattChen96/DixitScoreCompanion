"""Rules loader — returns the correct ``RulesEngine`` implementation by name.

Usage::

    from backend.rules.rules_loader import load_rules

    engine = load_rules("dixit")          # standard Dixit rules
    engine = load_rules("dixit_simple")   # future simplified variant

Adding a new ruleset:
1. Create a module under ``backend/rules/`` (e.g. ``dixit_simple.py``) that
   defines a class subclassing ``RulesEngine``.
2. Register the name → class mapping in ``_REGISTRY`` below.
3. No other files need to change.

The loader is intentionally kept O(1) via a dict — no dynamic import magic,
no plugin scanning. This keeps the module dependency graph explicit and
auditable, which matters more than convenience for a project this size.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from backend.rules.base_rules import RulesEngine

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
# Maps ruleset name → concrete RulesEngine subclass.
# Import the concrete class lazily inside the dict value only when the
# module is first used, so that adding future rulesets never forces an
# unconditional import at startup.

def _registry() -> dict[str, type[RulesEngine]]:
    """Build and return the name → class registry.

    Keeping this as a function (rather than a module-level dict) avoids
    importing every concrete implementation at import time.
    """
    from backend.rules.standard_dixit import StandardDixitRules  # noqa: PLC0415

    return {
        "dixit": StandardDixitRules,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_rules(ruleset_name: str) -> RulesEngine:
    """Return an instance of the rules engine for the given ruleset name.

    Args:
        ruleset_name: One of the names registered in ``_registry()``.
                      Currently supported: ``"dixit"``.

    Returns:
        A fresh ``RulesEngine`` instance for the requested ruleset.

    Raises:
        ValueError: If ``ruleset_name`` is not registered.
    """
    registry = _registry()
    cls = registry.get(ruleset_name)
    if cls is None:
        available = ", ".join(sorted(registry.keys()))
        raise ValueError(
            f"Unknown ruleset {ruleset_name!r}. "
            f"Available rulesets: {available}."
        )
    return cls()
