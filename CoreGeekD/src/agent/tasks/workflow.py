"""Task state machine for the 自进化类 tasks.

Timing facts that shape this machine (all from 接口文档):

* ``acceptTask`` returns nothing; the task *text* first appears one round later
  in ``phaseTask``.  So ACCEPT_PENDING and SOLVE_PENDING cannot be merged.
* ``prompt`` and ``executeCmd`` results arrive one round later, in ``llmResp``
  and ``lastCmdResult``.  Everything is therefore a cross-round state machine.
* A task ends on: completion, timeout, leaving the 1-cell ring around the own
  task point, or the pioneer dying -- and the text simply disappears from
  ``phaseTask``.  Those four causes are **indistinguishable** in the payload, so
  this machine never claims "completed".
* Grading is per-field (``通过率 = 正确字段数 / 全量字段数``) and the payout keeps
  the *best* pass rate submitted so far, so early partial submissions are free
  information rather than a commitment.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import DEFAULT, Config
from ..model import Observation, PlayerTask, Pos, distance
from ..tasks.memory import SkillLibrary, extract_answer

IDLE = "IDLE"
TRAVEL = "TRAVEL"
ACCEPT_PENDING = "ACCEPT_PENDING"
ACTIVE = "ACTIVE"
SOLVE_PENDING = "SOLVE_PENDING"
ANSWER_PENDING = "ANSWER_PENDING"
ENDED = "ENDED"

#: how many rounds we allow between two partial submissions
SUBMIT_COOLDOWN = 3


@dataclass
class TaskInstance:
    point_key: str
    start_round: int
    text: str = ""
    shape: str = ""
    stage: str = ACCEPT_PENDING
    accepted_round: int = 0
    prompt_round: int = 0
    exec_round: int = 0
    last_submit_round: int = 0
    submit_count: int = 0
    best_answer: str = ""
    pending_answer: str = ""
    evidence: list[str] = field(default_factory=list)
    timeout_rounds: int = 0
    score_reward: int = 0
    gold_reward: int = 0

    @property
    def deadline_round(self) -> int:
        if not self.timeout_rounds:
            return 0
        return self.start_round + self.timeout_rounds

    def elapsed(self) -> int:
        return self.start_round


class TaskMachine:
    """Owns the pioneer's task lifecycle and the LLM/sandbox round trip."""

    def __init__(self, skills: SkillLibrary, cfg: Config = DEFAULT):
        self.skills = skills
        self.cfg = cfg
        self.instance: TaskInstance | None = None
        self.cooldown_until: dict[str, int] = {}
        self.prompt_inflight = False
        self.exec_inflight = False
        self.prompt_sent_on = 0
        self.exec_sent_on = 0
        self.last_round = 0
        self.llm_calls_this_instance = 0
        self.log: list[str] = []

    # -- bookkeeping -------------------------------------------------------
    @property
    def in_task(self) -> bool:
        return self.instance is not None

    def active_text(self) -> str:
        return self.instance.text if self.instance else ""

    def _note(self, message: str) -> None:
        self.log.append(f"r{self.last_round}:{message}")
        del self.log[:-64]

    # -- feedback consumption ---------------------------------------------
    def consume_feedback(self, obs: Observation) -> None:
        """Advance the machine using last turn's ``llmResp`` / ``lastCmdResult``."""
        self.last_round = obs.round_no
        instance = self.instance

        if instance is not None:
            if obs.last_cmd_result:
                self._consume_command_result(instance, obs)
            if obs.llm_response:
                self._consume_llm(instance, obs)

        # The official signal that a task ended: the text is gone.
        if instance is not None and not obs.phase_task and \
                obs.round_no > instance.start_round:
            self._finish(obs, "text_gone")

        if instance is None and obs.phase_task:
            # we hold a task we did not start (e.g. after a restart): adopt it
            self._adopt(obs)

    def _adopt(self, obs: Observation) -> None:
        point = self._point_for(obs)
        self.instance = TaskInstance(
            point_key=self._key(point),
            start_round=obs.round_no,
            text=obs.phase_task,
            stage=ACTIVE,
            accepted_round=obs.round_no,
        )
        self._note("adopt_task")

    def _consume_llm(self, instance: TaskInstance, obs: Observation) -> None:
        self.prompt_inflight = False
        if self.prompt_sent_on and obs.round_no <= self.prompt_sent_on:
            return                       # stale response, refuse to use it
        raw = obs.llm_response
        instance.evidence.append(f"llm@{obs.round_no}:{raw[:200]}")
        skill = self.skills.lookup(instance.text)
        parser = skill.parser if skill else "raw"
        answer = extract_answer(raw, parser)
        if not answer:
            # the model may have answered with a command to run instead
            command = self._extract_exec(raw)
            if command and not instance.pending_answer:
                instance.pending_answer = ""
                instance.evidence.append("llm_requested_exec")
            return
        if len(answer) > len(instance.pending_answer):
            instance.pending_answer = answer
            self._note("llm_answer")

    def _consume_command_result(self, instance: TaskInstance, obs: Observation) -> None:
        self.exec_inflight = False
        if self.exec_sent_on and obs.round_no <= self.exec_sent_on:
            return
        result = obs.last_cmd_result
        instance.evidence.append(f"cmd@{obs.round_no}:{result[:200]}")
        parsed = parse_cmd_result(result)
        if parsed["status"] != "ok":
            # [TIMEOUT] / [JUDGER_ERROR] mean "no evidence", never an answer
            self._note(f"cmd_{parsed['status']}")
            return
        skill = self.skills.lookup(instance.text)
        parser = skill.parser if skill else "raw"
        answer = extract_answer(parsed["output"], parser)
        if answer and len(answer) > len(instance.pending_answer):
            instance.pending_answer = answer
            self._note("cmd_answer")

    def _finish(self, obs: Observation, why: str) -> None:
        instance = self.instance
        if instance is None:
            return
        # '' means we cannot attribute success; only claim it when a submission
        # was actually accepted and the text then vanished.
        if why == "text_gone" and instance.submit_count and instance.best_answer:
            self.skills.note_success(instance.text, obs.round_no)
            self.skills.remember_answer(instance.text, instance.best_answer)
            verdict = "assumed_ok"
        else:
            self.skills.note_failure(instance.text, obs.round_no, why)
            verdict = why
        self._note(f"end:{verdict}")
        point = instance.point_key
        self.cooldown_until[point] = obs.round_no + 30
        self.instance = None
        self.prompt_inflight = False
        self.exec_inflight = False
        self.llm_calls_this_instance = 0

    # -- decision ----------------------------------------------------------
    def plan(self, obs: Observation, pioneer, world=None) -> dict:
        """Return ``{"action":..., "answer":..., "prompt":..., "goto":...}``."""
        out: dict = {}
        if pioneer is None:
            return out
        instance = self.instance

        if instance is not None:
            if not self._still_legal(obs, instance, pioneer):
                self._finish(obs, "left_point_or_timeout")
                return out
            if instance.stage in (ACCEPT_PENDING,):
                if obs.phase_task:
                    instance.text = obs.phase_task
                    instance.stage = ACTIVE
                else:
                    return out
            if instance.stage in (ACTIVE, SOLVE_PENDING, ANSWER_PENDING):
                return self._active_plan(obs, instance, pioneer)
            return out

        # not in a task: accept one if a point is free and it is safe to
        point = self._choose_point(obs)
        if point is None:
            return out
        if not self._at_point(obs, point, pioneer):
            goal = _approach_cell(point, pioneer.pos)
            if goal is not None:
                out["goto"] = goal
            return out
        out["action"] = "acceptTask"
        self.instance = TaskInstance(
            point_key=self._key(point),
            start_round=obs.round_no,
            stage=ACCEPT_PENDING,
            timeout_rounds=point.timeout_rounds,
            score_reward=point.score_reward,
            gold_reward=point.gold_reward,
        )
        self._note("accept")
        return out

    def _active_plan(self, obs: Observation, instance: TaskInstance, pioneer) -> dict:
        out: dict = {}
        if not instance.text:
            return out

        # 1) an identical task answered before -> zero model calls, submit now
        cached = self.skills.cached_answer(instance.text)
        if cached:
            instance.pending_answer = cached
            instance.evidence.append("cache_hit")

        # 2) submit the best answer we hold, at a bounded rate
        if instance.pending_answer and \
                obs.round_no - instance.last_submit_round >= SUBMIT_COOLDOWN:
            out["action"] = "submitAnswer"
            out["answer"] = instance.pending_answer
            instance.best_answer = instance.pending_answer
            instance.last_submit_round = obs.round_no
            instance.submit_count += 1
            self._note("submit")
            return out

        # 3) otherwise gather evidence, respecting the per-instance cap
        if self.llm_calls_this_instance >= self.cfg.task_llm_max_per_instance:
            if instance.pending_answer:
                return out
            out["give_up"] = True
            return out

        if not self.prompt_inflight and not self.exec_inflight:
            skill = self.skills.lookup(instance.text)
            prompt = self._build_prompt(obs, instance, skill)
            if prompt:
                out["prompt"] = prompt
                self.prompt_inflight = True
                self.prompt_sent_on = obs.round_no
                self.llm_calls_this_instance += 1
                self._note("prompt")
        return out

    # -- helpers -----------------------------------------------------------
    def _still_legal(self, obs: Observation, instance: TaskInstance, pioneer) -> bool:
        if pioneer is None or not pioneer.alive:
            return False
        if obs.round_no > instance.start_round and not obs.phase_task:
            return False
        point = self._point_for(obs)
        if point is None:
            # the point is unknown this turn; stay put rather than wander off
            return True
        if not self._at_point(obs, point, pioneer):
            return False
        deadline = instance.deadline_round
        if deadline and obs.round_no > deadline:
            return False
        return True

    def _at_point(self, obs: Observation, point: PlayerTask, pioneer) -> bool:
        return point.accepts(pioneer.pos)

    def _choose_point(self, obs: Observation) -> PlayerTask | None:
        candidates = [t for t in obs.my_tasks() if t.valid and t.cooldown <= 0]
        if not candidates:
            return None
        free = [t for t in candidates
                if self.cooldown_until.get(self._key(t), 0) <= obs.round_no]
        pool = free or candidates
        return min(pool, key=lambda t: (self._key(t),))

    def _point_for(self, obs: Observation) -> PlayerTask | None:
        instance = self.instance
        if instance is not None:
            for task in obs.my_tasks():
                if self._key(task) == instance.point_key:
                    return task
        return self._choose_point(obs)

    @staticmethod
    def _key(point: PlayerTask) -> str:
        return f"{point.task_type}@{point.anchor.x},{point.anchor.y}"

    def _build_prompt(self, obs: Observation, instance: TaskInstance, skill) -> str:
        lines = [
            "You are solving one task for a game agent. Answer with the task "
            "answer only, no explanation, no markdown.",
            f"TASK:\n{instance.text}",
        ]
        if instance.prompt_round:
            lines.append(f"ROUND: {obs.round_no} (accepted round {instance.start_round})")
        if skill and skill.prompt_template:
            lines.append(f"SOP:\n{skill.prompt_template}")
        for item in instance.evidence[-3:]:
            lines.append(f"EVIDENCE: {item}")
        if instance.pending_answer:
            lines.append(f"BEST SO FAR: {instance.pending_answer}")
        instance.prompt_round = obs.round_no
        return "\n".join(lines)[:4000]

    @staticmethod
    def _extract_exec(response: str) -> str:
        """Pull a single executable command out of a model response."""
        for line in response.splitlines():
            stripped = line.strip()
            for prefix in ("CMD:", "RUN:", "EXEC:"):
                if stripped.upper().startswith(prefix):
                    return stripped[len(prefix):].strip()[:1500]
        return ""


