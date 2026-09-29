# How OpsMemory AI uses Hindsight

This document explains exactly how Hindsight is used, where it sits in the loop, what gets written to the bank, and why each choice was made. It is deliberately concrete: every claim below maps to a file and a function you can read.

The short version:

> **Hindsight is the reason this agent gets better.** Remove it and the system still detects incidents and still reasons about them — but every incident starts from zero, and it will recommend the same failed fix forever. Hindsight is what turns a series of one-off investigations into organizational experience.

---

## 1. Where Hindsight sits in the loop

Hindsight is called **three times per incident**, each with a distinct job:

```
OBSERVE ─ UNDERSTAND ─ INVESTIGATE ─▶ RECALL EXPERIENCE ─▶ REASON ─ RECOMMEND
                                          ▲  Hindsight.recall()
                                          │
                                    APPROVE ─▶ ACT ─▶ VERIFY
                                                        │
                                                        ▼
                                              LEARN ─▶ REMEMBER ─▶ IMPROVE
                                                    Hindsight.retain()
                                                          │
                                                          ▼
                                              Hindsight.reflect()  ← "what have we learned?"
```

| Call | When | File |
|---|---|---|
| `recall()` | Before any hypothesis is formed | `app/ai_engine/agents.py` (`ExperienceRecallAgent`), surfaced by the `recall_historical_incidents` tool in `app/ai_engine/tools.py` |
| `retain()` | After verification returns a verdict | `app/domain/incidents.py` + the `remember` tool, driven by the `LearningAgent` |
| `reflect()` | At the close of the incident, and on demand | `app/ai_engine/agents.py` (`LearningAgent`, post-retain) and `app/api/routes/memory.py` |

The ordering is the design. **Recall happens before reasoning, and retain happens after verification.** An agent that recalls after forming a hypothesis is just looking for confirmation. An agent that retains before verifying is writing guesses into organizational memory.

---

## 2. The bank

A single bank holds the organization's SRE experience. It is created once, with a mission and three standing directives:

```python
# app/memory/service.py
BANK_MISSION = (
    "You are the long-term organizational memory of an SRE team. You hold incident "
    "experience, remediation outcomes, deployment lessons and postmortem learning. "
    "Prefer evidence that has been verified end-to-end over evidence that has not. "
    "Treat a remediation as effective only when verification confirmed recovery."
)
```

```python
# app/memory/service.py
BANK_DIRECTIVES = (
    "Always distinguish failed remediation attempts from verified successful ones.",
    "Never assert that a change caused an incident purely from temporal proximity.",
    "Cite the incident identifier for every claim about past experience.",
)
```

The mission is not decoration. It is passed to `create_bank(mission=...)`, and it biases fact extraction at write time — a retain that says "we restarted the service" is extracted in the context of *did that work or not*, which is precisely the distinction the whole project depends on.

| Setting | Value | Where |
|---|---|---|
| Bank id | `opsmemory-org` | `HINDSIGHT_BANK_ID` |
| Base URL | `https://api.hindsight.vectorize.io` (cloud) or `http://localhost:8888` (self-hosted) | `HINDSIGHT_BASE_URL` |
| Disposition | `skepticism=4, literalism=4, empathy=2` | `HindsightStore.ensure_bank()` |

The disposition is deliberately skeptical and literal. SRE postmortems die from over-claiming; a memory system that softens a hard "verification failed" into "the fix was attempted" is worse than no memory at all.

---

## 3. What gets recalled — and what that unlocks

Recall is a single natural-language query built from the incident's fingerprint (`build_recall_query` in `app/memory/documents.py`). The query deliberately mixes three registers, because Hindsight's TEMPR retrieval drives four separate arms at once:

```python
parts.append(
    f"How have we previously resolved incidents on {fingerprint.service} in the "
    f"{fingerprint.environment} environment?"
)                                    # ← embedding arm: the operational concept
if fingerprint.symptom:
    parts.append(f"Symptoms were: {fingerprint.symptom}")   # ← BM25 arm: exact symptom words
if fingerprint.root_cause_category != "unknown":
    parts.append(f"Root cause category: {fingerprint.root_cause_category}")  # ← taxonomy
parts.append(
    "Which remediation attempts failed verification, and which remediation was verified "
    "to resolve it?"
)                                    # ← this is the sentence that makes the whole thing work
```

