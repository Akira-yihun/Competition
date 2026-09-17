"""Single-turn orchestration: parse -> model -> plan -> guard -> envelope.

``compute`` is the only strategy entry point and returns a complete response
envelope.  It is deliberately pure with respect to the transport: the HTTP layer
never sees a ``Decision`` object and this layer never touches a socket, so a
transport failure cannot corrupt strategy state and vice versa.

Deadline handling: the judge allows 5 s from request to response.  The budget
below is spent from one absolute deadline; if planning runs out we still emit a
*syntactically complete* empty envelope rather than risk a timeout, because a
timeout is an 异常 while an empty command map is merely a quiet turn.
"""
from __future__ import annotations

import time
from typing import Any

from .config import DEFAULT, Config
from .model import Observation, parse
from .protocol import envelope
from .state import SessionState
from .tasks.memory import SkillLibrary
from .tasks.workflow import TaskMachine
from .world import WorldView
from . import combat
from . import strategy
from .strategy import TurnPlan


def compute(payload: Any, state: SessionState, deadline: float | None = None,
            cfg: Config = DEFAULT) -> dict:
    """Produce one complete judge response."""
    started = time.monotonic()
    if deadline is None:
        deadline = started + cfg.compute_seconds

    observation = parse(payload)

    # --- lazily created, match-independent memory -------------------------
    if state.skills is None:
        state.skills = SkillLibrary()
    if state.task is None:
        state.task = TaskMachine(state.skills, cfg)

    state.llm.roll_day(observation.day_index)

    state.pending_tower_sites = [
        pos for pos in state.pending_tower_sites
        if not any(t.alive and t.pos == pos for t in observation.towers())
    ]
    world = WorldView(observation, cfg, state.knowledge, state.cooling,
                      state.pending_tower_sites)
    machine: TaskMachine = state.task

    machine.consume_feedback(observation)
    strategy.consume_feedback(world, state)

    plan = TurnPlan()
    try:
        if observation.is_day:
            strategy.plan_day(world, plan, cfg, state, machine)
        else:
            _plan_night(world, plan, cfg, machine)
    except Exception as exc:             # never let strategy kill the turn
        import traceback
        plan.note(f"strategy_exception:{type(exc).__name__}:{exc}")
        strategy_errors = traceback.format_exc().strip().splitlines()[-3:]
        state.last_strategy_errors = strategy_errors

    # --- record what we expect to learn next turn -------------------------
    if plan.build_attempt is not None:
        role_id, pos, kind = plan.build_attempt
        actor = next((u for u in observation.roles if u.unit_id == role_id), None)
        if actor is not None:
            strategy.note_build_attempt(state, world, actor, pos, kind)
        if kind != "wall" and pos not in state.pending_tower_sites:
            state.pending_tower_sites.append(pos)
    for role_id, command in plan.commands.items():
        if command.get("action") == "attack":
            tower_id = command.get("__towerId")
            for tower in observation.towers():
                if tower.unit_id == tower_id:
                    combat.record_cooldown(tower, observation.round_no,
                                           state.cooling)

    # --- final legality gate ----------------------------------------------
    result = _guarded(plan, observation, world, cfg)

    prompt = ""
    if plan.prompt and _prompt_allowed(state, machine):
        prompt = plan.prompt
        state.llm.note_call(in_task=machine.in_task)

    state.last_plan_diagnostics = list(plan.diagnostics) + \
        [f"reject:{r}:{why}" for r, why in result.rejections]
    # Keep what the world model learned this turn (e.g. a build site the judge
    # refused) next to the parse notes, so a systematic failure stays
    # inspectable instead of silently repeating every turn.
    state.notes = (list(observation.parse_notes) + list(world.notes)
                   + list(getattr(state, "last_strategy_errors", ())))
    del world.notes[:]

    elapsed = time.monotonic() - started
    if elapsed > cfg.compute_seconds:
        state.notes.append(f"slow_turn:{elapsed:.2f}s")

    return envelope(result.commands, prompt, plan.execute_cmd)


def _plan_night(world: WorldView, plan: TurnPlan, cfg: Config,
                machine: TaskMachine) -> None:
    """At night only defence counts: 1800 actions, zero slack.

    A pioneer that is mid-task is excluded from the tower roster on purpose --
    task book §5 ends a task the moment the pioneer leaves the 1-cell ring
    around its task point, so pulling it back to a tower would trade a whole
    task's score for one night of firepower.
    """
    excluded = frozenset()
    if cfg.pioneer_help_defend is False and machine.in_task:
        pioneer = world.obs.pioneer()
        if pioneer is not None:
            excluded = frozenset({pioneer.unit_id})
    strategy.plan_night(world, plan, cfg, pioneers_excluded=excluded)


def _prompt_allowed(state: SessionState, machine: TaskMachine) -> bool:
    if machine.in_task:
        return True
    return state.llm.may_call(in_task=False)


def _guarded(plan: TurnPlan, observation: Observation, world: WorldView,
             cfg: Config):
    from . import guard
    return guard.apply(plan.commands, observation, world, cfg=cfg)
