# OPSMEMORY AI — Pre-Build Research & Architecture Verdict

> This document is the phase-0 research the build is based on. It answers three questions:
> 1. What *is* Hindsight, exactly, and what can it actually do?
> 2. What kind of AI agent architecture wins this hackathon?
> 3. Which algorithms give the fastest, most defensible, most demo-able results?

---

## 1. The memory primitive: Hindsight (verified against real docs, not assumed)

Hindsight is **Vectorize.io's** agent-memory service (`vectorize-io/hindsight`). It is *not* a vector
database and must not be replaced by one. It is a memory **service** with a three-verb API and a
four-network storage model.

### The three operations

| Op | Signature (Python `hindsight-client`) | What it really does |
| --- | --- | --- |
| **Retain** | `client.retain(bank_id, content, context=?, timestamp=?, document_id=?, metadata=?, retain_async=?)` | Sends raw *content*. Hindsight runs **LLM extraction** on it: it decomposes the content into atomic facts, identifies entities, links them into a knowledge graph, and stores vectors + full text + dates. |
| **Recall** | `client.recall(bank_id, query, types=?, budget=?, max_tokens=?, tags=?, tags_match=?)` | Runs **TEMPR** — four retrievers in parallel (semantic, keyword/BM25, graph traversal, temporal) → RRF fusion (`Σ 1/(60+rank)`) → cross-encoder rerank → recency/time/proof boosts → token-budget packing. Returns structured *facts*, not documents. |
| **Reflect** | `client.reflect(bank_id, query, budget=?, context=?)` | An **agent loop**: `search_mental_models → search_observations → recall → expand` → synthesized, cited answer. Shaped by the bank's *mission*, *directives* and *disposition* (skepticism / literalism / empathy, 1–5). |

### The memory types (this is what makes OpsMemory AI possible)

- `world` — objective facts received ("payment-service deployed at 14:02").
- `experience` — **the bank's own actions and outcomes** ("I recommended a restart; it failed verification").
- `observation` — automatically **consolidated, deduplicated, evidence-grounded beliefs**, each
  carrying a proof count and source quotes, *refined rather than overwritten* when new evidence arrives.

**Why this matters for us.** Section 29 of the build spec demands *failure memory* — remembering what
was tried and failed, not just what worked. Hindsight's `experience` type is literally "what the agent
did", and `observation` is the consolidation engine that turns "restart failed 4 times over 30 days"
into a durable, cited belief. We get section 29 and section 33 (pattern detection at the knowledge
level) **for free from the memory layer** instead of hand-rolling it. That is the single most important
architectural insight in this project, and it is why Hindsight stays central rather than decorative.

### Deployment reality

Hindsight needs an LLM **server-side** (structured output required) for retain-extraction and reflect.
Two supported paths:

- **Cloud:** `HINDSIGHT_BASE_URL=https://api.hindsight.vectorize.io` + `HINDSIGHT_API_KEY=hsk_…`
- **Self-host:** `docker run ghcr.io/vectorize-io/hindsight:latest` (API `:8888`, control plane `:9999`)
  or `pip install hindsight-api && hindsight-api`, with `HINDSIGHT_API_LLM_API_KEY` set.

**Consequence for this build:** the memory layer is written behind a narrow interface
(`MemoryStore`) with two implementations — the real Hindsight client and a *faithful* in-process
fallback that mirrors the semantics (fact extraction, entity linking, observation consolidation with
proof counts, RRF-style hybrid retrieval with recency boosts). The app therefore runs, demos and
**is fully testable with zero keys**, and switches to real Hindsight by setting three env vars.
This is not a mock bolted on for tests: the fallback exists so the hackathon demo cannot die on a
venue wifi problem, and so the 40–50 test block of the spec (retain/recall/reflect/learning-update)
is actually executable in CI.

---

## 2. What kind of AI agent should this be? (the central research question)

### The four candidate architectures

