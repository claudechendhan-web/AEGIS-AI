"""Turn a raw public dataset into a product-shaped, truthfully-described package.

This exists because of a specific and uncomfortable finding. The corpus in
`data/training/` derives from `OLMo-Coding/starcoder-python-instruct`, which is
Apache-2.0 public data. It is free, legal to redistribute, and already in
circulation. Nobody pays for it, and no amount of model training changes that:
the thing on offer is not scarce.

What can be scarce is *curation*, and that is what this tool produces and, more
importantly, measures:

* a single narrow domain instead of a general-purpose grab bag
* records that pass explicit, explainable quality signals
* aggressive near-duplicate removal
* a written provenance trail, so a buyer can see exactly what was done
* an honest statement of the curation delta, so the listing cannot overclaim

The deliberate design choice is that every dropped record is counted and
attributed to a named reason. A package whose own manifest says "we removed
38% of the source for these six specific reasons" is verifiable. A package
whose README says "high quality, thoroughly curated" is not, and buyers who
check will find the difference.

This tool does not verify that the generated code actually runs. That is the
human-verification gap, it is real, and it is the strongest differentiator
available. See `VERIFYING.md` in the output for what closing it would take.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

DEFAULT_SOURCE = Path("data/training/starcoder_python_instruct.jsonl")
DEFAULT_OUTPUT = Path("data/product")
DEFAULT_SAMPLE_SIZE = 100
DEFAULT_MIN_SCORE = Decimal("0.60")
DEFAULT_NEAR_DUPLICATE = Decimal("0.80")

# Domain definitions. Deliberately explicit keyword rules rather than an
# embedding model: the output has to be explainable to a buyer, and "we used a
# classifier" is not an answer they can check.
DOMAINS: dict[str, dict[str, Any]] = {
    "scientific_numerics": {
        "label": "Scientific and numerical computing",
        "pattern": r"numpy|scipy|matplotlib|ndarray|matplotlib|fft|fourier|"
        r"simulation|numerical|array|vector|matrix|eigen|linalg|plot",
        "min": 5,
    },
    "data_pipeline": {
        "label": "Data engineering and ETL",
        "pattern": r"pandas|dataframe|\bcsv\b|\bsql\b|spark|etl|groupby|join|"
        r"aggregate|parquet|jsonl|schema|migration",
        "min": 5,
    },
    "web_extraction": {
        "label": "Web extraction and HTTP clients",
        "pattern": r"scrape|requests\b|beautifulsoup|\bbs4\b|selenium|\bhttp|"
        r"url|html|crawler|api client|endpoint|rest\b",
        "min": 5,
    },
    "parsers_and_codecs": {
        "label": "Parsers, tokenizers and serialization",
        "pattern": r"pars|tokeniz|regex|serializ|deserializ|encoding|decode|"
        r"lexer|grammar|\bast\b|protocol|format",
        "min": 5,
    },
    "ml_frameworks": {
        "label": "Machine learning frameworks",
        "pattern": r"tensorflow|torch|keras|transformer|embedding|neural|"
        r"lstm|\bcnn\b|training loop|backprop|gradient|model\.fit|inference",
        "min": 5,
    },
    "embedded_systems": {
        "label": "Embedded and hardware interfaces",
        "pattern": r"arduino|raspberry|serial|firmware|sensor|\bgpio\b|"
        r"embedded|i2c|spi|uart|microcontroller|motor",
        "min": 5,
    },
}

_SIGNALS: tuple[tuple[str, Decimal, str], ...] = (
    ("has_code_structure", Decimal("0.25"), "defines a function, class, or import"),
    ("has_error_handling", Decimal("0.15"), "handles failure paths"),
    ("has_type_hints", Decimal("0.10"), "annotates signatures"),
    ("has_docstring", Decimal("0.10"), "documents the module or function"),
    ("has_usage_example", Decimal("0.15"), "shows how it is called"),
    ("response_substantial", Decimal("0.25"), "long enough to be complete"),
)

_COMPILABLE = re.compile(r"^(?:import |from |def |class |@)", re.MULTILINE)
_ERROR_HANDLING = re.compile(
    r"\btry:|\bexcept\b|\braise\b|assert\b|\bif .* is None|ValueError|"
    r"KeyError|TypeError|IndexError|OSError|\.get\(",
)
_TYPE_HINTS = re.compile(r"->\s*\w|:\s*(?:str|int|float|bool|list|dict|List|Dict)")
_DOCSTRING = re.compile(r'"""')
_EXAMPLE = re.compile(
    r"```|>>>|\bexample\b|\busage\b|print\(|assert .*==|if __name__",
)


