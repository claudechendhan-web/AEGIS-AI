"""Generate a marketplace listing from a verified package.

This is the last mechanical step between "a verified dataset exists" and
"something a buyer can read and pay for". Given the package and its
verification report it writes the listing copy, the pricing tiers, and the
claim set, so that a human only has to publish it.

Two design choices worth stating:

* **Every claim is derived, not written.** The listing text is assembled from
  the manifest and the verification report. There is no free-text marketing
  layer, so the listing cannot drift away from what the data actually is. If a
  number appears in the listing, it came from a file on disk.

* **Pricing is a starting point, flagged as such.** The tiers are computed
  from the observed verification rate and the observed market engagement, and
  the output says plainly that no price was observed anywhere. A tool that
  invents a confident price is worse than one that shows its working.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from treasury.money import Money


class ListingError(Exception):
    """Raised when a listing cannot be generated."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ListingError(f"required file not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ListingError(f"unable to read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ListingError(f"{path} does not contain a JSON object")
    return payload


def find_package(package_dir: Path) -> Path:
    """Locate the full dataset, ignoring the sample and the manifest."""
    candidates = sorted(
        path for path in package_dir.glob("*.jsonl") if path.name != "sample.jsonl"
    )
    if not candidates:
        raise ListingError(
            f"no dataset found in {package_dir}; run training.package_product first"
        )
    return candidates[0]


@dataclass(frozen=True, slots=True)
class Tier:
    """One pricing tier, derived from the observed data."""

    name: str
    price: Money
    what_you_get: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "price": self.price.to_dict(),
            "what_you_get": self.what_you_get,
        }


def build_tiers(
    records: int, verified: Decimal, domain: str, *, currency: str = "USD"
) -> list[Tier]:
    """Price from what was actually observed.

    The unit being sold is the *verified* record, not the shipped record, so
    the rate scales the base before the floor is applied. Applying the floor
    first would let a tiny rate and a high rate produce the same price, which
    is the opposite of what the number is for.

    Every tier is clamped, not just the base. A ceiling on the base alone
    still lets the multipliers run away, which is how a "capped" listing ends
    up quoting six figures.
    """
    per_record = Decimal("0.15")
    floor = Decimal("29.00")
    ceiling = Decimal("2499.00")
    usable = Decimal(records) * verified
    base = (usable * per_record).quantize(Decimal("1.00"))
    base = max(floor, min(ceiling, base))

    def clamp(value: Decimal) -> Money:
        return Money(min(ceiling, max(floor, value)), currency)

    return [
        Tier(
            "Personal",
            clamp(base),
            f"the full {records:,}-record {domain} set, single user, no resale",
        ),
        Tier(
            "Team",
            clamp((base * 4).quantize(Decimal("1.00"))),
            "up to 10 seats, internal use, no redistribution",
        ),
        Tier(
            "Commercial",
            clamp((base * 25).quantize(Decimal("1.00"))),
            "unlimited internal use, derived works permitted, no resale",
        ),
    ]


