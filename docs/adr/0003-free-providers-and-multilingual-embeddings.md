---
status: accepted
---

# Free-only provider stack, per-role routing, and multilingual embeddings

Only free LLM tiers are available, and the goal is to **prove the differentiator**
(per-customer grounding + Hindi/Hinglish explanation), so models are assigned
**per role** rather than via one global chain: Gemini Flash (free) for
synthesis/RAG explanation (Hindi-strong → explanation-groundedness), Groq/Cerebras
Llama (free, fastest) for the latency-critical triage *intent classify*, Sarvam for
**language detection only**, and a **pinned** model for the eval judge (no free
fallback — a judge that swaps mid-suite makes cross-version metrics
incomparable). Embeddings use a **local multilingual model (multilingual-e5)** on
the live path; the existing `HashingEmbedder` is demoted to the offline/CI floor
because a hashing trick has no cross-lingual semantics and would make per-customer
Hindi retrieval — the whole differentiator — return garbage.

## Considered Options

- **Keep the deterministic stub/regex/HashingEmbedder system as-is** — rejected:
  it runs and passes its own eval, but cannot demonstrate cross-lingual
  per-customer grounding by construction.
- **One global LLM chain for every agent** (current code) — rejected: triage needs
  speed, synthesis needs Hindi quality, the judge needs stability; one chain can't
  serve all three.
- **Paid frontier (Opus/Sonnet) for synthesis** — rejected: no budget; free
  Gemini Flash covers the multilingual reasoning need.

## Consequences

- Free-tier **rate limits**, not dollars, are the scarce resource — the eval suite
  must throttle/batch and lean on the deterministic rubric judge for CI; the full
  real-model suite is not run per commit. "Cost" is tracked as tokens + rate-limit
  budget (`cost_usd` kept as a 0-valued forward-compat column).
- The provider abstraction stays; a future paid swap is a config + rate-table
  change. The Anthropic provider remains an optional fallback only if a key
  appears, never the default.
- Most agents use **no LLM** (supervisor, gates, action, domain map are
  deterministic) — the free stack only powers triage-classify, language detect,
  synthesis, and the judge.
