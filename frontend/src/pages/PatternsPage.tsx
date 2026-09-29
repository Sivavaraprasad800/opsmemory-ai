import { Link } from "react-router-dom";
import { api } from "../api";
import { BarList, StackedStrip } from "../components/charts";
import { Badge, Banner, Card, Empty, ErrorBanner, Loading, Stat } from "../components/ui";
import { formatDuration, formatPercent, humanise, toneForSeverity } from "../lib/format";
import { useAsync } from "../lib/hooks";

export default function PatternsPage() {
  const period = useAsync(() => api.period(45), [], 30000);
  const patterns = useAsync(() => api.patterns(45, 3), [], 30000);
  const reliability = useAsync(() => api.reliability(90), [], 30000);
  const fragility = useAsync(() => api.fragility(45), [], 30000);

  if (period.loading && !period.data) return <Loading label="Analysing the incident corpus…" />;
  if (period.error && !period.data) return <ErrorBanner error={period.error} onRetry={period.reload} />;

  const analysis = period.data!;
  const rows = patterns.data?.patterns ?? [];
  const actions = reliability.data?.actions ?? [];
  const services = fragility.data?.services ?? [];

  const weakest = [...actions]
    .filter((action) => action.total > 0)
    .sort((a, b) => (a.success_rate ?? 1) - (b.success_rate ?? 1))[0];

  return (
    <>
      <div className="grid stats">
        <Stat label="Incidents in window" value={analysis.incidents} hint={`last ${analysis.window_days} days`} />
        <Stat label="Still open" value={analysis.open_incidents} tone={analysis.open_incidents > 0 ? "warn" : "good"} />
        <Stat label="Median MTTR" value={formatDuration(analysis.median_mttr_seconds)} />
        <Stat
          label="Remediation success"
          value={analysis.remediation_success_rate === null ? "—" : formatPercent(analysis.remediation_success_rate)}
          hint={`${analysis.verified_success} verified · ${analysis.verification_failed} failed`}
          tone={
            analysis.remediation_success_rate === null
              ? "neutral"
              : analysis.remediation_success_rate >= 0.7
                ? "good"
                : "warn"
          }
        />
        <Stat label="Deployment-linked" value={analysis.deployment_related_incidents} hint={`of ${analysis.deployments} deployments`} />
        <Stat label="Patterns detected" value={rows.length} hint="recurrence, not one-offs" />
      </div>

      {weakest && weakest.reliability_label !== "reliable" && (
        <Banner tone="warn">
          <div className="strong">
            {humanise(weakest.action_code)} is the least reliable action on record
          </div>
          <div>
            {weakest.succeeded} succeeded, {weakest.failed} failed, {weakest.partial} partial (
            {formatPercent(weakest.success_rate ?? 0)} success rate). This is exactly the kind of
            evidence the agent cites when it declines to repeat an action.
          </div>
        </Banner>
      )}

      <div className="grid side">
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <Card
            title="Recurring patterns"
            subtitle="Clusters of at least three incidents sharing a root cause"
            flush
          >
            {rows.length === 0 ? (
              <Empty
                title="No pattern reaches the recurrence threshold"
                hint="Three incidents with the same root cause are required before the platform will call something a pattern."
              />
            ) : (
              <div className="list">
                {rows.map((pattern) => (
                  <div className="list-row" key={pattern.category}>
                    <div className="list-main">
                      <div className="btn-row" style={{ gap: 6 }}>
                        <span className="list-title">{humanise(pattern.category)}</span>
                        <Badge tone={pattern.evidence_strength === "strong" ? "good" : "warn"}>
                          {pattern.evidence_strength} evidence
                        </Badge>
                        <span className="chip">{pattern.occurrences}×</span>
                        {pattern.deployment_related > 0 && (
                          <span className="chip">{pattern.deployment_related} deployment-linked</span>
                        )}
                      </div>
                      <p className="small muted" style={{ margin: "6px 0" }}>
                        {pattern.statement}
                      </p>
                      <div className="small">
                        <span className="stat-label">Retention advice</span>
                        <div className="muted">{pattern.retention_advice}</div>
                      </div>
                      <div className="list-meta" style={{ marginTop: 6 }}>
                        <span>services: {pattern.services.join(", ")}</span>
                        <span>median MTTR {formatDuration(pattern.median_mttr_seconds)}</span>
                      </div>
                      {pattern.failed_actions.length > 0 && (
                        <div className="btn-row" style={{ marginTop: 6 }}>
                          <span className="faint small">failed before:</span>
                          {pattern.failed_actions.map((action) => (
                            <Badge tone="bad" key={action}>
                              {action}
                            </Badge>
                          ))}
                        </div>
                      )}
                      {pattern.successful_actions.length > 0 && (
                        <div className="btn-row" style={{ marginTop: 6 }}>
                          <span className="faint small">worked before:</span>
                          {pattern.successful_actions.map((action) => (
                            <Badge tone="good" key={action}>
                              {action}
                            </Badge>
                          ))}
                        </div>
                      )}
                      <div className="btn-row" style={{ marginTop: 8 }}>
                        {pattern.incident_ids.slice(0, 6).map((incidentId) => (
                          <Link className="chip" key={incidentId} to={`/incidents/${incidentId}`}>
                            #{incidentId}
                          </Link>
                        ))}
                        {pattern.incident_ids.length > 6 && (
                          <span className="faint small">+{pattern.incident_ids.length - 6} more</span>
                        )}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </Card>

          <Card
            title="Action reliability"
            subtitle="Proposed is not the same as worked — this is the verified record"
            flush
          >
            {actions.length === 0 ? (
              <Empty title="No remediation attempts on record" />
            ) : (
              <div className="table-wrap">
                <table className="data">
                  <thead>
                    <tr>
                      <th>Action</th>
                      <th>Reliability</th>
                      <th className="num">Succeeded</th>
                      <th className="num">Failed</th>
                      <th className="num">Partial</th>
                      <th className="num">Applied</th>
                      <th className="num">Success rate</th>
                    </tr>
                  </thead>
                  <tbody>
                    {actions.map((action) => (
                      <tr key={action.action_code}>
                        <td className="mono">{action.action_code}</td>
                        <td>
                          <Badge
                            tone={
                              action.reliability_label === "reliable"
                                ? "good"
                                : action.reliability_label === "unproven"
                                  ? "neutral"
                                  : action.reliability_label === "mixed"
                                    ? "warn"
                                    : "bad"
                            }
                          >
                            {humanise(action.reliability_label)}
                          </Badge>
                        </td>
                        <td className="num">{action.succeeded}</td>
                        <td className="num">{action.failed}</td>
                        <td className="num">{action.partial}</td>
                        <td className="num">{action.applied}</td>
                        <td className="num">
                          {action.success_rate === null ? "—" : formatPercent(action.success_rate)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <Card title="Root causes" subtitle="What actually keeps breaking">
            <BarList
              data={Object.entries(analysis.by_root_cause).map(([key, value]) => ({
                label: humanise(key),
                value,
                tone: "info",
              }))}
              format={(value) => String(value)}
            />
          </Card>

          <Card title="Severity mix">
            <StackedStrip
              segments={Object.entries(analysis.by_severity).map(([key, value]) => ({
                label: humanise(key),
                value,
                tone: toneForSeverity(key),
              }))}
            />
          </Card>

          <Card title="Verification outcomes" subtitle="Did recovery actually work?">
            <StackedStrip
              segments={[
                { label: "verified success", value: analysis.verified_success, tone: "good" },
                { label: "verification failed", value: analysis.verification_failed, tone: "bad" },
                {
                  label: "no verdict",
                  value: Math.max(0, analysis.verifications - analysis.verified_success - analysis.verification_failed),
                  tone: "neutral",
                },
              ]}
            />
          </Card>

          <Card title="Most fragile services" subtitle="Fragility, not blame" flush>
            {services.length === 0 ? (
              <Empty title="No incident history yet" />
            ) : (
              <div className="list">
                {[...services]
                  .sort((a, b) => b.incidents - a.incidents)
                  .slice(0, 10)
                  .map((service) => (
                    <div className="list-row" key={service.service_id}>
                      <div className="list-main">
                        <div className="list-title" style={{ fontSize: 12.5 }}>
                          {service.service}
                        </div>
                        <div className="list-meta">
                          <span>{service.tier}</span>
                          <span>
                            median MTTR {formatDuration(service.mean_mttr_seconds)}
                          </span>
                        </div>
                      </div>
                      <div className="list-side">
                        <span className="chip">{service.incidents} incidents</span>
                        <Badge tone={service.incidents >= 5 ? "bad" : service.incidents >= 3 ? "warn" : "neutral"}>
                          {service.incidents >= 5 ? "fragile" : service.incidents >= 3 ? "watch" : "stable"}
                        </Badge>
                      </div>
                    </div>
                  ))}
              </div>
            )}
          </Card>

          <Card title="What the numbers mean">
            <p className="small muted" style={{ margin: 0 }}>
              A pattern is only called a pattern when at least three incidents share a root cause, so
              a single bad night never becomes a trend. Reliability counts verified outcomes, not
              proposed actions: an action that ran and failed verification is recorded as a failure,
              which is what stops the platform from recommending it again.
            </p>
            <div className="btn-row" style={{ marginTop: 10 }}>
              <span className="chip" title={humanise("window")}>
                window {analysis.window_days}d
              </span>
              <span className="chip">min occurrences {patterns.data?.minimum_occurrences ?? 3}</span>
            </div>
          </Card>
        </div>
      </div>
    </>
  );
}
