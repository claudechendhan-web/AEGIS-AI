# Overnight harvesting

**This does not train a model.** Model weights are never read or written by
anything in this package, and they never will be on hardware that cannot run a
fine-tune. What it does is build a durable knowledge base: it crawls real
documentation into the local BM25 index, so tomorrow the agent can answer
questions about material it has actually read.

| | |
| --- | --- |
| Changes model weights | No |
| Survives a crash | Yes, everything is in SQLite |
| Costs money | Yes, metered from the spendable envelope |
| Touches the reserve | Never |
| Reversible | Delete `data/harvest_memory.db` |

Harvesting builds **knowledge**. A model retrained on a corpus you gathered is
a different, expensive project. A model with a good index over a corpus you
gathered is available tonight, on this CPU.

## Running it

```powershell
# see what it will crawl
python -m harvest seeds

# run it, detached
Start-Process python -ArgumentList "-m","harvest","start","--deadline-hours","24" `
    -RedirectStandardOutput data\harvest.out -RedirectStandardError data\harvest.err

# check on it, any time, without attaching to the process
python -m harvest status

# ask it to stop; it finishes the page in flight first
python -m harvest stop
```

The job must be given money to spend, or it stops immediately with
`stopped_reason: "budget"`:

```powershell
python -c "from treasury import Treasury, Money; t=Treasury('data/treasury.db'); t.earn(Money.parse('20.00','USD'), memo='harvest float')"
```

Or run unmetered with `--no-treasury` if you just want the corpus.

## Why it is built to survive being unattended

Nobody is watching this process at 03:00, so every hazard is handled in code:

* **A hard wall-clock deadline.** Running out of time is a *success*, recorded
  as `stopped_reason: "deadline"`. An unbounded job started before you sleep is
  a process you cannot reason about in the morning.
* **Everything is in SQLite.** Queue state, failures, per-host backoff, and an
  event log. A crash or reboot resumes rather than restarts.
* **A status file rewritten every cycle**, written atomically via a temp file
  and `replace`, so a reader never catches it half-written.
* **The most specific stop reason wins.** Running out of money and running out
  of work are very different mornings, so `frontier_empty` never overwrites
  `budget`.
* **In-flight URLs are restored on stop**, so the tail of the queue is not
  swallowed by a Ctrl-C.

## Politeness is not optional

* `robots.txt` is fetched once per host and cached. Disallowed paths are
  **raised, not silently skipped**, then counted in a `refused` counter
  separate from network failures.
* Requests are serialized with a configurable `--delay` (default 1.0s).
* A host that fails repeatedly is backed off via `harvest_hosts` and skipped
  entirely. One dead domain must not consume the night.
* `--max-depth` (default 2) and `--max-pages` (default 2000) bound the crawl.
* The `max_attempts` sweep in `Frontier.take` moves exhausted URLs from
  `pending` to `failed` with a reason. Leaving them `pending` would overstate
  remaining work and make a finished run look resumable when it is not.

## Budget enforcement

Every request is charged `cost_per_fetch` to the `OPERATE` envelope
(`--cost-per-fetch`, default 0.001) *before* the fetch is attempted. If the
agent cannot afford the next request, the worker stops with
`stopped_reason: "budget"` rather than starting something it cannot finish.

`stats.spent` is asserted in the tests to equal what the treasury actually
charged. That assertion exists because the first version computed the delta as
`after - before`; since spending *lowers* the balance, a `> 0` guard discarded
every charge and the run cheerfully reported `$0.00 spent` while paying for
every request.

## What it will and will not do overnight

The default seeds are ten pages of the Python standard library documentation.
At `--max-depth 2` that frontier is a few hundred pages, which at one request
per second finishes in **minutes, not 24 hours**. A 24-hour deadline on that
seed list means the job ends early and says `frontier_empty`.

That is the correct outcome, not a failure. To use the time productively, add
sources:

```powershell
python -m harvest start --deadline-hours 24 --source https://example.com/docs/ --max-depth 3
```

Passing `--source` replaces the defaults, so include the built-in seeds too if
you want both. A 24-hour run is only meaningful against a large enough
frontier, and the honest way to get one is to name the documentation you
actually need rather than to widen the crawl.

## Reading the results

```python
from memory.store import MemoryIndex
from memory.retriever import Retriever

index = MemoryIndex("data/harvest_memory.db")
print(index.stats().to_dict())

context = Retriever(index, token_budget=1200).retrieve("how does Decimal rounding work")
for citation in context.citations:
    print(citation["index"], citation["uri"], citation["score"])
```

`python -m harvest status` prints the last run's counters, the index size, the
frontier breakdown, the treasury balances, and the most recent failures with
their error text.
