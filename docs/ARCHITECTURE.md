# Architecture

How OpsMemory AI is put together, and why each layer is where it is.

---

## The one design rule

Build spec §3 proposed four architectures. This project implements **Option D — a hybrid**: a deterministic evidence pipeline with an LLM at *bounded decision points*.

The rule that follows from it:

> **The LLM decides judgement calls. Code decides facts.**

Concretely, the model chooses hypotheses, draws the conclusion, picks a registered remediation, and writes the postmortem prose. It never decides:

- what a metric value is
- whether a remediation improved anything
- whether an incident is resolved
- what enters organizational memory

Every one of those is computed from telemetry by code. This is why the system can report that its own recommended fix **failed** — the model is not grading its own homework.

Rejected alternatives, and why (full reasoning in [RESEARCH.md](RESEARCH.md)):

| Approach | Why not |
|---|---|
| One mega-prompt | Cannot show evidence, cannot audit, cannot fail safely |
| Fully autonomous ReAct loop | Unbounded tool use, unbounded cost, no safety gate, unreproducible |
| LLM-free deterministic pipeline | No hypothesis generation, no postmortem prose — and no real use of the LLM |

---

## Layers

```
┌──────────────────────────────────────────────────────────────────────┐
│  Frontend — React 18 + TypeScript + Vite          :5173              │
│  Guide · Overview · Incidents · Investigations · Memory ·            │
│  Patterns · Postmortems · Learning Loop                              │
└────────────────────────────────┬─────────────────────────────────────┘
                                 │  /api/* proxied by Vite (one origin)
┌────────────────────────────────▼─────────────────────────────────────┐
│  API — FastAPI                                            :8000      │
│  50 endpoints across auth · incidents · memory · ops · demo          │
│  RBAC dependencies · rate limits · security headers · typed errors   │
└────────────────────────────────┬─────────────────────────────────────┘
                                 │
        ┌────────────────────────┼────────────────────────┐
        │                        │                        │
┌───────▼──────────┐  ┌──────────▼───────────┐  ┌─────────▼──────────┐
│  DETECTION       │  │  AI ENGINE           │  │  MEMORY            │
│                  │  │                      │  │                    │
│ anomaly committee│  │ orchestrator         │  │ ▶ HINDSIGHT ◀      │
│ log templating   │  │ 8 specialist agents  │  │  retain/recall/    │
│ similarity       │  │ 25 typed tools       │  │  reflect           │
└──────────────────┘  │ bounded LLM calls    │  │ local fallback     │
                      └──────────┬───────────┘  └────────────────────┘
                                 │
        ┌────────────────────────┼────────────────────────┐
        │                        │                        │
┌───────▼──────────┐  ┌──────────▼───────────┐  ┌─────────▼──────────┐
│  DOMAIN          │  │  SIMULATOR           │  │  DATABASE          │
│ remediation      │  │ deterministic        │  │ SQLAlchemy 2       │
│ verification     │  │ telemetry from       │  │ 35 tables          │
│ patterns         │  │ modelled state       │  │ SQLite | Postgres  │
│ postmortems      │  │                      │  │                    │
└──────────────────┘  └──────────────────────┘  └────────────────────┘
```

---

## 1. Detection — the anomaly committee

A single anomaly detector produces a single opinion, and a single opinion is what creates alert fatigue. Four detectors vote, and an incident needs a quorum (`app/detection/metrics.py`):

| Detector | Method | Catches |
|---|---|---|
| Robust z-score | `0.6745 · (x − median) / MAD` | Outliers against a median baseline, resistant to the incident contaminating its own baseline |
| Rolling median | deviation from a trailing median | Slow drifts |
| EWMA residual | exponentially weighted moving average residual | Sustained level shifts |
| CUSUM | cumulative sum change-point | Small persistent shifts that never trip a threshold |

An incident is opened only when: **quorum met (2 of 4)** AND **operational floor exceeded** (so a 0.001% change is not an incident) AND **direction check passes** (a *drop* in throughput is a symptom; a drop in error rate is not) AND **two-pass debounce confirms** (so a single scrape cannot open an incident).

### The two-window design (the subtle one)

