"""Tests for the agent composition root.

`agent.py` is the one place where every subsystem meets, so these tests focus
on the seams: that degradation is clean when a model is missing, that the
tool set is actually registered, that budgets hold across a full cycle, and
that a category which measurably fails to pay stops being proposed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

import agent as agent_module
from agent import (
    DEFAULT_OPPORTUNITIES,
    _opportunities,
    _parse_opportunity,
    build_parser,
    build_state,
    check_model,
    main,
    status_report,
)
from tools.permissions import Capability
from treasury import Money


class _Args(argparse.Namespace):
    """Minimal stand-in for a parsed namespace."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


@pytest.fixture()
def paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "treasury": tmp_path / "t.db",
        "index": tmp_path / "m.db",
        "revenue": tmp_path / "r.db",
        "frontier": tmp_path / "h.db",
        "workspace": tmp_path / "ws",
    }


def state_args(paths: dict[str, Path], **overrides) -> dict:
    payload = {
        "model": "test-model",
        "base_url": "http://127.0.0.1:1",  # unreachable on purpose
        "treasury_path": paths["treasury"],
        "index_path": paths["index"],
        "revenue_path": paths["revenue"],
        "frontier_path": paths["frontier"],
        "workspace": paths["workspace"],
        "cost_per_request": 0.0,
        "delay": 0.0,
        "allow_network": True,
        "allow_execution": True,
    }
    payload.update(overrides)
    return payload


def seed_evidence(paths: dict[str, Path], *topics: str) -> None:
    """Put retrievable material in the index.

    Without this every opportunity is refused at the evidence gate, so no
    outcome is ever recorded and the learning loop can never fire. That is the
    subsystem working correctly, but it makes the cycle untestable.
    """
    from memory.store import MemoryIndex

    with MemoryIndex(paths["index"]) as index:
        for number, topic in enumerate(topics):
            index.add_document(
                f"https://market.test/{number}",
                f"Market notes about {topic}. Buyers pay for {topic} work. " * 12,
                title=f"{topic} market",
            )


def cli(paths: dict[str, Path], *args: str, run_log: Path | None = None) -> list[str]:
    payload = [
        "--treasury",
        str(paths["treasury"]),
        "--index",
        str(paths["index"]),
        "--revenue",
        str(paths["revenue"]),
        "--frontier",
        str(paths["frontier"]),
        "--workspace",
        str(paths["workspace"]),
        "--base-url",
        "http://127.0.0.1:1",
        "--cost-per-request",
        "0.0",
        "--delay",
        "0.0",
    ]
    if run_log is not None:
        payload += ["--run-log", str(run_log)]
    return payload + list(args)


# -- model detection --------------------------------------------------------


def test_check_model_never_raises() -> None:
    available, detail = check_model("m", "http://127.0.0.1:1", timeout=0.3)
    assert available is False
    assert detail


def test_check_model_reports_no_models_installed(monkeypatch) -> None:
    class _Client:
        def __init__(self, *a, **k):
            pass

        def health(self):
            return {"model_present": False, "available_models": []}

    monkeypatch.setattr(agent_module, "OllamaChat", _Client)
    available, detail = check_model("m", "http://x")
    assert available is False
    assert "no models" in detail
    assert "ollama pull" in detail


def test_check_model_accepts_a_present_model(monkeypatch) -> None:
    class _Client:
        def __init__(self, *a, **k):
            pass

        def health(self):
            return {"model_present": True, "available_models": ["m"]}

    monkeypatch.setattr(agent_module, "OllamaChat", _Client)
    available, _ = check_model("m", "http://x")
    assert available is True


# -- free-forever means local, and any local model will do ------------------


def _with_health(monkeypatch, tags: list[str], present: bool):
    class _Client:
        def __init__(self, model: str = "", *a, **k):
            # Recorded so a test can assert which model the agent chose.
            self.model = model

        def health(self):
            return {"model_present": present, "available_models": tags}

    monkeypatch.setattr(agent_module, "OllamaChat", _Client)


def test_no_models_is_still_a_refusal(monkeypatch) -> None:
    _with_health(monkeypatch, [], False)
    available, detail = check_model("qwen2.5-coder:7b", "http://x")
    assert available is False
    assert "ollama pull" in detail


