"""Stable strategy entry point kept for backwards compatibility.

The original module name is preserved because the previous design documents,
tests and packaging whitelist all refer to ``agent.brain.decide``.  It is now a
thin adapter over :mod:`agent.engine` plus a single-session fallback for callers
that want a one-shot decision without the HTTP layer.
"""
from __future__ import annotations

from typing import Any

from .config import DEFAULT, Config
from .engine import compute
from .state import Session, SessionStore


def decide(payload: dict[str, Any], cfg: Config = DEFAULT) -> dict:
    """One-shot decision on a fresh session (convenience for replay and tests).

    Production traffic goes through :func:`agent.server.serve`, which keeps a
    :class:`SessionStore` so that retries are idempotent.
    """
    session = Session(cfg)
    return session.decide(payload, lambda p, s, d: compute(p, s, d, cfg))


def make_handler(cfg: Config = DEFAULT, store: SessionStore | None = None):
    """Return a ``decide(payload) -> envelope`` callable backed by a session store."""
    sessions = store or SessionStore(cfg)

    def handler(payload: dict[str, Any]) -> dict:
        session = sessions.get(payload)
        return session.decide(payload, lambda p, s, d: compute(p, s, d, cfg))

    handler.sessions = sessions          # type: ignore[attr-defined]
    return handler


__all__ = ["decide", "make_handler"]
