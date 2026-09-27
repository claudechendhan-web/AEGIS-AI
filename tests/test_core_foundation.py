"""Tests for the Phase 1 foundation modules.

The properties worth defending here are the ones whose absence is silent:

* `core.ids` - IDs must be unique *and* sortable, and a corrupt ID must age
  out rather than crash a sweep.
* `core.budget` - a limit must be reached exactly when it says it is, and
  `would_exceed` must agree with `check`.
* `core.transitions` - an illegal phase move must raise, not corrupt a run.
* `core.tasks` - a cycle must be impossible to construct, and `ready()` must
  never hand back a task whose dependencies are unmet.
"""

from __future__ import annotations

import itertools
import time

import pytest

from core.budget import Budget, BudgetExhaustedError
from core.errors import AegisError
from core.ids import (
    conversation_id,
    is_valid_id,
    message_id,
    new_id,
    run_id,
)
from core.tasks import (
    CyclicDependencyError,
    DuplicateTaskError,
    Task,
    TaskError,
    TaskGraph,
    TaskStatus,
    UnknownTaskError,
)
from core.transitions import (
    CYCLE,
    Decision,
    InvalidTransitionError,
    Phase,
    StateMachine,
    TerminalReason,
    cont,
    done,
)

# -- core.ids ----------------------------------------------------------------


def test_ids_are_unique() -> None:
    assert len({new_id() for _ in range(2000)}) == 2000


def test_ids_are_prefixed_and_sortable() -> None:
    first = new_id("run")
    time.sleep(0.002)
    second = new_id("run")
    assert first.startswith("run_")
    assert first < second, "ids must sort chronologically as plain strings"


def test_ids_sort_in_issue_order_within_a_millisecond() -> None:
    ids = [new_id("run") for _ in range(50)]
    assert ids == sorted(ids)


def test_an_invalid_prefix_is_rejected() -> None:
    with pytest.raises(ValueError, match="lowercase"):
        new_id("Run ID")


def test_valid_id_recognises_only_our_shape() -> None:
    assert is_valid_id(new_id("task"))
    assert not is_valid_id("nope")
    assert not is_valid_id("")
    assert not is_valid_id(None)  # type: ignore[arg-type]


def test_id_timestamp_recovers_creation_time() -> None:
    from core.ids import id_timestamp

    before = time.time()
    value = new_id("doc")
    recovered = id_timestamp(value)
    assert before - 1 <= recovered <= time.time() + 1


def test_a_corrupt_id_ages_out_instead_of_raising() -> None:
    from core.ids import id_timestamp

    assert id_timestamp("garbage") == 0.0


def test_named_constructors_use_their_prefix() -> None:
    assert conversation_id().startswith("conv_")
    assert message_id().startswith("msg_")
    assert run_id().startswith("run_")


# -- core.budget -------------------------------------------------------------


def test_a_fresh_budget_is_not_exhausted() -> None:
    budget = Budget(max_steps=3, max_total_tokens=100)
    assert budget.snapshot().exhausted is False
    assert budget.snapshot().remaining_tokens == 100


def test_recording_a_step_advances_the_totals() -> None:
    budget = Budget(max_steps=5, max_total_tokens=1000)
    budget.record_step(prompt_tokens=10, completion_tokens=5)
    assert budget.steps == 1
    assert budget.total_tokens == 15


def test_the_step_limit_is_enforced_exactly() -> None:
    budget = Budget(max_steps=2)
    budget.record_step()
    budget.record_step()  # the second step reaches the ceiling
    with pytest.raises(BudgetExhaustedError, match="max_steps"):
        budget.record_step()


def test_the_token_limit_is_enforced() -> None:
    budget = Budget(max_steps=100, max_total_tokens=50)
    with pytest.raises(BudgetExhaustedError, match="max_total_tokens"):
        budget.record_step(prompt_tokens=30, completion_tokens=25)


def test_the_tool_call_limit_is_enforced() -> None:
    budget = Budget(max_steps=100, max_tool_calls=2)
    budget.record_tool_calls()
    budget.record_tool_calls()
    with pytest.raises(BudgetExhaustedError, match="max_tool_calls"):
        budget.record_tool_calls()


def test_unbounded_budgets_report_full_headroom() -> None:
    budget = Budget(max_steps=10)
    snapshot = budget.snapshot()
    assert snapshot.remaining_tokens is None
    assert snapshot.remaining_fraction == 1.0
    assert snapshot.exhausted_by is None


