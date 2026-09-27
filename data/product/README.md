# aegis-curated-python 0.1.0

Curated Python instruction/code pairs for supervised fine-tuning.

## What this is

- **400 records** retained from 5,003 read (0.0800 retention)
- Format: JSONL, one record per line
- Fields: `id`, `messages` (user/assistant), `metadata`
- Quality score and matched signals recorded per record in metadata

## Provenance, stated plainly

The upstream source is a **public, freely redistributable dataset**.
This package is not valuable because of access to it. It is valuable
only to the extent that the curation below is real and the signals
are worth paying for. If you can hire someone to redo this curation
for less than the price, do that instead.
## Curation applied

| Reason dropped | Records |
| --- | --- |
| no domain match | 2,352 |
| different domain | 1,836 |
| response too short | 344 |
| below quality floor | 70 |

## Quality signals and weights

| Signal | Weight | Meaning |
| --- | --- | --- |
| has_code_structure | 0.25 | defines a function, class, or import |
| has_error_handling | 0.15 | handles failure paths |
| has_type_hints | 0.10 | annotates signatures |
| has_docstring | 0.10 | documents the module or function |
| has_usage_example | 0.15 | shows how it is called |
| response_substantial | 0.25 | long enough to be complete |

Records scoring below 0.60 were excluded.

## What has NOT been verified

- generated code has not been executed or unit tested
- no human review of correctness
- no check that responses compile against their stated imports

Treat this dataset as untrusted input. Generated code has not been
executed, so it may not run, may not be safe, and may not be correct.

## Sample

`sample.jsonl` contains 100 records drawn across the corpus. Inspect before buying.

## Licence

The upstream source is Apache-2.0. Individual records derive from third-party code whose own licences may apply. Confirm before redistribution. This has NOT been verified by a lawyer.

