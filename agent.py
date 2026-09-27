"""AEGISAI agent: one command that runs the whole stack.

This is the composition root. It wires the five subsystems into a single
runnable agent:

    treasury    money, with the 80% reserve unreachable from the spend path
    memory      BM25 retrieval over a local index
    web         live search and fetch, metered and robots-compliant
    revenue     evidence-gated opportunity evaluation and a track record
    harvest     the background harvester that fills the index

Nothing existing was modified to build this. It is a new file.

    python agent.py status
    python agent.py run --goal "research the Python packaging market"
    python agent.py cycle --rounds 3
    python agent.py doctor

Degradation is deliberate and explicit. Only the language model needs Ollama,
so everything else -- harvesting, retrieval, pricing, budget enforcement --
works with no model at all. When a model is missing, the agent says so and
tells you the one command that fixes it, instead of failing obscurely.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from agents.loop import AgentLoop
from core.errors import AegisError
from escalation import OPEN, Kind, Severity, notify_new
from harvest.frontier import Frontier
from harvest.worker import HarvestConfig, HarvestWorker
from inference.chat import OllamaChat
from inference.options import ModelOptions
from memory.ingest import WebIngestor
from memory.retriever import Retriever
from memory.store import MemoryIndex
from revenue.evaluate import EvaluationPolicy, OpportunityEvaluator
from revenue.opportunity import CATEGORIES, build_opportunity
from revenue.trackrecord import TrackRecord
from tools.filesystem import build_filesystem_tools
from tools.permissions import Capability, PermissionPolicy
from tools.python_exec import build_exec_tools
from tools.registry import ToolRegistry
from tools.web import build_web_tools
from treasury.money import Money
from treasury.treasury import Treasury

DEFAULT_MODEL = "qwen2.5-coder:3b"
DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_WORKSPACE = Path("workspace")
DEFAULT_INDEX = Path("data/memory.db")
DEFAULT_TREASURY = Path("data/treasury.db")
DEFAULT_REVENUE = Path("data/revenue.db")
DEFAULT_FRONTIER = Path("data/harvest.db")
DEFAULT_RUN_LOG = Path("data/agent_runs.jsonl")

SYSTEM = """You are AEGIS-X, an autonomous research agent with money and web access.

You operate under a hard budget. 20% of your income is spendable and 80% is
locked in a reserve you cannot touch. Spending is metered, so a request you
cannot afford will fail rather than go through.

Operating rules:
1. Search before you assert. Use web_search to find current information and
   web_fetch to read a specific page. Never state a price, a market fact, or a
   competitor's number from memory.
2. Use the local knowledge base when it has relevant material; it was
   harvested from real sources and is cheaper than fetching.
3. Every claim you make about the market must trace to a URL you actually
   retrieved. An uncited number is a guess.
4. Compute before you commit. Check the arithmetic on margin and time cost.
5. When you have enough to answer, stop calling tools and give a direct
   answer. Your final message is the deliverable.

Available tools are listed in the request. Call one when you need facts you do
not have."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def usd(value: str) -> Money:
    return Money.parse(value, "USD")


@dataclass(slots=True)
class AgentState:
    """Everything the agent owns, in one place."""

    treasury: Treasury
    index: MemoryIndex
    revenue: TrackRecord
    frontier: Frontier
    registry: ToolRegistry
    policy: PermissionPolicy
    model_available: bool = False
    model_detail: str = ""
    client: OllamaChat | None = None
    notes: list[str] = field(default_factory=list)

    def close(self) -> None:
        for resource in (self.frontier, self.index, self.revenue, self.treasury):
            try:
                resource.close()
            except Exception:  # noqa: BLE001, S110 - shutdown must not raise
                # Closing must never raise; a failure here would mask the
                # real error that caused the shutdown.
                pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# --------------------------------------------------------------------------
# composition
# --------------------------------------------------------------------------


def check_model(model: str, base_url: str, timeout: float = 5.0) -> tuple[bool, str]:
    """Is there a usable model? Never raises.

    Free-forever means local. Ollama is the only inference that costs nothing
    and has no expiry or quota, so this never falls back to a paid or trial
    endpoint. What it does do is accept any model you have actually pulled:
    insisting on one exact name means a 1.5B model on slow hardware leaves the
    agent offline for no reason, when a 1.5B model runs perfectly well.
    """
    try:
        client = OllamaChat(model=model, base_url=base_url, timeout=timeout)
        health = client.health()
    except AegisError as exc:
        return False, f"Ollama unreachable at {base_url}: {exc}"
    except Exception as exc:  # noqa: BLE001 - health must never raise
        return False, f"unable to query Ollama: {exc}"
    if health.get("model_present"):
        return True, f"{model} is available"
    available = [
        name for name in (health.get("available_models") or []) if isinstance(name, str)
    ]
    if available:
        # Prefer the requested model if a tag variant of it is installed, then
        # fall back to whatever exists. Ollama tags like `qwen2.5-coder:7b`
        # and `qwen2.5-coder:7b-instruct-q4_0` are the same family.
        family = model.split(":")[0]
        related = [name for name in available if name.split(":")[0] == family]
        if related:
            return True, f"{model} is not pulled, but {related[0]} is; using it"
        return True, (
            f"{model} is not pulled. Using {available[0]} instead. "
            f"Installed: {', '.join(available[:6])}"
        )
    return (
        False,
        f"Ollama is running at {base_url} but has no models. Run: ollama pull {model}",
    )


def pick_model(model: str, base_url: str, timeout: float = 5.0) -> str:
    """The model name to actually use, which may not be the preferred one."""
    try:
        health = OllamaChat(model=model, base_url=base_url, timeout=timeout).health()
    except Exception:  # noqa: BLE001 - the caller already reports unavailability
        return model
    if health.get("model_present"):
        return model
    available = [
        name for name in (health.get("available_models") or []) if isinstance(name, str)
    ]
    if not available:
        return model
    family = model.split(":")[0]
    related = [name for name in available if name.split(":")[0] == family]
    return (related or available)[0]
    return (
        False,
        (
            f"Ollama is running at {base_url} but has no models. "
            f"Run: ollama pull {model}"
        ),
    )