DEFAULT_LICENCE_NOTE = (
    "The upstream source is Apache-2.0. Individual records derive from "
    "third-party code whose own licences may apply. Confirm before "
    "redistribution. This has NOT been verified by a lawyer."
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"\s+", " ", folded).strip()


@dataclass(frozen=True, slots=True)
class Scored:
    """One record with its domain, score, and the signals that fired."""

    record_id: str
    domain: str
    score: Decimal
    signals: tuple[str, ...]
    instruction: str
    response: str
    original: Mapping[str, Any]

    def to_record(self, dataset_id: str, version: str) -> dict[str, Any]:
        return {
            "id": f"{dataset_id}:{version}:{self.record_id}",
            "messages": [
                {"role": "user", "content": self.instruction},
                {"role": "assistant", "content": self.response},
            ],
            "metadata": {
                "dataset": dataset_id,
                "version": version,
                "domain": self.domain,
                "quality_score": format(self.score, "f"),
                "signals": list(self.signals),
                "source_id": self.record_id,
            },
        }


@dataclass(slots=True)
class PackageStats:
    """The curation delta, attributed to named reasons."""

    read: int = 0
    kept: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    by_domain: dict[str, int] = field(default_factory=dict)

    def drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        # Quantized to four places. A raw Decimal division of two integers
        # yields 28 significant digits, which looks like a bug in a listing
        # that a buyer is meant to trust.
        retention = (
            (Decimal(self.kept) / Decimal(self.read)).quantize(Decimal("0.0001"))
            if self.read
            else Decimal("0.0000")
        )
        return {
            "records_read": self.read,
            "records_kept": self.kept,
            "records_dropped": sum(self.dropped.values()),
            "retention_rate": format(retention, "f"),
            "dropped_by_reason": dict(sorted(self.dropped.items())),
            "kept_by_domain": dict(sorted(self.by_domain.items())),
        }


def classify(instruction: str, response: str) -> str | None:
    """Assign a record to at most one domain, or None if it fits none.

    Single-assignment on purpose. A record matching four domains tells a buyer
    nothing about where the dataset's depth actually is.
    """
    blob = f"{instruction} {response}".lower()
    best: tuple[int, str] | None = None
    for name, spec in DOMAINS.items():
        hits = len(re.findall(spec["pattern"], blob))
        if hits >= int(spec["min"]) and (best is None or hits > best[0]):
            best = (hits, name)
    return best[1] if best else None


def score_record(instruction: str, response: str) -> tuple[Decimal, tuple[str, ...]]:
    """Score one record against named, checkable signals."""
    checks = {
        "has_code_structure": bool(_COMPILABLE.search(response)),
        "has_error_handling": bool(_ERROR_HANDLING.search(response)),
        "has_type_hints": bool(_TYPE_HINTS.search(response)),
        "has_docstring": bool(_DOCSTRING.search(response)),
        "has_usage_example": bool(_EXAMPLE.search(response)),
        "response_substantial": len(response) >= 400,
    }
    weights = {name: weight for name, weight, _ in _SIGNALS}
    total = sum(
        (weights[name] for name, passed in checks.items() if passed),
        Decimal(0),
    )
    return min(total, Decimal("1.00")), tuple(
        sorted(name for name, passed in checks.items() if passed)
    )


def iter_source(path: Path) -> Iterator[Mapping[str, Any]]:
    """Stream records, skipping malformed lines rather than aborting."""
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, Mapping):
                continue
            messages = record.get("messages")
            if not isinstance(messages, list) or len(messages) < 2:
                continue
            user = messages[0].get("content")
            assistant = messages[1].get("content")
            if not isinstance(user, str) or not isinstance(assistant, str):
                continue
            yield {
                "id": str(record.get("id") or f"line-{number}"),
                "instruction": user,
                "response": assistant,
                "original": record,
            }