| # | Architecture | Latency | Reliability | Judge reaction | Verdict |
| --- | --- | --- | --- | --- | --- |
| A | **Single mega-prompt** ("here are logs, tell me the fix") | 3–8 s | Low — no evidence grounding, hallucinates | "it's a ChatGPT wrapper" → spec §2 violation | ✗ |
| B | **Fully autonomous ReAct loop** (model free-ross roaming tools until it declares done) | 30 s – 4 min, unbounded | Low-med — loops, forgets, non-reproducible | impressive until it hangs on stage | ✗ as primary |
| C | **Fixed pipeline, LLM only for text** | 1–2 s | High | "where's the AI?" — no tool use | ✗ |
| D | **Hybrid: deterministic evidence pipeline + LLM at bounded decision points, with typed tool-calling** | 6–20 s, bounded | **High** | "it actually investigated" | ✅ **chosen** |

### Why D wins — the argument

1. **The spec's own 22 success conditions are a *state machine*, not an open-ended task.** The loop is
   `observe → investigate → recall → reason → recommend → approve → act → verify → learn`. Stages 1–5
   and 8–9 are *deterministic data work* (parse logs, compute robust z-scores, run similarity math,
   measure before/after). Only the **causal reasoning** in the middle is genuinely open-ended.
   Putting deterministic work inside an LLM loop makes it slower, costlier and *less* reliable.
2. **Latency is a judging criterion in practice.** A demo that returns a defensible root cause in
   ~10 s while showing tool activity reads as engineering. One that spins for 90 s reads as broken.
   Target budget (enforced in this codebase):
   `detect ≤ 40 ms · similarity ≤ 15 ms · recall ≤ 900 ms · LLM reasoning ≤ 12 s · verify ≤ 1 s`.
3. **"Show tool activity, do not expose hidden chain-of-thought" (§45) is best satisfied by a real,
   auditable tool-call ledger.** Every tool invocation is persisted to `ai_tool_calls` with inputs,
   outputs, duration and the decision it fed. That is an artefact we can *show*; a raw CoT is not.
4. **Safety.** §20/§21/§22 require a remediation registry, autonomy levels and a safety gate.
   Bounded decision points make those gates enforceable code paths rather than prompt suggestions.
   An autonomous shell-roaming agent is explicitly forbidden by §2 and §49.

### The chosen shape

```
                       ┌──────────────────────────────────────────┐
                       │        INCIDENT ORCHESTRATOR             │
                       │  (state machine, persists every stage)    │
                       └───────────────┬──────────────────────────┘
   DETERMINISTIC                     │                      LLM-BOUNDED
   ─────────────                     │                      ───────────
   Evidence Agent ───────────────┐   │   ┌────────────── Hypothesis Agent
     • log parsing + signatures  │   │   │  (generate N candidates)
     • robust-z / EWMA / CUSUM   │   │   │
     • what-changed timeline     ▼   ▼   ▼
                              ┌──────────────┐
                              │ TOOL REGISTRY│  ← typed, schema-validated,
                              └──────┬───────┘    backend-executed only
                                     │
   Memory Agent ─────────────────────┤   ┌────────────── Remediation Agent
     • recall (Hindsight TEMPR)      │   │  (chooses from REGISTRY, never shell)
     • hybrid similarity + explain   │   │
                                     │   ┌────────────── Postmortem Agent
   Verification Agent ───────────────┘   │  (narrative from verified facts)
     • before/after, honest verdict      └── Learning Agent (retain + reflect)
```

**Decisions the LLM makes:** which hypotheses to raise, which hypothesis the evidence best supports,
which *registered* remediation to propose, and how to phrase the postmortem.
**Decisions the LLM never makes:** what the metrics are, whether a fix worked, whether an action is
allowed, or what gets written to memory.

### Winning strategy beyond architecture (what actually moves judges)

