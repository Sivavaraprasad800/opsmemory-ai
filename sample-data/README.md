# Sample data

Realistic fixtures from the `acme-payments` / `payments-platform` estate.

These files are **not** what the running application reads — the simulator generates its own telemetry, deterministically, from modelled service state. These fixtures exist to make the domain concrete: this is what a connection-pool exhaustion incident actually looks like in the raw, before and after normalisation, and this is the paper trail it leaves behind.

They are also the fastest way to understand the detection pipeline. Read
[`logs/raw/`](logs/raw/) and then [`logs/templates.md`](logs/templates.md) — the second file is what the first one becomes after normalisation, and seeing both side by side explains why the log detector works at all.

| Path | What it is |
|---|---|
| [`logs/raw/payment-service-incident.log`](logs/raw/payment-service-incident.log) | The raw incident window. Volatile tokens in every line — UUIDs, IPs, timestamps, durations — which is exactly why raw comparison fails |
| [`logs/templates.md`](logs/templates.md) | The same window after normalisation, with per-template counts against baseline |
| [`incidents/INC-1042.json`](incidents/INC-1042.json) | The incident record, with the AI conclusion, remediation attempts, and verification evidence |
| [`deployments/deployment-payment-service-v2.41.0.json`](deployments/deployment-payment-service-v2.41.0.json) | The deployment that caused it — the diff is three lines long, which is the point |
| [`runbooks/db-connection-pool-exhaustion.md`](runbooks/db-connection-pool-exhaustion.md) | The runbook the agent retrieves, which already warns that a restart will not work |
| [`postmortems/INC-1042-postmortem.md`](postmortems/INC-1042-postmortem.md) | The postmortem, written in the format a real team would review |

## The estate

| Service | Tier | Stack | Team | Roughly |
|---|---|---|---|---|
| `payment-service` | critical | python | payments | ~420 rps, small pool, latency-sensitive |
| `checkout-service` | critical | go | commerce | request orchestrator |
| `order-service` | high | python | commerce | order lifecycle |
| `auth-service` | critical | node | identity | token issuance |
| `inventory-service` | high | python | supply | stock ledger |
| `search-service` | medium | java | discovery | search and ranking |
| `notification-service` | low | python | platform | ~230 rps, async fan-out |
| `cache-service` | high | rust | platform | ~1 080 rps, high throughput |

Environments: `production`, `staging`, `development`.

## Reproducing this state

```bash
# Catalogue only (org, services, runbooks, users, policies)
curl -X POST http://127.0.0.1:8000/api/demo/clean-room -H "authorization: Bearer $TOKEN"

# The designed 60-incident historical corpus
curl -X POST "http://127.0.0.1:8000/api/demo/seed-history?count=60&days=45" \
  -H "authorization: Bearer $TOKEN"

# Or run the whole learning loop, which produces the incident above
curl -X POST http://127.0.0.1:8000/api/demo/learning-loop -H "authorization: Bearer $TOKEN"
```

The historical corpus in `seed_history` is deliberately designed rather than random: it contains no `connection_leak` on `payment-service`, so the first incident in the learning loop has genuinely **no precedent to recall**. That emptiness is what makes the second incident's recall meaningful.