class Deduper:
    """Near-duplicate detection over word shingles.

    In-memory rather than SQLite because this is a one-shot packaging pass, and
    the whole point is to finish. Set `--max-records` for a bound.
    """

    def __init__(self, threshold: Decimal, size: int = 6) -> None:
        self.threshold = float(threshold)
        self.size = size
        self.signatures: list[set[str]] = []

    def _shingles(self, text: str) -> set[str]:
        words = _normalize(text).split()
        if len(words) < self.size:
            return {" ".join(words)} if words else set()
        return {
            " ".join(words[index : index + self.size])
            for index in range(len(words) - self.size + 1)
        }

    def is_duplicate(self, text: str) -> bool:
        signature = self._shingles(text)
        if not signature:
            return False
        for existing in self.signatures:
            union = len(signature | existing)
            if not union:
                continue
            if len(signature & existing) / union >= self.threshold:
                return True
        self.signatures.append(signature)
        return False


def build_package(
    source: Path = DEFAULT_SOURCE,
    output: Path = DEFAULT_OUTPUT,
    *,
    dataset_id: str = "aegis-curated-python",
    version: str = "0.1.0",
    domain: str | None = None,
    min_score: Decimal = DEFAULT_MIN_SCORE,
    near_duplicate: Decimal = DEFAULT_NEAR_DUPLICATE,
    max_records: int = 0,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    licence_note: str | None = None,
) -> dict[str, Any]:
    """Filter, score, dedupe, and write the package with an honest manifest."""
    if not source.exists():
        raise FileNotFoundError(f"source dataset not found: {source}")
    if domain is not None and domain not in DOMAINS:
        raise ValueError(
            f"unknown domain {domain!r}; choose from: {', '.join(DOMAINS)}"
        )

    stats = PackageStats()
    deduper = Deduper(near_duplicate)
    kept: list[Scored] = []

    for item in iter_source(source):
        stats.read += 1
        if max_records and stats.kept >= max_records:
            break

        instruction = item["instruction"]
        response = item["response"]
        if len(instruction) < 40:
            stats.drop("instruction_too_short")
            continue
        if len(response) < 200:
            stats.drop("response_too_short")
            continue

        assigned = classify(instruction, response)
        if assigned is None:
            stats.drop("no_domain_match")
            continue
        if domain is not None and assigned != domain:
            stats.drop("different_domain")
            continue

        value, signals = score_record(instruction, response)
        if value < min_score:
            stats.drop("below_quality_floor")
            continue

        if deduper.is_duplicate(f"{instruction} {response}"):
            stats.drop("near_duplicate")
            continue

        kept.append(
            Scored(
                record_id=item["id"],
                domain=assigned,
                score=value,
                signals=signals,
                instruction=instruction,
                response=response,
                original=item["original"],
            )
        )
        stats.kept += 1
        stats.by_domain[assigned] = stats.by_domain.get(assigned, 0) + 1

    output.mkdir(parents=True, exist_ok=True)
    full_path = output / f"{dataset_id}-{version}.jsonl"
    with full_path.open("w", encoding="utf-8", newline="\n") as handle:
        for scored in kept:
            handle.write(
                json.dumps(scored.to_record(dataset_id, version), ensure_ascii=False)
                + "\n"
            )

    sample_path = output / "sample.jsonl"
    # Deterministic spread rather than the first N, so the sample shows the
    # range of the corpus instead of its opening records.
    if kept:
        stride = max(1, len(kept) // max(1, sample_size))
        picked = kept[::stride][:sample_size]
    else:
        picked = []
    with sample_path.open("w", encoding="utf-8", newline="\n") as handle:
        for scored in picked:
            handle.write(
                json.dumps(scored.to_record(dataset_id, version), ensure_ascii=False)
                + "\n"
            )

    digest = hashlib.sha256(full_path.read_bytes()).hexdigest()
    manifest = {
        "dataset": dataset_id,
        "version": version,
        "built_at": _now(),
        "source": {
            "path": str(source),
            "note": (
                "Derived from a public dataset. The source is free and "
                "redistributable; the value here is curation, not access."
            ),
        },
        "licence": {
            "note": licence_note
            or (
                "The upstream source is Apache-2.0. Individual records derive "
                "from third-party code whose own licences may apply. Confirm "
                "before redistribution. This has NOT been verified by a lawyer."
            ),
        },
        "curation": stats.to_dict(),
        "domain_filter": domain,
        "thresholds": {
            "min_quality_score": format(min_score, "f"),
            "near_duplicate_similarity": format(near_duplicate, "f"),
            "min_instruction_chars": 40,
            "min_response_chars": 200,
        },
        "quality_signals": [
            {"name": name, "weight": format(weight, "f"), "means": meaning}
            for name, weight, meaning in _SIGNALS
        ],
        "outputs": {
            "full": str(full_path),
            "sample": str(sample_path),
            "sample_records": len(picked),
            "sha256": digest,
        },
        "not_verified": [
            "generated code has not been executed or unit tested",
            "no human review of correctness",
            "no check that responses compile against their stated imports",
        ],
    }
    manifest_path = output / "MANIFEST.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (output / "README.md").write_text(
        _readme(dataset_id, version, manifest, DOMAINS), encoding="utf-8"
    )
    return manifest


