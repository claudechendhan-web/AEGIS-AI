"""The agent loop's state machine.

`agents/loop.py` grew its own `Continue` and `Terminal` classes with string
constants. They worked, but nothing stopped a caller from inventing
`Terminal("banana")`, and the phase a run is in was implicit in control flow
rather than recorded. This module makes both explicit:

* `Phase` is the position in the cycle, and `advance` is the only legal way to
  move, so an illegal transition raises instead of silently corrupting a run.
* `ContinueReason` and `TerminalReason` are `StrEnum`s, so an unknown reason is
  a `ValueError` at construction rather than a string comparison that quietly
  never matches.

The loop is free to keep its own `Continue`/`Terminal` classes for now; this is
the vocabulary they should be drawn from, and `agents/loop.py` is the one
caller that needs migrating.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from core.errors import AegisError


class InvalidTransitionError(AegisError):
    """Raised when a run is asked to move to a phase it cannot reach."""


class Phase(StrEnum):
    """Where a run is in the cycle.

    ORIENT -> SELECT -> PROMPT -> GENERATE -> DISPATCH -> OBSERVE -> VERIFY
      -> COMMIT -> (SELECT ... | TERMINAL)
    """

    IDLE = "idle"
    ORIENT = "orient"
    SELECT = "select"
    PROMPT = "prompt"
    GENERATE = "generate"
    DISPATCH = "dispatch"
    OBSERVE = "observe"
    VERIFY = "verify"
    COMMIT = "commit"
    TERMINAL = "terminal"


#: The cycle, in order. `advance` walks this list and wraps at the end.
CYCLE: tuple[Phase, ...] = (
    Phase.ORIENT,
    Phase.SELECT,
    Phase.PROMPT,
    Phase.GENERATE,
    Phase.DISPATCH,
    Phase.OBSERVE,
    Phase.VERIFY,
    Phase.COMMIT,
)

#: Phases from which a run may terminate. `DISPATCH` and `OBSERVE` are absent
#: deliberately: a run always passes through VERIFY before it can end, so a
#: tool result is never taken as a final answer without being checked.
TERMINAL_PHASES: frozenset[Phase] = frozenset(CYCLE) | {Phase.IDLE}


class ContinueReason(StrEnum):
    """Typed reasons a run kept going."""

    TOOL_USE = "tool_use"
    TOOL_RETRY = "tool_retry"
    REPLAN = "replan"
    RECOVERY_RETRY = "recovery_retry"
    CONTEXT_COMPACT = "context_compact"
    TASK_DECOMPOSED = "task_decomposed"
    SUBTASK_DELEGATED = "subtask_delegated"


class TerminalReason(StrEnum):
    """Typed reasons a run stopped."""

    COMPLETED = "completed"
    MAX_STEPS = "max_steps"
    BUDGET_EXHAUSTED = "budget_exhausted"
    BLOCKED = "blocked"
    PROVIDER_ERROR = "provider_error"
    TOOL_LOOP = "tool_loop"
    UNRECOVERABLE = "unrecoverable"
    USER_CANCELLED = "user_cancelled"


#: Reasons that mean the run succeeded. Everything else is a failure the
#: caller should surface rather than treat as an answer.
SUCCESS_REASONS: frozenset[TerminalReason] = frozenset({TerminalReason.COMPLETED})


def advance(phase: Phase) -> Phase:
    """The next phase in the cycle, wrapping COMMIT -> ORIENT.

    Raises from `IDLE` and `TERMINAL`, which have no successor: entering the
    cycle is `begin` and leaving it is `terminate`.
    """
    if phase in (Phase.IDLE, Phase.TERMINAL):
        raise InvalidTransitionError(f"{phase.value} has no successor")
    index = CYCLE.index(phase)
    return CYCLE[(index + 1) % len(CYCLE)]


@dataclass(frozen=True, slots=True)
class Decision:
    """Either 'keep going, and here is why' or 'stop, and here is why'."""

    reason: ContinueReason | TerminalReason
    detail: str = ""

    @property
    def is_terminal(self) -> bool:
        return isinstance(self.reason, TerminalReason)

    @property
    def succeeded(self) -> bool:
        return self.reason in SUCCESS_REASONS

    def to_dict(self) -> dict[str, object]:
        return {
            "reason": str(self.reason),
            "detail": self.detail,
            "terminal": self.is_terminal,
        }


def cont(reason: ContinueReason, detail: str = "") -> Decision:
    return Decision(reason=reason, detail=detail)


def done(
    reason: TerminalReason = TerminalReason.COMPLETED, detail: str = ""
) -> Decision:
    return Decision(reason=reason, detail=detail)


class StateMachine:
    """Tracks a run's phase and refuses illegal moves."""

    __slots__ = ("_history", "_phase")

    def __init__(self, phase: Phase = Phase.IDLE) -> None:
        self._phase = phase
        self._history: list[Phase] = [phase]

    @property
    def phase(self) -> Phase:
        return self._phase

    @property
    def history(self) -> tuple[Phase, ...]:
        return tuple(self._history)

    @property
    def finished(self) -> bool:
        return self._phase is Phase.TERMINAL

    def begin(self) -> Phase:
        """Enter the cycle. Only legal from `IDLE`."""
        if self._phase is not Phase.IDLE:
            raise InvalidTransitionError(f"cannot begin from {self._phase.value}")
        return self._move(Phase.ORIENT)

    def step(self) -> Phase:
        """Advance one phase along the cycle."""
        return self._move(advance(self._phase))

    def terminate(self, decision: Decision) -> Phase:
        """End the run. The decision must be a terminal one."""
        if not decision.is_terminal:
            raise InvalidTransitionError(
                f"cannot terminate with continue reason {decision.reason}"
            )
        if self._phase is Phase.TERMINAL:
            raise InvalidTransitionError("run is already terminated")
        return self._move(Phase.TERMINAL)

    def _move(self, target: Phase) -> Phase:
        self._phase = target
        self._history.append(target)
        return target