def _approach_cell(point: PlayerTask, from_pos: Pos) -> Pos | None:
    """The stand cell of ``point`` closest to ``from_pos``.

    A task point is an obstacle, so the navigation goal must be a *neighbouring*
    cell, not the point itself; standing anywhere within one cell counts
    (任务书 §4.4: 开拓者在己方任务点周围一格内触发).
    """
    stands = point.stands()
    if not stands:
        return None
    return min(stands, key=lambda p: (distance(from_pos, p), p.x, p.y))


def parse_cmd_result(result: str) -> dict:
    """Decode ``"[exitCode:N]\\n<output>"`` into ``{status, code, output}``."""
    if not isinstance(result, str) or not result:
        return {"status": "empty", "code": None, "output": ""}
    head, _, rest = result.partition("\n")
    head = head.strip()
    if head == "[TIMEOUT]":
        return {"status": "timeout", "code": None, "output": rest}
    if head == "[JUDGER_ERROR]":
        return {"status": "judger_error", "code": None, "output": rest}
    if head.startswith("[exitCode:") and head.endswith("]"):
        try:
            code = int(head[len("[exitCode:"):-1])
        except ValueError:
            return {"status": "malformed", "code": None, "output": rest}
        output = rest
        if output.endswith("[TRUNCATED]"):
            output = output[: -len("[TRUNCATED]")].rstrip("\n")
        if code != 0:
            return {"status": "nonzero", "code": code, "output": output}
        return {"status": "ok", "code": 0, "output": output}
    return {"status": "malformed", "code": None, "output": result}