def test_the_preferred_model_is_used_when_present(monkeypatch) -> None:
    _with_health(monkeypatch, ["qwen2.5-coder:7b"], True)
    available, _ = check_model("qwen2.5-coder:7b", "http://x")
    assert available is True
    assert agent_module.pick_model("qwen2.5-coder:7b", "http://x") == "qwen2.5-coder:7b"


def test_a_smaller_model_of_the_same_family_is_accepted(monkeypatch) -> None:
    """A 1.5B model runs fine on this hardware; refusing it would be silly."""
    _with_health(monkeypatch, ["qwen2.5-coder:1.5b"], False)
    available, detail = check_model("qwen2.5-coder:7b", "http://x")
    assert available is True
    assert "qwen2.5-coder:1.5b" in detail
    assert (
        agent_module.pick_model("qwen2.5-coder:7b", "http://x") == "qwen2.5-coder:1.5b"
    )


def test_any_installed_model_is_accepted(monkeypatch) -> None:
    _with_health(monkeypatch, ["llama3.2:3b"], False)
    available, detail = check_model("qwen2.5-coder:7b", "http://x")
    assert available is True
    assert "llama3.2:3b" in detail
    assert agent_module.pick_model("qwen2.5-coder:7b", "http://x") == "llama3.2:3b"


def test_a_family_match_is_preferred_over_an_unrelated_model(monkeypatch) -> None:
    _with_health(monkeypatch, ["llama3.2:3b", "qwen2.5-coder:1.5b"], False)
    assert (
        agent_module.pick_model("qwen2.5-coder:7b", "http://x") == "qwen2.5-coder:1.5b"
    )


def test_pick_model_never_raises_on_a_dead_endpoint() -> None:
    assert agent_module.pick_model("m", "http://127.0.0.1:1") == "m"


def test_the_agent_state_uses_the_model_that_is_actually_installed(
    paths: dict[str, Path], monkeypatch
) -> None:
    _with_health(monkeypatch, ["qwen2.5-coder:1.5b"], False)
    with build_state(**state_args(paths, model="qwen2.5-coder:7b")) as state:
        assert state.model_available is True
        assert state.client is not None
        assert state.client.model == "qwen2.5-coder:1.5b"


# -- composition ------------------------------------------------------------


def test_state_registers_the_web_tools(paths: dict[str, Path]) -> None:
    with build_state(**state_args(paths)) as state:
        names = {tool.name for tool in state.registry.list_tools()}
        assert "web_search" in names
        assert "web_fetch" in names


def test_state_registers_the_filesystem_and_exec_tools(paths: dict[str, Path]) -> None:
    with build_state(**state_args(paths)) as state:
        names = {tool.name for tool in state.registry.list_tools()}
        assert "read_file" in names
        assert "write_file" in names
        assert "run_python" in names


def test_network_can_be_switched_off(paths: dict[str, Path]) -> None:
    with build_state(**state_args(paths, allow_network=False)) as state:
        names = {tool.name for tool in state.registry.list_tools()}
        assert "web_search" not in names
        assert state.policy.allows(Capability.FILESYSTEM_READ)
        assert not state.policy.allows(Capability.NETWORK)


def test_workspace_is_created(paths: dict[str, Path]) -> None:
    with build_state(**state_args(paths)):
        assert paths["workspace"].exists()


def test_degrades_without_a_model(paths: dict[str, Path]) -> None:
    with build_state(**state_args(paths)) as state:
        assert state.model_available is False
        assert state.client is None
        # Everything that does not need a model still composed.
        assert state.registry.list_tools()
        report = status_report(state)
        assert report["model"]["available"] is False
        assert report["money"]["ledger_balanced"] is True


# -- the run log ------------------------------------------------------------


def test_run_without_a_model_exits_with_guidance(
    paths: dict[str, Path], capsys, tmp_path: Path
) -> None:
    code = main(
        [
            "--treasury",
            str(paths["treasury"]),
            "--index",
            str(paths["index"]),
            "--revenue",
            str(paths["revenue"]),
            "--frontier",
            str(paths["frontier"]),
            "--workspace",
            str(paths["workspace"]),
            "--base-url",
            "http://127.0.0.1:1",
            "run",
            "--goal",
            "research something",
        ]
    )
    assert code == 2
    output = capsys.readouterr().out
    assert "no model" in output
    assert "agent.py harvest" in output
    assert "agent.py evaluate" in output


