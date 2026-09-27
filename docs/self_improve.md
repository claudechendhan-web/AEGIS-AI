# Self-improvement data

`training/self_improve.py` turns the agent's own transcripts into supervised
fine-tuning data. It changes no weights. It produces a dataset you can
fine-tune on later, on hardware that can actually run one.

```powershell
python -m training.self_improve `
    --database data/aegisai.db `
    --output data/training/self_improve.jsonl `
    --manifest data/training/self_improve_manifest.json `
    --max-records 5000
```

Output records match the shape `training/prepare_dataset.py` emits, so both
feed the same downstream tooling:

```json
{
  "id": "self_improve:<message_id>",
  "messages": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ],
  "metadata": {"source": "self_improve", "conversation_id": "..."}
}
```

## Filtering is the entire point

Naively self-training on raw transcripts produces a dataset dominated by the
model's own failures, because failures are the abundant case. So the filter is
deliberately aggressive, and **every rejection carries a machine-readable
reason** in the manifest:

```json
{"reason": "secret_detected", "detail": "openai_key", "record": "m3"}
```

| Reason | Why |
| --- | --- |
| `secret_detected` | a credential appears in the text |
| `tool_protocol_dump` | the response is a raw JSON payload, not an answer |
| `hedging_response` | a refusal or an "I don't know" |
| `response_echoes_instruction` | the answer just restates the question |
| `response_too_short` / `_too_long` | outside the usable length band |
| `instruction_too_short` / `_too_long` | ditto for the prompt |
| `exact_duplicate` / `near_duplicate` | already represented in the dataset |
| `empty_instruction` | no prompt text |

## The secret scanner runs first

This ordering is deliberate and load-bearing. The length filters are cheaper,
so it is tempting to run them first, but then a short exchange containing a
live API key gets dropped as "too short" and **the leak is never reported**. A
credential in the transcript is worth surfacing whether or not the surrounding
exchange was ever usable.

Twelve patterns are covered, including AWS keys and secret assignments, RSA
private key blocks, GitHub tokens, OpenAI and Anthropic keys, Slack tokens,
Stripe keys, Google API keys, JWTs, bearer headers, and generic
`api_key = "..."` assignments.

Treat a `secret_detected` line in the manifest as an incident. It means a live
credential is sitting in your transcript database, which is worth rotating
regardless of what the dataset builder does with it.

## Deduplication

Exact matches first, by a hash over NFKC-normalized text. Normalization
matters more than it looks: the same answer typed with and without combining
accents produces different bytes and would slip past an exact-hash check.

Then near-duplicates, by Jaccard similarity over word shingles at a
configurable threshold (default 0.85). Repeated near-identical answers from
the same conversation would otherwise over-represent one narrow behaviour.

## Splits are deterministic

`train` / `validation` / `test` are assigned by hashing each record's id, not
by position. A split that reshuffles on every run makes it impossible to tell
whether a score changed because the data changed or because the shuffle did.
Defaults are 5% validation, 5% test.

## Privacy

Email addresses in both instruction and response are replaced with `[email]`
(`--strip-emails` behaviour, on by default). This is deliberately minimal.
Before fine-tuning on anything derived from real transcripts, check the
manifest's rejection list and sample the output yourself: a filter that
catches known credential formats is not a privacy policy.

## What this is for

The output is a dataset, not a model. To actually fine-tune you need a GPU this
machine does not have. Rent one (a single L4 or A10G runs a 7B QLoRA job in a
couple of hours for a few dollars), train on the `train` split, and evaluate on
`validation`.

The loop this enables is: run the agent on real work, keep the transcripts,
filter them, and periodically fine-tune. That is what "self-improving" means
in practice, and unlike 24 hours of scraping the internet, the quality signal
comes from work the agent actually did.
