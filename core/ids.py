"""Identifier generation.

Four modules each carried their own private `_new_id()` returning
`str(uuid.uuid4())`. Centralising them buys two things beyond tidiness:

* **Sortability.** `new_id` embeds a millisecond timestamp ahead of the random
  component, so IDs sort chronologically as plain strings. The run log and the
  event streams are read far more often than they are written, and ordering by
  `id` then matches ordering by time without a secondary sort key.
* **Provenance.** `new_id("run")` yields `run_1770000000000_000001a1b2c3`, so an
  ID found in a log or an error message says what kind of thing it refers to.

Two details that are easy to get wrong:

* The timestamp is 13 digits. Epoch milliseconds passed 12 digits in 2001, and
  a 12-wide field silently stops matching the moment it does.
* Timestamps alone are not ordered, because 50 IDs minted in one millisecond
  share a timestamp. The random component therefore *starts* with a
  process-local counter, which makes ordering strict within a millisecond. The
  counter is not a lock-guarded global: a run is single-threaded, and the
  ordering guarantee is per-process, which is what a local log needs.
"""

from __future__ import annotations

import re
import secrets
import threading
import time

# 48 bits of entropy, split as 24 bits of monotonic sequence and 24 bits of
# randomness. Ids are not secrets on their own - they are unguessable so that a
# leaked log line cannot be used to address someone else's record.
_RANDOM_HEX = 3
_SEQUENCE_HEX = 6
_TIME_WIDTH = 13

VALID_ID = re.compile(r"\A[a-z][a-z0-9_]*_[0-9]{13}_[0-9a-f]{12}\Z")

_lock = threading.Lock()
_last_ms = 0
_sequence = 0


def _stamp() -> tuple[str, str]:
    """Return `(millisecond, sequence)` for this call, strictly increasing."""
    global _last_ms, _sequence
    with _lock:
        now_ms = int(time.time() * 1000)
        if now_ms == _last_ms:
            _sequence += 1
        else:
            # A clock that steps backwards must not reissue an earlier stamp.
            if now_ms < _last_ms:
                now_ms = _last_ms
                _sequence += 1
            else:
                _last_ms = now_ms
                _sequence = 0
        return f"{now_ms:0{_TIME_WIDTH}d}", f"{_sequence:0{_SEQUENCE_HEX}x}"


def new_id(prefix: str = "") -> str:
    """Return a sortable, prefixed identifier.

    The prefix is optional; with one, the result is `run_1770000000000_0000
    01a1b2c3`. Without, it is `1770000000000_000001a1b2c3`.
    """
    if prefix and not re.fullmatch(r"[a-z][a-z0-9_]*", prefix):
        raise ValueError(f"prefix must be lowercase alphanumeric: {prefix!r}")
    millisecond, sequence = _stamp()
    token = f"{sequence}{secrets.token_hex(_RANDOM_HEX)}"
    return f"{prefix}_{millisecond}_{token}" if prefix else f"{millisecond}_{token}"


def is_valid_id(value: str) -> bool:
    """Whether `value` has the shape `new_id` produces."""
    return isinstance(value, str) and VALID_ID.fullmatch(value) is not None


def id_timestamp(value: str) -> float:
    """Recover the creation time, in epoch seconds, from an ID.

    Returns 0.0 for a malformed ID rather than raising: callers use this to
    age out stale records, and a corrupt row should be aged out, not crash the
    sweep.
    """
    if not is_valid_id(value):
        return 0.0
    for part in value.split("_"):
        if len(part) == _TIME_WIDTH and part.isdigit():
            return int(part) / 1000
    return 0.0


def conversation_id() -> str:
    return new_id("conv")


def message_id() -> str:
    return new_id("msg")


def run_id() -> str:
    return new_id("run")


def step_id() -> str:
    return new_id("step")


def tool_call_id() -> str:
    return new_id("call")


def document_id() -> str:
    return new_id("doc")