That last sentence is doing real work. It asks the memory layer a **comparative** question — failed versus succeeded — rather than a lookup. It is the difference between "have we seen this?" and "what should we *stop* doing about this?"

Recall requests all three memory types, so the agent sees both raw experience and consolidated belief:

```python
recall(query=..., types=[WORLD, EXPERIENCE, OBSERVATION], budget="mid", max_tokens=2048)
```

> **Note:** Hindsight rejects queries over 500 tokens, so the adapter truncates the generated query at 1 800 characters before sending (`HindsightStore.recall`). A query that gets rejected is a recall that silently returns nothing, which would look exactly like "we have never seen this" — worth guarding.

### What the agent does with the result

Recall feeds a **separate** reasoning stage. The agent receives:

- the retrieved memories, with their text and type
- the **similarity-scored** historical incidents, with per-feature attribution
- the **failed attempts** recorded against them

and it must cite which memories informed its recommendation. The response structure carries `memory_hits`, `memory_recall_used`, and the exact `explanation`, so a judge — or an SRE at 3am — can see *why* the agent chose what it chose rather than being asked to trust it.

---

## 4. What gets retained — and why failures get equal billing

This is the core of the project. **Every verified outcome is retained, including the failures, and failures are retained the moment the verdict lands** — not at the end of the incident.

The `AttemptRecord.render()` output for a failed attempt:

```
Attempt 1: restart_service (Restart the service)
  — verification FAILED.
  Reason: restart clears the connection pool, but the leak resumes.
  Observed: connection_saturation still climbing 5 minutes after the restart;
            error_rate unchanged at 3.4%
```

That text is worth more than ten successful resolutions, because it is the thing that stops the agent repeating itself.

Incident-level experience is retained as a structured narrative document (`IncidentExperience.render()`), not a JSON blob:

```
INCIDENT #1042 — Connection pool exhaustion on payment-service
Service payment-service in production environment, severity critical, detected 2026-09-29T14:02:11Z.
Time to resolution: 18.4 minutes.
Trigger: deployment v2.41.0 reduced DB_POOL_SIZE from 50 to 8.
Symptoms: connection_saturation climbing past 0.92; error_rate 3.4% on /authorize
Evidence:
  - connection_saturation rose from 0.21 to 0.94 over 14 minutes
  - ...
Remediation history:
  - Attempt 1: restart_service — verification FAILED. ...
  - Attempt 2: update_known_safe_configuration — verified successful.
Root cause: the deployment reduced the pool size below steady-state demand
  (category: configuration_regression)
Verification: 6 checks passed (3 blocking); p95 latency improved 88.0%
```

Rendered as prose on purpose. Hindsight runs fact extraction over the content, and prose with explicit outcomes extracts into far better facts than nested JSON does.

### Deliberately *not* retained

Build spec §12 is explicit that memory is not a database mirror. The document builder omits:

- raw metric samples and log lines (thousands of rows, no future reasoning value)
- internal IDs nobody will ever query by
- anything that has not been verified

Only information with **future reasoning value** is retained. That filter is the difference between a memory layer and a very expensive log sink.

---

## 5. Observations — memory that strengthens instead of being overwritten

Hindsight's `observation` memories are the reason this system improves at the *organization* level rather than only the incident level. Observations are consolidated, deduplicated, evidence-grounded beliefs that carry:

- a **proof count** — how many independent facts support it
- **`source_fact_ids`** — which raw facts it is built from
- a **`superseded_texts`** trail — the earlier, weaker wording, kept rather than deleted

So the bank does not accumulate forty slightly different versions of "connection leaks are bad". It accumulates one belief whose proof count climbs from 1 to 3 to 7 as evidence arrives. That is what a real incident-review process does, and it is why the Memory page shows a `proof_count: 3` badge next to a consolidated belief.

Critically, observations are **refined, not overwritten**. Deleting the previous wording would destroy the audit trail of what the organization believed and when — which is exactly what you need when an old postmortem turns out to have been wrong.