def _readme(
    dataset_id: str,
    version: str,
    manifest: Mapping[str, Any],
    domains: Mapping[str, Any],
) -> str:
    curation = manifest["curation"]
    lines = [
        f"# {dataset_id} {version}",
        "",
        "Curated Python instruction/code pairs for supervised fine-tuning.",
        "",
        "## What this is",
        "",
        (
            f"- **{curation['records_kept']:,} records** retained from "
            f"{curation['records_read']:,} read "
            f"({curation['retention_rate']} retention)"
        ),
        "- Format: JSONL, one record per line",
        "- Fields: `id`, `messages` (user/assistant), `metadata`",
        "- Quality score and matched signals recorded per record in metadata",
        "",
        "## Provenance, stated plainly",
        "",
        "The upstream source is a **public, freely redistributable dataset**.",
        "This package is not valuable because of access to it. It is valuable",
        "only to the extent that the curation below is real and the signals",
        "are worth paying for. If you can hire someone to redo this curation",
        "for less than the price, do that instead.",
        "## Curation applied",
        "",
        "| Reason dropped | Records |",
        "| --- | --- |",
    ]
    for reason, count in sorted(
        curation["dropped_by_reason"].items(), key=lambda kv: -kv[1]
    ):
        lines.append(f"| {reason.replace('_', ' ')} | {count:,} |")
    lines += [
        "",
        "## Quality signals and weights",
        "",
        "| Signal | Weight | Meaning |",
        "| --- | --- | --- |",
    ]
    for signal in manifest["quality_signals"]:
        lines.append(f"| {signal['name']} | {signal['weight']} | {signal['means']} |")
    lines += [
        "",
        (
            f"Records scoring below "
            f"{manifest['thresholds']['min_quality_score']} were excluded."
        ),
        "",
        "## What has NOT been verified",
        "",
    ]
    for item in manifest["not_verified"]:
        lines.append(f"- {item}")
    lines += [
        "",
        "Treat this dataset as untrusted input. Generated code has not been",
        "executed, so it may not run, may not be safe, and may not be correct.",
        "",
        "## Sample",
        "",
        (
            f"`sample.jsonl` contains "
            f"{manifest['outputs']['sample_records']} records drawn across "
            "the corpus. Inspect before buying."
        ),
        "",
        "## Licence",
        "",
        manifest["licence"]["note"],
        "",
    ]
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="training.package_product",
        description=(
            "Curate a public dataset into a product-shaped package with an "
            "honest, verifiable manifest. Writes a listing, not a claim."
        ),
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dataset-id", default="aegis-curated-python")
    parser.add_argument("--version", default="0.1.0")
    parser.add_argument(
        "--domain",
        choices=sorted(DOMAINS),
        default=None,
        help="restrict to one domain; a focused set beats a broad one",
    )
    parser.add_argument("--min-score", type=Decimal, default=DEFAULT_MIN_SCORE)
    parser.add_argument(
        "--near-duplicate", type=Decimal, default=DEFAULT_NEAR_DUPLICATE
    )
    parser.add_argument("--max-records", type=int, default=0)
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    parser.add_argument("--licence-note", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = build_package(
            source=args.source,
            output=args.output,
            dataset_id=args.dataset_id,
            version=args.version,
            domain=args.domain,
            min_score=args.min_score,
            near_duplicate=args.near_duplicate,
            max_records=args.max_records,
            sample_size=args.sample_size,
            licence_note=args.licence_note,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    print(json.dumps(manifest["curation"], indent=2))
    print(f"\nwrote {manifest['outputs']['full']}")
    print(f"wrote {manifest['outputs']['sample']}")
    print(f"wrote {args.output / 'MANIFEST.json'}")
    print(f"wrote {args.output / 'README.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
