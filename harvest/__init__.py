"""Bounded, resumable, treasury-metered documentation harvesting.

This package does not train a model and never will, on this machine or any
other that cannot run a fine-tune. What it does is build a durable knowledge
base: it crawls real documentation into the local BM25 index, so the agent can
answer questions about material it has actually read.

The honest framing, stated once so it is not mistaken for something else:

    Harvesting builds knowledge. It does not change weights.

    A model retrained on a corpus you gathered is a different, expensive
    project. A model with a good index over a corpus you gathered is
    available tonight, on this CPU, and is reversible by deleting one file.

Commands:

    python -m harvest seeds
    python -m harvest start --deadline-hours 8
    python -m harvest status
    python -m harvest stop

Run `start` detached, check on it with `status`. Everything the worker learned
is in the frontier, the index, and the treasury ledger, all of which survive a
crash and can be inspected in the morning.
"""

from harvest.errors import (
    DeadlineReachedError,
    FrontierExhaustedError,
    HarvestError,
    SourceConfigError,
)
from harvest.frontier import (
    DONE,
    FAILED,
    PENDING,
    SKIPPED,
    Frontier,
    FrontierItem,
)
from harvest.worker import (
    DEFAULT_SOURCES,
    Deadline,
    HarvestConfig,
    HarvestStats,
    HarvestWorker,
    allowed_source_hosts,
    build_config,
    default_sources,
    load_status,
    summarize_failures,
)

__all__ = [
    "DEFAULT_SOURCES",
    "DONE",
    "FAILED",
    "PENDING",
    "SKIPPED",
    "Deadline",
    "DeadlineReachedError",
    "Frontier",
    "FrontierExhaustedError",
    "FrontierItem",
    "HarvestConfig",
    "HarvestError",
    "HarvestStats",
    "HarvestWorker",
    "SourceConfigError",
    "allowed_source_hosts",
    "build_config",
    "default_sources",
    "load_status",
    "summarize_failures",
]