---

## 6. Pattern-level memory

Beyond individual incidents, recurring patterns are themselves retained (`render_pattern_insight`):

```
ORGANIZATIONAL PATTERN OBSERVED: connection pool exhaustion recurs after any deployment
  that lowers DB_POOL_SIZE, regardless of service.
The pattern 'configuration_regression' occurred 6 time(s) in 45 days.
Services affected by this pattern: payment-service, order-service, inventory-service
Remediation actions that have repeatedly failed for this pattern: restart_service
Remediation actions that have worked for this pattern: update_known_safe_configuration
```

This is the highest-leverage memory in the bank: it generalises a lesson across services, so the agent can apply what it learned about `payment-service` to `order-service` without having seen it fail there first. That is organizational learning rather than per-service learning.

---

## 7. Graceful degradation — and why it is honest

If Hindsight is unreachable, the platform does not die and does not pretend. It switches to a local store that implements the **same interface with the same semantics** — fact extraction, world/experience classification, an entity index, four-arm retrieval with reciprocal-rank fusion, recency and proof boosts, and observation consolidation with proof counts — persisting to `backend/.opsmemory/<bank_id>.json`.

Two rules make this safe rather than deceptive:

**The degradation is visible.** `/api/memory/status` reports `active_backend`, `configured_backend`, `degraded`, and the reasons. Any investigation that ran degraded is stamped `MEMORY_DEGRADED` in its own record. You are never shown fallback behaviour labelled as Hindsight.

**Degradation self-heals.** A transient failure at startup used to pin the process to the fallback store *permanently* — which silently defeated the mandatory Hindsight requirement while every screen still said everything was fine. That is precisely the kind of failure that makes a demo dishonest. The service now re-probes and returns to Hindsight once it is reachable again.

For a production deployment where memory is not optional, set `HINDSIGHT_REQUIRED=true` and the platform will **refuse to start** rather than run without it.

---

## 8. Verify it yourself

```bash
# Is the real memory layer actually in use?
curl -s http://127.0.0.1:8000/api/memory/status \
  -H "authorization: Bearer $TOKEN" | python -m json.tool
```

```json
{
  "status": {
    "active_backend": "hindsight",
    "configured_backend": "hindsight",
    "degraded": false,
    "reasons": [],
    "health": {
      "backend": "hindsight",
      "healthy": true,
      "base_url": "https://api.hindsight.vectorize.io",
      "bank_id": "opsmemory-org",
      "api_version": "0.10.1",
      "detail": "connected to Hindsight",
      "counts_available": true,
      "facts": 46,
      "observations": 13,
      "documents": 6,
      "entities": 26,
      "facts_by_type": { "world": 20, "experience": 11, "observation": 13 }
    }
  }
}
```

If `active_backend` and `configured_backend` differ, the app has degraded. `/api/health` carries the same information without needing a token.

```bash
# Read the bank directly, by type
curl -s "http://127.0.0.1:8000/api/memory/items?memory_type=observation&limit=5" \
  -H "authorization: Bearer $TOKEN" | python -m json.tool

# Ask the whole history a question
curl -s -X POST http://127.0.0.1:8000/api/memory/reflect \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"query":"What has historically worked for connection pool exhaustion on payment-service?"}' \
  | python -m json.tool
```

`reflect` is the most convincing single check: it produces an evidence-grounded answer that names the actual incident and the actual failed restart, because it is reading retained experience rather than reasoning from the current prompt.

The **Memory** page in the UI is the same data with the receipts attached — every fact, every observation with its proof count and source fact IDs, and every entity the bank has indexed.

---

## 9. Why these choices, in one paragraph

A memory layer is easy to add and easy to add badly. Three decisions separate a memory layer that makes an agent better from one that makes it confidently wrong: **retain failures with the same weight as successes** (otherwise the agent cannot learn what to stop doing), **retain only after verification** (otherwise the agent poisons its own memory with optimistic guesses), and **consolidate observations rather than overwriting them** (otherwise beliefs have no evidential weight and no audit trail). Everything else in this document is a consequence of those three.
