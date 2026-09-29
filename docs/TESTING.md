# Testing

**118 tests: 117 passed, 1 skipped.**

```bash
cd backend
./.venv/Scripts/python.exe -m pytest tests/ -q -p no:cacheprovider
```

On macOS/Linux use `./.venv/bin/python`. Expect roughly **three and a half minutes** (measured: 212s) — the suite runs real detection windows and real verification settle periods against a simulated clock.

To run one file:

```bash
./.venv/Scripts/python.exe -m pytest tests/test_detection.py -q -p no:cacheprovider
```

---

## Philosophy

**The suite is hermetic and offline.** It must pass on venue wifi with no API keys and no network. This is not laziness — a test suite that needs the internet to verify a system whose *selling point* is working under pressure is a test suite that will fail at the worst moment. So:

- The LLM is never called; the deterministic reasoner runs instead
- Hindsight is never called; the local fallback store runs instead
- The database is a throwaway SQLite file in `%TEMP%`
- The clock is the simulator's, not the wall clock

**The tests assert behaviour that would be embarrassing to get wrong**, not line coverage. The three most valuable tests in the suite all exist because something was actually broken.

---

## What each file covers

| File | Tests | Covers |
|---|---|---|
| `test_detection.py` | 26 | The four-detector committee, quorum, debounce, direction and floor gates, log templating, **the two-window comparison** |
| `test_similarity.py` | 18 | The seven-feature match, per-feature attribution, ranking, fingerprint stability |
| `test_memory.py` | 16 | Retain/recall/reflect semantics, world-vs-experience classification, observation consolidation and proof counts, local persistence |
| `test_hindsight_adapter.py` | 14 | **Live-adapter response handling** — shape parsing, entity splitting, count honesty, degradation recovery |
| `test_remediation_safety.py` | 15 | Ten safety checks, approval gating, autonomy ceilings, blocked-action recording |
| `test_verification.py` | 7 | The six checks, blocking-vs-advisory, settle windows, failure reporting |
| `test_api.py` | 19 | Auth, RBAC per role, error contracts, rate limits, an anonymous-access check |
| `test_e2e_learning_loop.py` | 3 | The entire loop, including the exact learning delta |

---

## The tests that matter most

### 1. The two-window detector (`test_detection.py`)

```python
def test_peak_severity_is_not_averaged_into_its_own_baseline(...)
```

This test exists because of a **real bug**. With a single detection window, a severe incident is averaged into its own baseline and `relative_change` collapses toward zero *at peak severity* — the detector goes blind exactly when it should be loudest. The implementation was rewritten to compare a recent window against a separate older reference window; this test pins that behaviour so it cannot regress.

### 2. Failure memory survives (`test_e2e_learning_loop.py`)

```python
def test_restart_fails_verification_and_good_config_change_succeeds(...)
```

Asserts that `restart_service` genuinely fails verification and that the *other* action genuinely passes — not that a flag was set. If the simulator ever stops being causally modelled, this test fails.

### 3. The refusal actually happens (`test_e2e_learning_loop.py`)

```python
def test_incident_2_avoids_the_failed_action(...)
```

The headline claim, asserted end to end: incident 2 must recall prior experience and must **not** choose the action that failed. A memory layer that doesn't change any decision is decoration, so this is the one test that most directly defends the product thesis.

### 4. Autonomy level 5 is off by default (`test_remediation_safety.py`)

```python
def test_level_five_is_off_by_default_whatever_is_requested(...)
def test_level_five_automation_is_narrowly_bounded(...)
```

Five conditions must all hold before fully autonomous action is permitted. These tests fail if anyone ever widens that gate casually.

### 5. The adapter can actually see the bank (`test_hindsight_adapter.py`)

Fourteen tests, all written **after** a bug that made the entire product look empty: the adapter was reading response fields that don't exist (`memories`/`results` instead of `items`), so a bank holding 44 real memories reported zero. An empty list is indistinguishable from an empty bank, and the UI said "nothing retained yet" while the cloud held 6 documents and 13 observations.

The same class of bug appeared three more times in the same afternoon — a `type`/`fact_type` mismatch labelling every observation as a world fact, a comma-separated `entities` string iterated character-by-character, and an async-SDK call from inside a running event loop that **permanently** pinned the app to the fallback store. These tests exist because response-shape handling is where integrations silently lie.

Also asserted here: **counts must be honest**. When the bank cannot report its own size, the API returns `counts_available: false` rather than a confident `0`, and the UI renders `—`. A wrong number that looks authoritative is worse than a missing one.

---

## What is *not* covered

Stated plainly, because a test document that oversells is worse than none:

- **The live Hindsight service is not exercised by the suite.** The adapter's *response handling* is tested offline with recorded shapes, but a real network round trip to the cloud is verified manually (`/api/memory/status`, and the Memory page). Run the learning loop against real Hindsight before a demo.
- **The live LLM is not exercised by the suite.** Tool-calling, retry, timeout and schema repair are implemented and were verified manually against a real endpoint; the tests run the deterministic reasoner.
- **The frontend has no unit tests yet.** It is checked by `tsc --noEmit` (clean) and by manual verification of every page.
- **No load or stress tests.** The ticker and detection are bounded by construction (fixed window sizes, batched writes), but there is no throughput benchmark.
- **The PostgreSQL path is implemented but not tested in CI** — the suite runs on SQLite, and there is no PostgreSQL server available here to point it at. The `psycopg` driver is installed and the schema uses portable SQLAlchemy 2 constructs, but this has not been exercised end to end.

---

## Verify the claims yourself

Anything asserted in the README or MEMORY.md can be checked directly:

```bash
# 1. The suite genuinely passes
cd backend && ./.venv/Scripts/python.exe -m pytest tests/ -q -p no:cacheprovider

# 2. The real memory layer is genuinely in use (not degraded)
curl -s http://127.0.0.1:8000/api/health | python -m json.tool
#    look for: checks.memory.active_backend == "hindsight", checks.memory.degraded == false

# 3. The learning loop genuinely changes a decision
curl -s -X POST "http://127.0.0.1:8000/api/demo/learning-loop" \
  -H "authorization: Bearer $TOKEN" | python -m json.tool
#    look for: "failed_action_avoided_in_incident_2": true

# 4. The frontend typechecks
cd frontend && npx tsc --noEmit
```

---

## Not reporting something as tested when it wasn't

A note on process, because it matters for a project whose pitch is honesty: **a test is only "passing" if it was run.** Where a claim in these documents has not been exercised — the PostgreSQL path, live-network Hindsight inside CI, frontend unit tests — the document says so rather than rounding up.

If you change the detector, the simulator's baseline profiles, the memory adapter, or the ticker, re-run the full suite. Simulator baselines in particular are load-bearing: a service placed too close to an "unhealthy" threshold will flip detection assertions, which is exactly what happened when per-service personalities were introduced (`payment-service` initially sat at 30.2% saturation, right on the boundary the tests encode). The fix was to move the profile, not to weaken the assertion.
