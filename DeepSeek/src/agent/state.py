"""Per-match session state: dedup, resync, budget and long-lived memory.

The judge may retry a request, and the baseline had no memory at all, so a
retried turn would re-run the strategy (and, once the LLM channel is used,
double-spend the daily prompt quota).  This module makes a turn idempotent:

* same round + same payload digest  -> replay the cached response byte for byte
* same round + different digest     -> conflict: emit a legal empty envelope
* round went backwards              -> new match/half: reset match-scoped memory
* round jumped forward              -> drop un-relatable pending work, resync

Match-scoped memory (learned build sites, task state, price calendar) is cleared
on reset; the *skill* library is deliberately kept, because it is in-process
knowledge that does not depend on the previous board.
"""
from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from typing import Any

from .config import DEFAULT, Config
from .protocol import empty_envelope
from .world import SiteKnowledge


def digest(payload: Any) -> str:
    try:
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        blob = repr(payload)
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()


class LlmBudget:
    """Tracks the 3-calls-per-game-day prompt quota.

    We never rely on ``errorCode 5`` to stop ourselves: the per-day counter is
    authoritative and is advanced the moment a prompt is actually sent.
    """

    def __init__(self, cfg: Config = DEFAULT):
        self.cfg = cfg
        self.day_index = 0
        self.used_today = 0
        self.exhausted = False

    def roll_day(self, day_index: int) -> None:
        if day_index != self.day_index:
            self.day_index = day_index
            self.used_today = 0
            self.exhausted = False

    def may_call(self, *, in_task: bool) -> bool:
        if in_task:
            return True                      # 任务期免额度 (接口文档 §1.7)
        if self.exhausted or not self.cfg.llm_out_of_task_enabled:
            return False
        return self.used_today < self.cfg.llm_calls_per_day

    def note_call(self, *, in_task: bool) -> None:
        if not in_task:
            self.used_today += 1

    def note_quota_error(self) -> None:
        self.exhausted = True
        self.used_today = self.cfg.llm_calls_per_day


class SessionState:
    """Mutable per-match state handed to the engine."""

    def __init__(self, cfg: Config = DEFAULT):
        self.cfg = cfg
        self.match_key = ""
        self.last_round = 0
        self.state_version = 0
        self.knowledge = SiteKnowledge()
        self.llm = LlmBudget(cfg)
        self.task: Any = None               # tasks.workflow.TaskMachine
        self.skills: Any = None             # tasks.memory.SkillLibrary
        self.price_calendar: Any = None
        self.treasure: Any = None
        self.cooling: dict[int, int] = {}   # tower id -> round when it may fire
        self.pending_tower_sites: list = []
        self.last_build_attempt: Any = None
        self.last_plan_diagnostics: list[str] = []
        self.last_strategy_errors: list[str] = []
        self.notes: list[str] = []
        self.phase_probe: list[dict] = []

    def reset_match(self, match_key: str) -> None:
        self.match_key = match_key
        self.last_round = 0
        self.knowledge = SiteKnowledge()
        self.task = None
        self.price_calendar = None
        self.treasure = None
        self.cooling = {}
        self.pending_tower_sites = []
        self.last_build_attempt = None
        self.last_plan_diagnostics = []
        self.last_strategy_errors = []
        self.notes = []
        self.phase_probe = []


class Session:
    """Serialises turns for one match and caches responses for retries."""

    def __init__(self, cfg: Config = DEFAULT, state: SessionState | None = None):
        self.cfg = cfg
        self.state = state or SessionState(cfg)
        self.lock = threading.Lock()
        self.cache: OrderedDict[int, tuple[str, dict]] = OrderedDict()

    # -- identity ----------------------------------------------------------
    @staticmethod
    def match_key(payload: Any) -> str:
        obj = payload if isinstance(payload, dict) else {}
        team = obj.get("teamOur") if isinstance(obj.get("teamOur"), dict) else {}
        info = obj.get("mapInfo") if isinstance(obj.get("mapInfo"), dict) else {}
        return "|".join((
            str(team.get("teamId", "")),
            str(team.get("type", "")),
            str(info.get("width", "")),
            str(info.get("height", "")),
        ))

    # -- main entry --------------------------------------------------------
    def decide(self, payload: Any, compute, deadline: float | None = None,
               now=None) -> dict:
        """Run one turn, returning a complete response envelope.

        ``compute`` receives ``(payload, state, deadline)`` and returns either a
        response envelope or ``None``.
        """
        import time
        clock = now or time.monotonic
        key = digest(payload)
        with self.lock:
            round_no = _round_of(payload)
            cached = self.cache.get(round_no)
            if cached is not None:
                cached_key, response = cached
                # Replay only an exact retry; anything else is a conflict.
                return response if cached_key == key else empty_envelope()

            match = self.match_key(payload)
            if match != self.state.match_key or round_no < self.state.last_round:
                # A round that goes backwards means a different half or match:
                # match-scoped memory (learned build sites, task instance, price
                # calendar) must not leak across.  The skill library survives,
                # because it describes task shapes rather than a board.
                self.state.reset_match(match)

            if deadline is None:
                deadline = clock() + self.cfg.compute_seconds

            try:
                response = compute(payload, self.state, deadline)
            except Exception:
                response = None
            if not isinstance(response, dict):
                response = empty_envelope()

            self.state.last_round = max(self.state.last_round, round_no)
            self.state.state_version += 1
            self.cache[round_no] = (key, response)
            while len(self.cache) > self.cfg.cached_rounds:
                self.cache.popitem(last=False)
            return response


def _round_of(payload: Any) -> int:
    obj = payload if isinstance(payload, dict) else {}
    raw = obj.get("roundNo")
    if isinstance(raw, bool):
        return -1
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            return -1
    return -1


class SessionStore:
    """Keeps one :class:`Session` per observed (team, side, map size)."""

    def __init__(self, cfg: Config = DEFAULT):
        self.cfg = cfg
        self.sessions: dict[str, Session] = {}
        self.lock = threading.Lock()

    def get(self, payload: Any) -> Session:
        key = Session.match_key(payload)
        with self.lock:
            session = self.sessions.get(key)
            if session is None:
                if len(self.sessions) >= self.cfg.max_sessions:
                    self.sessions.pop(next(iter(self.sessions)))
                session = Session(self.cfg)
                self.sessions[key] = session
            return session
