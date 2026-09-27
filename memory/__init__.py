"""Local BM25 retrieval over a SQLite index.

`memory.tokenize`    identifier splitting, stemming, and term frequency.
`memory.chunking`    document splitting into overlapping, token-bounded chunks.
`memory.store`       the index itself: `MemoryIndex`, `Hit`, `IndexStats`.
`memory.ingest`      robots-aware, allowlisted, byte-capped document fetching.
`memory.retriever`   `Retriever` and `Context` - ranked retrieval and budgeting.
"""
