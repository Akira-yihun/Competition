"""Wire codec and backwards-compatible model exports."""
from typing import Any
from .model import *
from .rules import *

def move_command(pos: Pos) -> dict[str, Any]:
    return {"action": "move", "targetPos": [pos.dump()]}


def collect_command(pos: Pos) -> dict[str, Any]:
    return {"action": "collect", "targetPos": [pos.dump()]}


def build_command(pos: Pos, name: str) -> dict[str, Any]:
    return {"action": "build", "targetPos": [pos.dump()], "name": name}


def attack_command(controller_id: int, targets: list[Pos]) -> dict[str, Any]:
    return {
        "action": "attack",
        "targetPos": [pos.dump() for pos in targets],
        "controllerId": str(controller_id),
    }


def sell_command(name: str, number: int) -> dict[str, Any]:
    return {"action": "sell", "name": name, "num": number}


def accept_task_command() -> dict[str, Any]:
    return {"action": "acceptTask"}


def submit_answer_command(answer: str) -> dict[str, Any]:
    return {"action": "submitAnswer", "taskAnswer": answer}


def empty_response():
    return {"roleCommandMap": {}, "prompt": "", "executeCmd": ""}

def decode(payload):
    if not isinstance(payload, dict):
        raise ValueError("request must be an object")
    turn = Turn.load(payload)
    if not 1 <= turn.round_no <= 1300 or not 1 <= turn.width <= 128 or not 1 <= turn.height <= 128:
        raise ValueError("observation outside bounds")
    if len(turn.ours) > 256 or len(turn.robots) > 4096:
        raise ValueError("observation capacity")
    return turn
