"""Request parsing, command construction and the single response envelope.

Two invariants carry the "never burn one of the five allowed 异常" policy:

1. :func:`parse` is *total* -- a malformed payload degrades into a less-informed
   observation, never an exception.
2. :func:`envelope` is the **only** producer of a response, and it always emits
   all three top-level keys with the types the protocol requires
   (``roleCommandMap`` dict, ``prompt`` str, ``executeCmd`` str).  The baseline
   emitted only ``roleCommandMap``, which a strict judge reads as a malformed
   envelope and discards entirely.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from .model import Pos, parse
from . import rules as R

__all__ = [
    "parse", "envelope", "empty_envelope",
    "move", "attack", "sell", "buy", "build", "remove", "accept_task",
    "submit_answer", "summon_treasure", "use", "drop", "collect",
]


# ---------------------------------------------------------------------------
# response envelope
# ---------------------------------------------------------------------------
def envelope(commands: Mapping[int | str, dict] | None = None,
             prompt: str = "",
             execute_cmd: str = "") -> dict:
    """Build the complete judge response.

    ``prompt`` and ``executeCmd`` are always present as strings, even when
    unused, because 接口文档 §2.1 lists them as top-level Response fields with no
    "optional" marker and the official sample carries them as ``""``.
    """
    body: dict[str, dict] = {}
    for key, command in (commands or {}).items():
        if not isinstance(command, dict) or not command:
            continue
        body[str(key)] = command
    return {
        "roleCommandMap": body,
        "prompt": prompt if isinstance(prompt, str) else "",
        "executeCmd": execute_cmd if isinstance(execute_cmd, str) else "",
    }


def empty_envelope() -> dict:
    return envelope()


# ---------------------------------------------------------------------------
# command builders -- 接口文档 §2.2 / §2.3
#
# Every builder keeps the exact field set and JSON types the protocol expects:
# ``targetPos`` is an array of {x:int, y:int}, ``controllerId`` is a *string*,
# ``num`` is an int.  Guards elsewhere decide *whether* to send, these decide
# only *how*.
# ---------------------------------------------------------------------------
def _targets(positions: Iterable[Pos]) -> list[dict[str, int]]:
    return [pos.dump() for pos in positions]


def move(pos: Pos) -> dict:
    return {"action": "move", "targetPos": _targets((pos,))}


def attack(controller_id: int, positions: Iterable[Pos]) -> dict:
    return {
        "action": "attack",
        "controllerId": str(controller_id),
        "targetPos": _targets(positions),
    }


def sell(name: str, num: int = 1) -> dict:
    return {"action": "sell", "name": name, "num": int(num)}


def buy(name: str, num: int = 1) -> dict:
    return {"action": "buy", "name": name, "num": int(num)}


def build(pos: Pos, name: str) -> dict:
    return {"action": "build", "targetPos": _targets((pos,)), "name": name}


def remove(pos: Pos) -> dict:
    return {"action": "remove", "targetPos": _targets((pos,))}


def accept_task() -> dict:
    return {"action": "acceptTask"}


def submit_answer(answer: str) -> dict:
    return {"action": "submitAnswer", "taskAnswer": str(answer)}


def summon_treasure(pos: Pos, items: Iterable[str]) -> dict:
    return {
        "action": "summonTreasure",
        "targetPos": _targets((pos,)),
        "item": [str(i) for i in items],
    }


def use(name: str, pos: Pos | None = None) -> dict:
    command: dict[str, Any] = {"action": "use", "name": str(name)}
    if pos is not None:
        command["targetPos"] = _targets((pos,))
    return command


def drop(name: str) -> dict:
    return {"action": "drop", "name": str(name)}


def collect(pos: Pos) -> dict:
    return {"action": "collect", "targetPos": _targets((pos,))}


# ---------------------------------------------------------------------------
# static legal check used by the guard's fast path
# ---------------------------------------------------------------------------
def is_known_action(command: Any) -> bool:
    return (isinstance(command, dict)
            and isinstance(command.get("action"), str)
            and command["action"] in R.ACTIONS)
