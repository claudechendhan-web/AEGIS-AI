# Revenue evaluation

**This package does not teach the agent to make money.** Nothing here can, and
any package that claims to is fiction. What it does is narrower and real: it
stops the agent from spending money on claims that have no evidence behind
them, and it learns from outcomes which categories of work actually paid.

That is the only part of "learn to make money" that is achievable from a
standing start. The bottleneck to revenue is a buyer, not knowledge, and no
amount of retrieval or scoring creates a buyer.

## The model

An `Opportunity` is a **falsifiable** claim:

| Field | Meaning |
| --- | --- |
| `ask_price` | what someone will pay |
| `expected_cost` | tools, API calls, compute |
| `effort_hours` | the agent's time, which is the scarce input |
| `evidence` | retrievable citations backing the claim |
| `horizon_days` | how long until the money arrives |
| `category` | one of a closed set of six |

Derived, all exact `Decimal`:

```python
opportunity.net  # ask_price - expected_cost
opportunity.margin_ratio  # net / ask_price
opportunity.effective_hourly  # net / effort_hours, rounded down
```

`effective_hourly` is the number that actually decides, and it is the one most
systems omit. Rounding is downward on purpose: a borderline opportunity is
treated as too weak rather than strong enough.

## The gates

`OpportunityEvaluator.evaluate` runs seven gates. Every gate **refuses rather
than warns**, because a warning in a budget system is a suggestion the caller
can ignore. Evaluation never raises for an economic refusal; it returns a
`Verdict` with `approved=False` and a reason.

| Gate | Refuses when |
| --- | --- |
| `evidence` | fewer than `min_evidence` **distinct** sources |
| `track_record` | the category was killed by its own history |
| `affordable` | the cost exceeds the spendable envelope |
| `positive_net` | revenue does not exceed direct cost |
| `margin` | net is under `min_margin_ratio` of the ask |
| `effective_hourly` | net per hour is under `min_hourly` |
| `payback_horizon` | the money takes longer than `max_horizon_days` |

`affordable` is the gate that ties back to the treasury: it asks
`treasury.can_spend`, which consults the `OPERATE` envelope only. **The
reserve is not a budget line.** It is the agent's savings, and an opportunity
that can only be funded by raiding savings is not an opportunity.

`evaluate` is free, so it can be run speculatively as often as you like.
`commit` is what spends, and it re-checks the evidence independently — a
hand-built `Verdict` claiming approval still cannot route an unsubstantiated
claim to the treasury.

## The track record, which is the part that learns

```python
record.record_outcome(opportunity.opportunity_id, Money.parse("200.00", "USD"))
stats = record.category_stats("content")
stats.hit_rate, stats.net, stats.killed, stats.reason
```

Four properties make this a feedback loop rather than a self-congratulation
mechanism:

* **Outcomes are money actually received.** Nothing here lets a model mark its
  own work as a success, so the record can only improve by being right.
* **A win is clearing the ask price**, not merely not losing money. Anything
  less counts as a miss, which is what the floor should measure.
* **Kill requires a minimum sample.** One failure does not condemn a
  category. Killing on noise leaves a system that will only ever repeat the
  one thing that worked twice.
* **The rule is one-directional.** A killed category cannot become healthy,
  because nothing here can change the past. The system's only response to
  failure is to become more conservative.

Two independent kill conditions: hit rate below the floor, or cumulative net
past `max_net_loss`. `net` is money received minus money **spent** — the cost
column is tracked separately, because subtracting *projected net* from
*realized* turns a 50 dollar loss into a 50 dollar gain and makes the loss gate
incapable of ever firing.

Categories are a closed set of six (`data_product`, `service`, `tooling`,
`content`, `integration`, `research`). An open vocabulary would let a model
mint a fresh category per bad idea and escape its own history entirely.

## Usage

```python
from revenue import Money, OpportunityEvaluator, TrackRecord, build_opportunity
from memory.retriever import Retriever

record = TrackRecord("data/revenue.db")
evaluator = OpportunityEvaluator(treasury=bank, track_record=record)

candidate = build_opportunity(
    "data_product",
    "Curated Python instruction set, per-seat licence",
    ask_price=Money.parse("200.00", "USD"),
    expected_cost=Money.parse("12.50", "USD"),
    effort_hours=6.0,
    query="curated python dataset licence price",
    retriever=Retriever(index),
)

verdict = evaluator.evaluate(candidate)
print(verdict.approved, verdict.reason)

if verdict.approved:
    evaluator.commit(verdict)
    # ... do the work, and once the money actually arrives:
    record.record_outcome(candidate.opportunity_id, Money.parse("200.00", "USD"))
```

`strict_evaluator(treasury=..., track_record=...)` raises the bar: two sources,
50% margin, 25/hour, 30-day payback.

## What this does and does not buy you

**Does:** an agent that can only spend on evidenced, profitable, affordable,
time-respecting work, and that narrows its own bets after they fail to pay.
`record.blocked_categories()` tells you which ideas it has already learned to
avoid, and why, with numbers.

**Does not:** find customers, close sales, or produce revenue on its own. An
agent with no capital, no exclusive data, no reputation and no distribution has
no edge, and a set of well-designed gates does not manufacture one. The
gates make the budget last longer; they do not make it grow.

If the goal is revenue, the missing piece is a buyer and a way to reach them.
That is distribution work, and it is yours, not the agent's.