1. **The learning curve must be visible side-by-side.** Two incidents, one screen: Incident 1
   "no prior experience — investigated from scratch", Incident 2 "recalled 3 experiences, knows
   *restart already failed*". This comparison view is the highest-value pixel in the product.
2. **Failure memory is the differentiator.** Every team will demo "it remembered the fix". Verifying
   that *restart alone does not fix a connection leak* — so verification honestly returns
   `VERIFICATION_FAILED`, the runtime ledger records attempt 1 as failed, and Incident 2 recalls it —
   is a genuinely rare demonstration of real learning. The simulator is built so this is causally true.
3. **No fabricated numbers (§24/§49).** Every figure on screen traces to a metrics row or a memory id.
   The UI exposes the provenance of each number. Judges test this.
4. **Explainable similarity, not a scalar.** "Same service ✓, same DB signature ✓, connection spike ✓,
   different environment ✗ (staging vs prod), different deployment ✗" (§11). A single float is
   unfalsifiable; per-feature contributions are evidence.
5. **Idempotent clean-room demo (§52).** One command resets DB + memory + simulator to a known state
   so the loop can be run live, twice, without a dead first act.

---

## 3. Algorithm selection (the "best and fastest" answer)

### 3.1 Anomaly detection — use a *committee*, not a single test

| Detector | Formula | Catches | Complexity |
| --- | --- | --- | --- |
| **Robust z-score (MAD)** | `0.6745·(x − median)/MAD` | outliers without assuming normality; immune to the very spike we're detecting | O(n log n) once per window |
| **Rolling median baseline** | `x` vs `median(w)` + `k·1.4826·MAD(w)` | slow drifts, seasonal-ish shifts | O(n) with a deque |
| **EWMA control** | `z_t = λx_t + (1−λ)z_{t−1}`, ±`L·σ_z` | *gradual* degradation (leaks, memory growth) that never trips a threshold | O(1)/sample |
| **CUSUM / change-point** | `C⁺ = max(0, C⁺ + x − μ − k)` | the *moment* behaviour changed → "what changed?" correlation | O(1)/sample |

**Why MAD over mean/std:** the spec's own note. A single 500 ms→4.8 s spike inflates σ and *hides
itself*. MAD's 50 % breakdown point means the spike cannot mask itself. This is a concrete,
defensible algorithmic choice we can explain in one sentence to a judge.

Detection = weighted vote of the committee + **consecutive-breach debounce** (N of M ticks) so a single
noisy sample does not open an incident. Output is a *severity*, never a causality claim: the engine
emits `correlated_with` / `preceded_by` / `consistent_with` (§8), never `caused_by`.

### 3.2 Log analysis — deterministic first, LLM never

`normalize → parse → template → signature(hash) → frequency → spike`. Variable masking (uuids, ips,
ids, durations, paths) turns `DB connection failed for user 1001|1002|1003` into one signature
`DB connection failed for user <ID>` with `count`, `first_seen`, `last_seen`. Counting 10 000 logs
costs microseconds; the LLM sees only the ranked signature table (§9). **Never ship raw logs to the
model.**

### 3.3 Incident similarity — hybrid with per-feature attribution

Text similarity alone is the classic trap (§11 explicitly forbids it). The score is an explicit
weighted sum over independently computed features, which is also *why it is explainable*:

| Feature | Method | Weight |
| --- | --- | --- |
| Service match | exact / family | 0.14 |
| Environment match | exact | 0.06 |
| Error-signature overlap | weighted Jaccard of signature hashes | 0.24 |
| Metric-pattern similarity | Pearson ρ over aligned robust-z profiles | 0.20 |
| Root-cause category | canonical taxonomy match | 0.16 |
| Deployment relationship | temporal proximity + same change class | 0.12 |
| Symptom text | cosine over TF-IDF (fast, no API call) | 0.08 |

Every feature contributes a signed, human-readable line to the explanation — the exact `✓ similar /
✗ different` list required by §11 — and each contribution is stored so the UI can render it.
Weights live in config so they are tunable, not magic. **No black-box embedding decides the root cause.**

