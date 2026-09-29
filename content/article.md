# Teaching an SRE Agent to Remember Failed Fixes With Hindsight

I spent a week building an agent that responds to production incidents. The interesting part was not the reasoning. It was the remembering.

Here is the failure mode that convinced me memory is the whole game. A payment service starts leaking database connections. Error rates climb, latency climbs. The obvious mitigation is to restart the service — it clears the connection pool, and for about ninety seconds the dashboards go green. Then the leak resumes, because the leak was never the pool. It was a config change three deploys ago that shrank the pool to a third of its steady-state demand.

A stateless agent does not learn this. It will recommend the restart on Tuesday, watch it fail, and recommend the same restart on Thursday with the same confidence. It has no opinion about what *doesn't* work here, because it has no here.

That is the problem I wanted to solve: an agent whose memory of its own failures is strong enough to change its next decision.

## What the system does

OpsMemory AI watches a simulated payments estate — eight services across three environments — and runs a full incident loop:

```
OBSERVE → UNDERSTAND → INVESTIGATE → RECALL EXPERIENCE → REASON → RECOMMEND
   → APPROVE → ACT → VERIFY → LEARN → REMEMBER → IMPROVE NEXT TIME
```

The loop is mostly ordinary engineering: a four-model anomaly detection committee, log templating, a tool-calling reasoner, a remediation safety gate, and multi-signal verification. The part that is not ordinary is that **recall happens before the agent forms a hypothesis, and retention happens only after verification returns a verdict.**

That ordering is the design. An agent that recalls after forming a hypothesis is just looking for confirmation. An agent that retains before verifying is writing guesses into organizational memory.

## Why I put Hindsight in the middle of the loop

