"""Task decomposition as a directed acyclic graph.

A goal is not a list of steps, it is a graph: some steps cannot start until
others finish, and a plan that cannot be topologically sorted is not a plan.
The failure this prevents is the expensive one - an agent that discovers at
step 9 that step 3 was a precondition, after having already spent eight steps
and burned the budget.

Cycle detection is therefore the centre of this module, not an afterthought.
`add` refuses an edge that would close a cycle, so an invalid graph cannot be
constructed rather than being detected later by a sort that silently returns a
partial order.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from core.errors import AegisError
from core.ids import new_id


class TaskError(AegisError):
    """Base class for task-graph failures."""


class UnknownTaskError(TaskError):
    """A referenced task id is not in the graph."""


class DuplicateTaskError(TaskError):
    """A task id was added twice."""


class CyclicDependencyError(TaskError):
    """An edge would close a cycle."""


class TaskStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


#: Statuses from which no further transition is legal.
TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.SKIPPED}
)

#: The transitions the graph permits. Anything absent is rejected by
#: `Task.transition`, so an out-of-order state change fails loudly.
#:
#: `PENDING -> DONE` is legal because a task with no dependencies is already
#: complete-able: making it hop through READY and RUNNING first would mean a
#: caller had to invent a run that never happened. Same reasoning for
#: `READY -> DONE`, which is what an externally-completed task looks like.
ALLOWED_TRANSITIONS: Mapping[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset(
        {
            TaskStatus.READY,
            TaskStatus.DONE,
            TaskStatus.BLOCKED,
            TaskStatus.SKIPPED,
        }
    ),
    TaskStatus.READY: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.DONE,
            TaskStatus.BLOCKED,
            TaskStatus.SKIPPED,
        }
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.DONE,
            TaskStatus.FAILED,
            TaskStatus.BLOCKED,
            TaskStatus.SKIPPED,
        }
    ),
    TaskStatus.BLOCKED: frozenset(
        {TaskStatus.READY, TaskStatus.SKIPPED, TaskStatus.FAILED}
    ),
    TaskStatus.DONE: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.SKIPPED: frozenset(),
}


@dataclass(slots=True)
class Task:
    """One node: a description, its status, and what it depends on."""

    description: str
    id: str = field(default_factory=lambda: new_id("task"))
    status: TaskStatus = TaskStatus.PENDING
    depends_on: list[str] = field(default_factory=list)
    result: str = ""
    error: str = ""

    def transition(self, target: TaskStatus) -> None:
        """Move to `target`, or raise if the graph forbids it."""
        if target not in ALLOWED_TRANSITIONS[self.status]:
            raise TaskError(
                f"task {self.id} cannot move from {self.status.value} to {target.value}"
            )
        self.status = target

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "description": self.description,
            "status": self.status.value,
            "depends_on": list(self.depends_on),
            "result": self.result,
            "error": self.error,
        }


class TaskGraph:
    """A dependency graph of tasks that is acyclic by construction."""

    __slots__ = ("_tasks",)

    def __init__(self, tasks: Iterable[Task] = ()) -> None:
        self._tasks: dict[str, Task] = {}
        for task in tasks:
            self.add(task)

    def __len__(self) -> int:
        return len(self._tasks)

    def __contains__(self, task_id: object) -> bool:
        return task_id in self._tasks

    def add(self, task: Task) -> Task:
        """Add a task, rejecting duplicates and any edge that closes a cycle.

        The cycle check runs over the whole graph *after* insertion, not per
        edge before it. Checking before insertion cannot work: a task that is
        not yet in the graph has no incoming edges, so no dependency of its own
        can ever reach it, and the check would be dead code. Validating the
        finished graph is the only placement that actually holds the invariant.
        """
        if task.id in self._tasks:
            raise DuplicateTaskError(f"task already exists: {task.id}")
        for dependency in task.depends_on:
            if dependency not in self._tasks:
                raise UnknownTaskError(
                    f"task {task.id} depends on unknown task {dependency}"
                )
        self._tasks[task.id] = task
        try:
            self.topological_order()
        except CyclicDependencyError:
            del self._tasks[task.id]
            raise
        return task

    def link(self, task_id: str, dependency_id: str) -> Task:
        """Add `dependency_id` as a prerequisite of an existing `task_id`.

        This is the operation that can actually introduce a cycle, so it is
        where the check belongs. The edge is rolled back if it closes a loop.
        """
        task = self.get(task_id)
        self.get(dependency_id)
        if dependency_id not in task.depends_on:
            task.depends_on.append(dependency_id)
        try:
            self.topological_order()
        except CyclicDependencyError:
            task.depends_on.remove(dependency_id)
            raise
        return task

    def create(
        self,
        description: str,
        depends_on: Iterable[str] = (),
    ) -> Task:
        """Add a task by description, wiring its dependencies."""
        return self.add(Task(description=description, depends_on=list(depends_on)))

    def get(self, task_id: str) -> Task:
        try:
            return self._tasks[task_id]
        except KeyError:
            raise UnknownTaskError(f"no such task: {task_id}") from None

    def all_tasks(self) -> list[Task]:
        return list(self._tasks.values())

    def ready(self) -> list[Task]:
        """Pending tasks whose dependencies are all done, in insertion order.

        Insertion order rather than topological order, because a plan is
        written in the order its author intended and reordering it would
        silently change what gets attempted first.
        """
        ready: list[Task] = []
        for task in self._tasks.values():
            if task.status is not TaskStatus.PENDING:
                continue
            if all(
                self._tasks[d].status is TaskStatus.DONE
                for d in task.depends_on
                if d in self._tasks
            ):
                ready.append(task)
        return ready

    def dependents(self, task_id: str) -> list[Task]:
        """Tasks that list `task_id` as a dependency."""
        self.get(task_id)
        return [t for t in self._tasks.values() if task_id in t.depends_on]

    def topological_order(self) -> list[Task]:
        """A full ordering, or raise if the graph is cyclic.

        Iterative rather than recursive: a chain of a few thousand steps is a
        legitimate plan, and the recursive form would raise `RecursionError`
        on it - which is a much worse failure than a clean cycle report,
        because it looks like a bug rather than a rejected input.

        This is the primary cycle guard. `add` and `link` both call it, so an
        invalid graph cannot be constructed in the first place.
        """
        order: list[Task] = []
        seen: set[str] = set()
        temporary: set[str] = set()

        for root in self._tasks:
            if root in seen:
                continue
            # Each stack frame is (task_id, index of the next dependency to
            # visit), which is what turns the recursion into a loop.
            stack: list[list[Any]] = [[root, 0]]
            temporary.add(root)
            while stack:
                frame = stack[-1]
                task_id, index = frame[0], frame[1]
                dependencies = [
                    d for d in self._tasks[task_id].depends_on if d in self._tasks
                ]
                if index < len(dependencies):
                    frame[1] = index + 1
                    dependency = dependencies[index]
                    if dependency in seen:
                        continue
                    if dependency in temporary:
                        raise CyclicDependencyError(
                            f"cycle detected at task {dependency}"
                        )
                    temporary.add(dependency)
                    stack.append([dependency, 0])
                    continue
                stack.pop()
                temporary.discard(task_id)
                seen.add(task_id)
                order.append(self._tasks[task_id])
        return order

    def complete(self, task_id: str, result: str = "") -> Task:
        """Mark a task done, and unblock anything that was waiting on it."""
        task = self.get(task_id)
        task.transition(TaskStatus.DONE)
        task.result = result
        for dependent in self.dependents(task_id):
            if dependent.status is TaskStatus.BLOCKED:
                dependent.transition(TaskStatus.READY)
        return task

    def fail(self, task_id: str, error: str = "") -> Task:
        task = self.get(task_id)
        task.transition(TaskStatus.FAILED)
        task.error = error
        return task

    def skip(self, task_id: str) -> Task:
        task = self.get(task_id)
        task.transition(TaskStatus.SKIPPED)
        return task

    @property
    def complete_count(self) -> int:
        return sum(1 for t in self._tasks.values() if t.status is TaskStatus.DONE)

    @property
    def is_finished(self) -> bool:
        return bool(self._tasks) and all(t.is_terminal for t in self._tasks.values())

    @property
    def blocked(self) -> list[Task]:
        return [t for t in self._tasks.values() if t.status is TaskStatus.BLOCKED]

    def to_dict(self) -> dict[str, object]:
        return {
            "tasks": [t.to_dict() for t in self._tasks.values()],
            "complete": self.complete_count,
            "total": len(self._tasks),
        }