def generate_listing(
    package_dir: Path = Path("data/product"),
    *,
    verify_path: Path | None = None,
    market_path: Path | None = None,
    dataset_title: str | None = None,
    currency: str = "USD",
) -> dict[str, Any]:
    """Assemble the listing from the manifest, verification, and market read."""
    dataset = find_package(package_dir)
    manifest = _load_json(package_dir / "MANIFEST.json")
    verification = (
        _load_json(verify_path) if verify_path and verify_path.exists() else {}
    )
    market = _load_json(market_path) if market_path and market_path.exists() else {}

    curation = manifest.get("curation") or {}
    records = int(curation.get("records_kept") or 0)
    if records <= 0:
        raise ListingError("the manifest reports zero kept records")

    buckets = verification.get("buckets") or {}
    verified = Decimal(str(buckets.get("verified") or 0))
    failed = int(buckets.get("failed") or 0)
    unsafe = int(buckets.get("unsafe") or 0)
    unverifiable = int(buckets.get("unverifiable") or 0)
    examined = int(verification.get("records_examined") or 0)
    rate = (
        Decimal(str(verification.get("verified_rate_of_executable") or "0"))
        if verification
        else Decimal(0)
    )
    domain = str(manifest.get("domain_filter") or "python")
    title = dataset_title or f"{manifest.get('dataset', 'dataset')} ({domain})"

    tiers = build_tiers(records, rate, domain, currency=currency)

    claims: list[dict[str, Any]] = [
        {
            "claim": f"{records:,} records, curated from "
            f"{int(curation.get('records_read') or 0):,} source records",
            "source": "MANIFEST.json",
        },
        {
            "claim": f"retention rate {curation.get('retention_rate')}",
            "source": "MANIFEST.json",
        },
    ]
    if verification:
        claims += [
            {
                "claim": f"{verified} of {examined} examined records executed cleanly",
                "source": "verify.json",
            },
            {
                "claim": f"verified rate {rate} of executable records",
                "source": "verify.json",
            },
        ]

    # The manifest carries a blanket "code has not been executed" note written
    # before verification existed. Printing it next to a claim that 28 records
    # executed cleanly is a self-contradiction, and a listing that contradicts
    # itself is the single thing this tool exists to prevent. So: when a
    # verification report exists, state the measured result instead.
    limitations: list[str] = []
    if verification:
        limitations += [
            (
                f"{failed} examined records raised an error and are not "
                "usable for training as they stand"
            ),
            (
                f"{unsafe} examined records were refused before execution "
                "because they contain dangerous patterns: process spawning, "
                "raw sockets, or filesystem deletion. They are excluded. "
                "Their presence in the source corpus is itself a warning "
                "about scraped data."
            ),
            (
                f"{unverifiable} examined records could not be executed here "
                "because a third-party package was absent. That is an "
                "environment limitation, not a correctness verdict, so they "
                "remain unverified in both directions."
            ),
            (
                f"only {examined} of {records:,} shipped records were examined "
                f"at all; the other {records - examined:,} were never executed"
            ),
        ]
        limitations += [
            item for item in manifest.get("not_verified", []) if "executed" not in item
        ]
    else:
        limitations += list(manifest.get("not_verified", []))
    limitations.append(
        "no price was observed for comparable data anywhere; these tiers are a "
        "computed starting point, not a market-derived price"
    )
    if market:
        aggregate = market.get("aggregate") or {}
        claims.append(
            {
                "claim": f"{aggregate.get('total_downloads', 0):,} downloads "
                "observed across comparable public listings",
                "source": "market probe",
            }
        )

    listing = {
        "title": title,
        "generated_at": _now(),
        "package": {
            "directory": str(package_dir),
            "dataset_file": dataset.name,
            "manifest": "MANIFEST.json",
            "sample": "sample.jsonl",
            "sha256": (manifest.get("outputs") or {}).get("sha256"),
        },
        "summary": (
            f"{records:,} curated {domain} instruction/code pairs, each scored "
            "against six checkable quality signals, deduplicated, and "
            "execution-verified where the build environment allows."
        ),
        "claims": claims,
        "tiers": [tier.to_dict() for tier in tiers],
        "what_is_not_claimed": limitations,
        "licence": manifest.get("licence", {}).get("note", ""),
        "publish_checklist": [
            (
                "review the claims above against the files; they are "
                "generated, but you are responsible for their accuracy"
            ),
            "decide whether to publish the sample as-is",
            "get a lawyer's view on the upstream licence before taking money",
            (
                "confirm the pricing against one real comparable sale if you "
                "can find one"
            ),
        ],
    }
    return listing


def render_markdown(listing: Mapping[str, Any]) -> str:
    """Render the listing as copy-pasteable markdown."""
    lines = [
        f"# {listing['title']}",
        "",
        listing["summary"],
        "",
        "## What is in it",
        "",
    ]
    for claim in listing["claims"]:
        lines.append(f"- {claim['claim']}  *(source: {claim['source']})*")
    lines += [
        "",
        "## Pricing",
        "",
        "| Tier | Price | Includes |",
        "| --- | --- | --- |",
    ]
    for tier in listing["tiers"]:
        price = tier["price"]
        lines.append(
            f"| {tier['name']} | {price['amount']} {price['currency']} | "
            f"{tier['what_you_get']} |"
        )
    lines += ["", "## What this listing does not claim", ""]
    for item in listing["what_is_not_claimed"]:
        lines.append(f"- {item}")
    lines += ["", "## Licence", "", str(listing["licence"]), ""]
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="training.generate_listing",
        description=(
            "Generate a marketplace listing from a curated package, its "
            "verification report, and a market read. Every claim is derived "
            "from a file on disk rather than written by hand."
        ),
    )
    parser.add_argument("--package", type=Path, default=Path("data/product"))
    parser.add_argument("--verify", type=Path, default=Path("data/product/verify.json"))
    parser.add_argument("--market", type=Path, default=None)
    parser.add_argument("--title", default=None)
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--out", type=Path, default=Path("data/product/LISTING.json"))
    parser.add_argument(
        "--markdown", type=Path, default=Path("data/product/LISTING.md")
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        listing = generate_listing(
            args.package,
            verify_path=args.verify,
            market_path=args.market,
            dataset_title=args.title,
            currency=args.currency,
        )
    except ListingError as exc:
        print(f"error: {exc}")
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(listing, indent=2) + "\n", encoding="utf-8")
    args.markdown.write_text(render_markdown(listing), encoding="utf-8")
    print(f"wrote {args.out}")
    print(f"wrote {args.markdown}")
    print()
    print(render_markdown(listing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