[Hindsight](https://github.com/vectorize-io/hindsight) is a memory layer built for agents, and the reason I wired it into the decision path rather than the logging path is the shape of its retrieval. It runs parallel retrieval arms — embedding, keyword, entity graph, temporal — fuses them, and reranks. That means I can ask a question that mixes an operational concept, exact technical vocabulary, and a taxonomy, and get one coherent answer.

Here is the query the agent actually sends before it reasons:

```python
parts.append(
    f"How have we previously resolved incidents on {fingerprint.service} in the "
    f"{fingerprint.environment} environment?"
)
if fingerprint.symptom:
    parts.append(f"Symptoms were: {fingerprint.symptom}")
if fingerprint.root_cause_category != "unknown":
    parts.append(f"Root cause category: {fingerprint.root_cause_category}")
parts.append(
    "Which remediation attempts failed verification, and which remediation was verified "
    "to resolve it?"
)
```

That last sentence is the one doing the work. It is a *comparative* question — failed versus succeeded — not a lookup. The difference between "have we seen this?" and "what should we stop doing about this?" is the difference between a search engine and a colleague.

## Retaining failures is the hard part

Everyone retains successes. It is intuitive and it makes the demo look good. It is also close to useless, because the knowledge that saves you is the knowledge of what does not work.

So attempts are retained individually, the moment the verdict lands — not bundled at the end of the incident, when it is already closed:

```python
# app/memory/documents.py
verdict = {
    "failed": "Verification failed, so the incident was not resolved.",
    "succeeded": "Verification succeeded and the incident was resolved.",
    "partial": "It only partially helped and did not resolve the incident.",
}.get(attempt.outcome, f"Outcome: {attempt.outcome}.")
```

Retaining at the verdict rather than at closure matters more than it sounds. The failure exists in memory even while the incident is still open — which is precisely the state the demo needs, and precisely the state a real on-call engineer is in.

Two more rules keep the memory trustworthy:

**Nothing unverified is retained.** The agent cannot poison its own memory with an optimistic guess. There is no code path that writes "this probably helped".

**Observations are consolidated, not overwritten.** Hindsight's observation memories carry a proof count and the IDs of the supporting facts, so a belief *strengthens* as evidence accumulates rather than being replaced by the newest write. A previous wording is superseded, not deleted, which preserves the audit trail of what the org believed and when.

## The part that makes it credible: the LLM doesn't grade itself

If a language model decides whether its own fix worked, then memory becomes a record of the model's optimism. So I split the responsibilities hard: the model decides judgement calls — hypotheses, the conclusion, which registered action to try, the postmortem prose. **Code decides facts** — metric values, whether anything improved, whether the incident is resolved, and what enters memory.

Verification is six deterministic checks, three of them blocking. The one that matters is `root_cause_removed`:

```python
active_fault = engine.active_fault(service.name)
fault_cleared = active_fault is None
checks.append(
    CheckOutcome(
        check_name="root_cause_removed",
        passed=fault_cleared,
        detail=(
            "the underlying fault is no longer active"
            if fault_cleared
            else f"the underlying fault '{active_fault.kind}' is still active — "
                 f"the remediation addressed symptoms only"
        ),
    )
)
```

This is why the restart genuinely fails. It clears the pool, so latency and error rate improve and the naive view says "fixed". But the fault is still active, so the blocking check fails and the verdict is `verification_failed`. Nothing about that outcome is scripted — it falls out of a simulator built causally, where metrics are derived from service state rather than invented.

## Before and after

The whole thesis in two incident records:

**Incident 1** — `payment-service`, production, connection pool exhaustion. No precedent in the bank. The agent investigates, concludes `configuration_regression`, and recommends a config change — but an operator applies the obvious mitigation instead: `restart_service`. Verification returns `verification_failed`. That failure is now memory, with its evidence and its reason.

**Incident 2** — the same failure signature, in staging, four days later. Recall returns eight memory items and the similar historical incident at 0.73 similarity. The agent chooses `update_known_safe_configuration`, and explicitly avoids the restart because it is recorded as having failed:

```json
"learning_delta": {
  "incident_1_recommended": "restart_service",
  "incident_2_recommended": "update_known_safe_configuration",
  "failed_action_avoided_in_incident_2": true,
  "incident_2_recall_hits": 8
}
```

Same model. Same prompt. Same tools. Different decision, because of what it remembered.

## Four lessons

**Memory belongs in the decision path, not the logging path.** If recall doesn't change what the agent does next, it's telemetry with extra steps. The test I care most about asserts the refusal actually happens.

**Retain failures, and retain them at the verdict.** Successes are the easy half and the less useful half. Waiting until incident closure to write means the memory doesn't exist when it's most needed.

**Never let the model grade its own work.** Verification has to be a computation over evidence. A model can be talked into believing a restart helped; a counter can't.

**An empty list is indistinguishable from an empty bank.** I lost half a day to an integration that was reading response fields that didn't exist — the bank held 44 real memories and every screen said "nothing retained yet". If a memory layer reports zero, verify that zero before you believe it. Corollary: when counts genuinely aren't available, return `null` and render a dash. A confident wrong number is worse than a missing one.

**A detector that averages an incident into its own baseline goes blind at peak severity.** This one cost me a rewrite. A single detection window means a severe incident drags its own baseline up, collapsing the reported change toward zero exactly when it should be loudest. Comparing a recent window against a separate older reference window is the fix.

## What I'd tell you if you're building this

Agent memory is easy to add and easy to add badly. Three decisions separate a memory layer that makes an agent better from one that makes it confidently wrong: retain failures with equal weight to successes, retain only after verification, and consolidate observations rather than overwriting them. Everything else follows.

The agent in this system doesn't get smarter because the model got bigger. It gets smarter because it remembers what it tried, what failed, and how recovery was actually verified — and then refuses to repeat itself.

If you want the memory layer itself, start with the [Hindsight documentation](https://hindsight.vectorize.io/) and the [Hindsight GitHub repo](https://github.com/vectorize-io/hindsight). If you want the conceptual grounding first, [Vectorize's explanation of agent memory](https://vectorize.io/what-is-agent-memory) is the piece I'd read before writing any of this.