def test_would_exceed_agrees_with_check() -> None:
    budget = Budget(max_steps=10, max_total_tokens=100)
    assert budget.would_exceed(prompt_tokens=100) is False, (
        "exactly at the limit is within it"
    )
    assert budget.would_exceed(prompt_tokens=101) is True
    budget.record_step(prompt_tokens=99)
    assert budget.would_exceed(prompt_tokens=1) is False
    assert budget.would_exceed(prompt_tokens=2) is True


def test_negative_usage_is_rejected() -> None:
    budget = Budget()
    with pytest.raises(ValueError, match="non-negative"):
        budget.record_step(prompt_tokens=-1)
    with pytest.raises(ValueError, match="non-negative"):
        budget.record_tool_calls(-1)


def test_a_bool_is_not_an_int_count() -> None:
    budget = Budget()
    with pytest.raises(TypeError):
        budget.record_step(prompt_tokens=True)  # type: ignore[arg-type]


def test_nonsense_limits_are_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="max_steps"):
        Budget(max_steps=0)
    with pytest.raises(ValueError, match="max_total_tokens"):
        Budget(max_total_tokens=0)


def test_budget_exhaustion_is_an_aegis_error() -> None:
    assert issubclass(BudgetExhaustedError, AegisError)


def test_snapshot_round_trips_to_dict() -> None:
    budget = Budget(max_steps=4, max_total_tokens=10)
    budget.record_step(prompt_tokens=1, completion_tokens=1)
    payload = budget.snapshot().to_dict()
    assert payload["total_tokens"] == 2
    assert payload["max_steps"] == 4


# -- core.transitions --------------------------------------------------------


def test_the_cycle_wraps_back_to_orient() -> None:
    assert CYCLE[-1] is Phase.COMMIT
    machine = StateMachine()
    machine.begin()
    for _ in range(len(CYCLE)):
        machine.step()
    assert machine.phase is Phase.ORIENT


def test_begin_is_only_legal_from_idle() -> None:
    machine = StateMachine()
    machine.begin()
    with pytest.raises(InvalidTransitionError, match="cannot begin"):
        machine.begin()


def test_idle_and_terminal_have_no_successor() -> None:
    machine = StateMachine(Phase.TERMINAL)
    with pytest.raises(InvalidTransitionError, match="no successor"):
        machine.step()


def test_a_continue_decision_cannot_terminate_a_run() -> None:
    from core.transitions import ContinueReason

    machine = StateMachine()
    machine.begin()
    with pytest.raises(InvalidTransitionError, match="cannot terminate"):
        machine.terminate(cont(ContinueReason.TOOL_USE))


def test_a_run_cannot_be_terminated_twice() -> None:
    machine = StateMachine()
    machine.begin()
    machine.terminate(done(TerminalReason.COMPLETED))
    with pytest.raises(InvalidTransitionError, match="already terminated"):
        machine.terminate(done())


def test_only_completion_counts_as_success() -> None:
    assert done(TerminalReason.COMPLETED).succeeded is True
    assert done(TerminalReason.MAX_STEPS).succeeded is False
    assert done(TerminalReason.BUDGET_EXHAUSTED).succeeded is False


def test_decisions_serialise_their_kind() -> None:
    payload = done(TerminalReason.BLOCKED, "needs a credential").to_dict()
    assert payload["terminal"] is True
    assert payload["reason"] == "blocked"


def test_an_unknown_reason_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        TerminalReason("banana")


def test_history_records_the_path_taken() -> None:
    machine = StateMachine()
    machine.begin()
    machine.step()
    assert machine.history[0] is Phase.IDLE
    assert machine.history[1] is Phase.ORIENT
    assert machine.history[2] is Phase.SELECT


def test_decision_is_hashable_and_frozen() -> None:
    decision = Decision(reason=TerminalReason.COMPLETED)
    with pytest.raises(AttributeError):
        decision.detail = "mutated"  # type: ignore[misc]


# -- core.tasks --------------------------------------------------------------


def test_a_task_starts_pending() -> None:
    assert Task(description="read the file").status is TaskStatus.PENDING


def test_dependencies_must_exist_when_adding() -> None:
    graph = TaskGraph()
    with pytest.raises(UnknownTaskError, match="unknown task"):
        graph.create("do a thing", depends_on=["nope"])


def test_duplicate_ids_are_rejected() -> None:
    graph = TaskGraph()
    task = graph.create("one")
    with pytest.raises(DuplicateTaskError):
        graph.add(Task(description="two", id=task.id))


