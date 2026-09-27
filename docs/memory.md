# Retrieval memory

The memory subsystem gives the agent knowledge it was never trained on,
without training anything. Documents are chunked into a persistent BM25 index;
relevant chunks are retrieved at query time and injected as context.

This is the honest way to give a 7B model knowledge of the world on a machine
that cannot fine-tune it. See the "Why not just train it" section at the end.

`memory/__init__.py` was left untouched, so import from the modules directly:

```python
from memory.store import MemoryIndex
from memory.ingest import WebIngestor
from memory.retriever import Retriever
```

## Why BM25 and not embeddings

The usual advice is "embed your documents". Two things rule it out here:

- This machine has a 4GB GeForce 840M (compute capability 5.2). PyTorch 2.x
  CUDA builds require 7.0+, so a local embedding model cannot run here at all.
- A cloud embedding API needs a key and ships the corpus to a third party.

BM25 over an inverted index is a strong retrieval baseline, needs no
dependencies, and runs on CPU in milliseconds. The tokenizer splits
identifiers, so a query for `retry` matches `retry_after_seconds`, and a query
for `HTTPResponse` matches text containing that class.

## Usage

```python
from memory.store import MemoryIndex
from memory.ingest import WebIngestor
from memory.retriever import Retriever

index = MemoryIndex("data/memory.db")

# Local files are free and never touch the treasury.
WebIngestor(index).ingest_paths(Path("docs").rglob("*.md"))

# The web is rate-limited, robots-checked, and metered.
ingestor = WebIngestor(index, treasury=bank, delay=1.0)
ingestor.cost_per_fetch = 0.01
report = ingestor.ingest_urls(["https://example.com/pricing"])
report.indexed, report.refused, report.failed

retriever = Retriever(index, token_budget=1200, max_per_document=2)
context = retriever.retrieve("how is the reserve locked")
```

Then, at query time:

```python
message = retriever.as_system_message(user_question)
messages = ([{"role": "system", "content": message}] if message else []) + history
```

## Design decisions worth knowing

### Absent context produces no message

`as_system_message` returns `None` when nothing relevant was found, rather
than a message saying "no relevant information was found". Injecting a
no-context notice into every prompt teaches the model to hedge even when it
does know the answer. Absence should produce absence.

### The token budget includes citation headers

Each admitted chunk costs `header + body`. Omitting the header from the count
lets the assembled prompt exceed its budget by one line per citation, which
is the kind of drift that quietly crowds out the real instructions.

### Chunks are capped per document

Ten chunks from one verbose page is one source, not ten. Without this, a
single long page crowds out every other document in the corpus.

### Snippets are extracted, not truncated

A 220-token chunk often buries its one relevant sentence in the middle, so the
densest window of sentences is selected. A chunk that does not fit the budget
is skipped entirely rather than cut mid-sentence into a misleading fragment.

### Scores are computed in Python

The query fetches candidate postings by term and ranks them in Python. Pushing
a floating-point scoring function into SQL buys nothing and hides the formula.

### Re-ingesting a URI replaces it

Documents are keyed by a hash of their URI, so refreshing a page updates it
instead of duplicating it, and document frequencies are decremented for the
terms that only the old version contributed.

## robots.txt

`RobotsDisallowedError` is **raised**, not logged and skipped. A refusal is a
result the caller has to handle, so a corpus cannot quietly fill with content a
site asked not to be crawled. `ingest_urls` collects refusals in their own
report bucket, kept separate from network failures, so the two are never
confused.

Other limits in the same spirit: a content-type allowlist, a hard byte ceiling
checked against `Content-Length` and again during the read, and serialized
requests with a politeness delay.

## Learning costs money

With a treasury attached, every fetch spends from `OPERATE` at
`cost_per_fetch`. Research competes with everything else for the same 20%, and
an ingest stops cleanly with `BudgetExhaustedError` when it cannot afford the
next request. The reserve is never touched.

## Why not just train it

Three independent blockers, any one of which is sufficient:

1. **The hardware cannot.** A QLoRA fine-tune of a 7B model needs roughly
   24GB of VRAM. This card has 4GB and predates PyTorch's minimum supported
   CUDA architecture.
2. **The model is not trainable in this form.** `qwen2.5-coder:7b` from Ollama
   is a quantized GGUF. GGUF has no gradients; continued pretraining needs the
   original bf16 weights.
3. **Raw internet text makes models worse.** Web-scale unfiltered text is
   mostly duplicate boilerplate and SEO spam. Quality filtering moves quality
   far more than volume does, and bulk scraping also drags in robots.txt,
   terms-of-service, and copyright exposure.

Retrieval sidesteps all three, and it is reversible: delete the index and the
agent is back to only knowing what it was trained on.
