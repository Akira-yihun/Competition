"""Backwards-compatible shim.

Pathfinding now lives in :mod:`agent.navigation`, which bounds every search and
supports the occupied-cell fallback the baseline lacked.  This module is kept so
that existing imports (``from .grid import next_step``) and any packaging
whitelist keep working.
"""
from __future__ import annotations

from .config import DEFAULT, Config
from .model import Observation, Pos
from .navigation import distances_from, next_step as _next_step

__all__ = ["next_step", "distances_from"]


def next_step(turn, moving, goal, cfg: Config = DEFAULT) -> Pos | None:
    """Kept for compatibility with the original ``grid.next_step(turn, unit, pos)``."""
    if isinstance(turn, Observation):
        obs = turn
    else:                                   # legacy Turn-like object
        obs = getattr(turn, "observation", None) or getattr(turn, "obs", None)
        if obs is None:
            raise TypeError("next_step expects an Observation")
    return _next_step(obs, moving, goal, cfg)