```
        reference window                 recent window
   ┌──────────────────────────┐    ┌──────────────────┐
   │   30 min, ends 5 min ago │    │   5 min, ends now│
   └──────────────────────────┘    └──────────────────┘
                    ▲
                    └── must be healthy
```

`detect_anomaly_vs_baseline` compares the recent window against an **older reference window**, not against the recent window's own mean.

This matters more than it looks. With a single window, a severe incident is averaged into its own baseline, and the reported `relative_change` collapses toward zero *at peak severity* — the detector goes blind exactly when it should be loudest. The reference window deliberately ends 5 minutes ago so it never contains the incident being measured. This bug was found by testing, not by review, and there is now a regression test pinning it.

### Logs

Raw logs cannot be compared directly — every line has a different UUID, timestamp, and request ID. The pipeline (`app/detection/logs.py`) normalises each line into a **template** by replacing volatile tokens with placeholders:

```
<UUID> <IP> <HASH> <EMAIL> <HOST> <TIMESTAMP> <DURATION> <SIZE> <ID> <NUM> <SHA>
```

Templates are hashed, counted per window, and compared against their own baseline. The result is "this *kind* of error is new" rather than "there are more lines today", which is the only version of that question an SRE can act on.

### Incident similarity

Comparing incidents is a weighted seven-feature sum (`app/detection/similarity.py`):

| Feature | Weight |
|---|---|
| Error signatures | 0.24 |
| Metric pattern | 0.20 |
| Root cause category | 0.16 |
| Service | 0.14 |
| Deployment relationship | 0.12 |
| Symptom text | 0.08 |
| Environment | 0.06 |

Every score ships with **per-feature attribution** — you can see *why* two incidents are 0.73 similar, not just that they are. The weights are opinionated on purpose: two incidents are more usefully "similar" when they fail the same way than when they merely happen to the same service.

---

## 2. The AI engine — eight agents, bounded

`app/ai_engine/orchestrator.py` drives the incident through eight specialists (`app/ai_engine/agents.py`):

| Agent | Decides |
|---|---|
| `EvidenceAgent` | what the evidence actually shows |
| `MemoryAgent` | recall experience, compare incidents, mine patterns |
| `HypothesisAgent` | candidate explanations |
| `RootCauseAgent` | the conclusion, with a confidence **status** |
| `RemediationAgent` | which registered action to try |
| `VerificationAgent` | *(records; the verdict itself comes from code)* |
| `PostmortemAgent` | the narrative |
| `LearningAgent` | what to retain |

### Tools, not free-roaming

The model never touches the database and never runs a command. It may only *request* one of **25 strongly typed tools** (`app/ai_engine/tools.py`), and the registry is the sole authority on whether a request is legitimate:

```
get_incident · get_current_metrics · get_metric_anomalies · get_current_logs
aggregate_log_signatures · get_deployment_history · get_change_history
get_service_topology · get_runbooks · recall_historical_incidents
compare_with_past_incidents · find_similar_patterns · reflect_on_experience
list_remediation_actions · propose_remediation · check_safety_constraints
apply_remediation · verify_remediation · remember · get_incident_timeline · ...
```

Every invocation — successful or not — is written to `ai_tool_calls` with its arguments, result, duration, and the decision it informed. The UI renders that ledger rather than hidden chain-of-thought: judges and SREs see *what the agent did*, which is auditable, rather than *what it thought*, which is not.

### Cost and latency are bounded by construction

- max tool rounds per investigation (`OPENAI_MAX_TOOL_ROUNDS`, default 6)
- request timeout and retry with backoff
- schema repair via a forced function call when the model returns malformed JSON
- a hard cap of 3 remediation attempts per incident
- `temperature=0`

---

## 3. Memory

Detail in [MEMORY.md](MEMORY.md). Architecturally:

`MemoryService` is a singleton facade over a `MemoryStore` protocol with two implementations — the real `HindsightStore` and a faithful `FallbackStore`. The service owns policy (retain only verified outcomes, degrade visibly, self-heal), and the stores own transport.

**The async-SDK trap.** The Hindsight SDK can drive an asyncio transport internally. Calling a sync SDK method from a thread that already has a running event loop fails with *"This event loop is already running"* — which is exactly the situation inside FastAPI's lifespan and async routes. Every SDK call therefore hops onto a dedicated single-worker thread that never has a loop, so identical code works from a CLI script, a threadpool request handler, and an async startup hook.

