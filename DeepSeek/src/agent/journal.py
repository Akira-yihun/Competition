"""Per-round plaintext journal (R1).

One round produces exactly one block of lines, in the order the operator asked
for:

    ROUND 85
    req {<raw request, one line>}
    rsp {<raw response, one line>}
    官方新闻 "..."
    民间传闻 "..."
    自进化类任务要求 "..."
    req的llm调用结果 "..."
    rsp的llm调用prompt "..."
    req的executecmd结果 "..."
    rsp的executecmd命令 "..."
    diagnostics {...}

Design constraints, in order of importance:

1. **The decision path must never block on I/O.**  A judge that fills its stdout
   pipe and stops reading turns a write into a hang, and a hang is a 5-second
   超时异常.  So a bounded queue plus one background writer thread does all the
   writing; when the queue is full the event is *dropped and counted*, never
   awaited.
2. **Only what was actually sent is logged.**  The engine calls :func:`record`
   after the guard, so ``rsp`` is byte-for-byte the response the judge received.
3. **One line per item.**  Every value goes through ``json.dumps`` so embedded
   newlines are escaped and a downstream ``grep`` / ``awk`` keeps working.

Sinks (``DS_AGENT_LOG``): unset/``stdout`` -> standard output, ``off``/``0``/
``""`` -> disabled, anything else -> appended to that path.
"""
from __future__ import annotations

import atexit
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any

QUEUE_LIMIT = 256

_QUEUE: "queue.Queue[tuple[str, str]]" = queue.Queue(maxsize=QUEUE_LIMIT)
_LOCK = threading.Lock()
_STARTED = False
_DROPPED = 0
_FAILED = 0
_SEQ = 0


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------
def _dump(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                          default=str)
    except (TypeError, ValueError):
        return json.dumps(repr(value), ensure_ascii=False)


def _news(req: dict, field: str) -> str:
    news = req.get("worldNews")
    if isinstance(news, dict):
        return str(news.get(field, "") or "")
    return str(news or "") if field == "officialNews" else ""


def _text(req: dict, field: str) -> str:
    value = req.get(field, "")
    return value if isinstance(value, str) else str(value or "")


def format_turn(payload: Any, response: Any, diagnostics: Any = None,
                dropped: int = 0) -> str:
    """Render one round as the fixed multi-line block (no trailing newline)."""
    req = payload if isinstance(payload, dict) else {}
    rsp = response if isinstance(response, dict) else {}
    round_no = req.get("roundNo")
    lines = [
        f"ROUND {round_no if round_no is not None else '?'}",
        "req " + _dump(req),
        "rsp " + _dump(rsp),
        "官方新闻 " + _dump(_news(req, "officialNews")),
        "民间传闻 " + _dump(_news(req, "folkLegends")),
        "自进化类任务要求 " + _dump(_text(req, "phaseTask")),
        "req的llm调用结果 " + _dump(_text(req, "llmResp")),
        "rsp的llm调用prompt " + _dump(_text(rsp, "prompt")),
        "req的executecmd结果 " + _dump(_text(req, "lastCmdResult")),
        "rsp的executecmd命令 " + _dump(_text(rsp, "executeCmd")),
    ]
    if diagnostics:
        lines.append("diagnostics " + _dump(diagnostics))
    if dropped:
        lines.append(f"journal_dropped_events {dropped}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# asynchronous sink
# ---------------------------------------------------------------------------
def _sink() -> str:
    return os.environ.get("DS_AGENT_LOG", "stdout").strip()


def _write(sink: str, block: str) -> None:
    if sink == "stdout":
        stream = sys.stdout
        if stream is None:
            return
        stream.write(block + "\n")
        stream.flush()
        return
    path = Path(sink)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(block + "\n")


def _writer() -> None:
    global _FAILED
    while True:
        sink, block = _QUEUE.get()
        try:
            _write(sink, block)
        except Exception:
            # Diagnostics must never turn a good turn into a fallback.  A dead
            # stdout (closed pipe) simply disables the journal from here on.
            _FAILED += 1
            if _FAILED > 5 and sink == "stdout":
                return
        finally:
            _QUEUE.task_done()


def _ensure_writer() -> None:
    global _STARTED
    if _STARTED:
        return
    thread = threading.Thread(target=_writer, name="journal", daemon=True)
    thread.start()
    _STARTED = True


def record(payload: Any, response: Any, diagnostics: Any = None) -> bool:
    """Queue one round block.  Never raises, never blocks, never times out."""
    global _DROPPED
    sink = _sink()
    if sink in ("", "off", "0", "none"):
        return False
    try:
        block = format_turn(payload, response, diagnostics, dropped=_DROPPED)
    except Exception:
        return False
    with _LOCK:
        _ensure_writer()
        try:
            _QUEUE.put_nowait((sink, block))
            _DROPPED = 0
            return True
        except queue.Full:
            _DROPPED += 1
            return False


def flush(timeout: float = 2.0) -> None:
    """Best-effort drain, used by tests and by process shutdown."""
    end = time.monotonic() + timeout
    while _QUEUE.unfinished_tasks and time.monotonic() < end:
        time.sleep(0.002)


atexit.register(flush)
