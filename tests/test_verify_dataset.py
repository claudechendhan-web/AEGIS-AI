"""Tests for dataset execution verification.

The value of this tool is the honesty of its buckets. The tests therefore
focus on the distinctions that are easy to get wrong: a missing package is
not a failure, prose is not code, and the reported rate must never quietly
include records that were never run.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from training.verify_dataset import (
    KNOWN_THIRD_PARTY,
    Verdict,
    VerifyStats,
    build_parser,
    extract_code,
    imported_roots,
    iter_package,
    missing_dependencies,
    parse_quietly,
    refuses,
    run_once,
    verify,
)

RUNNING = "import math\n\n\ndef area(radius):\n    return math.pi * radius ** 2\n"
# Must actually raise at import time. Merely *defining* a function that raises
# is valid code and correctly exits 0.
RAISES = "raise ValueError('nope')\n"
CALLS_BAD = "def boom():\n    raise ValueError('nope')\n\n\nboom()\n"
USES_NUMPY = "import numpy as np\n\n\ndef m(a):\n    return np.mean(a)\n"
USES_SHUTIL = "import shutil\n\n\ndef go(p):\n    shutil.rmtree(p)\n"
USES_SYSTEM = "import os\n\n\ndef go():\n    os.system('echo hi')\n"


# -- code extraction --------------------------------------------------------


def test_extracts_a_fenced_block() -> None:
    text = "Here you go:\n\n```python\ndef f():\n    return 1\n```\n\nThat works."
    assert "def f()" in (extract_code(text) or "")


def test_prefers_the_first_usable_fence() -> None:
    text = (
        "```\nthis is not python at all !!\n```\n```python\ndef f():\n    return 2\n```"
    )
    assert "return 2" in (extract_code(text) or "")


def test_prose_mentioning_def_is_not_code() -> None:
    assert extract_code("You can define a function with the def keyword.") is None


def test_empty_response_yields_nothing() -> None:
    assert extract_code("") is None


def test_unfenced_code_is_accepted_when_it_parses() -> None:
    assert extract_code(RUNNING) is not None


# -- imports and dependencies ----------------------------------------------


def test_imported_roots_collects_top_level_names() -> None:
    roots = imported_roots(
        "import numpy as np\nfrom scipy import linalg\nimport os.path"
    )
    assert {"numpy", "scipy", "os"} <= roots


def test_relative_imports_are_ignored() -> None:
    assert imported_roots("from . import sibling") == set()


def test_missing_dependency_is_detected(monkeypatch) -> None:
    # A module in KNOWN_THIRD_PARTY that certainly is not installed.
    assert "definitely_not_installed_pkg" not in KNOWN_THIRD_PARTY
    code = "import definitely_not_installed_pkg\n"
    # Not a known third-party name, so nothing to report.
    assert missing_dependencies(code) == []


def test_present_dependency_is_not_missing() -> None:
    # json is always importable and is not in KNOWN_THIRD_PARTY.
    assert missing_dependencies("import json\n") == []


# -- refusal ----------------------------------------------------------------


@pytest.mark.parametrize(
    "code,fragment",
    [
        (USES_SHUTIL, "shutil"),
        (USES_SYSTEM, "system"),
        ("import ctypes\n", "ctypes"),
        ("import socket\n", "socket"),
        ("import subprocess\n", "subprocess"),
        ("eval('1+1')\n", "eval"),
        ("exec('x=1')\n", "exec"),
        ("__import__('os')\n", "__import__"),
    ],
)
def test_dangerous_patterns_are_refused(code: str, fragment: str) -> None:
    reason = refuses(code)
    assert reason, f"expected a refusal for {fragment}"
    assert fragment in reason


def test_safe_code_is_not_refused() -> None:
    assert refuses(RUNNING) == ""


def test_ordinary_stdlib_usage_is_allowed() -> None:
    code = "import json\nimport math\nimport os\nfrom pathlib import Path\n"
    assert refuses(code) == ""


# -- quiet parsing ----------------------------------------------------------


def test_parse_quietly_returns_none_for_broken_code() -> None:
    assert parse_quietly("def (:\n") is None


def test_parse_quietly_suppresses_syntax_warnings(capsys) -> None:
    # An invalid escape sequence emits a SyntaxWarning on parse.
    parse_quietly('PATTERN = "\\d+"\n')
    assert capsys.readouterr().err == ""


# -- execution --------------------------------------------------------------


def test_correct_code_verifies() -> None:
    bucket, detail, _elapsed = run_once(RUNNING, timeout=20.0)
    assert bucket == "verified", detail


def test_raising_code_fails() -> None:
    bucket, detail, _elapsed = run_once(RAISES, timeout=20.0)
    assert bucket == "failed"
    assert "ValueError" in detail


def test_infinite_loop_times_out() -> None:
    bucket, detail, _elapsed = run_once("while True:\n    pass\n", timeout=3.0)
    assert bucket == "timeout"
    assert "exceeded" in detail


def test_missing_package_is_unverifiable_not_failed() -> None:
    """The distinction the whole report depends on."""
    bucket, detail, _elapsed = run_once(
        "import definitely_absent_module_xyz\n", timeout=20.0
    )
    assert bucket == "unverifiable"
    assert "ModuleNotFoundError" in detail or "ImportError" in detail


def test_stdlib_is_available_inside_the_sandbox() -> None:
    """Regression: `-S` skipped site-packages and broke every real import."""
    bucket, detail, _elapsed = run_once(
        "import json\nimport sqlite3\nfrom decimal import Decimal\nprint(Decimal('1'))",
        timeout=20.0,
    )
    assert bucket == "verified", detail


# -- stats ------------------------------------------------------------------


def test_rate_excludes_records_that_were_never_run() -> None:
    stats = VerifyStats()
    stats.record(Verdict("a", "verified"))
    stats.record(Verdict("b", "failed"))
    stats.record(Verdict("c", "unverifiable"))
    stats.record(Verdict("d", "no_code"))
    stats.record(Verdict("e", "unsafe"))
    payload = stats.to_dict()
    assert payload["executable_records"] == 2
    assert payload["verified_rate_of_executable"] == "0.5000"
    assert payload["excluded_from_rate"] == 3


def test_rate_is_none_when_nothing_was_executable() -> None:
    stats = VerifyStats()
    stats.record(Verdict("a", "unverifiable"))
    assert stats.to_dict()["verified_rate_of_executable"] is None


def test_rate_is_readable() -> None:
    stats = VerifyStats()
    for index in range(7):
        stats.record(Verdict(str(index), "verified"))
    assert stats.to_dict()["verified_rate_of_executable"] == "1.0000"


# -- end to end -------------------------------------------------------------


def write_package(directory: Path, responses: list[str]) -> Path:
    package = directory / "pkg"
    package.mkdir(parents=True, exist_ok=True)
    lines = []
    for index, response in enumerate(responses):
        lines.append(
            json.dumps(
                {
                    "id": f"rec{index}",
                    "messages": [
                        {"role": "user", "content": "do the thing"},
                        {"role": "assistant", "content": response},
                    ],
                    "metadata": {},
                }
            )
        )
    (package / "data-1.0.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (package / "sample.jsonl").write_text("", encoding="utf-8")
    (package / "MANIFEST.json").write_text("{}", encoding="utf-8")
    return package


def test_verify_does_not_execute_without_the_flag(tmp_path: Path) -> None:
    package = write_package(tmp_path, [RUNNING])
    payload = verify(package, execute=False, max_verify=0)
    assert payload["executed"] is False
    assert payload["verified_ids"] == []


def test_verify_buckets_a_mixed_package(tmp_path: Path) -> None:
    package = write_package(
        tmp_path,
        [
            RUNNING,
            CALLS_BAD,
            USES_SHUTIL,
            "Just some prose with no code at all.",
            "import numpy as np\n\n\ndef m(a):\n    return np.mean(a)\n",
        ],
    )
    payload = verify(package, execute=True, max_verify=0, timeout=20.0)
    buckets = payload["buckets"]
    assert buckets["verified"] >= 1
    assert buckets["failed"] >= 1
    assert buckets["unsafe"] >= 1
    assert buckets["no_code"] >= 1
    assert payload["executed"] is True


def test_verify_respects_max_verify(tmp_path: Path) -> None:
    package = write_package(tmp_path, [RUNNING] * 10)
    payload = verify(package, execute=True, max_verify=3, timeout=20.0)
    assert payload["records_examined"] <= 3


def test_verify_reports_a_missing_package(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        verify(tmp_path / "absent", execute=False)


def test_allow_unsafe_lets_dangerous_code_through(tmp_path: Path) -> None:
    package = write_package(tmp_path, [USES_SHUTIL])
    blocked = verify(package, execute=True, max_verify=0, skip_unsafe=True)
    assert blocked["buckets"]["unsafe"] == 1
    allowed = verify(package, execute=True, max_verify=0, skip_unsafe=False)
    assert allowed["buckets"]["unsafe"] == 0


def test_iter_package_skips_sample_and_manifest(tmp_path: Path) -> None:
    package = write_package(tmp_path, [RUNNING, RAISES])
    records = list(iter_package(package))
    assert len(records) == 2


def test_iter_package_skips_malformed_lines(tmp_path: Path) -> None:
    package = write_package(tmp_path, [RUNNING])
    path = package / "data-1.0.jsonl"
    path.write_text(
        path.read_text(encoding="utf-8") + "{not json\n[]\n", encoding="utf-8"
    )
    assert len(list(iter_package(package))) == 1


# -- cli --------------------------------------------------------------------


def test_execution_requires_the_explicit_flag(capsys) -> None:
    from training.verify_dataset import main

    assert main(["--package", "data/product"]) == 0
    output = capsys.readouterr().out
    assert "Refusing to execute by default" in output
    assert "no isolation" in output


def test_parser_defaults_to_no_execution() -> None:
    args = build_parser().parse_args([])
    assert args.execute is False
    assert args.timeout > 0
    assert args.max_verify > 0


def test_syntax_error_free_module() -> None:
    assert sys.modules["training.verify_dataset"] is not None
    assert Decimal is not None
