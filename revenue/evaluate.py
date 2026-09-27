"""Deciding whether an opportunity is worth acting on.

This is the enforcement point. `Opportunity` describes what the model claims;
this module decides whether the claim survives contact with evidence, cost,
and history. Every gate refuses rather than warns, because a warning in a
budget system is just a suggestion the caller can ignore.

The gates, in order:

1. **Evidence.** At least `min_evidence` retrievable citations. A claim with
   no source is not an opportunity, it is a guess with a price attached.
2. **Affordability.** The cost must fit inside the *spendable* envelope. The
   reserve is not a budget line; it is the agent's savings.
3. **Positive net.** Revenue must exceed direct cost.
4. **Margin floor.** Net must clear `min_margin_ratio` of the ask price, so a
   high-revenue, barely-profitable idea does not crowd out a smaller solid one.
5. **Effective hourly rate.** Net per hour must clear `min_hourly`. This is the
   gate that actually matters when time is the scarce input, and it is the one
   most systems omit.
6. **Payback horizon.** Must resolve within `max_horizon_days`. Money that
   arrives eventually is not money the agent has.
7. **Track record.** The category must not be killed by its own history.

Gates 1 and 2 are the ones that keep this honest. The rest are tuning.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from revenue.errors import (
    NoAffordabilityError,
    UnsubstantiatedOpportunityError,
)
from revenue.opportunity import Opportunity
from revenue.trackrecord import TrackRecord

VERDICT_WORTHWHILE = "worthwhile"
VERDICT_REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class GateResult:
    name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"gate": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class Verdict:
    """The outcome of evaluating one opportunity."""

    opportunity: Opportunity
    gates: tuple[GateResult, ...]
    treasury: Any = None
    record: TrackRecord | None = None

    @property
    def approved(self) -> bool:
        return all(gate.passed for gate in self.gates)

    @property
    def first_failure(self) -> GateResult | None:
        for gate in self.gates:
            if not gate.passed:
                return gate
        return None

    @property
    def reason(self) -> str:
        failure = self.first_failure
        if failure is None:
            return "all gates passed"
        return f"{failure.name}: {failure.detail}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "opportunity_id": self.opportunity.opportunity_id,
            "category": self.opportunity.category,
            "summary": self.opportunity.summary,
            "approved": self.approved,
            "reason": self.reason,
            "gates": [gate.to_dict() for gate in self.gates],
            "opportunity": self.opportunity.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class EvaluationPolicy:
    """Thresholds for the economic gates."""

    min_evidence: int = 1
    min_margin_ratio: Decimal = Decimal("0.35")
    min_hourly: Decimal = Decimal(10)
    max_horizon_days: int = 60
    require_positive_net: bool = True
    currency: str = "USD"

    def __post_init__(self) -> None:
        if self.min_evidence < 0:
            raise ValueError("min_evidence must not be negative")
        if self.min_margin_ratio < 0:
            raise ValueError("min_margin_ratio must not be negative")
        if self.max_horizon_days <= 0:
            raise ValueError("max_horizon_days must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_evidence": self.min_evidence,
            "min_margin_ratio": format(self.min_margin_ratio, "f"),
            "min_hourly": format(self.min_hourly, "f"),
            "max_horizon_days": self.max_horizon_days,
            "require_positive_net": self.require_positive_net,
            "currency": self.currency,
        }


class OpportunityEvaluator:
    """Applies the gates, and optionally records what it decided."""

    def __init__(
        self,
        *,
        policy: EvaluationPolicy | None = None,
        treasury: Any = None,
        track_record: TrackRecord | None = None,
    ) -> None:
        self.policy = policy or EvaluationPolicy()
        self.treasury = treasury
        self.track_record = track_record

    # -- individual gates -------------------------------------------------

    def _evidence_gate(self, opportunity: Opportunity) -> GateResult:
        count = len(opportunity.evidence)
        sources = len({item.uri for item in opportunity.evidence})
        passed = sources >= self.policy.min_evidence
        return GateResult(
            "evidence",
            passed,
            f"{sources} distinct source(s) of {count} citation(s); "
            f"minimum {self.policy.min_evidence}",
        )

    def _affordability_gate(self, opportunity: Opportunity) -> GateResult:
        if self.treasury is None:
            return GateResult("affordable", True, "no treasury attached")
        cost = opportunity.expected_cost
        try:
            affordable = self.treasury.can_spend(cost)
        except Exception as exc:  # noqa: BLE001 - a policy error is a refusal
            return GateResult("affordable", False, f"policy error: {exc}")
        if affordable:
            return GateResult("affordable", True, f"{cost} fits the spendable envelope")
        return GateResult(
            "affordable",
            False,
            f"cost {cost} exceeds the spendable envelope; the reserve is not "
            "available to fund work",
        )

    def _net_gate(self, opportunity: Opportunity) -> GateResult:
        if not self.policy.require_positive_net:
            return GateResult("positive_net", True, "not required")
        net = opportunity.net
        return GateResult(
            "positive_net",
            net.is_positive,
            f"net {net} after {opportunity.expected_cost} of direct cost",
        )

    def _margin_gate(self, opportunity: Opportunity) -> GateResult:
        ratio = opportunity.margin_ratio
        return GateResult(
            "margin",
            ratio >= self.policy.min_margin_ratio,
            f"margin {format(ratio, 'f')} vs floor "
            f"{format(self.policy.min_margin_ratio, 'f')}",
        )

    def _hourly_gate(self, opportunity: Opportunity) -> GateResult:
        hourly = opportunity.effective_hourly
        floor = Decimal(str(self.policy.min_hourly))
        return GateResult(
            "effective_hourly",
            hourly.amount >= floor,
            f"{hourly} per hour vs floor {floor}",
        )

    def _horizon_gate(self, opportunity: Opportunity) -> GateResult:
        return GateResult(
            "payback_horizon",
            opportunity.horizon_days <= self.policy.max_horizon_days,
            f"{opportunity.horizon_days} day(s) vs ceiling "
            f"{self.policy.max_horizon_days}",
        )

    def _track_record_gate(self, opportunity: Opportunity) -> GateResult:
        if self.track_record is None:
            return GateResult("track_record", True, "no track record attached")
        stats = self.track_record.category_stats(opportunity.category)
        return GateResult(
            "track_record",
            not stats.killed,
            f"{stats.category}: hit rate {format(stats.hit_rate, 'f')} over "
            f"{stats.attempts} attempt(s); {stats.reason}",
        )

    # -- evaluation -------------------------------------------------------

    def evaluate(self, opportunity: Opportunity) -> Verdict:
        """Run every gate. Never raises for an economic refusal."""
        verdict = Verdict(
            opportunity=opportunity,
            gates=(
                self._evidence_gate(opportunity),
                self._track_record_gate(opportunity),
                self._affordability_gate(opportunity),
                self._net_gate(opportunity),
                self._margin_gate(opportunity),
                self._hourly_gate(opportunity),
                self._horizon_gate(opportunity),
            ),
            treasury=self.treasury,
            record=self.track_record,
        )
        if verdict.approved and self.track_record is not None:
            # Only approved work is recorded, so the history stays a record of
            # what was actually pursued rather than of everything imagined.
            self.track_record.record_opportunity(opportunity)
        return verdict

    def evaluate_many(self, opportunities: list[Opportunity]) -> list[Verdict]:
        return [self.evaluate(opportunity) for opportunity in opportunities]

    def commit(
        self,
        verdict: Verdict,
        *,
        memo: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> Any:
        """Spend the cost of a committed opportunity, and log it.

        Separate from `evaluate` on purpose: judging must be free so it can be
        run speculatively many times, while only committed work costs money.
        """
        if not verdict.approved:
            raise NoAffordabilityError(
                f"refusing to commit a rejected opportunity: {verdict.reason}"
            )
        opportunity = verdict.opportunity
        # Defence in depth. The evidence gate already refused this, so
        # reaching here means the verdict was built elsewhere or the
        # policy was loosened after evaluation. Cheap to re-check, and the
        # failure it prevents is spending real money on an unsubstantiated
        # claim.
        if not opportunity.evidence:
            raise UnsubstantiatedOpportunityError(
                f"refusing to commit {opportunity.opportunity_id} with no evidence"
            )
        if self.treasury is None:
            return None
        if not opportunity.expected_cost.is_positive:
            return None
        return self.treasury.spend(
            opportunity.expected_cost,
            memo=memo or f"opportunity: {opportunity.summary[:80]}",
            metadata=dict(metadata or {})
            | {
                "opportunity_id": opportunity.opportunity_id,
                "category": opportunity.category,
                "ask_price": format(opportunity.ask_price.amount, "f"),
            },
        )


def strict_evaluator(
    treasury: Any = None, track_record: TrackRecord | None = None
) -> OpportunityEvaluator:
    """An evaluator with deliberately demanding defaults.

    Useful when the stakes are real money rather than a rehearsal.
    """
    return OpportunityEvaluator(
        policy=EvaluationPolicy(
            min_evidence=2,
            min_margin_ratio=Decimal("0.50"),
            min_hourly=Decimal(25),
            max_horizon_days=30,
        ),
        treasury=treasury,
        track_record=track_record,
    )