def test_status_command_succeeds(paths: dict[str, Path], capsys) -> None:
    code = main(
        [
            "--treasury",
            str(paths["treasury"]),
            "--index",
            str(paths["index"]),
            "--revenue",
            str(paths["revenue"]),
            "--frontier",
            str(paths["frontier"]),
            "--workspace",
            str(paths["workspace"]),
            "--base-url",
            "http://127.0.0.1:1",
            "status",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert "money" in payload
    assert payload["money"]["reserve_locked"] is True


def test_doctor_reports_the_blocking_item(paths: dict[str, Path], capsys) -> None:
    code = main(
        [
            "--treasury",
            str(paths["treasury"]),
            "--index",
            str(paths["index"]),
            "--revenue",
            str(paths["revenue"]),
            "--frontier",
            str(paths["frontier"]),
            "--workspace",
            str(paths["workspace"]),
            "--base-url",
            "http://127.0.0.1:1",
            "doctor",
        ]
    )
    output = capsys.readouterr().out
    assert "AEGISAI doctor" in output
    assert "language model" in output
    assert code in (0, 1)


# -- opportunity parsing ----------------------------------------------------


def test_parse_opportunity_defaults_to_service() -> None:
    category, ask, _cost, hours = _parse_opportunity("something odd")
    assert category == "service"
    assert ask == "200.00"
    assert hours == 4.0


def test_parse_opportunity_reads_all_fields() -> None:
    parsed = _parse_opportunity("content:20.00:0.50:1.5")
    assert parsed == ("content", "20.00", "0.50", 1.5)


def test_parse_opportunity_survives_a_bad_hours_field() -> None:
    _category, _ask, _cost, hours = _parse_opportunity("service:1.00:0.00:banana")
    assert hours == 4.0


def test_supplied_opportunities_replace_the_defaults() -> None:
    """Regression: `action="append"` plus a set default extends the list."""
    args = _Args(opportunity=["content:1.00:0.00:1"])
    assert _opportunities(args) == ["content:1.00:0.00:1"]


def test_defaults_apply_when_nothing_is_supplied() -> None:
    assert _opportunities(_Args(opportunity=None)) == list(DEFAULT_OPPORTUNITIES)
    assert _opportunities(_Args()) == list(DEFAULT_OPPORTUNITIES)


# -- the learning cycle -----------------------------------------------------


def test_cycle_learns_and_blocks_a_failing_category(
    paths: dict[str, Path], capsys
) -> None:
    """Three measured losses must stop the category being proposed."""
    seed_evidence(paths, "research")
    with build_state(**state_args(paths)) as state:
        state.treasury.earn(Money.parse("100.00", "USD"), memo="funding")
    for _ in range(3):
        code = main(
            cli(
                paths,
                "cycle",
                "--rounds",
                "1",
                "--commit",
                "--record-outcome",
                "0.00",
                "--query",
                "research",
                "--opportunity",
                "research:20.00:0.10:1",
            )
        )
        assert code == 0
    output = capsys.readouterr().out
    assert "BLOCKED" in output
    with build_state(**state_args(paths)) as state:
        assert "research" not in state.revenue.allowed_categories()
        assert "research" in state.revenue.blocked_categories()


def test_cycle_keeps_a_winning_category(paths: dict[str, Path], capsys) -> None:
    seed_evidence(paths, "integration")
    with build_state(**state_args(paths)) as state:
        state.treasury.earn(Money.parse("100.00", "USD"), memo="funding")
    for _ in range(3):
        main(
            cli(
                paths,
                "cycle",
                "--rounds",
                "1",
                "--commit",
                "--record-outcome",
                "20.00",
                "--query",
                "integration",
                "--opportunity",
                "integration:20.00:0.10:1",
            )
        )
    capsys.readouterr()
    with build_state(**state_args(paths)) as state:
        assert "integration" in state.revenue.allowed_categories()


def test_cycle_commits_within_budget_and_never_touches_reserve(
    paths: dict[str, Path], capsys
) -> None:
    seed_evidence(paths, "tooling")
    with build_state(**state_args(paths)) as state:
        state.treasury.earn(Money.parse("10.00", "USD"), memo="funding")
    main(
        cli(
            paths,
            "cycle",
            "--rounds",
            "1",
            "--commit",
            "--query",
            "tooling",
            "--opportunity",
            "tooling:20.00:0.25:1",
        )
    )
    capsys.readouterr()
    with build_state(**state_args(paths)) as state:
        assert state.treasury.ledger.balance("env:OPERATE") == Money.parse(
            "1.75", "USD"
        )
        assert state.treasury.ledger.balance("env:RESERVE") == Money.parse(
            "8.00", "USD"
        )


def test_cycle_refuses_what_it_cannot_afford(paths: dict[str, Path], capsys) -> None:
    seed_evidence(paths, "content")
    main(
        cli(
            paths,
            "cycle",
            "--rounds",
            "1",
            "--commit",
            "--query",
            "content",
            "--opportunity",
            "content:5000.00:4000.00:1",
        )
    )
    output = capsys.readouterr().out
    assert "REFUSE" in output
    with build_state(**state_args(paths)) as state:
        assert state.treasury.ledger.balance("env:OPERATE") == Money.parse(
            "0.00", "USD"
        )


def test_cycle_writes_a_run_log(paths: dict[str, Path], tmp_path: Path, capsys) -> None:
    log = tmp_path / "runs.jsonl"
    seed_evidence(paths, "content")
    main(
        cli(
            paths,
            "cycle",
            "--rounds",
            "1",
            "--query",
            "content",
            "--opportunity",
            "content:20.00:0.10:1",
            run_log=log,
        )
    )
    capsys.readouterr()
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert lines
    assert json.loads(lines[0])["command"] == "cycle"


# -- parser -----------------------------------------------------------------


def test_parser_exposes_every_command() -> None:
    parser = build_parser()
    for command in ("status", "doctor", "run", "evaluate", "harvest", "cycle"):
        assert parser.parse_args(
            [command] + (["--goal", "g"] if command == "run" else [])
        )


def test_cycle_parser_accepts_opportunity() -> None:
    args = build_parser().parse_args(["cycle", "--opportunity", "content:1.00:0.00:1"])
    assert args.opportunity == ["content:1.00:0.00:1"]


def test_unknown_command_is_rejected() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["teleport"])


# -- global flags after a subcommand ---------------------------------------
#
# Regression, hit twice. A subparser that redeclares a global option must use
# `default=argparse.SUPPRESS`. With `default=None` it writes None into the
# *same* namespace the top-level parser already populated, so the global
# default is destroyed and the failure surfaces as an opaque
# "Path() argument should be a str, not NoneType" from deep inside a round.
# Nothing to restore from, because the original value is already gone.

GLOBAL_FLAGS = ("--escalations", "--security", "--selftune", "--notify", "--package")


@pytest.mark.parametrize("command", ["autonomous", "inbox", "tune", "permissions"])
@pytest.mark.parametrize("flag", GLOBAL_FLAGS)
def test_global_defaults_survive_a_subcommand(command: str, flag: str) -> None:
    args = build_parser().parse_args([command])
    value = getattr(args, flag.lstrip("-").replace("-", "_"), None)
    assert value is not None, f"{flag} lost its default under `{command}`"


@pytest.mark.parametrize("command", ["autonomous", "inbox", "tune"])
def test_notify_can_be_set_after_the_subcommand(command: str) -> None:
    args = build_parser().parse_args([command, "--notify", "none"])
    assert args.notify == "none"


def test_notify_none_actually_survives_to_the_handler() -> None:
    """The bug that mattered: --notify none silently became the global default."""
    args = build_parser().parse_args(["autonomous", "--notify", "none"])
    assert args.notify == "none"


def test_a_subcommand_flag_overrides_the_global() -> None:
    args = build_parser().parse_args(["autonomous", "--escalations", "data/x.db"])
    assert str(args.escalations).endswith("x.db")


def test_every_autonomous_round_argument_is_reachable() -> None:
    """The arguments command_autonomous reads must all exist post-parse.

    A missing attribute is a crash at 4am, not a failure at type-check time,
    because the round body only runs when the command executes.
    """
    args = build_parser().parse_args(["autonomous", "--hours", "0.01"])
    for name in (
        "hours",
        "max_rounds",
        "max_errors",
        "task",
        "sleep",
        "min_documents",
        "harvest_minutes",
        "harvest_pages",
        "source",
        "market_query",
        "viable_median",
        "query",
        "opportunity",
        "realized",
        "status_path",
        "escalations",
        "notify",
        "package",
        "selftune",
        "revenue",
        "treasury",
        "index",
        "frontier",
        "workspace",
        "run_log",
        "cost_per_request",
        "delay",
        "no_lessons",
    ):
        assert hasattr(args, name), f"command_autonomous reads args.{name}"
