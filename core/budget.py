"""Budget accounting for a single run.

The loop in `agents/loop.py` already accumulates `prompt_tokens` and
`completion_tokens`, but it had no ceiling to accumulate against, so "the run
got expensive" was only ever noticed after the fact. A budget makes the limit
a precondition rather than a post-mortem, and lets the loop terminate with a
typed reason instead of running to `max_steps`.

Three independent limits, because they fail differently:

* `max_steps` bounds iteration count, which bounds latency and tool calls.
* `max_total_tokens` bounds spend, which a model can hit in one huge step.
* `max_tool_calls` bounds side effects, which tokens do not track at all.

`Budget` is deliberately not thread-safe. A run is single-threaded by
construction, and a lock here would imply a concurrency that does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.errors import AegisError


class BudgetExhaustedError(AegisError):
    """Raised when an operation would exceed a configured budget."""


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    steps: int
    tool_calls: int
    prompt_tokens: int
    completion_tokens: int
    max_steps: int
    max_tool_calls: int | None
    max_total_tokens: int | None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def exhausted(self) -> bool:
        return self.exhausted_by is not None

    @property
    def exhausted_by(self) -> str | None:
        """The name of the first limit exceeded, or None while within budget.

        Strictly greater-than on every ceiling: a budget of `max_steps=2`
        permits two steps and fails on the third. Using `>=` would fail on the
        step that reached the limit, which is the one the caller was promised.
        """
        if self.steps > self.max_steps:
            return "max_steps"
        if self.max_tool_calls is not None and self.tool_calls > self.max_tool_calls:
            return "max_tool_calls"
        if (
            self.max_total_tokens is not None
            and self.total_tokens > self.max_total_tokens
        ):
            return "max_total_tokens"
        return None

    @property
    def remaining_tokens(self) -> int | None:
        if self.max_total_tokens is None:
            return None
        return max(0, self.max_total_tokens - self.total_tokens)

    @property
    def remaining_fraction(self) -> float:
        """Fraction of the token budget left, 0.0-1.0. 1.0 when unbounded.

        The loop uses this to decide when to start compacting context, which is
        why an unbounded budget reports full headroom rather than dividing by
        zero.
        """
        if self.max_total_tokens is None or self.max_total_tokens <= 0:
            return 1.0
        return self.remaining_tokens / self.max_total_tokens  # type: ignore[operator]

    def to_dict(self) -> dict[str, object]:
        return {
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "max_steps": self.max_steps,
            "max_tool_calls": self.max_tool_calls,
            "max_total_tokens": self.max_total_tokens,
            "exhausted_by": self.exhausted_by,
        }


@dataclass(slots=True)
class Budget:
    """Mutable running totals against a set of ceilings.

    `max_total_tokens=None` and `max_tool_calls=None` mean unbounded, which is
    the default for a one-shot CLI request and the wrong default for an
    unattended loop.
    """

    max_steps: int = 12
    max_tool_calls: int | None = None
    max_total_tokens: int | None = None
    steps: int = field(default=0)
    tool_calls: int = field(default=0)
    prompt_tokens: int = field(default=0)
    completion_tokens: int = field(default=0)

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        for name in ("max_tool_calls", "max_total_tokens"):
            value = getattr(self, name)
            if value is not None and value < 1:
                raise ValueError(f"{name} must be at least 1 or None")

    def snapshot(self) -> BudgetSnapshot:
        return BudgetSnapshot(
            steps=self.steps,
            tool_calls=self.tool_calls,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            max_steps=self.max_steps,
            max_tool_calls=self.max_tool_calls,
            max_total_tokens=self.max_total_tokens,
        )

    def record_step(
        self, *, prompt_tokens: int = 0, completion_tokens: int = 0
    ) -> None:
        """Count one iteration and its token usage, then re-check the limits."""
        self._require_non_negative("prompt_tokens", prompt_tokens)
        self._require_non_negative("completion_tokens", completion_tokens)
        self.steps += 1
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.check()

    def record_tool_calls(self, count: int = 1) -> None:
        """Count tool invocations, which tokens do not track."""
        if count < 0:
            raise ValueError("count must be non-negative")
        self.tool_calls += count
        self.check()

    def check(self) -> None:
        """Raise if any limit is now reached. Call after mutating totals."""
        reason = self.snapshot().exhausted_by
        if reason is not None:
            raise BudgetExhaustedError(f"budget exhausted: {reason}")

    def would_exceed(self, prompt_tokens: int = 0, completion_tokens: int = 0) -> bool:
        """Whether one more step of this size would cross a ceiling.

        Lets the caller stop *before* spending, which is the only point at
        stopping is cheap.
        """
        self._require_non_negative("prompt_tokens", prompt_tokens)
        self._require_non_negative("completion_tokens", completion_tokens)
        projected = self.total_tokens + prompt_tokens + completion_tokens
        if self.max_total_tokens is not None and projected > self.max_total_tokens:
            return True
        return self.steps + 1 > self.max_steps

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @staticmethod
    def _require_non_negative(name: str, value: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"{name} must be an int")
        if value < 0:
            raise ValueError(f"{name} must be non-negative")