### 3.4 Remediation semantics — the simulator is causal, so verification is honest

The simulated environment defines *what each action actually does* to the service's state:

| Action | Simulated effect | Verifies? |
| --- | --- | --- |
| `restart_service` | clears connections **now**, leak resumes → pool refills within the verification window | **FAILS** on the 2nd check |
| `update_known_safe_configuration` | applies correct pool size + leak fix | **PASSES** |
| `rollback_deployment` | reverts the bad change | **PASSES** for deploy-induced incidents |
| `scale_service` | buys headroom, does not fix the leak | **PARTIAL → FAILS** |
| `clear_safe_cache` | transient relief only | depends on root cause |

This is deliberate: it makes §29 (failure memory) *demonstrable* rather than asserted, and it makes
§23 (a command succeeding ≠ incident fixed) physically true in the demo.

### 3.5 Verification — multi-signal with a real wait

`execute → settle window → {health, connections, error rate, latency, log signatures, API probe} →
compare against the pre-incident baseline that was captured at detection`. Verdict is
`VERIFIED_SUCCESS` only if the *primary* signal for the diagnosed root cause returns to baseline and
no new critical signature appeared. Otherwise `VERIFICATION_FAILED`, and the orchestrator re-enters
investigation with the failure as fresh evidence (the loop in §23/§56).

---

## 4. Cost / latency budget (enforced, not aspirational)

| Stage | Budget | Strategy |
| --- | --- | --- |
| Log + metric ingestion | 40 ms | in-process numpy, zero-copy windows |
| Signature extraction | 20 ms | compiled regex, streaming |
| Similarity vs 60 incidents | 15 ms | vectorised feature matrix, precomputed fingerprints |
| Hindsight recall | 900 ms | `budget="mid"`, `max_tokens` capped, parallel types |
| LLM reasoning round | ≤ 12 s | `max 6 tool rounds`, per-call timeout 30 s, retry ×2 with jitter |
| Verification | 1 s + settle | settle window is *simulated* time (fast-forwardable) |

LLM round cap + retry + malformed-response repair is what keeps the demo bounded; the retry/backoff
and schema-repair paths are unit-tested (spec §30–40).

---

## 5. Explicit decisions taken (and what was rejected)

| Decision | Chosen | Rejected & why |
| --- | --- | --- |
| Memory | Hindsight `retain/recall/reflect` via `MemoryStore` interface | pgvector / "RAG db" — forbidden by spec, and would not give us `experience`+`observation` consolidation |
| LLM role | bounded decision points + typed tools | autonomous shell loop — §2/§49 forbid it, and it is unreproducible on stage |
| Anomaly detection | committee (MAD + rolling + EWMA + CUSUM) | single mean/σ z-score — self-masking on the spikes we care about |
| Similarity | weighted multi-feature + attribution | pure embedding cosine — §11 forbids, and unexplainable |
| Remediation | registry + safety gate + autonomy levels | free-form commands — §20/§49 forbid |
| Verification | multi-signal vs captured pre-incident baseline | "command exited 0" — §23 forbids |
| DB default | SQLite (Postgres-compatible DDL) via `DATABASE_URL` | Docker-only Postgres — no docker on the target machine, and demo must survive |
| Frontend deps | hand-written CSS + SVG charts | chart/DnD libraries — fewer failure modes, and §43 demands restraint |

## 6. Honest scope statement

The full spec is a multi-month product. This build delivers the **complete core learning loop**
end-to-end with real algorithms, real tests and a real memory layer, plus the UI that demonstrates it.
Sections 25–27 (CI/CD + AI code-fix loop) and 90–100 (load/stress) are implemented as first-class
*modules and schemas* with a working narrow slice, and are listed as roadmap in `README.md` rather
than claimed as finished. Nothing in the demo is faked; anything not built is labelled as not built.
