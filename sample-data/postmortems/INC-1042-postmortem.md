# Postmortem: INC-1042 — Connection pool exhaustion on payment-service

| | |
|---|---|
| **Incident** | INC-1042 |
| **Severity** | Critical |
| **Status** | Resolved |
| **Detected** | 2026-09-29 13:48:02 UTC |
| **Resolved** | 2026-09-29 14:06:31 UTC |
| **Time to resolution** | 18 minutes 29 seconds |
| **Services affected** | `payment-service` (primary), `checkout-service` (downstream) |
| **Author** | OpsMemory AI, reviewed by sre-payments@acme.test |
| **Postmortem id** | 187 |

---

## Summary

Deployment `v2.41.0` refactored the database pool factory and, in the same change, reduced `DB_POOL_SIZE` from 50 to 8 and set `DB_POOL_LEAK_GUARD=false`. Steady-state demand exceeded the reduced capacity, and disabling the leak guard removed the mechanism that reclaimed connections abandoned by the refactored code path. Connection saturation reached 0.97, POST `/authorize` returned 503s, and `checkout-service` began failing authorisation for live orders.

The first remediation applied was a service restart. It cleared the pool and improved latency by 46%, but the underlying fault remained active, so saturation resumed climbing within six minutes. The incident was resolved by restoring the known-safe pool configuration.

## Impact

- 41 failed POST `/authorize` requests over 18 minutes
- 1 aborted checkout (`ORD-8821347`), customer-visible
- p95 latency on payment endpoints peaked at 4,602ms
- No data loss and no incorrect payment was captured

## Timeline

| Time (UTC) | Event |
|---|---|
| 13:27:14 | Deployment `v2.41.0` released to production |
| 13:33:41 | Connection saturation begins drifting above baseline (0.21 → 0.34) |
| 13:47:01 | First pool warning: `active=44 idle=6 max=50` |
| 13:48:02 | Detection engine opens INC-1042 after 3-of-4 committee quorum and second-pass debounce |
| 13:48:03 | First `503` on POST `/authorize` |
| 13:49:41 | Pool config reload line confirms `leak_guard=false` |
| 13:52:20 | AI investigation concludes `configuration_regression`, recommends a configuration fix, flags `restart_service` as previously ineffective for this root cause |
| 13:54:08 | Operator applies `restart_service` as an immediate stabilisation step |
| 13:59:11 | Verification runs after the 300-second settle window — latency and error rate pass, but `root_cause_removed` **fails**: the fault is still active. Verdict `verification_failed` |
| 14:03:47 | Connection saturation has climbed back to 0.89; the failure is recorded to organizational memory |
| 14:04:55 | AI re-investigates, cites the failed restart plus the runbook's explicit warning, and applies `update_known_safe_configuration` |
| 14:06:31 | Verification passes all six checks, three blocking. Improvement 88.0%. Incident resolved |
| 14:07:10 | Experience retained: incident record, both attempts, and their verification evidence |

## Root cause

A configuration change shipped alongside an unrelated refactor. The configuration values were not covered by any test, and the review view collapsed all `deploy/*.yaml` changes into a single file entry, so two changed lines inside a 412-line diff were not visible at review time.

## Contributing factors

1. **No CI assertion on pool configuration.** `DB_POOL_SIZE` and `DB_POOL_LEAK_GUARD` are load-bearing in production and asserted nowhere.
2. **No canary stage.** The release went to the full production fleet directly, so there was no window in which the regression was visible without customer impact.
3. **Change outside the window.** Released at 13:27, outside the Tuesday/Thursday 10:00–12:00 change window, which reduced reviewer attention.
4. **Medium risk score did not block.** Risk scored 0.72. It was above the informational threshold but below the approval gate, so it proceeded with a single reviewer.
5. **The `INFO` config-reload line is easy to miss.** The single most diagnostic line of the incident was logged at `INFO` and sits among routine noise.

## What went well

- Detection opened the incident 21 seconds after the first 503, with no manual alerting
- The failure template identified a previously unseen failure mode, which pointed triage directly at the pool
- The restart failed verification rather than being recorded as a success, so the resolution path was not derailed
- Downstream impact was correctly scoped to `checkout-service` from the dependency graph
- MTTR was 18 minutes, against a 40-minute median for critical incidents in this service over the previous quarter

## What did not go well

- A restart was applied first despite the runbook explicitly stating it would not help for this root cause
- The early part of the investigation was spent re-deriving something a prior incident had already established

## Action items

| # | Action | Owner | Type | Status |
|---|---|---|---|---|
| 1 | Assert `DB_POOL_LEAK_GUARD=true` and validate `DB_POOL_SIZE` in CI for production environments | payments | prevent | open |
| 2 | Add a canary stage to the `payment-service` release pipeline | platform | prevent | open |
| 3 | Alert on any change to `DB_POOL_SIZE` as a configuration event, independent of deploy outcome | platform | detect | open |
| 4 | Promote the connection-acquisition failure template to a named alert | payments | detect | open |
| 5 | Raise the pool-size configuration review threshold so `deploy/*.yaml` changes are reviewed field by field | sre | mitigate | open |
| 6 | Require the resolution path to consult recorded remediation history for the same root cause before proposing an action | platform | mitigate | done |

> Action item 6 is the reason this postmortem is worth reviewing. It is not a process change — it is a capability change, and it is what turns the next occurrence of this incident into a two-minute fix instead of an eighteen-minute one.
