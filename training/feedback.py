"""Feedback capture: past outputs become future instructions.

A model does not improve by generating. It improves when good outputs are
captured, judged, and fed back into the context it will use next time. This
is in-context learning rather than retraining, and that is a deliberate
choice, not a compromise:

* It is free. No GPU, no rental, no fine-tune. Consistent with the
  "free only, no expiry" constraint.
* It is immediate. The next run benefits, not the next training cycle.
* It is inspectable. Every exemplar in the prompt can be traced to a specific
  scored run, and deleting the store removes them.

Two signals feed it, and they are deliberately kept separate:

* **Automatic heuristics**, so a loop can run with zero human input. Cheap
  signals: did it answer, did it use a tool when the goal implied research,
  is it a sane length, did any step error, is it self-repetitive.
* **Human scores**, which override the heuristics. You know things the
  heuristics cannot see, so a human judgement always wins.

The failure mode to avoid is the obvious one: a model that only ever learns
from its own output will amplify its own mistakes, because a confidently
wrong answer scores well on "did it answer, is it the right length". That is
why nothing is promoted to an exemplar without a positive signal, why the
worst-scoring runs become explicit warnings rather than being quietly dropped,
and why a human score of 0 is the fastest way to make something stop
appearing in the prompt.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

# Words a small model repeats when it has nothing to say. Their presence
# across several sentences is a decent proxy for a degenerate answer.
_FILLER = (
    "i cannot",
    "i can't",
    "as an ai",
    "i'm sorry",
    "i do not know",
    "i don't know",
    "it is important to note",
    "please note",
)

SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS feedback_runs (
        run_id TEXT PRIMARY KEY,
        goal TEXT NOT NULL,
        answer TEXT NOT NULL,
        steps INTEGER NOT NULL DEFAULT 0,
        tools_used TEXT NOT NULL DEFAULT '[]',
        auto_score TEXT NOT NULL DEFAULT '0',
        human_score INTEGER,
        critique TEXT NOT NULL DEFAULT '',
        verdict TEXT NOT NULL DEFAULT 'neutral',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS feedback_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        run_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_feedback_verdict
    ON feedback_runs (verdict, created_at)
    """,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())[:12]


class FeedbackError(Exception):
    """Raised when feedback cannot be recorded or read."""


@dataclass(frozen=True, slots=True)
class RunRecord:
    """One captured run, with whatever scores are known."""

    run_id: str
    goal: str
    answer: str
    steps: int = 0
    tools_used: tuple[str, ...] = ()
    auto_score: float = 0.0
    human_score: int | None = None
    critique: str = ""
    verdict: str = "neutral"
    created_at: str = field(default_factory=_now)

    @property
    def effective_score(self) -> float:
        """A human judgement overrides the heuristic, always."""
        if self.human_score is not None:
            return max(0.0, min(1.0, self.human_score / 5.0))
        return max(0.0, min(1.0, self.auto_score))

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "answer": self.answer,
            "steps": self.steps,
            "tools_used": list(self.tools_used),
            "auto_score": round(self.auto_score, 4),
            "human_score": self.human_score,
            "effective_score": round(self.effective_score, 4),
            "critique": self.critique,
            "verdict": self.verdict,
            "created_at": self.created_at,
        }


