"""Task-solving memory: the concrete meaning of "自进化" for this agent.

任务书 §5.3 asks for a system that "根据任务1探索的内容，形成固定SOP或者SKILL，实现
Agent自进化，进而快速做出后续任务".  The protocol has no field for submitting a
SOP, so the measurable equivalent is: **cut the (完成回合 - 接取回合) delta of later
tasks**, because ``score_1``'s speed bonus is computed from exactly that delta.

Therefore a "skill" here is a compilation of a *task shape* into a deterministic
solver, so the second and later instances of that shape need no model call at
all.  Successes are recorded only against feedback we can actually attribute,
and the library is intentionally *not* cleared on a new match: it is in-process
knowledge about task shapes, not about a board.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: ordinal words used to parameterise a task template
_ORDINALS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
_DIGITS = re.compile(r"\d+")
_QUESTION_MARK = re.compile(r"[?？]")
_PREAMBLE = re.compile(
    r"^(?:sure|of course|certainly|here (?:is|are)|the answer is|answer\s*[:：]|"
    r"好的|当然|答案\s*[:：]|如下)", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Reduce a task body to a shape key: strip digits/whitespace, keep words."""
    if not text:
        return ""
    out = _WHITESPACE.sub("", text)
    out = _DIGITS.sub("#", out)
    return out[:512]


#: clause separators: the leading clause carries the instruction, the rest
#: carries the parameters
_CLAUSE_BREAK = re.compile(r"[:：。；;\n]|[?？]")

#: "任务1" / "Task 3" style enumerators: structure, not content
_LEADING_MARKER = re.compile(r"^\s*(?:任务|题目|第)?\s*\d+\s*[、.．)）:：]?\s*")


def instruction_clause(text: str) -> str:
    """The part of a task description that states *what to do*.

    Real self-evolution tasks are deliberately repetitive: "给你一个三方的天气
    查询API接口文档…任务1：请查询北京天气 / 任务2：请查询上海天气".  The shared
    instruction is what makes them one task *shape*, and the changing entity is
    what makes them different *instances*.  Splitting on the first clause
    separator captures that split, so the second instance can reuse the first
    instance's compiled solver.
    """
    if not text:
        return ""
    body = _LEADING_MARKER.sub("", text.strip())
    head = _CLAUSE_BREAK.split(body, maxsplit=1)[0].strip()
    while not head:
        body = body.lstrip(":：。；;\n ").strip()
        if not body:
            return ""
        head = _CLAUSE_BREAK.split(body, maxsplit=1)[0].strip()
        break
    return normalize(head or body)[:120]


def shape_key(text: str) -> str:
    """A stable fingerprint for "same kind of task, different parameters"."""
    clause = instruction_clause(text)
    if clause:
        return clause
    return normalize(text)[:160]


def instruction_terms(text: str) -> frozenset[str]:
    """Character unigrams of the instruction clause, stops removed.

    Used only as a *secondary* signal; see ``SkillLibrary.lookup`` for why the
    primary key is the instruction clause itself.
    """
    clause = instruction_clause(text)
    return frozenset(
        ch for ch in clause
        if ch not in _STOP_CHARS and not ch.isdigit()
    )


#: words that carry no distinguishing power for a *task shape*
_STOP_CHARS = frozenset("请的了吗呢把给一个并和与及在到用为是有")

#: how much of the instruction vocabulary must overlap to reuse a solver
TERM_OVERLAP = 0.6


def similarity(left: str, right: str) -> float:
    """Overlap coefficient of instruction vocabulary (0..1).

    Deliberately asymmetric-safe and conservative: character bigrams are a poor
    discriminator for Chinese task text ("查询北京天气" vs "查询上海天气" scores
    *lower* than "查询北京天气" vs "查询北京股票"), so this is only a tie-breaker
    behind the instruction-clause key, never the primary match.
    """
    a, b = instruction_terms(left), instruction_terms(right)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


@dataclass
class Skill:
    key: str
    name: str = ""
    solver: str = "llm"                 # "llm" | "python" | "literal"
    prompt_template: str = ""
    exec_template: str = ""
    field_spec: tuple[str, ...] = ()
    parser: str = "raw"                 # "raw" | "lines" | "json" | "numbers"
    successes: int = 0
    failures: int = 0
    last_used_round: int = 0
    evidence: list[str] = field(default_factory=list)

    @property
    def trusted(self) -> bool:
        return self.successes > 0 and self.successes >= self.failures

    @property
    def confidence(self) -> float:
        total = self.successes + self.failures
        return 0.0 if total == 0 else self.successes / total