def build_state(
    *,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    treasury_path: Path = DEFAULT_TREASURY,
    index_path: Path = DEFAULT_INDEX,
    revenue_path: Path = DEFAULT_REVENUE,
    frontier_path: Path = DEFAULT_FRONTIER,
    workspace: Path = DEFAULT_WORKSPACE,
    cost_per_request: float = 0.02,
    delay: float = 1.0,
    allow_network: bool = True,
    allow_execution: bool = True,
    confine_workspace: bool = False,
) -> AgentState:
    """Assemble every subsystem and register the tools.

    The agent runs unrestricted by default: it may read and write any path the
    current user can, run any program, and reach the network. Pass
    `confine_workspace=True` to confine the filesystem tools to `workspace`.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    treasury = Treasury(treasury_path)
    index = MemoryIndex(index_path)
    revenue = TrackRecord(revenue_path)
    frontier = Frontier(frontier_path)

    capabilities: list[Capability] = [Capability.READ_ONLY]
    if allow_network:
        capabilities.append(Capability.NETWORK)
    if allow_execution:
        capabilities.extend(
            [
                Capability.FILESYSTEM_READ,
                Capability.FILESYSTEM_WRITE,
                Capability.SANDBOXED_EXECUTION,
            ]
        )
    policy = PermissionPolicy(capabilities)

    registry = ToolRegistry()
    registry.permission_policy = policy
    for tool in build_filesystem_tools(workspace if confine_workspace else None):
        registry.register(tool)
    if allow_execution:
        for tool in build_exec_tools(workspace):
            registry.register(tool)
    if allow_network:
        for tool in build_web_tools(
            treasury=treasury,
            index=index,
            delay=delay,
            cost_per_request=cost_per_request,
        ):
            registry.register(tool)

    available, detail = check_model(model, base_url)
    state = AgentState(
        treasury=treasury,
        index=index,
        revenue=revenue,
        frontier=frontier,
        registry=registry,
        policy=policy,
        model_available=available,
        model_detail=detail,
    )
    if available:
        # Use whichever model is actually installed, not necessarily the
        # preferred name, so a smaller local model still works.
        state.client = OllamaChat(
            model=pick_model(model, base_url),
            base_url=base_url,
            options=ModelOptions(),
        )
    return state


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def status_report(state: AgentState) -> dict[str, Any]:
    currency = state.treasury.policy.currency
    stats = state.index.stats()
    return {
        "at": _now(),
        "model": {
            "available": state.model_available,
            "detail": state.model_detail,
        },
        "money": {
            "spendable": state.treasury.ledger.balance(
                state.treasury.policy.operate.account_id
            ).to_dict(),
            "reserve": state.treasury.ledger.balance(
                state.treasury.policy.reserve.account_id
            ).to_dict(),
            "reserve_locked": state.treasury.policy.is_locked("RESERVE"),
            "ledger_balanced": state.treasury.ledger.net_position(currency).is_zero,
        },
        "knowledge": stats.to_dict(),
        "opportunities": {
            "allowed_categories": list(state.revenue.allowed_categories()),
            "blocked": state.revenue.blocked_categories(),
        },
        "frontier": state.frontier.counts(),
        "tools": sorted(tool.name for tool in state.registry.list_tools()),
    }


def append_run_log(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def print_report(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, default=str))


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def command_status(args: argparse.Namespace) -> int:
    with build_state(**state_args(args)) as state:
        print_report(status_report(state))
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    """Check every dependency and say exactly what is missing."""
    checks: list[tuple[str, bool, str]] = []
    available, detail = check_model(args.model, args.base_url)
    checks.append(("language model", available, detail))

    try:
        import torch

        cuda = bool(torch.cuda.is_available())
        detail = (
            f"torch {torch.__version__}, cuda {cuda}"
            if cuda
            else f"torch {torch.__version__}, CPU only. Fine-tuning is not "
            "possible on this machine; retrieval and tools are unaffected."
        )
        checks.append(("pytorch", True, detail))
    except ImportError:
        checks.append(("pytorch", False, "not installed; not required"))

    for label, path in (
        ("index", args.index),
        ("treasury", args.treasury),
        ("revenue", args.revenue),
        ("frontier", args.frontier),
    ):
        exists = path.exists()
        size = f"{path.stat().st_size} bytes" if exists else "not created yet"
        checks.append((f"{label} database", True, f"{path} ({size})"))

    for label, env_var in (
        ("brave search", "BRAVE_SEARCH_API_KEY"),
        ("tavily search", "TAVILY_API_KEY"),
        ("serper search", "SERPER_API_KEY"),
    ):
        import os

        set_now = bool(os.environ.get(env_var))
        checks.append(
            (
                label,
                True,
                "configured"
                if set_now
                else f"not set ({env_var}); wikipedia is the default",
            )
        )

    width = max(len(label) for label, _, _ in checks)
    print("AEGISAI doctor\n")
    for label, ok, detail in checks:
        mark = "OK  " if ok else "MISS"
        print(f"  [{mark}] {label.ljust(width)}  {detail}")
    blocking = [label for label, ok, _ in checks if not ok]
    print()
    if blocking:
        print(f"Blocking: {', '.join(blocking)}")
        print("The non-model subsystems still work. Run `python agent.py status`.")
    else:
        print("All checks passed.")
    return 1 if blocking else 0


def command_run(args: argparse.Namespace) -> int:
    """One goal, full stack, logged."""
    with build_state(**state_args(args)) as state:
        report = status_report(state)
        print(f"model    : {report['model']['detail']}")
        print(
            f"spendable: {report['money']['spendable']['amount']} "
            f"{state.treasury.policy.currency}"
        )
        print(f"reserve  : {report['money']['reserve']['amount']} (locked)")
        print(f"tools    : {', '.join(report['tools'])}")
        print()

        if not state.model_available:
            print("Cannot run the reasoning loop: no model.")
            print(f"  {state.model_detail}")
            print()
            print("Everything that does not need a model still works:")
            print("  python agent.py harvest      # fill the knowledge base")
            print("  python agent.py evaluate     # price opportunities from evidence")
            print("  python agent.py status")
            return 2

        retriever = Retriever(state.index)
        prompt = system_prompt(args)
        context = retriever.retrieve(args.goal, limit=4)
        sections: list[str] = []
        if not context.is_empty:
            sections.append(
                "From the local knowledge base, retrieved from real "
                f"sources. Use only if relevant:\n\n{context.text}"
            )
            print(f"injected  : {len(context.hits)} chunk(s) from local memory")
        volatile = volatile_context(args)
        if volatile:
            sections.append(volatile)

        # model_available is the guard, but mypy cannot narrow the Optional
        # from it, so bind it to a local and assert rather than cast.
        client = state.client
        if client is None:  # pragma: no cover - guarded above
            print("model became unavailable between checks")
            return 2
        loop = AgentLoop(
            client,
            state.registry,
            system=prompt,
            context="\n\n".join(sections),
            max_steps=args.max_steps,
        )
        started = time.perf_counter()
        result = loop.run(args.goal)
        elapsed = round(time.perf_counter() - started, 2)

        print()
        for step in result.steps:
            names = [call["name"] for call in step.tool_calls]
            print(
                f"  step {step.index}: {step.continuation:12} "
                f"tools={names or '-'} obs={step.observation_chars}"
            )
        print()
        print(result.answer)
        print()
        print(f"run {result.run_id} in {elapsed}s, {len(result.steps)} steps")

        payload = {
            "at": _now(),
            "run_id": result.run_id,
            "goal": args.goal,
            "answer": result.answer,
            "steps": [
                {
                    "index": step.index,
                    "continuation": step.continuation,
                    "tools": [call["name"] for call in step.tool_calls],
                    "observation_chars": step.observation_chars,
                    "latency_ms": step.latency_ms,
                }
                for step in result.steps
            ],
            "elapsed_seconds": elapsed,
            "spendable_after": state.treasury.ledger.balance(
                state.treasury.policy.operate.account_id
            ).to_dict(),
        }
        append_run_log(args.run_log, payload)
        print(f"logged to {args.run_log}")
        try:
            from training.feedback import FeedbackStore

            with FeedbackStore(args.feedback) as store:
                saved = store.record(
                    args.goal,
                    result.answer,
                    run_id=result.run_id,
                    steps=len(result.steps),
                    tools=[
                        name
                        for step in result.steps
                        for name in (call["name"] for call in step.tool_calls)
                    ],
                )
                print(
                    f"feedback: {saved.effective_score:.2f} ({saved.verdict}). "
                    f"Rate it: python agent.py review {saved.run_id} --score 0-5"
                )
        except Exception:  # noqa: BLE001, S110 - feedback must not break a run
            # A scoring failure must not cost the user the run they just
            # waited several minutes for. The answer is already logged.
            pass
        return 0


def command_evaluate(args: argparse.Namespace) -> int:
    """Price an opportunity against real retrieved evidence. No model needed."""
    with build_state(**state_args(args)) as state:
        retriever = Retriever(state.index)
        spendable = state.treasury.ledger.balance(
            state.treasury.policy.operate.account_id
        )
        print(
            f"spendable: {spendable}   reserve: "
            f"{state.treasury.ledger.balance(state.treasury.policy.reserve.account_id)}"
        )
        print(f"evidence : {state.index.stats().to_dict()}")
        print()
        evaluator = OpportunityEvaluator(
            policy=EvaluationPolicy(min_evidence=args.min_evidence),
            treasury=state.treasury,
            track_record=state.revenue,
        )
        for spec in _opportunities(args):
            category, ask, cost, hours = _parse_opportunity(spec)
            candidate = build_opportunity(
                category,
                spec,
                ask_price=usd(ask),
                expected_cost=usd(cost),
                effort_hours=hours,
                query=args.query or spec,
                retriever=retriever,
            )
            verdict = evaluator.evaluate(candidate)
            status = "APPROVE" if verdict.approved else "REFUSE"
            print(f"[{status}] {category}: {spec[:60]}")
            print(
                f"         net {verdict.opportunity.net}  "
                f"margin {format(verdict.opportunity.margin_ratio, 'f')}  "
                f"{verdict.opportunity.effective_hourly}/hr"
            )
            print(f"         evidence: {len(candidate.evidence)} source(s)")
            print(f"         {verdict.reason[:100]}")
            print()
    return 0


DEFAULT_OPPORTUNITIES = (
    "data_product:200.00:12.50:6",
    "service:500.00:5.00:3",
)


def _opportunities(args: argparse.Namespace) -> list[str]:
    """User-supplied opportunities, or the defaults when none were given.

    `action="append"` combines with a set default by *extending* the list, so
    passing one opportunity would silently still evaluate the two defaults.
    """
    supplied = getattr(args, "opportunity", None)
    return list(supplied) if supplied else list(DEFAULT_OPPORTUNITIES)


def _parse_opportunity(spec: str) -> tuple[str, str, str, float]:
    """`category:ask:cost:hours` with sensible defaults."""
    parts = spec.split(":")
    category = parts[0].strip() if parts else "service"
    if category not in CATEGORIES:
        category = "service"
    ask = parts[1].strip() if len(parts) > 1 else "200.00"
    cost = parts[2].strip() if len(parts) > 2 else "10.00"
    try:
        hours = float(parts[3]) if len(parts) > 3 else 4.0
    except ValueError:
        hours = 4.0
    return category, ask, cost, max(0.1, hours)


def command_harvest(args: argparse.Namespace) -> int:
    with build_state(**state_args(args)) as state:
        config = HarvestConfig(
            sources=tuple(args.source) if args.source else HarvestConfig().sources,
            deadline_hours=args.deadline_hours,
            max_pages=args.max_pages,
            max_depth=args.max_depth,
            delay_seconds=args.delay,
            cost_per_fetch=args.cost_per_request,
            index_path=args.index,
            frontier_path=args.frontier,
            status_path=Path("data/harvest_status.json"),
        )
        print(
            f"harvesting for up to {config.deadline_hours}h, "
            f"max {config.max_pages} pages, delay {config.delay_seconds}s"
        )
        with HarvestWorker(
            config,
            treasury=state.treasury,
            ingestor_factory=lambda: _metered_ingestor(state, config),
        ) as worker:
            stats = worker.run()
        print(json.dumps(stats.to_dict(), indent=2))
    return 0


def _metered_ingestor(state: AgentState, config: HarvestConfig) -> WebIngestor:
    # WebIngestor takes no chunk sizing; that is applied by MemoryIndex when
    # add_document is called, so the harvest config carries it instead.
    ingestor = WebIngestor(
        state.index,
        treasury=state.treasury,
        delay=config.delay_seconds,
    )
    ingestor.cost_per_fetch = config.cost_per_fetch
    return ingestor


def command_cycle(args: argparse.Namespace) -> int:
    """The autonomous loop: research, price, commit, record, learn.

    Deliberately model-free. The learning signal here is measured revenue,
    not model output, which is why this part can run without Ollama at all.
    """
    with build_state(**state_args(args)) as state:
        retriever = Retriever(state.index)
        evaluator = OpportunityEvaluator(
            treasury=state.treasury, track_record=state.revenue
        )
        blocked = state.revenue.blocked_categories()
        if blocked:
            print("blocked categories (measured as not paying):")
            for category, reason in blocked.items():
                print(f"  {category}: {reason}")
            print()

        cycles: list[dict[str, Any]] = []
        killed_now: set[str] = set(blocked)
        for round_number in range(1, args.rounds + 1):
            print(f"--- round {round_number}/{args.rounds} ---")
            approved = 0
            specs = _opportunities(args)
            for category in state.revenue.allowed_categories():
                for spec in specs:
                    parsed_category, ask, cost, hours = _parse_opportunity(spec)
                    if parsed_category != category:
                        continue
                    candidate = build_opportunity(
                        category,
                        spec,
                        ask_price=usd(ask),
                        expected_cost=usd(cost),
                        effort_hours=hours,
                        query=args.query or spec,
                        retriever=retriever,
                    )
                    verdict = evaluator.evaluate(candidate)
                    label = "APPROVE" if verdict.approved else "REFUSE"
                    print(f"  [{label}] {category}: {verdict.reason[:88]}")
                    if not verdict.approved:
                        continue
                    approved += 1
                    if args.commit:
                        evaluator.commit(verdict)
                        print(
                            f"         committed {candidate.expected_cost}; "
                            f"operate now "
                            f"{state.treasury.ledger.balance(state.treasury.policy.operate.account_id)}"
                        )
                    if args.record_outcome is not None:
                        state.revenue.record_outcome(
                            candidate.opportunity_id, usd(args.record_outcome)
                        )
                        # The kill rule can fire on this very outcome, so the
                        # verdict above ("APPROVE") and the blocked list at
                        # the end of the run would otherwise contradict each
                        # other with no explanation.
                        fresh = state.revenue.category_stats(category)
                        if fresh.killed and category not in killed_now:
                            killed_now.add(category)
                            print(
                                f"         category '{category}' is now BLOCKED: "
                                f"{fresh.reason}"
                            )
            print(f"  approved {approved} of {len(specs)}")
            cycles.append({"round": round_number, "approved": approved})
            print()

        blocked_now = state.revenue.blocked_categories()
        print("allowed categories:", ", ".join(state.revenue.allowed_categories()))
        if blocked_now:
            print("blocked now      :", ", ".join(blocked_now))
        append_run_log(
            args.run_log,
            {
                "at": _now(),
                "command": "cycle",
                "cycles": cycles,
                "blocked": blocked_now,
            },
        )
    return 0


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------


def command_market(args: argparse.Namespace) -> int:
    """Go and find out whether anyone is actually building this.

    This is the closest thing to "try to make money" that is honest without
    accounts or a payment rail: read real engagement numbers off a public
    marketplace and report whether the demand evidence is there. It will not
    tell you a price, because the platform does not publish one.
    """
    from revenue.market import MarketProbeError, probe_market

    queries = args.query or [
        "python instruct",
        "numpy dataset",
        "scientific python",
        "python code instructions",
    ]
    print(f"probing {len(queries)} quer(y/ies); this can take a minute or two...")
    try:
        report = probe_market(queries, viable_median=args.viable_median, probe=None)
    except MarketProbeError as exc:
        print(f"error: {exc}")
        return 1

    print()
    print(f"demand read : {'VIABLE' if report.viable else 'NO DEMAND EVIDENCE'}")
    print(f"competition : {'CROWDED' if report.crowded else 'OPEN'}")
    print(f"downloads observed: {report.total_downloads:,}")
    print()
    for signal in report.signals:
        best = signal.top
        print(f"  {signal.query}")
        print(
            f"    {signal.count} listing(s), "
            f"{signal.total_downloads:,} downloads, "
            f"median {format(signal.median_downloads, 'f')}"
        )
        if best is not None:
            print(f"    best: {best.identifier} ({best.downloads:,} downloads)")
    print()
    for reason in report.reasons:
        print(f"  - {reason}")
    print()
    print("what this does NOT tell you:")
    for caveat in report.caveats:
        print(f"  - {caveat}")

    payload = report.to_dict()
    payload["probed_queries"] = list(queries)
    append_run_log(args.run_log, payload)
    print(f"\nlogged to {args.run_log}")

    if not report.viable:
        print(
            "\nNo demand evidence. Do not build or promote anything for this "
            "domain; the correct action is to probe a different one."
        )
        return 2
    print(
        "\nDemand evidence exists. Pricing and distribution are still yours: "
        "nothing in this system can quote a price or reach a buyer."
    )
    return 0


def volatile_context(args: argparse.Namespace) -> str:
    """What the agent has learned about itself, which changes every run.

    Deliberately separate from the system prompt. This content is rewritten
    whenever a run is scored or the tuner learns, so folding it into the system
    turn would change the request prefix on every invocation and defeat
    Ollama's prompt cache - which measured 148x on this workload. The system
    turn stays byte-identical run to run; this rides in the user turn, after
    the cached tool block.

    Three things get folded in, each from a durable store rather than from
    anything it just said:

    * **retrieved knowledge** from the local index, for the goal at hand
    * **lessons** from the track record, about which categories pay
    * **exemplars and warnings** from scored past runs, about how it answers

    Nothing else. It does not get to write its own instructions.
    """
    sections: list[str] = []

    if not args.no_lessons:
        try:
            from revenue.selftune import SelfTuner

            with SelfTuner(args.selftune) as tuner:
                tuner.learn_from_outcomes()
                block = tuner.lesson_block()
        except Exception:  # noqa: BLE001 - a missing store must not block a run
            block = ""
        if block:
            sections.append(
                "MEASURED HISTORY (do not contradict this):\n"
                f"{block}\n\n"
                "If a suggestion below contradicts a measured result, say so "
                "plainly and follow the measurement."
            )

    if not args.no_feedback:
        try:
            from training.feedback import FeedbackStore

            with FeedbackStore(args.feedback) as store:
                block = store.prompt_block()
        except Exception:  # noqa: BLE001 - a missing store must not block a run
            block = ""
        if block:
            sections.append(f"PAST RUNS (your own scored history):\n{block}")

    return "\n\n".join(sections)


def system_prompt(args: argparse.Namespace) -> str:
    """The agent's standing instructions.

    Static by design: this string must be byte-identical between runs so that
    Ollama can cache it, together with the tool schemas, as the request prefix.
    Run-specific material belongs in `volatile_context`.
    """
    return SYSTEM


def command_review(args: argparse.Namespace) -> int:
    """Rate a past run, which is what makes the loop real.

    The heuristic is a proxy. A person saying "that was wrong" is the signal
    that matters, and it overrides the heuristic permanently.
    """
    from training.feedback import FeedbackError, FeedbackStore

    with FeedbackStore(args.feedback) as store:
        if args.import_log:
            count = store.import_runs(args.run_log)
            print(f"imported {count} run(s) from {args.run_log}")
            return 0

        if args.run_id is None:
            recent = store.recent(limit=10)
            if not recent:
                print("no runs captured yet. Run something first:")
                print('  python agent.py run --goal "..."')
                return 0
            print("recent runs, worst first. Rate with --run-id and --score\n")
            for record in sorted(recent, key=lambda item: item.effective_score):
                human = (
                    f"you: {record.human_score}/5"
                    if record.human_score is not None
                    else "unrated"
                )
                print(
                    f"  {record.run_id}  auto={record.auto_score:.2f}  "
                    f"{human:12} {record.verdict:8} {record.goal[:52]}"
                )
            print(
                f"\n  python agent.py review {recent[0].run_id} --score 4 --note '...'"
            )
            return 0

        if not 0 <= args.score <= 5:
            print("--score must be 0 to 5")
            return 1
        try:
            updated = store.score(args.run_id, args.score, critique=args.note or "")
        except FeedbackError as exc:
            print(f"error: {exc}")
            return 1
        if updated is None:
            print(f"no run with id {args.run_id!r}")
            return 1
        print(
            f"{updated.run_id} rated {args.score}/5 -> "
            f"{updated.verdict} (effective {updated.effective_score:.2f})"
        )
        block = store.prompt_block()
        if block:
            print()
            print("now in its system prompt:")
            for line in block.splitlines()[:8]:
                print(f"  {line}")
        return 0


def command_autonomous(args: argparse.Namespace) -> int:
    """Run unattended until the deadline, with no further input.

    This is the "does it need me all the time" answer: you start it once, and
    it runs the model-free pipeline on repeat without being asked again.

    What it does each round:
        1. reads its own state from disk, so a crash resumes rather than
           restarts
        2. tops up the knowledge base if the index is staler than asked
        3. probes live markets for real demand evidence
        4. evaluates opportunities against retrieved evidence and its budget
        5. records measured outcomes, which is what moves the track record
        6. writes an atomic status file and appends to the run log

    What it deliberately never does, regardless of flags:
        * spend the locked reserve
        * commit an opportunity without retrieved evidence
        * report its own work as revenue
        * run unbounded: every loop answers to a wall-clock deadline
        * run past `max_rounds` even if time remains

    With no model available it still does everything except `run`, because
    harvesting, pricing, budget enforcement and the learning loop are all
    model-free by design.
    """
    started = time.perf_counter()
    deadline = started + args.hours * 3600.0
    tasks = set(args.task or ["harvest", "market", "evaluate", "record"])
    rounds_done = 0
    summary: dict[str, Any] = {
        "started_at": _now(),
        "hours_budgeted": args.hours,
        "tasks": sorted(tasks),
        # Must be recorded here or the status writer falls back to a path the
        # caller never asked for, and `--status-path` is silently ignored.
        "status_path": str(args.status_path),
        "rounds": [],
    }

    print(f"autonomous run: {args.hours}h budget, tasks={','.join(sorted(tasks))}")
    print("  stop early with Ctrl-C; progress resumes on the next start")
    print()

    with build_state(**state_args(args)) as state:
        summary["model"] = state.model_available
        if not state.model_available:
            print(f"  note: {state.model_detail}")
            print("  continuing with the model-free pipeline only.")
            print()

        while True:
            if time.perf_counter() >= deadline:
                summary["stopped_reason"] = "deadline"
                break
            if rounds_done >= args.max_rounds:
                summary["stopped_reason"] = "max_rounds"
                break
            rounds_done += 1
            print(f"--- round {rounds_done} ---")
            entry: dict[str, Any] = {"round": rounds_done}
            try:
                if "harvest" in tasks:
                    entry["harvest"] = _auto_harvest(state, args)
                if "market" in tasks:
                    entry["market"] = _auto_market(args)
                if "evaluate" in tasks:
                    entry["evaluate"] = _auto_evaluate(state, args)
                if "record" in tasks:
                    entry["record"] = _auto_record(state, args)
                entry["status"] = "ok"
                # Detection runs every round, not once at startup, because a
                # blocker can appear mid-run: the spendable envelope drains,
                # a category gets killed, the budget runs out. Filing only at
                # startup would miss exactly the failures that happen at 4am.
                entry["blockers"] = _file_blockers(state, args)
            except KeyboardInterrupt:
                entry["status"] = "interrupted"
                summary["stopped_reason"] = "interrupt"
                summary["rounds"].append(entry)
                print("\nstopping at your request")
                break
            except Exception as exc:  # noqa: BLE001 - an unattended run must not die
                # A single bad round must not end an eight-hour job. Record it,
                # count it, and keep going. The traceback goes in the detail
                # because an error you cannot locate is barely better than a
                # crash at 4am.
                import traceback

                where = traceback.extract_tb(exc.__traceback__)
                origin = (
                    f"{where[-1].filename}:{where[-1].lineno}" if where else "unknown"
                )
                entry["status"] = "error"
                entry["error"] = f"{type(exc).__name__}: {exc}"
                entry["error_origin"] = origin
                print(f"  round error at {origin}: {entry['error']}")
                summary.setdefault("errors", 0)
                summary["errors"] += 1
            summary["rounds"].append(entry)
            print(
                f"  operate={state.treasury.ledger.balance(state.treasury.policy.operate.account_id)} "
                f"reserve={state.treasury.ledger.balance(state.treasury.policy.reserve.account_id)} "
                f"index={state.index.stats().documents} docs"
            )
            print()
            _write_autonomous_status(state, summary)
            if (
                entry.get("status") == "error"
                and summary.get("errors", 0) >= args.max_errors
            ):
                summary["stopped_reason"] = "too_many_errors"
                print(f"stopping: {summary['errors']} consecutive round errors")
                break
            if args.sleep and time.perf_counter() < deadline:
                time.sleep(args.sleep)

        summary.setdefault("stopped_reason", "deadline")
        summary["elapsed_seconds"] = round(time.perf_counter() - started, 1)
        summary["rounds_completed"] = rounds_done
        summary["final"] = status_report(state)
        _write_autonomous_status(state, summary)
        append_run_log(
            args.run_log,
            {
                "at": _now(),
                "command": "autonomous",
                **{
                    key: summary[key]
                    for key in (
                        "rounds_completed",
                        "stopped_reason",
                        "elapsed_seconds",
                        "tasks",
                    )
                },
            },
        )
        print(
            f"stopped: {summary['stopped_reason']} after {rounds_done} round(s) "
            f"in {summary['elapsed_seconds']}s"
        )
        print(f"status written to {args.status_path}")
    return 0


def _file_blockers(state: AgentState, args: argparse.Namespace) -> list[dict[str, Any]]:
    """Detect human-only blockers and file them. Returns what is now open."""
    from escalation import RequestQueue

    found = _blockers(state, args)
    if not found:
        return []
    with RequestQueue(args.escalations) as queue:
        # Snapshot first, so only genuinely new blockers are announced. The
        # previous approach compared created_at against updated_at, which are
        # two separate clock reads and therefore never equal.
        before = {item.request_id for item in queue.open_requests()}
        for request in found:
            filed = queue.file(request)
            if filed.request_id not in before:
                print(f"  blocked: {filed.kind} - {filed.title}")
                print(f"           you: {filed.unblock[:96]}")
        if args.notify != "none":
            notify_new(queue, method=args.notify)
        return [item.to_dict() for item in queue.open_requests()]


def _auto_harvest(state: AgentState, args: argparse.Namespace) -> dict[str, Any]:
    """Top up the index, but only if it is actually stale."""
    stats = state.index.stats()
    if stats.documents >= args.min_documents:
        return {"skipped": True, "reason": f"{stats.documents} docs present"}
    config = HarvestConfig(
        sources=tuple(args.source) if args.source else HarvestConfig().sources,
        deadline_hours=min(args.harvest_minutes / 60.0, args.hours),
        max_pages=args.harvest_pages,
        max_depth=1,
        delay_seconds=args.delay,
        cost_per_fetch=args.cost_per_request,
        index_path=args.index,
        frontier_path=args.frontier,
        status_path=args.status_path,
        settle_seconds=0.0,
    )
    with HarvestWorker(
        config,
        treasury=state.treasury,
        ingestor_factory=lambda: _metered_ingestor(state, config),
    ) as worker:
        result = worker.run()
    return {
        "documents_after": state.index.stats().documents,
        "indexed": result.indexed,
        "spent": result.spent,
    }


def _auto_market(args: argparse.Namespace) -> dict[str, Any]:
    from revenue.market import probe_market

    queries = args.market_query or ["python instruct", "scientific python"]
    report = probe_market(queries, viable_median=args.viable_median)
    print(
        f"  market: {report.total_downloads:,} downloads, "
        f"{'VIABLE' if report.viable else 'no demand'}, "
        f"{'crowded' if report.crowded else 'open'}"
    )
    return {
        "viable": report.viable,
        "crowded": report.crowded,
        "total_downloads": report.total_downloads,
        "queries": list(queries),
    }


def _auto_evaluate(state: AgentState, args: argparse.Namespace) -> dict[str, Any]:
    """Price the standing opportunity set against current evidence."""
    retriever = Retriever(state.index)
    evaluator = OpportunityEvaluator(
        treasury=state.treasury, track_record=state.revenue
    )
    approved = 0
    considered = 0
    for spec in _opportunities(args):
        category, ask, cost, hours = _parse_opportunity(spec)
        candidate = build_opportunity(
            category,
            spec,
            ask_price=usd(ask),
            expected_cost=usd(cost),
            effort_hours=hours,
            query=args.query or spec,
            retriever=retriever,
        )
        verdict = evaluator.evaluate(candidate)
        considered += 1
        if verdict.approved:
            approved += 1
    print(f"  evaluate: {approved}/{considered} approved")
    return {"considered": considered, "approved": approved}


def _auto_record(state: AgentState, args: argparse.Namespace) -> dict[str, Any]:
    """Record a measured outcome, which is the only thing that teaches."""
    if args.realized is None:
        blocked = state.revenue.blocked_categories()
        print(
            f"  record: no --realized given, so nothing recorded. "
            f"blocked categories: {len(blocked)}"
        )
        return {"recorded": 0, "blocked": blocked}
    latest = state.revenue.history(limit=1)
    if not latest:
        return {"recorded": 0, "reason": "nothing to record against"}
    opportunity_id = latest[0]["opportunity_id"]
    already = latest[0].get("realized")
    state.revenue.record_outcome(opportunity_id, usd(args.realized))
    stats = state.revenue.category_stats(
        state.revenue.get_opportunity(opportunity_id).category
    )
    print(
        f"  record: {args.realized} against {latest[0]['category']}; "
        f"hit rate {format(stats.hit_rate, 'f')} over {stats.attempts} attempt(s)"
    )
    return {
        "recorded": 1,
        "opportunity_id": opportunity_id,
        "previously_recorded": already,
        "hit_rate": format(stats.hit_rate, "f"),
        "killed": stats.killed,
    }


def _write_autonomous_status(state: AgentState, summary: dict[str, Any]) -> None:
    """Atomic status write, same reason as the harvest worker."""
    path = Path(summary.get("status_path", "data/autonomous_status.json"))
    payload = {"at": _now(), **summary}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8"
        )
        temporary.replace(path)
    except OSError:
        pass


def _blockers(state: AgentState, args: argparse.Namespace) -> list[Any]:
    """Work out what is currently stopping the agent, and why.

    Everything here is something the agent provably cannot do on its own. It
    is not a wish list; each item is a step that requires a human identity, a
    credential, a judgement, or a payment rail.
    """
    from escalation import detect_blockers

    package = Path(args.package)
    records = 0
    verified = 0
    published = False
    manifest_path = package / "MANIFEST.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            records = int((manifest.get("curation") or {}).get("records_kept") or 0)
            published = bool(manifest.get("published"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            records = 0
    verify_path = package / "verify.json"
    if verify_path.exists():
        try:
            verified = int(
                (
                    json.loads(verify_path.read_text(encoding="utf-8")).get("buckets")
                    or {}
                ).get("verified")
                or 0
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            verified = 0

    return detect_blockers(
        model_available=state.model_available,
        model_detail=state.model_detail,
        spendable=state.treasury.ledger.balance(
            state.treasury.policy.operate.account_id
        ),
        reserve=state.treasury.ledger.balance(state.treasury.policy.reserve.account_id),
        # Count only unresolved opportunities. Total history would include work
        # already transacted, which is the opposite of what is blocking.
        approved_pending=sum(stats.pending for stats in state.revenue.all_stats()),
        blocked_categories=state.revenue.blocked_categories(),
        published=published,
        package_records=records,
        verified_records=verified,
    )


def command_inbox(args: argparse.Namespace) -> int:
    """Show what the agent is waiting on.

    Detection runs here as well as during an autonomous run, because asking
    "is anything blocked?" should answer the question now rather than report
    only what some earlier run happened to file.
    """
    from escalation import RequestQueue

    with RequestQueue(args.escalations) as queue:
        if not args.no_detect:
            try:
                with build_state(**state_args(args)) as state:
                    for request in _blockers(state, args):
                        queue.file(request)
            except Exception as exc:  # noqa: BLE001 - the inbox must still list
                print(f"note: could not run detection: {exc}\n")

        pending = queue.open_requests()
        print("AEGISAI inbox - what the agent cannot do alone\n")
        if not pending:
            print("  nothing is waiting on you.")
            print(f"  history: {queue.counts() or 'no requests ever filed'}")
            return 0
        for request in pending:
            print(f"[{request.severity}] #{request.request_id} {request.kind}")
            print(f"  {request.title}")
            print(f"  why  : {request.detail}")
            print(f"  you  : {request.unblock}")
            print()
        print(f"{len(pending)} open. Clear one with:")
        print(
            f'  python agent.py resolve {pending[0].request_id} --note "what you did"'
        )
        sent = 0
        if args.notify != "none":
            sent = notify_new(queue, method=args.notify)
            if sent:
                print(f"sent {sent} desktop notification(s)")
        return 0


def command_request(args: argparse.Namespace) -> int:
    """File a blocker by hand."""
    from escalation import Kind, Request, RequestQueue, Severity

    with RequestQueue(args.escalations) as queue:
        filed = queue.file(
            Request(
                kind=Kind(args.kind),
                severity=Severity(args.severity),
                title=args.title,
                detail=args.detail,
                unblock=args.unblock,
            )
        )
        print(f"#{filed.request_id} {filed.kind} [{filed.severity}] {filed.title}")
        print("  check it with: python agent.py inbox")
        return 0


def command_resolve(args: argparse.Namespace) -> int:
    """Mark a blocker handled."""
    from escalation import RequestQueue

    with RequestQueue(args.escalations) as queue:
        existing = queue.get(args.request_id)
        if existing is None:
            print(f"no request with id {args.request_id!r}")
            return 1
        if existing.state != OPEN:
            print(f"#{args.request_id} is already {existing.state}")
            return 1
        updated = queue.resolve(args.request_id, note=args.note)
        print(f"#{args.request_id} answered: {args.note or 'no note'}")
        if updated is not None:
            print(f"  {updated.title}")
        return 0


def command_tune(args: argparse.Namespace) -> int:
    """Self-improvement: re-derive thresholds from measured outcomes.

    Changes numbers, never logic, and never any of the capabilities or budget
    rules. Every change is bounded, logged with before and after, and
    reversible with `--revert`.
    """
    from revenue.selftune import SelfTuner

    if args.revert:
        with SelfTuner(args.selftune) as tuner:
            value = tuner.revert(args.revert)
            print(f"{args.revert} reverted to {value}")
            return 0

    def load(path: Path | None) -> dict[str, Any] | None:
        if path is None or not Path(path).exists():
            return None
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    with SelfTuner(args.selftune, track_record=TrackRecord(args.revenue)) as tuner:
        tuner.learn_from_outcomes()
        proposals = tuner.propose(
            verification=load(args.verify), market=load(args.market)
        )
        print(f"self-improvement pass at {tuner.all_parameters()}")
        print()
        if not proposals:
            print("  nothing to change; current settings match the evidence.")
        for item in proposals:
            arrow = f"{format(item.current, 'f')} -> {format(item.proposed, 'f')}"
            print(f"  {item.name}  {arrow}")
            print(f"    why : {item.reason}")
            print(f"    data: {item.evidence}")
        if proposals and args.apply:
            applied = tuner.apply(proposals)
            print()
            print(f"applied {len(applied)} adjustment(s); all within bounds")
        elif proposals:
            print()
            print("dry run. apply with: python agent.py tune --apply")

        block = tuner.lesson_block()
        if block:
            print()
            print("lessons now in its system prompt:")
            for line in block.splitlines():
                print(f"  {line}")
        return 0


def command_permissions(args: argparse.Namespace) -> int:
    """Grant, revoke, or list durable capabilities.

    This is the answer to "for every permission it asks, I should be able to
    control or change it later". Nothing is granted until you grant it, and
    a revocation takes effect on the very next check.
    """
    from security.grants import Capability, GrantStore

    with GrantStore(args.security) as store:
        if args.command_permissions == "grant":
            capability = Capability(args.capability)
            record = store.grant(
                capability,
                scope=args.scope or "",
                hours=args.hours,
                note=args.note or "",
            )
            if capability.needs_human:
                print(
                    f"note: {capability.value} affects people other than you. "
                    "It is now recorded in the inbox and in the audit log."
                )
            print(
                f"granted {capability.value}"
                + (f" scope={record.scope}" if record.scope else " (unscoped)")
                + (
                    f" expires={record.expires_at}"
                    if record.expires_at
                    else " (no expiry)"
                )
            )
            return 0

        if args.command_permissions == "revoke":
            if args.capability == "ALL":
                count = store.revoke_all()
                print(f"revoked {count} grant(s)")
                return 0
            count = store.revoke(Capability(args.capability), scope=args.scope or "")
            print(
                f"revoked {count} grant(s) for {args.capability}"
                if count
                else f"no active grant for {args.capability}"
            )
            return 0

        report = store.report()
        print("AEGISAI permissions - durable, revocable, audited\n")
        if report["granted"]:
            print("  GRANTED")
            for grant in report["granted"]:
                limit = grant["expires_at"] or "no expiry"
                scope = grant["scope"] or "any scope"
                flag = "  <-- affects others" if grant["needs_human"] else ""
                print(
                    f"    {grant['capability']:16} scope={scope:24} until={limit}{flag}"
                )
            print()
        print("  NOT GRANTED (the agent cannot use these)")
        for capability in report["ungranted"]:
            print(f"    {capability}")
        print()
        print("  grant:  python agent.py permissions grant CAMERA --hours 2")
        print("  revoke: python agent.py permissions revoke CAMERA")
        print("  panic:  python agent.py permissions revoke ALL")
        if report["recent_denials"]:
            print()
            print("  recent refusals (the agent asked and was told no)")
            for denial in report["recent_denials"][:5]:
                print(
                    f"    {denial['capability']:16} {denial['reason']:14} "
                    f"{denial['scope']}"
                )
        return 0


def state_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "model": args.model,
        "base_url": args.base_url,
        "treasury_path": args.treasury,
        "index_path": args.index,
        "revenue_path": args.revenue,
        "frontier_path": args.frontier,
        "workspace": args.workspace,
        "cost_per_request": args.cost_per_request,
        "delay": args.delay,
        "allow_network": not args.no_network,
        "allow_execution": not args.no_execution,
        "confine_workspace": args.confine_workspace,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent.py",
        description=(
            "AEGISAI agent: treasury, retrieval, live web, and revenue "
            "evaluation in one runnable process."
        ),
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--treasury", type=Path, default=DEFAULT_TREASURY)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--revenue", type=Path, default=DEFAULT_REVENUE)
    parser.add_argument("--frontier", type=Path, default=DEFAULT_FRONTIER)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--run-log", type=Path, default=DEFAULT_RUN_LOG)
    parser.add_argument("--cost-per-request", type=float, default=0.02)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--no-network", action="store_true")
    parser.add_argument("--no-execution", action="store_true")
    parser.add_argument(
        "--confine-workspace",
        action="store_true",
        help=(
            "Confine the filesystem tools to --workspace. Off by default: the "
            "agent otherwise reads and writes any path you can."
        ),
    )
    parser.add_argument("--escalations", type=Path, default=Path("data/escalations.db"))
    parser.add_argument("--package", type=Path, default=Path("data/product"))
    parser.add_argument("--security", type=Path, default=Path("data/security.db"))
    parser.add_argument("--selftune", type=Path, default=Path("data/selftune.db"))
    parser.add_argument("--feedback", type=Path, default=Path("data/feedback.db"))
    parser.add_argument(
        "--no-lessons",
        action="store_true",
        help="do not inject measured history into the system prompt",
    )
    parser.add_argument(
        "--no-feedback",
        action="store_true",
        help="do not inject scored past runs into the system prompt",
    )
    parser.add_argument(
        "--notify", choices=["auto", "msg", "powershell", "none"], default="auto"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show money, knowledge, and category health")
    sub.add_parser("doctor", help="check every dependency and say what is missing")
    inbox = sub.add_parser(
        "inbox", help="show what the agent cannot do alone and is waiting on you"
    )
    inbox.add_argument(
        "--no-detect",
        action="store_true",
        help="list filed requests only, without re-running detection",
    )
    # Also accepted after the subcommand. It is a global flag, and
    # `agent.py inbox --notify none` is what anyone will actually type.
    inbox.add_argument(
        "--notify",
        choices=["auto", "msg", "powershell", "none"],
        default=argparse.SUPPRESS,
        help="override the global notification method",
    )

    review = sub.add_parser(
        "review",
        help="rate past runs so the agent learns from them",
    )
    review.add_argument("run_id", nargs="?", default=None)
    review.add_argument("--score", type=int, default=None)
    review.add_argument("--note", default=None)
    review.add_argument("--import-log", action="store_true")

    tune = sub.add_parser(
        "tune",
        help="self-improvement: re-derive thresholds from measured outcomes",
    )
    tune.add_argument("--apply", action="store_true", help="commit the changes")
    tune.add_argument("--verify", type=Path, default=Path("data/product/verify.json"))
    tune.add_argument("--market", type=Path, default=None)
    tune.add_argument(
        "--revert", default=None, help="restore one parameter to its default"
    )
    tune.add_argument(
        "--notify",
        choices=["auto", "msg", "powershell", "none"],
        default=argparse.SUPPRESS,
        help="override the global notification method",
    )

    permissions = sub.add_parser(
        "permissions",
        help="grant, revoke, or list durable capabilities. Nothing is granted by default.",
    )
    permissions.add_argument(
        "action", nargs="?", choices=["list", "grant", "revoke"], default="list"
    )
    # Positional, because `permissions grant CAMERA` is what anyone types.
    # `--capability` also works for muscle memory from other CLIs.
    permissions.add_argument("name", nargs="?", default=None)
    permissions.add_argument("--capability", default=None)
    permissions.add_argument("--scope", default=None)
    permissions.add_argument("--hours", type=float, default=None)
    permissions.add_argument("--note", default=None)

    request = sub.add_parser("request", help="file a blocker by hand")
    request.add_argument("--kind", choices=[k.value for k in Kind], required=True)
    request.add_argument("--title", required=True)
    request.add_argument("--detail", default="")
    request.add_argument("--unblock", required=True)
    request.add_argument(
        "--severity", choices=[s.value for s in Severity], default="blocking"
    )

    resolve = sub.add_parser("resolve", help="mark a blocker handled")
    resolve.add_argument("request_id")
    resolve.add_argument("--note", default="")

    market = sub.add_parser(
        "market",
        help="probe a public marketplace for real demand evidence",
    )
    market.add_argument(
        "--query",
        action="append",
        default=None,
        help="search terms; repeatable. Defaults to four python-related probes",
    )
    market.add_argument("--viable-median", type=int, default=1000)
    run = sub.add_parser("run", help="run one goal through the full stack")
    run.add_argument("--goal", required=True)
    run.add_argument("--max-steps", type=int, default=8)

    evaluate = sub.add_parser(
        "evaluate", help="price opportunities against retrieved evidence"
    )
    evaluate.add_argument(
        "--opportunity",
        action="append",
        default=None,
        help="category:ask:cost:hours, repeatable",
    )
    evaluate.add_argument("--query", default="")
    evaluate.add_argument("--min-evidence", type=int, default=1)

    harvest = sub.add_parser("harvest", help="fill the knowledge base from the web")
    harvest.add_argument("--source", action="append", default=None)
    harvest.add_argument("--deadline-hours", type=float, default=1.0)
    harvest.add_argument("--max-pages", type=int, default=200)
    harvest.add_argument("--max-depth", type=int, default=1)

    cycle = sub.add_parser(
        "cycle", help="the model-free learning loop: research, price, record"
    )
    cycle.add_argument("--rounds", type=int, default=1)
    cycle.add_argument("--query", default="")
    cycle.add_argument(
        "--opportunity",
        action="append",
        default=None,
        help="category:ask:cost:hours, repeatable; defaults to two samples",
    )
    cycle.add_argument("--commit", action="store_true")
    cycle.add_argument(
        "--record-outcome",
        default=None,
        help="record this realized amount for each approved opportunity, e.g. 0.00",
    )
    autonomous = sub.add_parser(
        "autonomous",
        help=(
            "run unattended for a bounded time: harvest, probe markets, "
            "evaluate, and record outcomes without further input"
        ),
    )
    autonomous.add_argument("--hours", type=float, default=6.0)
    autonomous.add_argument("--max-rounds", type=int, default=100)
    autonomous.add_argument(
        "--max-errors",
        type=int,
        default=3,
        help="give up after this many failed rounds rather than spin on a fault",
    )
    autonomous.add_argument(
        "--task",
        action="append",
        default=None,
        choices=["harvest", "market", "evaluate", "record"],
        help="restrict the work done; defaults to all four",
    )
    autonomous.add_argument("--sleep", type=float, default=60.0)
    autonomous.add_argument("--min-documents", type=int, default=20)
    autonomous.add_argument("--harvest-minutes", type=float, default=5.0)
    autonomous.add_argument("--harvest-pages", type=int, default=25)
    autonomous.add_argument("--source", action="append", default=None)
    autonomous.add_argument("--market-query", action="append", default=None)
    autonomous.add_argument("--viable-median", type=int, default=1000)
    autonomous.add_argument("--query", default="")
    autonomous.add_argument(
        "--opportunity",
        action="append",
        default=None,
        help="category:ask:cost:hours, repeatable",
    )
    autonomous.add_argument(
        "--realized",
        default=None,
        help=(
            "amount actually received, recorded against the latest "
            "opportunity. Omit to record nothing, which is the honest default."
        ),
    )
    autonomous.add_argument(
        "--status-path", type=Path, default=Path("data/autonomous_status.json")
    )
    # Also accepted after the subcommand, because they are global flags and
    # putting them here is what anyone will type first.
    autonomous.add_argument("--escalations", type=Path, default=argparse.SUPPRESS)
    autonomous.add_argument(
        "--notify",
        choices=["auto", "msg", "powershell", "none"],
        default=argparse.SUPPRESS,
        help="override the global notification method",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "permissions":
        # Accept the capability positionally or as a flag; positional is what
        # anyone actually types.
        args.capability = args.capability or args.name
        if args.action != "list" and not args.capability:
            print(
                f"permissions {args.action} needs --capability",
                file=sys.stderr,
            )
            return 2
        args.command_permissions = args.action
    if args.command == "status":
        return command_status(args)
    if args.command == "doctor":
        return command_doctor(args)
    if args.command == "market":
        return command_market(args)
    if args.command == "inbox":
        return command_inbox(args)
    if args.command == "permissions":
        return command_permissions(args)
    if args.command == "tune":
        return command_tune(args)
    if args.command == "review":
        return command_review(args)
    if args.command == "request":
        return command_request(args)
    if args.command == "resolve":
        return command_resolve(args)
    if args.command == "autonomous":
        return command_autonomous(args)
    if args.command == "run":
        return command_run(args)
    if args.command == "evaluate":
        return command_evaluate(args)
    if args.command == "harvest":
        return command_harvest(args)
    if args.command == "cycle":
        return command_cycle(args)
    print("unknown command", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
