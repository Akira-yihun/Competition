"""Task channel: self-evolving task solving.

``workflow`` holds the cross-round state machine, ``memory`` holds the skill
library that makes later instances of a task shape cheap.
"""
from .memory import Skill, SkillLibrary, extract_answer, shape_key
from .workflow import TaskMachine, parse_cmd_result

__all__ = [
    "Skill", "SkillLibrary", "extract_answer", "shape_key",
    "TaskMachine", "parse_cmd_result",
]
