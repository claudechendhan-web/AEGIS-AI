"""Execute a curated dataset and record which records actually run.

This is the step that turns a keyword-filtered corpus into something worth
paying for. Keyword filtering is an afternoon's work; proving that generated
code executes is not, and the output is a claim a buyer can check.

    python -m training.verify_dataset --package data/product/... --execute

Every record lands in exactly one bucket, and the buckets are deliberately
not equivalent:

    verified      ran to completion with exit code 0
    failed        ran and raised. This is a real defect signal.
    timeout       hung until the wall-clock limit
    unverifiable  could not run here, usually a missing third-party package.
                  NOT a quality signal, and reported separately so it cannot
                  be quietly folded into the pass rate.
    no_code       the response contained nothing extractable to run
    unsafe        contained import or call patterns refused before execution

The unverifiable bucket is the important one. Most records in a scraped corpus
reference numpy, pandas, or requests, and if those are absent then "failed"
and "unverifiable" are completely different facts. Reporting them as one
number would produce a confident, meaningless pass rate, which is worse than
reporting nothing.

SAFETY, PLEASE READ
-------------------
Executing scraped, model-generated code is genuinely dangerous. It is
arbitrary code written by an unknown party, and running it on your machine
can read your files, exfiltrate data, or damage what you have. The standard
library cannot give you a real security boundary:

* there is no stdlib sandbox, container, or seccomp filter
* a wall-clock timeout stops a hang, not a fork bomb or a disk wipe
* confining the working directory does not confine file access

What this tool does provide: a fixed argv with no shell, a short timeout, a
throwaway working directory, no stdin, and an opt-in `--execute` flag so
nothing runs by accident. What it does not provide is isolation. If you need
real isolation, run this inside a container or a disposable VM, and treat
`--execute` on unvetted data as roughly equivalent to opening the file in an
editor that executes it.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

DEFAULT_PACKAGE_DIR = Path("data/product")
DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_VERIFY = 200

# Third-party packages commonly referenced by scraped Python corpora. A record
# importing one of these cannot be verified without it installed, which is an
# environment fact and not a quality verdict.
KNOWN_THIRD_PARTY = frozenset(
    {
        "numpy",
        "pandas",
        "scipy",
        "matplotlib",
        "sklearn",
        "torch",
        "tensorflow",
        "keras",
        "requests",
        "bs4",
        "selenium",
        "flask",
        "django",
        "sqlalchemy",
        "pymongo",
        "boto3",
        "PIL",
        "cv2",
        "seaborn",
        "plotly",
        "nltk",
        "spacy",
        "transformers",
        "fastapi",
        "pydantic",
        "pytest",
        "yaml",
        "toml",
        "redis",
        "celery",
        "psycopg2",
    }
)

_FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)


def parse_quietly(code: str) -> ast.AST | None:
    """Parse without letting the corpus write to stderr.

    Scraped code triggers `SyntaxWarning` on invalid escape sequences, and at
    any useful sample size that floods the terminal and buries the report the
    user actually asked for. A SyntaxWarning is not an error here; the record
    is simply skipped if it will not parse.
    """
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            return ast.parse(code)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            return None


@dataclass(slots=True)
class Verdict:
    """The outcome for one record."""

    record_id: str
    bucket: str
    detail: str = ""
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "bucket": self.bucket,
            "detail": self.detail[:400],
            "duration_ms": self.duration_ms,
        }


@dataclass(slots=True)
class VerifyStats:
    """Aggregate counts. Never a single blended pass rate."""

    examined: int = 0
    verified: int = 0
    failed: int = 0
    timeout: int = 0
    unverifiable: int = 0
    no_code: int = 0
    unsafe: int = 0
    not_executed: int = 0
    verdicts: list[Verdict] = field(default_factory=list)

    def record(self, verdict: Verdict) -> None:
        self.examined += 1
        setattr(self, verdict.bucket, getattr(self, verdict.bucket) + 1)
        self.verdicts.append(verdict)

    def to_dict(self) -> dict[str, Any]:
        runnable = self.verified + self.failed + self.timeout
        return {
            "records_examined": self.examined,
            "buckets": {
                "verified": self.verified,
                "failed": self.failed,
                "timeout": self.timeout,
                "unverifiable": self.unverifiable,
                "no_code": self.no_code,
                "unsafe": self.unsafe,
                "not_executed": self.not_executed,
            },
            # Only meaningful over records we could actually attempt.
            "executable_records": runnable,
            "verified_rate_of_executable": (
                format(
                    (Decimal(self.verified) / Decimal(runnable)).quantize(
                        Decimal("0.0001")
                    ),
                    "f",
                )
                if runnable
                else None
            ),
            "excluded_from_rate": self.unverifiable + self.no_code + self.unsafe,
            "note": (
                "unverifiable, no_code and unsafe are excluded from the rate: "
                "they are environment and content facts, not correctness facts."
            ),
        }


def extract_code(response: str) -> str | None:
    """Pull runnable Python out of an assistant response.

    Fenced blocks are preferred. Without a fence, fall back to the whole
    response only if it actually parses as Python, because a prose answer
    that happens to mention `def` is not a runnable program.
    """
    blocks = [block.strip() for block in _FENCE.findall(response)]
    candidates = [block for block in blocks if block] or [response.strip()]
    for candidate in candidates:
        if not candidate:
            continue
        tree = parse_quietly(candidate)
        if tree is None:
            continue
        if any(
            isinstance(
                node,
                (
                    ast.Import,
                    ast.ImportFrom,
                    ast.FunctionDef,
                    ast.AsyncFunctionDef,
                    ast.ClassDef,
                ),
            )
            for node in ast.walk(tree)
        ):
            return candidate
    return None


def imported_roots(code: str) -> set[str]:
    """Top-level module names imported by the code."""
    tree = parse_quietly(code)
    if tree is None:
        return set()
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def missing_dependencies(code: str) -> list[str]:
    """Third-party imports with no importable module behind them."""
    import importlib.util

    missing: list[str] = []
    for root in sorted(imported_roots(code)):
        if root not in KNOWN_THIRD_PARTY:
            continue
        try:
            if importlib.util.find_spec(root) is None:
                missing.append(root)
        except (ImportError, ValueError):
            missing.append(root)
    return missing


def refuses(code: str) -> str:
    """Return a reason to refuse execution, or an empty string.

    A deliberately thin denylist. It is not a security boundary, it exists so
    that obviously destructive or exfiltrating samples are visible in the
    report rather than executed silently.
    """
    tree = parse_quietly(code)
    if tree is None:
        return ""
    banned_calls = {
        "eval",
        "exec",
        "compile",
        "__import__",
        "system",
        "popen",
        "remove",
        "rmdir",
        "unlink",
        "removedirs",
        "rmtree",
        "chmod",
        "chown",
        "kill",
        "fork",
        "setuid",
    }
    banned_modules = {
        "socket",
        "shutil",
        "subprocess",
        "ctypes",
        "urllib",
        "http",
        "ftplib",
        "smtplib",
        "multiprocessing",
        "pty",
        "signal",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in banned_calls:
                return f"calls {node.func.id}()"
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in banned_calls:
                return f"calls .{node.func.attr}()"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in banned_modules:
                    return f"imports {alias.name}"
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.split(".")[0] in banned_modules
        ):
            return f"imports {node.module}"
    return ""


def run_once(code: str, timeout: float) -> tuple[str, str, int]:
    """Execute in a throwaway directory. Returns (bucket, detail, ms)."""
    started = datetime.now(UTC)
    with tempfile.TemporaryDirectory(prefix="aegis-verify-") as workdir:
        script = Path(workdir) / "candidate.py"
        script.write_text(code, encoding="utf-8")
        try:
            # `-I` only: isolated mode, so PYTHONPATH, PYTHONSTARTUP and the
            # user site directory are all ignored. Deliberately NOT `-S`,
            # which skips site-packages entirely and makes numpy, pandas and
            # requests unimportable. With `-S` almost every record in a real
            # corpus lands in the unverifiable bucket, and the number you get
            # back is a measurement of the sandbox rather than of the code.
            completed = subprocess.run(
                [sys.executable, "-I", str(script)],
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                shell=False,
                env={
                    "PATH": "",
                    "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
                    "PYTHONHASHSEED": "0",
                },
                check=False,
            )
        except subprocess.TimeoutExpired:
            elapsed = int((datetime.now(UTC) - started).total_seconds() * 1000)
            return "timeout", f"exceeded {timeout:.0f}s", elapsed
        except OSError as exc:
            return "unverifiable", f"could not launch: {exc}", 0

    elapsed = int((datetime.now(UTC) - started).total_seconds() * 1000)
    if completed.returncode == 0:
        return "verified", "", elapsed
    stderr = (completed.stderr or "").strip().splitlines()
    detail = stderr[-1] if stderr else f"exit code {completed.returncode}"
    if "ModuleNotFoundError" in detail or "ImportError" in detail:
        return "unverifiable", detail, elapsed
    return "failed", detail, elapsed


def iter_package(package: Path) -> Iterator[Mapping[str, Any]]:
    """Stream the full dataset, skipping the sample and the manifest."""
    candidates = sorted(
        path for path in package.glob("*.jsonl") if path.name != "sample.jsonl"
    )
    if not candidates:
        return
    with candidates[0].open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, Mapping) and isinstance(record.get("messages"), list):
                yield record


def verify(
    package: Path,
    *,
    execute: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    max_verify: int = DEFAULT_MAX_VERIFY,
    skip_unsafe: bool = True,
) -> dict[str, Any]:
    """Classify every record, executing only when explicitly asked."""
    if not package.is_dir():
        raise FileNotFoundError(f"package directory not found: {package}")
    stats = VerifyStats()
    verified_ids: list[str] = []
    examined = 0

    for record in iter_package(package):
        if max_verify and examined >= max_verify:
            break
        examined += 1
        record_id = str(record.get("id") or f"row-{examined}")
        messages = record.get("messages") or []
        response = ""
        for message in messages:
            if isinstance(message, Mapping) and message.get("role") == "assistant":
                response = str(message.get("content") or "")
                break
        if not response:
            stats.record(Verdict(record_id, "no_code", "empty response"))
            continue

        code = extract_code(response)
        if code is None:
            stats.record(Verdict(record_id, "no_code", "no extractable python"))
            continue

        reason = refuses(code)
        if reason and skip_unsafe:
            stats.record(Verdict(record_id, "unsafe", reason))
            continue

        missing = missing_dependencies(code)
        if missing:
            stats.record(
                Verdict(record_id, "unverifiable", f"missing: {', '.join(missing)}")
            )
            continue

        if not execute:
            stats.record(Verdict(record_id, "not_executed", "pass --execute"))
            continue

        bucket, detail, elapsed = run_once(code, timeout)
        stats.record(Verdict(record_id, bucket, detail, elapsed))
        if bucket == "verified":
            verified_ids.append(record_id)

    payload = stats.to_dict()
    payload["executed"] = execute
    payload["timeout_seconds"] = timeout if execute else None
    payload["verified_ids"] = verified_ids[:2000]
    payload["at"] = datetime.now(UTC).isoformat()
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="training.verify_dataset",
        description=(
            "Execute a curated dataset and report which records actually run. "
            "Unverifiable records are reported separately from failures: a "
            "missing package is not a correctness verdict."
        ),
    )
    parser.add_argument("--package", type=Path, default=DEFAULT_PACKAGE_DIR)
    parser.add_argument(
        "--execute",
        action="store_true",
        help=(
            "actually run the code. Without this flag nothing is executed and "
            "the report only shows what would be attempted."
        ),
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument(
        "--max-verify",
        type=int,
        default=DEFAULT_MAX_VERIFY,
        help="upper bound on records examined; a safety limit, not a sample size",
    )
    parser.add_argument(
        "--allow-unsafe",
        action="store_true",
        help="do not apply the refused-pattern denylist",
    )
    parser.add_argument("--out", type=Path, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.execute:
        print(
            "Refusing to execute by default. This runs arbitrary scraped code "
            "on your machine.\n"
            "The standard library provides no sandbox, so --execute means "
            "'no isolation'.\n"
            "Re-run with --execute inside a container or disposable VM if you "
            "intend to proceed."
        )
    try:
        payload = verify(
            args.package,
            execute=args.execute,
            timeout=args.timeout,
            max_verify=args.max_verify,
            skip_unsafe=not args.allow_unsafe,
        )
    except FileNotFoundError as exc:
        print(f"error: {exc}")
        return 1
    print(json.dumps(payload, indent=2))
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
