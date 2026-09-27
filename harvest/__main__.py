"""Entry point: ``python -m harvest <command>``."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from harvest.errors import HarvestError
from harvest.frontier import Frontier
from harvest.worker import (
    DEFAULT_CYCLE_PAGES,
    DEFAULT_DELAY_SECONDS,
    DEFAULT_FRONTIER_PATH,
    DEFAULT_INDEX_PATH,
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_PAGES,
    DEFAULT_STATUS_PATH,
    DEFAULT_TREASURY_PATH,
    HarvestConfig,
    HarvestWorker,
    default_sources,
    load_status,
)
from treasury.treasury import Treasury

PID_PATH = Path("data/harvest.pid")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harvest",
        description=(
            "Bounded overnight documentation harvester. Populates the local "
            "memory index from real sources, metered by the treasury. Trains "
            "nothing: model weights are never modified."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(target: argparse.ArgumentParser) -> None:
        target.add_argument("--index", type=Path, default=DEFAULT_INDEX_PATH)
        target.add_argument("--frontier", type=Path, default=DEFAULT_FRONTIER_PATH)
        target.add_argument("--status", type=Path, default=DEFAULT_STATUS_PATH)

    start = sub.add_parser("start", help="run the harvester in the foreground")
    add_common(start)
    start.add_argument("--source", action="append", default=None)
    start.add_argument("--deadline-hours", type=float, default=8.0)
    start.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    start.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH)
    start.add_argument("--cycle-pages", type=int, default=DEFAULT_CYCLE_PAGES)
    start.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS)
    start.add_argument(
        "--no-follow-links",
        action="store_true",
        help="harvest only the seed urls, do not expand the frontier",
    )
    start.add_argument(
        "--no-treasury",
        action="store_true",
        help="do not meter fetching against a treasury",
    )
    start.add_argument("--treasury", type=Path, default=DEFAULT_TREASURY_PATH)
    start.add_argument(
        "--cost-per-fetch",
        type=float,
        default=0.001,
        help="amount charged to the spendable envelope per request",
    )

    status = sub.add_parser("status", help="show the last recorded status")
    status.add_argument("--status", type=Path, default=DEFAULT_STATUS_PATH)
    status.add_argument("--frontier", type=Path, default=DEFAULT_FRONTIER_PATH)
    status.add_argument(
        "--failures", type=int, default=10, help="how many failures to list"
    )

    stop = sub.add_parser("stop", help="ask a running harvester to stop")
    stop.add_argument("--pid", type=Path, default=PID_PATH)

    sub.add_parser("seeds", help="print the default source urls")
    return parser


def _write_pid(path: Path) -> int:
    import os

    pid = os.getpid()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(pid), encoding="utf-8")
    except OSError:
        pass
    return pid


def _clear_pid(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def command_start(args: argparse.Namespace) -> int:
    sources = tuple(args.source) if args.source else default_sources()
    config = HarvestConfig(
        sources=sources,
        deadline_hours=args.deadline_hours,
        max_pages=args.max_pages,
        max_depth=args.max_depth,
        cycle_pages=args.cycle_pages,
        delay_seconds=args.delay,
        follow_links=not args.no_follow_links,
        cost_per_fetch=args.cost_per_fetch,
        index_path=args.index,
        frontier_path=args.frontier,
        status_path=args.status,
    )
    treasury = None
    if not args.no_treasury:
        treasury = Treasury(args.treasury)

    pid = _write_pid(PID_PATH)
    print(f"harvest pid {pid}")
    print(f"  deadline   {config.deadline_hours} hour(s)")
    print(f"  sources    {len(config.sources)}")
    print(f"  status     {config.status_path}")
    print("Ctrl-C to stop; progress is resumable.")
    try:
        with HarvestWorker(config, treasury=treasury) as worker:
            stats = worker.run()
    except HarvestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        _clear_pid(PID_PATH)
        return 1
    finally:
        if treasury is not None:
            treasury.close()
    _clear_pid(PID_PATH)
    print(json.dumps(stats.to_dict(), indent=2))
    return 0


def command_status(args: argparse.Namespace) -> int:
    payload = load_status(args.status)
    if payload is None:
        print(f"no status file at {args.status}; the job has not run yet")
        return 1
    print(json.dumps(payload, indent=2, sort_keys=True))
    if args.failures > 0 and Path(args.frontier).exists():
        with Frontier(args.frontier) as frontier:
            failures = frontier.iter_failures(limit=args.failures)
        if failures:
            print("\nrecent failures:")
            for item in failures:
                print(
                    f"  {item['host']} attempts={item['attempts']} "
                    f"{item['last_error'][:70]}"
                )
    return 0


def command_stop(args: argparse.Namespace) -> int:
    import os
    import signal as signal_module

    if not args.pid.exists():
        print(f"no pid file at {args.pid}; nothing to stop")
        return 1
    try:
        pid = int(args.pid.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        print(f"unreadable pid file at {args.pid}")
        return 1
    try:
        os.kill(pid, signal_module.SIGTERM)
    except (OSError, ProcessLookupError) as exc:
        print(f"unable to signal pid {pid}: {exc}")
        _clear_pid(args.pid)
        return 1
    print(f"sent SIGTERM to pid {pid}; it will finish the current page and exit")
    return 0


def command_seeds(args: argparse.Namespace) -> int:
    for source in default_sources():
        print(source)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "start":
        return command_start(args)
    if args.command == "status":
        return command_status(args)
    if args.command == "stop":
        return command_stop(args)
    return command_seeds(args)


if __name__ == "__main__":
    raise SystemExit(main())
