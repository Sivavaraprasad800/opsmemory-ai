import { Link } from "react-router-dom";
import { api } from "../api";
import { BarList, LineChart, StackedStrip } from "../components/charts";
import { Badge, Card, Empty, ErrorBanner, Loading, Stat } from "../components/ui";
import PageIntro from "../components/PageIntro";
import {
  formatDateTime,
  formatDuration,
  formatPercent,
  formatRelative,
  humanise,
  toneForSeverity,
  toneForStatus,
} from "../lib/format";
import { useAsync, useTicker } from "../lib/hooks";

/**
 * The overview composes live incident state, the memory ledger and the derived learning curve.
 * The curve is computed from retained memory references — it is evidence of what actually got
 * remembered, not a decorative mock.
 */
export default function OverviewPage() {
  const now = useTicker(5000);
  const overview = useAsync(() => api.overview(), [], 10000);
  const references = useAsync(() => api.memoryReferences(300), [], 30000);

  if (overview.loading && !overview.data) return <Loading label="Loading incident room…" />;
  if (overview.error && !overview.data) return <ErrorBanner error={overview.error} onRetry={overview.reload} />;

  const data = overview.data!;
  const severity = data.status.severity_counts;
  const references_ = references.data?.references ?? [];

  // Cumulative retained documents, bucketed by day. Bucketing keeps the curve readable when
  // the corpus was backfilled in a single request.
  const buckets = new Map<string, number>();
  [...references_]
    .sort((a, b) => a.retained_at.localeCompare(b.retained_at))
    .forEach((reference) => {
      const day = reference.retained_at.slice(0, 10);
      buckets.set(day, (buckets.get(day) ?? 0) + 1);
    });
  let running = 0;
  const growth = [...buckets.entries()].map(([day, count]) => {
    running += count;
    return { label: day.slice(5), value: running };
  });

  const scopes = references_.reduce<Record<string, number>>((accumulator, reference) => {
    accumulator[reference.scope] = (accumulator[reference.scope] ?? 0) + 1;
    return accumulator;
  }, {});

  return (
    <>
      <PageIntro
        icon="◉"
        title="Overview"
        what="The estate at a glance: is anything broken right now, are the services healthy, and how much has the assistant learned so far."
        why="This is the answer to “what is happening”, before any of the detail. Nothing here is decoration — every number is read from the simulator or the memory bank on each refresh."
        lookFor={
          <>
            <strong>Active incidents</strong> should read 0 on a quiet estate. If it is above 0, open
            the Incidents page. The <strong>Learning curve</strong> below starts empty on a fresh
            install: run the Learning Loop once and it fills in.
          </>
        }
      />

      <div className="grid stats">
        <Stat
          label="Active incidents"
          value={data.status.active_incidents}
          hint={data.status.active_incidents === 0 ? "estate is quiet" : "awaiting resolution"}
          tone={data.status.active_incidents > 0 ? "warn" : "good"}
        />
        {/* The bank is the source of truth for what the organization remembers. Counting the
            platform's own retention ledger here instead made this card disagree with the
            Observations card next to it - 0 documents beside 13 observations. */}
        <Stat
          label="Memory documents"
          value={data.status.memory.health?.documents ?? "—"}
          hint={`${data.status.memory.health?.facts ?? "—"} facts in the bank`}
        />
        <Stat
          label="Observations"
          value={data.status.memory.health?.observations ?? 0}
          hint="consolidated, evidence-grounded"
        />
        <Stat
          label="Reasoning"
          value={data.status.reasoning_mode === "llm" ? "OpenAI + tools" : "Deterministic"}
          hint={
            data.status.reasoning_mode === "llm"
              ? "model decides at bounded checkpoints only"
              : "no key configured — evidence engine decides"
          }
        />
        <Stat
          label="Autonomy ceiling"
          value={`L${data.status.autonomy_level}`}
          hint="configured maximum for this deployment"
        />
        <Stat
          label="Memory backend"
          value={data.status.memory.active_backend}
          hint={
            data.status.memory.degraded
              ? (data.status.memory.reasons ?? []).join("; ") || "degraded"
              : data.status.memory.configured_backend === data.status.memory.active_backend
                ? "configured backend active"
                : `configured as ${data.status.memory.configured_backend}`
          }
          tone={data.status.memory.degraded ? "warn" : "good"}
        />
      </div>

      {data.status.memory.degraded && (
        <div className="banner warn">
          <span>!</span>
          <div>
            <div className="strong">Memory is running in a degraded mode</div>
            <div>
              {(data.status.memory.reasons ?? []).join("; ") ||
                "The configured backend is unreachable; incidents still retain and recall through the in-process store."}
            </div>
          </div>
        </div>
      )}

      <div className="grid side">
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <Card
            title="Learning curve"
            subtitle="Cumulative experience retained, from this platform's retention ledger"
            right={<Badge tone="accent">{references_.length} documents</Badge>}
          >
            {growth.length < 2 ? (
              <Empty
                title="Not enough history to plot yet"
                hint="Run the learning loop, or seed the historical corpus, and the curve appears here."
              />
            ) : (
              <LineChart
                series={[{ name: "Retained documents", colour: "#38bdf8", points: growth }]}
                height={220}
              />
            )}
          </Card>

          <Card
            title="Active incidents"
            subtitle="Open now, newest first"
            right={<Link to="/incidents" className="small">All incidents →</Link>}
            flush
          >
            {data.active_incidents.length === 0 ? (
              <Empty title="No open incidents" hint="Nothing is breaching right now." />
            ) : (
              <div className="list">
                {data.active_incidents.map((incident) => (
                  <Link key={incident.id} to={`/incidents/${incident.id}`} className="list-row clickable">
                    <div className="list-main">
                      <div className="list-title">{incident.title}</div>
                      <div className="list-meta">
                        <span className="mono">#{incident.id}</span>
                        <span>{incident.service}</span>
                        <span>{incident.environment}</span>
                        <span>{formatRelative(incident.detected_at, now)}</span>
                        {incident.root_cause_category && (
                          <span className="faint">{humanise(incident.root_cause_category)}</span>
                        )}
                      </div>
                    </div>
                    <div className="list-side">
                      <Badge tone={toneForSeverity(incident.severity)}>{incident.severity}</Badge>
                      <Badge tone={toneForStatus(incident.status)}>{humanise(incident.status)}</Badge>
                    </div>
                  </Link>
                ))}
              </div>
            )}
          </Card>

          <Card title="Service health" subtitle="Live gauges from the simulated estate" flush>
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Service</th>
                    <th>Tier</th>
                    <th className="num">Health</th>
                    <th className="num">Saturation</th>
                    <th className="num">p-latency</th>
                    <th className="num">Errors</th>
                    <th className="num">RPS</th>
                  </tr>
                </thead>
                <tbody>
                  {[...data.services]
                    .sort((a, b) => (a.gauges?.health_score ?? 1) - (b.gauges?.health_score ?? 1))
                    .map((service) => {
                      const gauge = service.gauges ?? {};
                      const health = gauge.health_score ?? 1;
                      const faulted = Boolean(data.active_faults[service.name]);
                      return (
                        <tr key={service.id}>
                          <td>
                            <div className="strong">
                              {service.name}{" "}
                              {faulted && <Badge tone="bad">fault injected</Badge>}
                            </div>
                            <div className="faint small">{humanise(service.owner_team)}</div>
                          </td>
                          <td>
                            <Badge tone={service.tier === "critical" ? "warn" : "neutral"}>{service.tier}</Badge>
                          </td>
                          <td className="num" style={{ color: health < 0.8 ? "var(--bad)" : "var(--good)" }}>
                            {(health * 100).toFixed(1)}%
                          </td>
                          <td className="num">{((gauge.connection_saturation ?? 0) * 100).toFixed(1)}%</td>
                          <td className="num">{gauge.latency_ms?.toFixed(0) ?? "—"} ms</td>
                          <td className="num">{((gauge.error_rate ?? 0) * 100).toFixed(2)}%</td>
                          <td className="num">{gauge.rps?.toFixed(0) ?? "—"}</td>
                        </tr>
                      );
                    })}
                </tbody>
              </table>
            </div>
          </Card>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <Card title="Severity mix" subtitle="Open incidents by severity">
            <StackedStrip
              segments={["critical", "high", "medium", "low"].map((level) => ({
                label: humanise(level),
                value: severity[level] ?? 0,
                tone: toneForSeverity(level),
              }))}
            />
          </Card>

          <Card title="What memory is made of" subtitle="Retained documents by scope">
            <BarList
              data={Object.entries(scopes).map(([scope, count]) => ({
                label: humanise(scope),
                value: count,
                tone: "info",
              }))}
              format={(value) => String(value)}
            />
          </Card>

          <Card title="Learning activity" subtitle="Every retain, recall and reflection" flush>
            {data.learning_activity.length === 0 ? (
              <Empty title="Nothing learned yet" hint="Resolve an incident to start the ledger." />
            ) : (
              <div className="list scroll-y">
                {data.learning_activity.map((event) => (
                  <div className="list-row" key={event.id}>
                    <div className="list-main">
                      <div className="list-title" style={{ fontSize: 12.5 }}>
                        {event.summary}
                      </div>
                      <div className="list-meta">
                        <Badge tone="info">{event.kind}</Badge>
                        {event.incident_id !== null && (
                          <Link to={`/incidents/${event.incident_id}`} className="mono">
                            #{event.incident_id}
                          </Link>
                        )}
                        <span>{formatRelative(event.created_at, now)}</span>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </Card>

          {data.top_pattern && (
            <Card title="Strongest recurring pattern" subtitle={data.top_pattern.category}>
              <p className="small muted" style={{ marginTop: 0 }}>
                {data.top_pattern.statement}
              </p>
              <BarList
                data={[
                  { label: "occurrences", value: data.top_pattern.occurrences, tone: "accent" },
                  { label: "services", value: data.top_pattern.services.length, tone: "info" },
                  { label: "deployment-linked", value: data.top_pattern.deployment_related, tone: "warn" },
                ]}
                format={(value) => String(value)}
              />
              <div className="small faint" style={{ marginTop: 10 }}>
                Median MTTR {formatDuration(data.top_pattern.median_mttr_seconds)} ·{" "}
                {data.top_pattern.failed_actions.length} failed action(s) on record
              </div>
              <div style={{ marginTop: 10 }}>
                <Link to="/learning" className="small">
                  Open pattern analysis →
                </Link>
              </div>
            </Card>
          )}

          <Card title="Latest AI investigations" subtitle="Most recent reasoning runs" flush>
            {data.investigations.length === 0 ? (
              <Empty title="No investigations yet" />
            ) : (
              <div className="list">
                {data.investigations.slice(0, 6).map((investigation) => (
                  <Link
                    key={investigation.id}
                    to={`/incidents/${investigation.incident_id}?tab=investigation`}
                    className="list-row clickable"
                  >
                    <div className="list-main">
                      <div className="list-title" style={{ fontSize: 12.5 }}>
                        {investigation.root_cause_category
                          ? humanise(investigation.root_cause_category)
                          : investigation.summary?.slice(0, 70) ?? "Investigation"}
                      </div>
                      <div className="list-meta">
                        <span className="mono">#{investigation.incident_id}</span>
                        <span>{investigation.tool_call_count} tools</span>
                        <span>{investigation.memory_hits} memory hits</span>
                        <span>{formatDateTime(investigation.started_at)}</span>
                      </div>
                    </div>
                    <div className="list-side">
                      <Badge tone={investigation.memory_recall_used ? "info" : "neutral"}>
                        {investigation.memory_recall_used ? "memory used" : "no precedent"}
                      </Badge>
                    </div>
                  </Link>
                ))}
              </div>
            )}
          </Card>

          <Card title="Recent deployments" subtitle="Change feed around incidents" flush>
            {data.deployments.length === 0 ? (
              <Empty title="No deployments recorded" />
            ) : (
              <div className="list">
                {data.deployments.slice(0, 6).map((deployment) => (
                  <div className="list-row" key={deployment.id}>
                    <div className="list-main">
                      <div className="list-title" style={{ fontSize: 12.5 }}>
                        <span className="mono">{deployment.version}</span> · {deployment.service}
                      </div>
                      <div className="list-meta">
                        <span>{deployment.environment}</span>
                        <span>{deployment.change_class ?? "change"}</span>
                        <span>{formatRelative(deployment.started_at, now)}</span>
                      </div>
                    </div>
                    <div className="list-side">
                      {deployment.is_suspected_cause && <Badge tone="bad">suspected cause</Badge>}
                      {deployment.risk_score !== null && (
                        <span className="chip">risk {formatPercent(deployment.risk_score * 100, 0)}</span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </Card>
        </div>
      </div>
    </>
  );
}
