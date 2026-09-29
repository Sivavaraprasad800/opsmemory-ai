# OpsMemory AI

**An AI SRE agent that gets better at incident response because it remembers what it tried, what failed, and what actually fixed things.**

Most AI agents reset to zero every conversation. An SRE agent that forgets is worse than useless during an incident — it will confidently recommend the same failed restart it recommended last Tuesday. OpsMemory AI is built the other way round: **memory is the product, not a feature.**

---

## 📋 Quick Links

- **🎯 [For Judges & Evaluators](JUDGES_README.md)** — 5-minute evaluation guide
- **🎬 [Demo Walkthrough](DEMO_WALKTHROUGH.md)** — Step-by-step demo script
- **✅ [Verification Guide](VERIFICATION_GUIDE.md)** — How to verify learning is real
- **📝 [Demo Checklist](DEMO_CHECKLIST.md)** — Printable quick reference
- **🗺️ [Navigation Map](NAVIGATION_MAP.md)** — Where to find every page

---

```
OBSERVE → UNDERSTAND → INVESTIGATE → RECALL EXPERIENCE → REASON → RECOMMEND
   → APPROVE → ACT → VERIFY → LEARN → REMEMBER → IMPROVE NEXT TIME
```

---

## The 60-second version

A payment service starts leaking database connections. Error rates climb.

| | Without memory | With OpsMemory AI |
|---|---|---|
| **Incident 1** | Recommends `restart_service` — the obvious fix | Same. No precedent exists yet |
| **Outcome** | Pool clears, leak resumes, verification **FAILS** | The failure is recorded with its evidence and its proof |
| **Incident 2** (similar, 4 days later) | Recommends `restart_service` again | Recall surfaces the failed attempt, the AI **explicitly avoids** the restart, and applies `update_known_safe_configuration` instead |
| **Outcome** | Fails again | **Verification passes — 88% improvement** |

That is the whole thesis in one table. The agent does not get smarter because it has a bigger model. It gets smarter because it remembers its own failures, with proof attached, and refuses to repeat them.

---

## Quickstart (no API keys required)

The platform boots and runs the complete learning loop with **zero external services**. The LLM and the memory layer both degrade to faithful local implementations.

```bash
# 1. Backend
cd backend
python -m venv .venv
./.venv/Scripts/activate          # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
cd ..

# 2. Environment (safe defaults — nothing is required)
cp .env.example .env

# 3. Run
cd backend && ./.venv/Scripts/python.exe -m uvicorn app.main:app --port 8000 &
cd ../frontend && npm install && npm run dev
```

Open **http://127.0.0.1:5173** and sign in as `admin@acme.test` / `password123`.

> **New here? Open the `How this works` page** — it is first in the sidebar and explains the whole system in plain English, including a five-step demo script you can read aloud.

### Optional: turn on real memory and real reasoning

Nothing below is required, but it is the difference between a demo and the real thing.

```bash
# Memory — Hindsight Cloud
HINDSIGHT_BASE_URL=https://api.hindsight.vectorize.io
HINDSIGHT_API_KEY=hsk_...

# Memory — or self-hosted Hindsight
docker run -p 8888:8888 -e HINDSIGHT_API_LLM_API_KEY=$OPENAI_API_KEY \
  ghcr.io/vectorize-io/hindsight:latest

# Reasoning — OpenAI, or any OpenAI-compatible endpoint (Groq is fast and has a free tier)
OPENAI_API_KEY=gsk_...
OPENAI_BASE_URL=https://api.groq.com/openai/v1
OPENAI_MODEL=openai/gpt-oss-120b
```

To prove the memory layer is genuinely live rather than quietly degraded:

```bash
curl -s http://127.0.0.1:8000/api/health | python -m json.tool
#   "memory": { "active_backend": "hindsight", "configured_backend": "hindsight",
#               "degraded": false, "reasons": [],
#               "health": { "healthy": true, "facts": 46, "observations": 13, ... } }
```

`/api/health` is public. For the full picture — counts by type, the configured budget, whether Hindsight is mandatory — use the authenticated `/api/memory/status`.

---

## Run the demo

> **Want to verify the AI actually works?** See **[VERIFICATION_GUIDE.md](VERIFICATION_GUIDE.md)** — it shows exactly what evidence to look for and how to prove learning happens (memory growth, verification failures, measurable improvement).

Two ways, same code path:

**In the UI** — `Learning Loop` in the sidebar, press **Run the learning loop**. It returns the full five-step narrative with evidence attached to each step.

