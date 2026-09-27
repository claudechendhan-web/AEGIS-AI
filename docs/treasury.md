# The treasury

The treasury gives the AEGIS-X agent money of its own, and puts a hard limit
on what it can do with it. It is a standalone package: nothing outside
`treasury/` was modified, and removing the directory removes the feature.

## The rule

Income is split immediately, 20% spendable and 80% reserved:

```text
                     income 100.00
                          |
        +-----------------+-----------------+
        |                                   |
   OPERATE (20.00)                    RESERVE (80.00)
   spendable by the agent             locked
```

The agent can spend from `OPERATE` freely within the configured limits. It
cannot spend from `RESERVE` at all, because `RESERVE` is a locked envelope and
`spend` refuses it with `ReserveLockedError` before any balance is consulted.
Moving reserve money requires `release_reserve`, which is a different method
and demands an owner-supplied `authorization` string. There is no single call
that both spends and unlocks.

## Usage

```python
from treasury import Money, Treasury


def usd(value: str) -> Money:
    return Money.parse(value, "USD")


with Treasury("data/treasury.db") as bank:
    split = bank.earn(usd("100.00"), memo="invoice 1042")
    split.spendable  # 20.00000000 USD
    split.reserve  # 80.00000000 USD

    bank.spend(usd("1.25"), memo="inference API call")
    bank.spend(
        usd("0.50"),
        memo="web research",
    )  # allowed

    bank.spend(usd("5.00"), memo="a data centre", envelope_name="RESERVE")
    # treasury.errors.ReserveLockedError

    bank.release_reserve(
        usd("10.00"), memo="seed a project", authorization="owner-approved-2026-09-26"
    )
```

## Why the ledger looks the way it does

### Amounts are integers, not text

Postings store `amount_minor INTEGER`, the amount scaled by 10^8. This is
load-bearing. `SUM()` over a `TEXT` column makes SQLite coerce to floating
point, so a ledger of small postings drifts by around 1e-14 and stops
reconciling. `SUM()` over `INTEGER` is exact. The first draft of this package
had the float bug and a test caught it.

### Amounts are never floats, anywhere

`Money` rejects `float` outright rather than converting it, because
`Money(0.1, "USD")` looks harmless and silently carries the error already
present in the literal. Pass a `str` or `Decimal`. Inputs with more than 8
decimal places are rejected, not rounded, so a payment cannot quietly lose
money.

### Balances are derived, never stored

There is no balance column. `balance()` sums postings, so an update bug cannot
inflate a total, and a tampered database is detectable instead of invisible.

### The split is one entry, not two writes

`earn` posts a single balanced entry with three legs: debit `ext:world`, credit
`OPERATE`, credit `RESERVE`. Because it is one atomic entry, a crash can never
record 20% without 80%.

### Rounding cannot mint money

`allocate` rounds the spendable side **down** and gives the remainder to the
reserve, so `spendable + reserve == income` exactly. Rounding the reserve down
instead would hand out spendable money that was never allocated. A property
test sweeps sub-dollar incomes to confirm the pair always sums back.

## Guarantees

The invariant, checkable at any time:

```python
bank.ledger.assert_balanced("USD")  # raises unless net position is exactly 0
```

`sum(credits) - sum(debits) == 0`, per currency, across the whole ledger.

| Property | Enforced by |
| --- | --- |
| Income always splits 20/80 | `BudgetPolicy.allocate`, single balanced entry |
| Split always reconciles exactly | rounding down spendable, remainder to reserve |
| Reserve is unspendable | locked envelope, refused in `authorize_spend` |
| Reserve needs owner consent | `release_reserve(..., authorization=...)` |
| Cannot overspend an envelope | `available()` check against derived balance |
| Cannot double-spend a held amount | reservations reduce `available()` |
| Rounding cannot create money | exact integer storage, derived balances |
| Learning is not free | `record_observation(cost=...)` spends from `OPERATE` |

## Circuit breakers

`spend_ratio` is not the only limit. `BudgetPolicy` also accepts:

- `max_spend_per_run` - a hard ceiling on one run's spending.
- `max_spend_ratio_of_income` - a ceiling relative to income to date, so a
  single run cannot spend income the run itself has not yet earned.

Both raise `SpendLimitExceededError`. These exist because "the agent may spend
20% for any purpose" still needs a bound: an agent stuck in a retry loop will
happily spend its entire budget on a service that is already failing.

## Learning is metered

`record_observation` logs what the agent learned and charges `OPERATE` for it.
Research competes with everything else for the same 20%, which is the point:
knowing things has a cost, and the ledger is where that becomes visible.

```python
bank.record_observation(
    source="web",
    uri="https://example.com/pricing",
    title="competitor pricing",
    cost=usd("0.50"),
    run_id=run.run_id,
)
bank.research_cost()  # 0.50000000 USD
```

## What this does not do

It does not make money. The treasury is a budget and an audit trail, not a
revenue source. An agent with no capital, no exclusive data, no reputation and
no distribution has no edge, and a 20% spending cap does not create one. What
this package provides is the part that is genuinely buildable: money the agent
earns is real, its spending is bounded, and its reserve is genuinely
untouchable. The revenue mechanism has to come from somewhere real.
