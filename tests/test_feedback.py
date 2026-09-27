"""Tests for the feedback loop.

The mechanism's one real failure mode is a model amplifying its own mistakes:
a confidently wrong answer scores well on "did it answer, is it a reasonable
length". Every test here is aimed at that, so the assertions are about what is
*excluded* from the prompt at least as much as what is included.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from training.feedback import (
    FeedbackError,
    FeedbackStore,
)

GOAL = "What is SQLite WAL mode?"

GOOD_ANSWER = (
    "SQLite uses a write-ahead logging (WAL) mode that allows concurrent "
    "reads and writes by writing changes to a separate log file before "
    "updating the main database file."
)
EMPTY_ANSWER = ""
FILLER = "I cannot help with that. As an AI, I do not know the answer to this question."
REPETITIVE = "the value is the value of the value. " * 40


@pytest.fixture()
def store() -> Iterator[FeedbackStore]:
    with FeedbackStore(":memory:") as feedback:
        yield feedback


# -- the heuristic ----------------------------------------------------------


def test_an_empty_answer_scores_zero(store: FeedbackStore) -> None:
    assert store.auto_score("a goal", EMPTY_ANSWER, steps=1, tools=()) == 0.0


def test_a_specific_informed_answer_scores_well(store: FeedbackStore) -> None:
    score = store.auto_score(
        "What is SQLite WAL mode?", GOOD_ANSWER, steps=2, tools=["web_search"]
    )
    assert score >= 0.6


def test_filler_is_penalised_heavily(store: FeedbackStore) -> None:
    score = store.auto_score("What is WAL mode?", FILLER, steps=1, tools=())
    assert score < 0.4


def test_repetition_is_penalised(store: FeedbackStore) -> None:
    score = store.auto_score("What is the value?", REPETITIVE, steps=1, tools=())
    assert score < 0.6


def test_a_research_goal_answered_without_a_tool_is_penalised(
    store: FeedbackStore,
) -> None:
    with_tool = store.auto_score(
        "Search the web for the current SQLite version",
        GOOD_ANSWER,
        steps=2,
        tools=["web_search"],
    )
    without = store.auto_score(
        "Search the web for the current SQLite version",
        GOOD_ANSWER,
        steps=1,
        tools=(),
    )
    assert with_tool > without


def test_an_errored_run_is_penalised(store: FeedbackStore) -> None:
    clean = store.auto_score("goal", GOOD_ANSWER, steps=2, tools=["web_search"])
    errored = store.auto_score(
        "goal", GOOD_ANSWER, steps=2, tools=["web_search"], errored=True
    )
    assert errored < clean


def test_scores_stay_in_range(store: FeedbackStore) -> None:
    for answer in (EMPTY_ANSWER, FILLER, REPETITIVE, GOOD_ANSWER, "x" * 30000):
        for tools in ((), ("web_search",)):
            score = store.auto_score("g", answer, steps=3, tools=tools)
            assert 0.0 <= score <= 1.0


# -- capture ----------------------------------------------------------------


def test_recording_a_run(store: FeedbackStore) -> None:
    record = store.record(
        "What is WAL mode?", GOOD_ANSWER, steps=2, tools=["web_search"]
    )
    assert record.run_id
    assert store.get(record.run_id) is not None
    assert record.effective_score > 0


def test_a_record_serialises(store: FeedbackStore) -> None:
    record = store.record(GOAL, GOOD_ANSWER, steps=2, tools=["web_search"])
    payload = record.to_dict()
    json.dumps(payload)
    assert payload["tools_used"] == ["web_search"]


# -- human judgement wins ---------------------------------------------------


def test_a_human_score_overrides_the_heuristic(store: FeedbackStore) -> None:
    record = store.record(GOAL, GOOD_ANSWER, steps=2, tools=["web_search"])
    assert record.verdict == "exemplar"
    # A person says it was wrong. That beats any heuristic.
    updated = store.score(record.run_id, 1, critique="factually incorrect")
    assert updated is not None
    assert updated.verdict == "warning"
    assert updated.effective_score == 0.2


def test_a_low_human_score_demotes_an_exemplar(store: FeedbackStore) -> None:
    record = store.record(GOAL, GOOD_ANSWER, steps=2, tools=["web_search"])
    assert store.by_verdict("exemplar")
    store.score(record.run_id, 0, critique="useless")
    assert store.by_verdict("exemplar") == []
    assert store.by_verdict("warning")


def test_a_human_score_can_promote_a_bad_looking_run(store: FeedbackStore) -> None:
    record = store.record(GOAL, FILLER, steps=1, tools=())
    updated = store.score(record.run_id, 5, critique="actually correct")
    assert updated is not None
    assert updated.verdict == "exemplar"


def test_an_out_of_range_score_is_refused(store: FeedbackStore) -> None:
    record = store.record(GOAL, GOOD_ANSWER)
    with pytest.raises(FeedbackError):
        store.score(record.run_id, 9)
    with pytest.raises(FeedbackError):
        store.score(record.run_id, -1)


def test_scoring_an_unknown_run_returns_none(store: FeedbackStore) -> None:
    assert store.score("nope", 5) is None


# -- what reaches the prompt ------------------------------------------------


def test_a_prompt_block_starts_empty(store: FeedbackStore) -> None:
    """Silence teaches nothing and costs no tokens."""
    assert store.prompt_block() == ""


def test_exemplars_reach_the_prompt(store: FeedbackStore) -> None:
    store.record("What is WAL mode?", GOOD_ANSWER, steps=2, tools=["web_search"])
    block = store.prompt_block()
    assert "GOOD" in block
    assert "web_search" in block


def test_warnings_reach_the_prompt_with_the_reason(store: FeedbackStore) -> None:
    record = store.record("What is the reserve?", FILLER, steps=1, tools=())
    store.score(record.run_id, 0, critique="refused instead of answering")
    block = store.prompt_block()
    assert "BAD" in block
    assert "refused instead of answering" in block


def test_a_demoted_run_leaves_the_exemplar_block(store: FeedbackStore) -> None:
    record = store.record(GOAL, GOOD_ANSWER, steps=2, tools=["web_search"])
    assert "GOOD" in store.prompt_block()
    store.score(record.run_id, 0, critique="wrong")
    assert "GOOD" not in store.prompt_block()


def test_human_scored_exemplars_are_listed_first() -> None:
    with FeedbackStore(":memory:") as store:
        # Auto-scored only, lower auto score than the other.
        store.record("What is WAL mode?", GOOD_ANSWER, steps=2, tools=["web_search"])
        # Auto-scored high, but a person rated it higher still.
        human = store.record(
            "What is WAL mode?", GOOD_ANSWER, steps=2, tools=["web_search"]
        )
        store.score(human.run_id, 5, critique="excellent")
        exemplars = store.exemplars(limit=5)
        assert exemplars[0].run_id == human.run_id


def test_exemplar_answers_are_truncated(store: FeedbackStore) -> None:
    record = store.record(
        GOAL, GOOD_ANSWER * 20, steps=2, tools=["web_search"], human_score=5
    )
    assert record.verdict == "exemplar"
    stored = store.get(record.run_id)
    assert stored is not None
    # The stored copy stays whole even though the prompt copy is cut.
    assert len(stored.answer) == len(GOOD_ANSWER) * 20
    assert len(store.exemplars(limit=1)[0].answer) <= 400


def test_the_block_ends_with_an_instruction(store: FeedbackStore) -> None:
    store.record(GOAL, GOOD_ANSWER, steps=2, tools=["web_search"])
    block = store.prompt_block()
    assert "avoid" in block.lower()


# -- persistence ------------------------------------------------------------


def test_feedback_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "feedback.db"
    with FeedbackStore(path) as first:
        record = first.record("goal", GOOD_ANSWER, steps=2, tools=["web_search"])
    with FeedbackStore(path) as second:
        loaded = second.get(record.run_id)
        assert loaded is not None
        assert loaded.goal == "goal"


def test_human_scores_persist(tmp_path: Path) -> None:
    path = tmp_path / "feedback.db"
    with FeedbackStore(path) as first:
        record = first.record("goal", GOOD_ANSWER, steps=2, tools=["web_search"])
        first.score(record.run_id, 0, critique="wrong")
    with FeedbackStore(path) as second:
        loaded = second.get(record.run_id)
        assert loaded is not None
        assert loaded.human_score == 0
        assert loaded.verdict == "warning"


# -- importing the run log --------------------------------------------------


def test_import_from_the_agent_log(tmp_path: Path) -> None:
    log = tmp_path / "agent_runs.jsonl"
    log.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "run_id": "r1",
                        "goal": "What is WAL mode?",
                        "answer": GOOD_ANSWER,
                        "steps": [
                            {
                                "index": 1,
                                "continuation": "tool_use",
                                "tools": ["web_search"],
                            }
                        ],
                    }
                ),
                json.dumps({"not_a_run": True}),
                "{malformed",
                "",
            ]
        ),
        encoding="utf-8",
    )
    with FeedbackStore(tmp_path / "f.db") as store:
        assert store.import_runs(log) == 1
        loaded = store.get("r1")
        assert loaded is not None
        assert loaded.tools_used == ("web_search",)


def test_import_is_idempotent(tmp_path: Path) -> None:
    log = tmp_path / "agent_runs.jsonl"
    log.write_text(
        json.dumps({"run_id": "r1", "goal": "g", "answer": GOOD_ANSWER, "steps": []}),
        encoding="utf-8",
    )
    with FeedbackStore(tmp_path / "f.db") as store:
        assert store.import_runs(log) == 1
        assert store.import_runs(log) == 0


def test_importing_a_missing_log_is_zero(tmp_path: Path) -> None:
    with FeedbackStore(":memory:") as store:
        assert store.import_runs(tmp_path / "nope.jsonl") == 0


# -- reporting --------------------------------------------------------------


def test_counts_and_report(store: FeedbackStore) -> None:
    store.record(GOAL, GOOD_ANSWER, steps=2, tools=["web_search"])
    report = store.to_dict()
    assert report["exemplar_floor"] == 0.75
    assert "counts" in report
    json.dumps(report)


def test_events_are_logged(store: FeedbackStore) -> None:
    record = store.record(GOAL, GOOD_ANSWER)
    store.score(record.run_id, 4)
    kinds = [event["kind"] for event in store.events()]
    assert "recorded" in kinds
    assert "scored" in kinds


def test_an_empty_store_reports_cleanly(store: FeedbackStore) -> None:
    assert store.counts() == {}
    assert store.recent() == []
    assert store.prompt_block() == ""