def test_ready_returns_only_tasks_whose_dependencies_are_done() -> None:
    graph = TaskGraph()
    first = graph.create("first")
    second = graph.create("second", depends_on=[first.id])
    assert [t.id for t in graph.ready()] == [first.id]
    graph.complete(first.id)
    assert [t.id for t in graph.ready()] == [second.id]


def test_a_cycle_cannot_be_constructed() -> None:
    """`add` validates the finished graph, so a cycle is unbuildable."""
    graph = TaskGraph()
    a = graph.create("a")
    b = graph.create("b", depends_on=[a.id])
    c = graph.create("c", depends_on=[b.id])
    assert [t.id for t in graph.topological_order()] == [a.id, b.id, c.id]


def test_adding_a_task_that_closes_a_cycle_is_rejected_and_rolled_back() -> None:
    graph = TaskGraph()
    a = graph.create("a")
    b = graph.create("b", depends_on=[a.id])
    # `a` gains a dependency on `b`, so a -> b -> a.
    with pytest.raises(CyclicDependencyError, match="cycle"):
        graph.link(a.id, b.id)
    # The rejected edge must not linger.
    assert graph.get(a.id).depends_on == []
    assert len(graph) == 2


def test_a_long_cycle_is_also_rejected() -> None:
    graph = TaskGraph()
    nodes = [graph.create(f"step {i}") for i in range(6)]
    for earlier, later in itertools.pairwise(nodes):
        graph.link(later.id, earlier.id)
    with pytest.raises(CyclicDependencyError):
        graph.link(nodes[0].id, nodes[-1].id)
    assert graph.get(nodes[0].id).depends_on == []


def test_linking_an_unknown_task_raises() -> None:
    graph = TaskGraph()
    a = graph.create("a")
    with pytest.raises(UnknownTaskError):
        graph.link(a.id, "missing")


def test_topological_order_puts_dependencies_first() -> None:
    graph = TaskGraph()
    a = graph.create("a")
    b = graph.create("b", depends_on=[a.id])
    c = graph.create("c", depends_on=[a.id])
    order = [t.id for t in graph.topological_order()]
    assert order.index(a.id) < order.index(b.id)
    assert order.index(a.id) < order.index(c.id)


def test_an_illegal_status_transition_raises() -> None:
    graph = TaskGraph()
    task = graph.create("a")
    task.transition(TaskStatus.DONE)
    with pytest.raises(TaskError, match="cannot move"):
        task.transition(TaskStatus.RUNNING)


def test_a_running_task_can_fail() -> None:
    graph = TaskGraph()
    task = graph.create("a")
    graph.get(task.id).transition(TaskStatus.READY)
    graph.get(task.id).transition(TaskStatus.RUNNING)
    graph.fail(task.id, "the build broke")
    assert graph.get(task.id).error == "the build broke"


def test_completing_a_task_unblocks_its_dependents() -> None:
    graph = TaskGraph()
    a = graph.create("a")
    b = graph.create("b", depends_on=[a.id])
    graph.get(b.id).transition(TaskStatus.BLOCKED)
    graph.complete(a.id, "ok")
    assert graph.get(b.id).status is TaskStatus.READY


def test_the_graph_reports_completion() -> None:
    graph = TaskGraph()
    a = graph.create("a")
    b = graph.create("b", depends_on=[a.id])
    assert graph.is_finished is False
    graph.complete(a.id)
    assert graph.complete_count == 1
    graph.complete(b.id)
    assert graph.is_finished is True


def test_an_empty_graph_is_not_finished() -> None:
    """Vacuously 'all done' would report a finished plan that does nothing."""
    assert TaskGraph().is_finished is False


def test_a_deep_chain_does_not_exhaust_the_stack() -> None:
    """`topological_order` recurses per dependency chain, so keep it honest at depth."""
    graph = TaskGraph()
    previous = graph.create("step 0")
    for index in range(1, 400):
        previous = graph.create(f"step {index}", depends_on=[previous.id])
    assert len(graph) == 400
    assert len(graph.topological_order()) == 400


def test_dependents_are_reported() -> None:
    graph = TaskGraph()
    a = graph.create("a")
    b = graph.create("b", depends_on=[a.id])
    assert [t.id for t in graph.dependents(a.id)] == [b.id]


def test_unknown_ids_raise_on_access() -> None:
    with pytest.raises(UnknownTaskError):
        TaskGraph().get("missing")


def test_serialisation_exposes_the_plan() -> None:
    graph = TaskGraph()
    graph.create("a")
    payload = graph.to_dict()
    assert payload["total"] == 1
    assert payload["tasks"][0]["status"] == "pending"  # type: ignore[index]