class FeedbackStore:
    """Captures runs, scores them, and distils them into prompt material."""

    EXEMPLAR_FLOOR = 0.75
    WARNING_FLOOR = 0.25

    def __init__(
        self,
        database_path: str | Path = "data/feedback.db",
        *,
        timeout: float = 5.0,
    ) -> None:
        memory = str(database_path) == ":memory:"
        self.database_path = Path(":memory:") if memory else Path(database_path)
        if not memory:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._connection: sqlite3.Connection | None = sqlite3.connect(
                str(self.database_path), timeout=timeout, check_same_thread=False
            )
        except sqlite3.Error as exc:
            raise FeedbackError(f"unable to open feedback store: {exc}") from exc
        self._connection.row_factory = sqlite3.Row
        try:
            self.initialize()
        except Exception:
            self.close()
            raise

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise FeedbackError("feedback store is closed")
        return self._connection

    def initialize(self) -> None:
        with self.connection:
            for statement in SCHEMA:
                self.connection.execute(statement)

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- scoring ----------------------------------------------------------

    @staticmethod
    def auto_score(
        goal: str,
        answer: str,
        *,
        steps: int,
        tools: Sequence[str],
        errored: bool = False,
    ) -> float:
        """Cheap heuristic quality score in [0, 1].

        Deliberately conservative. Every signal here is a proxy, and a
        heuristic that awards 0.9 to a confidently wrong answer would poison
        the exemplar set, which is the one way this mechanism can actively
        make the agent worse.
        """
        if not answer.strip():
            return 0.0
        score = 0.20

        length = len(answer)
        if 40 <= length <= 4000:
            score += 0.20
        elif length > 12000:
            score -= 0.10

        sentences = [s for s in re.split(r"(?<=[.!?])\s+", answer) if s.strip()]
        if len(sentences) >= 2 or length > 250:
            score += 0.10

        # Repetition: the same 40 characters three times is not an answer.
        normalized = " ".join(answer.lower().split())
        if len(normalized) >= 120:
            chunk = normalized[:40]
            if normalized.count(chunk) > 2:
                score -= 0.2

        lowered = answer.lower()
        filler_hits = sum(1 for phrase in _FILLER if phrase in lowered)
        if filler_hits:
            score -= min(0.4, 0.15 * filler_hits)

        # Four letters, not five: most topic words in a short goal are four,
        # so a five-letter key silently discarded "what is WAL mode?" and the
        # automatic path could never reach the exemplar floor in practice.
        goal_words = set(re.findall(r"[a-z]{4,}", goal.lower()))
        answer_words = set(re.findall(r"[a-z]{4,}", lowered))
        if goal_words and goal_words & answer_words:
            score += 0.20

        if tools:
            score += 0.15

        # A goal that asks for current or external facts should have used a
        # tool. Answering such a question from memory is the classic failure.
        research_words = (
            "search",
            "find",
            "look up",
            "current",
            "latest",
            "news",
            "price",
            "market",
            "web",
            "online",
        )
        wants_research = any(word in goal.lower() for word in research_words)
        if wants_research and tools:
            score += 0.15
        elif wants_research and not tools:
            score -= 0.20

        if tools and steps > 1:
            score += 0.05

        if errored:
            score -= 0.30

        return max(0.0, min(1.0, score))

    # -- capture ----------------------------------------------------------

    def record(
        self,
        goal: str,
        answer: str,
        *,
        run_id: str | None = None,
        steps: int = 0,
        tools: Sequence[str] = (),
        errored: bool = False,
        human_score: int | None = None,
        critique: str = "",
    ) -> RunRecord:
        record = RunRecord(
            run_id=run_id or _new_id(),
            goal=goal,
            answer=answer,
            steps=steps,
            tools_used=tuple(tools),
            auto_score=self.auto_score(
                goal, answer, steps=steps, tools=tools, errored=errored
            ),
            human_score=human_score,
            critique=critique,
            verdict=self._verdict(
                human_score,
                self.auto_score(
                    goal, answer, steps=steps, tools=tools, errored=errored
                ),
            ),
        )
        with self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO feedback_runs
                    (run_id, goal, answer, steps, tools_used, auto_score,
                     human_score, critique, verdict, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.run_id,
                    record.goal,
                    record.answer,
                    record.steps,
                    json.dumps(list(record.tools_used)),
                    format(record.auto_score, "f"),
                    record.human_score,
                    record.critique,
                    record.verdict,
                    record.created_at,
                    _now(),
                ),
            )
        self._event(record.run_id, "recorded", f"score {record.effective_score:.2f}")
        return record

    @staticmethod
    def _verdict(human: int | None, auto: float) -> str:
        if human is not None:
            normalized = max(0.0, min(1.0, human / 5.0))
        else:
            normalized = auto
        if normalized >= FeedbackStore.EXEMPLAR_FLOOR:
            return "exemplar"
        if normalized <= FeedbackStore.WARNING_FLOOR:
            return "warning"
        return "neutral"

    def score(
        self, run_id: str, human_score: int, critique: str = ""
    ) -> RunRecord | None:
        """Apply a human judgement. Overrides the heuristic permanently."""
        if not 0 <= human_score <= 5:
            raise FeedbackError("human_score must be between 0 and 5")
        record = self.get(run_id)
        if record is None:
            return None
        with self.connection:
            self.connection.execute(
                "UPDATE feedback_runs SET human_score = ?, critique = ?, "
                "verdict = ?, updated_at = ? WHERE run_id = ?",
                (
                    human_score,
                    critique,
                    self._verdict(human_score, record.auto_score),
                    _now(),
                    run_id,
                ),
            )
        self._event(run_id, "scored", f"{human_score}/5 {critique}".strip())
        return self.get(run_id)

    def _event(self, run_id: str, kind: str, detail: str) -> None:
        try:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO feedback_events (at, run_id, kind, detail) "
                    "VALUES (?, ?, ?, ?)",
                    (_now(), run_id, kind, detail[:400]),
                )
        except sqlite3.Error:
            pass

    # -- reading ----------------------------------------------------------

    @staticmethod
    def _row(row: Mapping[str, Any]) -> RunRecord:
        try:
            tools = tuple(json.loads(row["tools_used"] or "[]"))
        except (json.JSONDecodeError, TypeError):
            tools = ()
        return RunRecord(
            run_id=row["run_id"],
            goal=row["goal"],
            answer=row["answer"],
            steps=int(row["steps"]),
            tools_used=tools,
            auto_score=float(row["auto_score"] or 0.0),
            human_score=row["human_score"],
            critique=row["critique"] or "",
            verdict=row["verdict"] or "neutral",
            created_at=row["created_at"],
        )

    def get(self, run_id: str) -> RunRecord | None:
        row = self.connection.execute(
            "SELECT * FROM feedback_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return self._row(row) if row else None

    def by_verdict(self, verdict: str, limit: int = 10) -> list[RunRecord]:
        rows = self.connection.execute(
            "SELECT * FROM feedback_runs WHERE verdict = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (verdict, limit),
        ).fetchall()
        return [self._row(row) for row in rows]

    def recent(self, limit: int = 20) -> list[RunRecord]:
        rows = self.connection.execute(
            "SELECT * FROM feedback_runs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row(row) for row in rows]

    def events(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM feedback_events ORDER BY event_id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def counts(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT verdict, COUNT(*) AS n FROM feedback_runs GROUP BY verdict"
        ).fetchall()
        return {row["verdict"]: int(row["n"]) for row in rows}

    # -- distilling into prompt material ----------------------------------

    def exemplars(self, limit: int = 3, *, max_chars: int = 400) -> list[RunRecord]:
        """The best-scoring runs, best first.

        Human-scored rows come first, because a heuristic that happens to be
        generous should not outrank a person's judgement.
        """
        rows = self.connection.execute(
            "SELECT * FROM feedback_runs WHERE verdict = 'exemplar' "
            "ORDER BY (human_score IS NULL) ASC, "
            "COALESCE(human_score, 0) DESC, auto_score DESC, created_at DESC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
        results: list[RunRecord] = []
        for row in rows:
            record = self._row(row)
            # Truncate the answer, not the record, so the stored copy stays
            # whole. Built by field rather than `vars()`, which fails on a
            # frozen slots dataclass.
            results.append(
                RunRecord(
                    run_id=record.run_id,
                    goal=record.goal,
                    answer=record.answer[:max_chars],
                    steps=record.steps,
                    tools_used=record.tools_used,
                    auto_score=record.auto_score,
                    human_score=record.human_score,
                    critique=record.critique,
                    verdict=record.verdict,
                    created_at=record.created_at,
                )
            )
        return results

    def warnings(self, limit: int = 3) -> list[RunRecord]:
        return self.by_verdict("warning", limit)

    def prompt_block(self, *, limit: int = 3, max_chars: int = 400) -> str:
        """A prompt-ready block of what worked and what did not.

        Empty when there is nothing worth saying, which is the correct output
        for an agent with no scored history: silence teaches nothing and
        costs no tokens.
        """
        good = self.exemplars(limit=limit, max_chars=max_chars)
        bad = self.warnings(limit=limit)
        if not good and not bad:
            return ""
        lines = ["What you have done well and badly before:"]
        for record in good:
            tools = ", ".join(record.tools_used) or "no tools"
            lines.append(
                f"- GOOD (score {record.effective_score:.2f}, used {tools}): "
                f'for a goal like "{record.goal[:80]}", you answered '
                f'"{record.answer[:160]}..."'
            )
        for record in bad:
            reason = record.critique or f"scored {record.effective_score:.2f}"
            lines.append(
                f'- BAD ({reason}): for a goal like "{record.goal[:80]}" your '
                f'answer was "{record.answer[:120]}..."'
            )
        lines.append(
            "Follow the pattern of the good ones and avoid the specific "
            "mistake in the bad ones. If a new goal resembles a bad case, "
            "change your approach deliberately."
        )
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": _now(),
            "counts": self.counts(),
            "exemplar_floor": self.EXEMPLAR_FLOOR,
            "warning_floor": self.WARNING_FLOOR,
            "exemplars": [record.to_dict() for record in self.exemplars(limit=5)],
            "warnings": [record.to_dict() for record in self.warnings(limit=5)],
        }

    def import_runs(self, log_path: str | Path) -> int:
        """Import runs from the agent's JSONL log.

        Only answers the agent actually produced, scored by the same
        conservative heuristic. Nothing is invented.
        """
        path = Path(log_path)
        if not path.exists():
            return 0
        imported = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or "run_id" not in payload:
                continue
            if "answer" not in payload or "goal" not in payload:
                continue
            steps = payload.get("steps") or []
            tools: list[str] = []
            for step in steps:
                if isinstance(step, dict):
                    tools.extend(step.get("tools") or [])
            if self.get(str(payload["run_id"])) is not None:
                continue
            self.record(
                str(payload["goal"]),
                str(payload.get("answer") or ""),
                run_id=str(payload["run_id"]),
                steps=len(steps),
                tools=tools,
            )
            imported += 1
        return imported