**From the API:**

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@acme.test","password":"password123"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

curl -s -X POST "http://127.0.0.1:8000/api/demo/learning-loop" \
  -H "authorization: Bearer $TOKEN" | python -m json.tool
```

Takes about **two minutes**: it runs real AI investigations and real writes to the memory layer. The five steps it returns are:

1. `incident_1_detected` — the detector opens an incident
2. `incident_1_investigated` — the AI investigates with **no useful prior experience**
3. `failed_attempt` — the naive restart is applied and **verification FAILS**
4. `incident_1_resolved` — the AI re-investigates, cites the failure, and finds the real cause
5. `incident_2_resolved` — a similar incident arrives, and recall makes the response better

Step 5 is the punchline, and the response carries the receipts:

```json
"learning_delta": {
  "incident_1_recommended": "restart_service",
  "incident_2_recommended": "update_known_safe_configuration",
  "failed_action_avoided_in_incident_2": true,
  "incident_2_recall_hits": 8
}
```

---

## How Hindsight is used

Hindsight sits at the centre of the loop, not beside it. Per incident it is called **three times**, each with a different job:

| When | Call | What it is for |
|---|---|---|
| Investigation start | `recall(bank, query)` | "Have we seen this before?" — pulls prior incidents, past attempts, and consolidated observations |
| After an action is verified | `retain(bank, experience)` | Writes the verified (or failed) attempt with its evidence, so it can never be forgotten or quietly rewritten |
| On request | `reflect(bank, query)` | Asks the whole history a question — "what has historically worked for this service?" |

Three designs matter more than the call list:

**Failures are retained with equal weight to successes.** This is the whole point. A memory layer that only remembers what worked cannot teach an agent what to stop doing. Attempts are written with their verification verdict, so `restart_service → verification_failed` is a first-class, retrievable fact.

**Nothing unverified enters memory.** Retain is called *after* verification returns, never after an action is merely proposed or attempted. The agent cannot poison its own memory with an optimistic guess.

**Observations are consolidated, not overwritten.** Hindsight's `observation` memories carry a **proof count** and the IDs of the facts that support them, so a belief strengthens as evidence accumulates instead of being replaced by the newest write.

Full detail, including the exact document shapes and the recall query construction: **[docs/MEMORY.md](docs/MEMORY.md)**.

---

## What is real and what is simulated

Honesty matters more than a polished pitch, so this is stated plainly.

**Real:** the detection algorithms, the memory layer (real Hindsight Cloud or the local fallback), the LLM reasoning (real Groq/OpenAI tool-calling), the remediation safety gate (10 checks), the verification checks, the audit trail, and the database.

**Simulated:** the infrastructure being observed. There is no real payment service — there is a deterministic simulator that generates genuine metrics and logs from modelled service state. It is causally built, which is the important part: `restart_service` really does clear the pool and the leak really does resume, so verification genuinely fails on its own merits rather than being scripted to.

---

## Architecture at a glance

```
              ┌──────────── React + TypeScript (port 5173) ────────────┐
              │  Guide · Overview · Incidents · Investigations ·        │
              │  Memory · Patterns · Postmortems · Learning Loop        │
              └────────────────────────┬───────────────────────────────┘
                                       │  /api/*  (proxied)
              ┌────────────────────────▼───────────────────────────────┐
              │                FastAPI (port 8000)                     │
              │  auth · incidents · memory · ops · demo                 │
              └────────────────────────┬───────────────────────────────┘
                                       │
     ┌─────────────────────────────────┼──────────────────────────────────┐
     │                                 │                                  │
┌────▼─────────────┐   ┌───────────────▼──────────────┐   ┌───────────────▼─────┐
│ Detection        │   │ Reasoning                    │   │ Memory              │
│ ─────────────    │   │ ──────────                   │   │ ──────              │
│ 4-model anomaly  │   │ typed tool-calling agent     │   │ ▶ HINDSIGHT  ◀      │
│ committee        │   │ bounded decision points      │   │ retain / recall /   │
│ log templating   │   │ 8 specialist agents          │   │ reflect             │
│ 7-feature        │   │                              │   │                     │
│ similarity       │   │ deterministic guardrails     │   │ + local fallback    │
└──────────────────┘   └──────────────┬───────────────┘   └─────────────────────┘
                                      │
                         ┌────────────▼─────────────┐
                         │ Remediation + Verification│
                         │ 10 safety checks         │
                         │ 6 verification checks    │
                         └──────────────────────────┘
```

Design detail: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**

---

## The engineering decisions that make it credible

Three choices worth calling out, because they are where this project differs from a chatbot with a database:

**An LLM never decides whether a fix worked.** Verification is six deterministic checks over the metrics before and after the action, with a settle window sized to the action. Three of them are blocking: the primary signal for the diagnosed root cause, error-rate normalisation, and whether the underlying fault is actually gone. The model proposes; evidence disposes. A language model can be talked into believing a restart helped. A counter cannot.

**The detector compares an incident against a *separate*, healthy reference window.** A single window averages the incident into its own baseline and reports the change as ~0% at peak severity. Two windows — recent 5 minutes vs the 30 minutes ending 5 minutes ago — is the difference between detecting the incident and missing it entirely.

**Causality is never asserted from timing.** The knowledge graph emits only `preceded_by`, `correlated_with`, `consistent_with`, and `evidence_suggests`. Confidence is a *status* (`supported`, `weakly_supported`, `needs_more_evidence`, `contradicted`, `ruled_out`) with a stated evidence basis — never an arbitrary percentage. "The deploy happened first, therefore the deploy caused it" is exactly the reasoning that ruins real postmortems.

---

## Testing

```bash
cd backend && ./.venv/Scripts/python.exe -m pytest tests/ -q -p no:cacheprovider
```

**117 passed, 1 skipped.** The suite covers the detection committee, similarity attribution, the memory adapter's response handling, remediation safety boundaries (including that autonomy level 5 is off by default and narrowly bounded), the eight verification checks, the API surface, and the full end-to-end learning loop.

The tests are deliberately **offline and hermetic** — they must pass on venue wifi with no keys and no network. The live Hindsight adapter and live LLM path are verified separately against the real service.

Detail: **[docs/TESTING.md](docs/TESTING.md)**

---

## Documentation map

| Document | What it answers |
|---|---|
| **[docs/MEMORY.md](docs/MEMORY.md)** | How Hindsight is used, exactly — document shapes, call sites, consolidation, degradation |
| **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** | How the system is put together and why |
| **[docs/DEMO.md](docs/DEMO.md)** | The demo script, with what to say at each step |
| **[docs/TESTING.md](docs/TESTING.md)** | What is tested, what is not, and how to verify claims yourself |
| **[docs/RESEARCH.md](docs/RESEARCH.md)** | Pre-build research: agent architecture, algorithm selection, explicit rejections |
| **[content/](content/)** | Article, social post, and video script |

---

## Repository layout

```
opsmemory-ai/
├── backend/           FastAPI application
│   ├── app/
│   │   ├── detection/   anomaly committee, log templating, incident similarity
│   │   ├── memory/      Hindsight adapter + faithful local fallback
│   │   ├── ai_engine/   agents, typed tools, orchestrator, prompts
│   │   ├── domain/      incidents, remediation, verification, patterns, postmortems
│   │   ├── sim/         the deterministic fault simulator
│   │   ├── api/         HTTP routes
│   │   └── database/    schema, sessions, seed corpus
│   └── tests/           117 tests
├── frontend/          React 18 + TypeScript + Vite
├── docs/              research and design documentation
├── content/           article, social post, video script
├── sample-data/       realistic telemetry and incident fixtures
└── docker-compose.yml one-command self-hosted stack
```

---

## Configuration

Everything lives in `.env` (see [`.env.example`](.env.example) for the annotated list). The handful that matter:

| Variable | Default | Purpose |
|---|---|---|
| `HINDSIGHT_BASE_URL` | *(empty)* | Empty ⇒ local fallback memory store |
| `HINDSIGHT_REQUIRED` | `false` | Set `true` to **refuse to start** without a reachable Hindsight |
| `OPENAI_API_KEY` | *(empty)* | Empty ⇒ deterministic offline reasoner |
| `MAX_AUTONOMY_LEVEL` | `4` | Level 5 (fully autonomous) must be opted into explicitly |
| `DATABASE_URL` | `sqlite:///./opsmemory.db` | PostgreSQL supported |

Demo accounts, all with password `password123`:

| Email | Role | Can |
|---|---|---|
| `admin@acme.test` | admin | Everything — **use this one for the demo** |
| `sre@acme.test` | sre | Investigate, approve, execute |
| `analyst@acme.test` | analyst | Investigate, propose |
| `viewer@acme.test` | viewer | Read-only |

---

## License

MIT. See [LICENSE](LICENSE).
