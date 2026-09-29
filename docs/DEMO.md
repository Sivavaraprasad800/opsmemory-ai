# Demo script

Everything here is verified against the running application. Times assume the learning loop has already been run once, so the pages are warm.

---

## Before you start (5 minutes)

```bash
# 1. Backend
cd backend
./.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 2. Frontend (separate terminal)
cd frontend
npm run dev
```

**Checklist:**

- [ ] **http://127.0.0.1:5173** opens and the login page loads
- [ ] Sign in as **`admin@acme.test`** / **`password123`** — *only `admin` may run the learning loop; the other roles will see the button disabled*
- [ ] The sidebar footer shows **Memory · hindsight** and **LLM · live**
- [ ] If it says `fallback` or `offline`, see [Troubleshooting](#troubleshooting) before you present — a degraded run is still honest, but the real thing tells a better story

> **Run the loop once in advance.** It takes about two minutes and populates the Memory page. Walking on stage to an empty bank wastes the best moment you have.

---

## The narrative, in one breath

> "An AI agent that fixes production incidents is easy to build. An AI agent that remembers *which of its fixes failed* — and refuses to try them again — is worth paying for. Let me show you the difference between the two, twice, in real time."

---

## The demo (~5 minutes)

### 0. The plain-English page — 45 seconds

Open **How this works** (first item in the sidebar).

> "Before anything technical: this page exists so nobody has to guess what they're looking at."

Point at the **without memory / with memory** comparison.

> "Same incident, same agent, two outcomes. The only difference is whether it remembered its own failure. That's the whole product."

### 1. The estate is real — 30 seconds

Click **Overview**.

> "Eight services, three environments, live telemetry. Every gauge here is derived from modelled service state — a connection pool, a latency curve, an error rate. It isn't random numbers."

### 2. Run the loop — 30 seconds

Click **Learning Loop**, then press the run button.

> "One click runs the entire loop: observe, understand, investigate, recall, reason, recommend, approve, act, verify, learn, remember, improve. It takes about two minutes because it's doing real work — real AI investigations and real writes to the memory layer."

While it runs, stay on the page. Do not talk over it; say instead:

> "While that runs — the thing to watch is step three. The system is about to report that one of its fixes *failed*."

### 3. The failure — 60 seconds

When **`failed_attempt`** appears:

> "There it is. A restart cleared the connection pool — so latency genuinely improved and it *looks* like a fix. But the underlying leak was still there, so connections climbed straight back up inside the settle window."

> "Verification failed. And notice what it didn't do — it didn't round that up to a success. Three of the six checks are blocking, and 'is the underlying fault actually gone' is one of them. No LLM decided that. A counter did."

### 4. The refusal — 90 seconds ⭐

When **`incident_2_resolved`** appears, this is the moment.

> "A similar incident. Different environment, four days later. And *this* is the whole project:"

Read the recommendation:

> "It chose `update_known_safe_configuration`, and it **explicitly avoided** `restart_service` — because it remembers that the restart failed."

> "Without memory, this is a generic agent that recommends the same wrong fix every time. With Hindsight, it's an agent that has an opinion about what *doesn't* work here. That's the difference between an assistant and a colleague."

### 5. Show the receipts — 60 seconds

Click **Organizational Memory**.

> "This isn't a claim, it's the bank. Six documents, forty-six facts, thirteen consolidated observations — each one with a proof count and the IDs of the facts that support it."

Click the **Observations** tab.

> "An observation isn't a log line. It's a belief the system consolidated from evidence. And it *strengthens* as more evidence arrives instead of being overwritten — you can see the proof count climbing."

### 6. Close — 30 seconds

> "So: the agent failed, remembered the failure with proof, and changed its behaviour the next time. That's what 'learns from experience' has to mean to be worth anything — not a nicer summary, an actually different decision."

---

## Questions you will be asked

**"Is the memory real, or is it a database you're calling memory?"**
"We call Hindsight's `recall` before the agent forms a hypothesis, and `retain` only after verification returns a verdict. Failures are retained with the same weight as successes. Observations are consolidated with proof counts, not overwritten. If Hindsight is unreachable the app says so — `degraded: true` — rather than quietly pretending."

**"Isn't this just a bigger prompt window?"**
"No. A prompt can't tell you that an action failed last Tuesday and was fixed by something else. And more importantly, the failure evidence has to survive the session — a prompt dies with the conversation."

**"Why did the fix fail?"**
"Because the simulator is causally built: `restart_service` clears the pool but the leak is still active, so saturation climbs again. The failure is earned, not scripted. That's what makes the learning real."

**"What stops it doing something dangerous?"**
"Ten safety checks before anything executes, an approval requirement above a configured risk level, and autonomy level 5 is off by default. It's bounded and it's written to the audit log."

---

## Video (2–5 minutes)

The same story, tighter. Structure that works:

| Time | Show | Say |
|---|---|---|
| 0:00–0:30 | You, briefly | What you built, one sentence on why |
| 0:30–1:00 | **How this works** page | The without-memory / with-memory comparison |
| 1:00–1:30 | **Overview** | The estate is real telemetry, not random numbers |
| 1:30–2:30 | **Learning Loop** running | Narrate the failure: pool cleared, leak resumed, verification failed |
| 2:30–3:30 | **Learning Loop** result | The refusal — `restart_service` avoided because it's remembered as failed |
| 3:30–4:15 | **Organizational Memory** | The bank, observations with proof counts |
| 4:15–4:45 | You | One takeaway, one thing that surprised you |

Recording notes: 1080p minimum, screen recording not a phone. Increase terminal/editor font size, close notifications. Practice once, then record — authenticity beats polish. A full script is in [content/video-script.md](../content/video-script.md).

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Loop button disabled | Signed in as a non-admin | Sign in as `admin@acme.test` |
| Sidebar says `Memory · fallback` | `HINDSIGHT_BASE_URL`/`_API_KEY` not set, or Hindsight unreachable | Set both in `.env`, restart the backend, re-check `/api/health` |
| Sidebar says `LLM · offline` | `OPENAI_API_KEY` unset | Set it (Groq works: set `OPENAI_BASE_URL=https://api.groq.com/openai/v1`, `OPENAI_MODEL=openai/gpt-oss-120b`) |
| Memory page shows 0 documents | Wrong bank, or nothing retained yet | Run the learning loop once; check `/api/memory/scopes` |
| Learning loop times out in a proxy | It genuinely takes ~2 minutes | Do not put a short proxy timeout in front of it |
| Loop ran but nothing was retained | The run degraded mid-flight | Check `/api/memory/status` — degraded runs record `MEMORY_DEGRADED` on the investigation |
| Port already in use | Another server running | `netstat -ano \| grep :8000` then stop it, or change the port |

Verify the live stack at any point:

```bash
# Public: reasoning mode and memory backend
curl -s http://127.0.0.1:8000/api/health | python -m json.tool

# Authenticated: full memory status, including counts by type
curl -s http://127.0.0.1:8000/api/memory/status \
  -H "authorization: Bearer $TOKEN" | python -m json.tool
```
