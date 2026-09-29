# Runbook: Database connection pool exhaustion

| | |
|---|---|
| **Slug** | `db-connection-pool-exhaustion` |
| **Category** | database |
| **Owner** | payments |
| **Times used** | 14 |
| **Times effective** | 11 |
| **Effectiveness** | 79% |

**Applies to:** `payment-service`, `order-service`, `inventory-service`, `auth-service`

---

## Symptoms

- `connection_saturation` climbing above 0.85 and staying there
- A previously unseen log template containing `Cannot acquire connection from pool`
- p95 latency on affected endpoints rising into the seconds, with the rise tracking pool wait time
- `error_rate` climbing as requests exhaust their connection timeout

---

## Steps

### 1. Confirm pool saturation

Read `connections`, `pool_size`, and `connection_saturation` for the affected service. Confirm saturation **and** confirm that active connections are at or near the pool maximum.

If `connection_saturation` is high but active connections are *low*, you are looking at a different problem — a leaked or never-returned connection held by an idle thread, not a capacity problem. Go to step 3's second branch.

### 2. Check whether a recent deployment changed pool sizing

Look for a release completed within the last 60 minutes. In particular, check `deploy/*.yaml` for changes to:

- `DB_POOL_SIZE`
- `DB_POOL_LEAK_GUARD`

A refactor that touches the pool factory is a common carrier for these values changing unnoticed — the diff is large, and two configuration lines are easy to lose inside it.

### 3. Choose the remedy — **read this before restarting**

> ⚠️ **Restarting the service clears the pool but does NOT remove a connection leak.** Saturation will return to baseline for a few minutes and then climb again. A restart that appears to work and then fails twenty minutes later is the most common way this incident gets mishandled.

**If a deploy disabled `DB_POOL_LEAK_GUARD` or reduced `DB_POOL_SIZE`:**
→ Apply the known-safe pool configuration and confirm `DB_POOL_LEAK_GUARD` is `true`. This removes the cause.

**If there is no config change and active connections are unexpectedly low:**
→ Restart as a *stabilisation* step only, then treat the leak itself as the incident. Restarting without finding the leak means doing this again.

### 4. Verify

Confirm `connection_saturation` returns to baseline within one minute and **stays there for at least five minutes**. A single healthy sample after a restart is not verification — it is the pool being empty.

### 5. Follow up

- Does the service's pool size match measured steady-state demand, or was it sized from a load test that no longer reflects production?
- Should `DB_POOL_SIZE` changes be gated in CI, independently of the deploy outcome?
- Is there a named alert for the connection-acquisition failure template?

---

## Remediation actions the agent may propose

| Action | Addresses root cause? | Notes |
|---|---|---|
| `update_known_safe_configuration` | ✅ yes | Removes the cause. Requires a defined rollback |
| `scale_connection_pool` | partially | Buys headroom; does not stop a leak |
| `restart_service` | ❌ no | Clears symptoms only. **Will refill** |
| `rollback_deployment` | ✅ yes, when a deploy caused it | Broader blast radius than a config fix |

## History

| Date | Service | Root cause | What worked | What was tried first |
|---|---|---|---|---|
| 2026-09-29 | payment-service | config regression (pool 50→8, leak guard off) | known-safe config | `restart_service` — failed |
| 2026-08-14 | order-service | config regression (pool 40→12) | known-safe config | `restart_service` — failed |
| 2026-07-02 | inventory-service | connection leak in new code path | code fix + config | `scale_connection_pool` — partial |
| 2026-05-21 | payment-service | traffic spike during promotion | `scale_connection_pool` | — |

The pattern is worth noticing: **when the cause is a configuration regression, a restart has not worked once.** That is exactly the kind of thing an organization knows but cannot retrieve in time, which is the problem this project exists to solve.