---

## 4. Remediation safety

An agent that can act on production needs a gate that is more than a prompt. `evaluate_safety_gate` (`app/domain/remediation.py`) runs ten checks and returns an explicit verdict with per-check reasons:

```
action_registered · environment_allowed · risk_acceptable · addresses_root_cause
policy_allows · approval_satisfied · role_permitted · rollback_available
verification_defined · autonomy_sufficient
```

A registered action with no rollback, no verification workflow, or an autonomy requirement above the configured ceiling is **blocked with a reason**, and the block is recorded. `allowed` means "may proceed once approval is satisfied" — it is not "already approved".

`MAX_AUTONOMY_LEVEL` defaults to **4** (execute approved actions only). Level 5 is fully autonomous and must be opted into explicitly; there is a test asserting it is off by default and stays narrowly bounded when enabled.

## 5. Verification

Six checks, three of them blocking (`app/domain/verification.py`):

| Check | Blocking |
|---|---|
| `primary_signal` — the primary metric for *this* root cause | ✅ |
| `error_rate_normalised` | ✅ |
| `root_cause_removed` — is the underlying fault actually gone? | ✅ |
| `latency_normalised` | |
| `no_new_critical_signatures` | |
| `service_healthy` | |

`root_cause_removed` is the check that makes the failure memory possible. `restart_service` clears the connection pool — so latency and error rate genuinely improve — but the fault is still active, so verification **fails on the merits**. Nothing about that outcome is scripted; it falls out of a causally-built simulator.

Each action carries its own settle window (a restart needs 300s of soak; a config change needs less), read from the action's `verification_workflow` rather than hardcoded.

---

## 6. The simulator

`app/sim/` generates telemetry from modelled service state. Metrics are **derived** from state, never invented:

```
pool_size → active_connections → connection_saturation → latency → error_rate
```

Faults are modelled causally, so a fault that is not removed keeps producing symptoms regardless of how many times you restart. Each service has a stable personality derived from its name and kind (`SERVICE_PROFILES`), so the estate looks like a real estate — a cache cluster at ~1 080 rps is not a clone of a notification worker at ~230 rps — while staying fully deterministic and reproducible.

Three environments (production, staging, development) and eight services in the seeded org `acme-payments`.

---

## 7. Data model

35 tables (`app/database/models.py`), in five groups:

| Group | Tables |
|---|---|
| Catalogue | organizations, projects, environments, services, service_dependencies, runbooks, teams, users, roles, policies |
| Telemetry | metric_samples, log_events, log_templates, deployments, configuration_changes |
| Incidents | incidents, incident_evidence, incident_events, incident_relationships |
| AI | ai_investigations, ai_hypotheses, ai_tool_calls, ai_decisions, memory_operations |
| Remediation | remediation_actions, remediation_runs, verification_runs, verification_results |
| Learning | postmortems, learning_events, knowledge_patterns, test_runs, test_results, audit_log, integrations |

`TZDateTime` stores naive UTC and returns aware UTC — a small type that removes an entire category of timezone bug, which matters because every detection window is a time comparison.

---

## 8. Failure behaviour

The system is designed to be honest when things go wrong, not to look good:

| Failure | Behaviour |
|---|---|
| Hindsight unreachable | Falls back to a faithful local store, marks the investigation `MEMORY_DEGRADED`, and **self-heals** when reachable again |
| LLM unavailable or erroring | Deterministic evidence-driven reasoner takes over; the full loop still runs, only prose is templated |
| LLM returns malformed JSON | Schema repair via a forced function call |
| Remediation unsafe | Blocked with per-check reasons, recorded in the audit log |
| Remediation fails verification | Recorded as a failure and **retained to memory** — this is the feature, not the bug |
| Max attempts exhausted | Incident stays open and escalates rather than silently closing |

---

## 9. Security

- PBKDF2 password hashing, JWT sessions
- RBAC across four roles (`viewer`, `analyst`, `sre`, `admin`) with a `PERMISSIONS` map and `require_permission` dependency
- rate limits on expensive endpoints (investigations, learning loop)
- security headers on every response
- secrets never reach the browser; only `VITE_*` variables are exposed to the frontend
- every state-changing action writes an `AuditLog` row with actor, role, target, result, and reason