class SkillLibrary:
    """In-process, bounded, cross-match cache of task-shaped solvers."""

    def __init__(self, limit: int = 64):
        self.limit = limit
        self.skills: dict[str, Skill] = {}
        self.answer_cache: dict[str, str] = {}

    # -- lookup ------------------------------------------------------------
    def lookup(self, task_text: str) -> Skill | None:
        """Find a trusted solver for this task shape.

        Two tiers, both deterministic:

        1. exact match on the **instruction clause** (the part before the first
           clause separator) -- this is what groups the documented
           "task1: query Beijing / task2: query Shanghai" family;
        2. a high-overlap vocabulary match as a fallback.

        Matching is deliberately conservative.  A false negative costs one model
        call, which is *free* while a task is active (接口文档 §1.7); a false
        positive applies the wrong solver and can waste the whole task window on
        a wrong answer.
        """
        key = shape_key(task_text)
        skill = self.skills.get(key)
        if skill is not None and skill.trusted:
            return skill
        best: Skill | None = None
        best_score = TERM_OVERLAP
        for candidate in self.skills.values():
            if not candidate.trusted:
                continue
            score = similarity(task_text, candidate.name or candidate.key)
            if score > best_score:
                best, best_score = candidate, score
        return best

    def cached_answer(self, task_text: str) -> str | None:
        """An identical task (same parameters) answered before."""
        return self.answer_cache.get(normalize(task_text)[:512])

    # -- recording ---------------------------------------------------------
    def remember_answer(self, task_text: str, answer: str) -> None:
        if not answer:
            return
        key = normalize(task_text)[:512]
        if not key:
            return
        self.answer_cache[key] = answer
        while len(self.answer_cache) > self.limit * 4:
            self.answer_cache.pop(next(iter(self.answer_cache)))

    def record(self, task_text: str, skill: Skill | None = None) -> Skill:
        key = shape_key(task_text)
        existing = self.skills.get(key)
        if existing is None:
            existing = skill or Skill(key=key)
            if not existing.name:
                existing.name = normalize(task_text)[:160]
            self.skills[key] = existing
            while len(self.skills) > self.limit:
                self.skills.pop(next(iter(self.skills)))
        return existing

    def note_success(self, task_text: str, round_no: int) -> Skill:
        skill = self.record(task_text)
        skill.successes += 1
        skill.last_used_round = round_no
        return skill

    def note_failure(self, task_text: str, round_no: int, why: str = "") -> Skill:
        skill = self.record(task_text)
        skill.failures += 1
        skill.last_used_round = round_no
        if why and len(skill.evidence) < 16:
            skill.evidence.append(why)
        return skill

    # -- output parsing ----------------------------------------------------
    def extract(self, text: str, parser: str = "raw") -> str:
        return extract_answer(text, parser)


def extract_answer(text: str, parser: str = "raw", limit: int = 1024) -> str:
    """Turn a model/sandbox response into a ``taskAnswer`` payload.

    Only the answer crosses over -- never the model's prose.  Submitting the raw
    response (a common baseline mistake) guarantees ``errorCode 2``.
    """
    if not isinstance(text, str) or not text:
        return ""
    if parser == "json":
        import json
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return ""
        if isinstance(data, dict):
            return json.dumps(data, ensure_ascii=False, sort_keys=True)[:limit]
        if isinstance(data, list):
            return json.dumps(data, ensure_ascii=False)[:limit]
        return str(data)[:limit]
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("```")]
    if parser == "lines":
        return "\n".join(lines)[:limit]
    if parser == "numbers":
        found = _DIGITS.findall(text)
        return ",".join(found)[:limit]
    if not lines:
        return ""
    # Drop chatty preamble: the answer is never "Sure! Here is the answer:".
    body = [ln for ln in lines
            if not _PREAMBLE.search(ln) and not ln.endswith((":", "："))]
    if not body:
        body = lines
    # Prefer an explicitly numeric line -- most derived answers are numbers.
    for line in body:
        cleaned = line.strip("`*_ ")
        if cleaned and _DIGITS.search(cleaned) and not _QUESTION_MARK.search(cleaned):
            return cleaned[:limit]
    return max(body, key=len).strip("`*_ ")[:limit]
