# Web tools

The agent can reach the internet live. `web_search` and `web_fetch` are
ordinary `Tool` implementations, so they register in `ToolRegistry`, require
`Capability.NETWORK`, and are dispatched by the existing agent loop with no
changes to it.

```python
from tools.permissions import Capability, PermissionPolicy
from tools.registry import ToolRegistry
from tools.web import build_web_tools
from treasury import Money, Treasury

with Treasury("data/treasury.db") as bank:
    bank.earn(Money.parse("20.00", "USD"), memo="float")

    registry = ToolRegistry()
    registry.permission_policy = PermissionPolicy(
        [Capability.READ_ONLY, Capability.NETWORK]
    )
    for tool in build_web_tools(treasury=bank, cost_per_request=0.02):
        registry.register(tool)

    result = registry.execute(
        "web_search",
        {"query": "sqlite wal mode", "limit": 3},
        permission_policy=registry.permission_policy,
    )
```

Inside the agent loop, nothing special is needed — the tools advertise
themselves through the normal schema path:

```python
from agents.loop import AgentLoop

loop = AgentLoop(client, registry, max_steps=8)
outcome = loop.run("What changed in Python 3.14? Use the web.")
```

```
step 1: tool_use    tools=['web_search']   obs_chars=555
step 2: tool_use    tools=['web_fetch']    obs_chars=926
step 3: none        tools=-               obs_chars=0
operate: 0.98000000 USD   reserve: 4.00000000 USD (untouched)
```

## Why fetching is not reimplemented

`web_fetch` delegates to `memory.ingest.WebIngestor` rather than opening its
own socket. That means robots.txt, the content-type allowlist, the byte
ceiling, the politeness delay, and the treasury metering are the same code
already exercised by the harvest worker. One implementation, one set of
guarantees.

Both tools share a single ingestor, so the robots.txt cache and the delay are
shared too. Two tools with separate clients would each keep their own delay
and could together hammer a host.

## Search providers, and the honest constraint

General web search has **no free, keyless, official API**. The providers that
appear to offer one — scraping DuckDuckGo's or Bing's HTML endpoints — breach
their terms of service. Building on that gets an agent blocked and makes the
project unusable for anyone else, so it is not done here.

| Provider | Key needed | Notes |
| --- | --- | --- |
| `wikipedia` | no | Public MediaWiki API. Default. |
| `brave` | `BRAVE_SEARCH_API_KEY` | Generous free tier. |
| `tavily` | `TAVILY_API_KEY` | Cleaned excerpts; best fit for agents. |
| `serper` | `SERPER_API_KEY` | Google results. |
| `bing` | `BING_SEARCH_API_KEY` | Microsoft endpoint. |

Selection is automatic: an explicit `provider=`, then any configured key in
that order, then Wikipedia. `python`-side, `describe_providers()` reports what
is configured right now, and `web_search` returns it in metadata on every
call.

A missing key raises `SearchCredentialsMissingError` rather than returning
empty results, so "no key configured" is never mistaken for "nothing exists on
the web".

## Failures stay visible

This is worth stating because it was a real bug. `ToolResult.failed()` returns
an empty output mapping, but `ToolRegistry.execute` validates output against
`output_schema` for **every** result, including failures. An empty mapping
therefore fails validation, and the registry raises `InvalidToolOutputError` —
which hides the actual cause. "robots.txt says no" became an unhelpful
schema error.

The fix is in these tools, not in the registry: a failure emits a
schema-shaped payload carrying the error, with `success=False`.

```
budget   ->  cannot afford to fetch https://example.com/; spendable too low
robots   ->  robots.txt disallows https://site.test/private/doc
input    ->  url must start with http:// or https://
too_large->  https://site.test/big exceeded the 100 byte limit
search   ->  brave search needs an API key. Set BRAVE_SEARCH_API_KEY ...
```

Each carries `metadata["kind"]` so a caller can branch on the cause without
parsing the message.

## Cost and permissions

Every `web_fetch` is charged `cost_per_request` to the **spendable** envelope
before the request is attempted. When the agent cannot afford the next fetch
it gets a `budget` failure and stops, rather than starting something it cannot
pay for. The reserve is never touched, and that is asserted in the tests.

`web_search` is unmetered by default because the keyless provider is a public
API; pass `cost_per_request` if you want searches billed too.

Permissions are enforced before any of this runs. Without `NETWORK` in the
policy, both tools raise `PermissionDeniedError` — asserted for each tool
separately, because a tool that declares a capability and can be called
without it is not a permission system.

## Input and output limits

`web_fetch` accepts `max_chars` (200 to 50000, default 6000) and reports
`truncated` honestly rather than silently returning a partial page. `web_search`
caps `limit` at 20. Both schemas set `additionalProperties: false`, so a model
inventing an argument is rejected by the validator rather than ignored.

Note that a real model has to be pulled before the loop can drive these:

```powershell
ollama pull qwen2.5-coder:7b
```

That is 4GB+ of weights, and on this hardware it will run on CPU. The
integration test above uses a scripted stand-in for the model, which is why it
proves the wiring without a multi-gigabyte download.
