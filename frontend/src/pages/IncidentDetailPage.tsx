import { useEffect, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { BarList, SimilarityAttribution } from "../components/charts";
import {
  Badge,
  Banner,
  Card,
  Collapsible,
  Empty,
  ErrorBanner,
  KeyValue,
  Loading,
  Stat,
  Tabs,
  Value,
} from "../components/ui";
import {
  formatDateTime,
  formatDuration,
  formatPercent,
  formatRelative,
  humanise,
  prettyJson,
  toneForSeverity,
  toneForStatus,
  toneForVerdict,
} from "../lib/format";
import { useAction, useAsync, useTicker } from "../lib/hooks";

type TabKey =
  | "summary"
  | "evidence"
  | "hypotheses"
  | "timeline"
  | "investigation"
  | "remediation"
  | "similar"
  | "postmortem";

const LIFECYCLE = [
  "detect",
  "evidence",
  "investigate",
  "recall",
  "reason",
  "recommend",
  "approve",
  "act",
  "verify",
  "learn",
  "remember",
];

export default function IncidentDetailPage() {
  const { id } = useParams();
  const incidentId = Number(id);
  const [params, setParams] = useSearchParams();
  const [tab, setTab] = useState<TabKey>((params.get("tab") as TabKey) ?? "summary");
  const { can } = useAuth();
  const now = useTicker(5000);

  const detail = useAsync(() => api.incident(incidentId), [incidentId], 15000);
  const similar = useAsync(() => api.similar(incidentId).catch(() => ({ incident_id: incidentId, neighbours: [] })), [incidentId]);
  const registry = useAsync(() => api.registry(), []);

  useEffect(() => {
    setParams(tab === "summary" ? {} : { tab }, { replace: true });
  }, [tab, setParams]);

  const investigate = useAction((execute: boolean) => api.investigate(incidentId, { execute }));
  const propose = useAction((actionCode: string, rationale: string) =>
    api.proposeRemediation(incidentId, { action_code: actionCode, rationale }),
  );
  const approve = useAction((runId: number) => api.approve(incidentId, runId, { reason: "approved from console" }));
  const executeRun = useAction((runId: number) => api.execute(incidentId, runId));

  const [actionCode, setActionCode] = useState("");
  const [rationale, setRationale] = useState("");

  const incident = detail.data;
  const investigation = incident?.investigations?.[incident.investigations.length - 1] ?? null;
  const latestVerification = incident?.verifications?.[incident.verifications.length - 1] ?? null;
  const latestRun = incident?.remediation_runs?.[incident.remediation_runs.length - 1] ?? null;

  useEffect(() => {
    if (investigation?.recommended_remediation && !actionCode) {
      setActionCode(investigation.recommended_remediation);
    }
  }, [investigation, actionCode]);

  const reached = useMemo(() => {
    if (!incident) return [] as string[];
    const phases: string[] = ["detect", "evidence"];
    if (incident.investigations.length > 0) phases.push("investigate", "recall", "reason", "recommend");
    if (incident.remediation_runs.some((run) => run.approved_at)) phases.push("approve");
    if (incident.remediation_runs.some((run) => run.executed_at)) phases.push("act");
    if (incident.verifications.length > 0) phases.push("verify");
    if (incident.memory_references.length > 0) phases.push("learn", "remember");
    return phases;
  }, [incident]);

  if (detail.loading && !incident) return <Loading label={`Loading incident #${incidentId}…`} />;
  if (detail.error && !incident) return <ErrorBanner error={detail.error} onRetry={detail.reload} />;
  if (!incident) return <Empty title="Incident not found" />;

  const actions = registry.data?.actions ?? [];

  function afterMutation() {
    detail.reload();
    similar.reload();
  }

  return (
    <>
      <div className="btn-row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <div className="btn-row" style={{ gap: 8, marginBottom: 4 }}>
            <Link to="/incidents" className="small">
              ← Incidents
            </Link>
            <span className="mono faint">#{incident.id}</span>
            <Badge tone={toneForSeverity(incident.severity)}>{incident.severity}</Badge>
            <Badge tone={toneForStatus(incident.status)}>{humanise(incident.status)}</Badge>
            {incident.is_historical && <Badge tone="neutral">historical</Badge>}
          </div>
          <h1 style={{ fontSize: 19 }}>{incident.title}</h1>
          <div className="faint small">
            {incident.service} · {incident.environment} · detected {formatDateTime(incident.detected_at)} (
            {formatRelative(incident.detected_at, now)})
          </div>
        </div>
        <div className="btn-row">
          <button
            className="btn"
            disabled={!can("investigate") || investigate.running || incident.status === "verified"}
            onClick={async () => {
              await investigate.run(false);
              afterMutation();
            }}
          >
            {investigate.running ? <span className="spinner" /> : "✦"}
            {investigate.running ? "Investigating…" : "Run AI investigation"}
          </button>
        </div>
      </div>

      <Card title="Agent loop" subtitle="How far through the loop this incident has travelled">
        <div className="loop">
          {LIFECYCLE.map((phase) => (
            <span key={phase} className={`loop-step ${reached.includes(phase) ? "done" : ""}`}>
              {phase}
            </span>
          ))}
        </div>
      </Card>

      {investigate.error ? <ErrorBanner error={investigate.error} /> : null}
      {propose.error ? <ErrorBanner error={propose.error} /> : null}
      {approve.error ? <ErrorBanner error={approve.error} /> : null}
      {executeRun.error ? <ErrorBanner error={executeRun.error} /> : null}

      <div className="grid stats">
        <Stat label="MTTR" value={formatDuration(incident.mttr_seconds)} hint={incident.resolved_at ? "to resolution" : "still open"} />
        <Stat label="Evidence items" value={incident.evidence.length} />
        <Stat label="Hypotheses" value={incident.hypotheses.length} hint={`${incident.hypotheses.filter((h) => h.status === "supported").length} supported`} />
        <Stat label="Remediation attempts" value={incident.remediation_runs.length} hint={`${incident.remediation_runs.filter((r) => r.outcome === "failed").length} failed`} tone={incident.remediation_runs.some((r) => r.outcome === "failed") ? "warn" : undefined} />
        <Stat
          label="Verification"
          value={latestVerification ? humanise(latestVerification.verdict) : "not run"}
          tone={latestVerification ? toneForVerdict(latestVerification.verdict) : "neutral"}
          hint={latestVerification?.improvement_pct !== null && latestVerification ? `${formatPercent(latestVerification.improvement_pct)} improvement` : undefined}
        />
        <Stat label="Remembered as" value={incident.memory_references.length} hint="retained memory documents" />
      </div>

      <Tabs
        active={tab}
        onChange={setTab}
        tabs={[
          { key: "summary", label: "Summary" },
          { key: "evidence", label: "Evidence", count: incident.evidence.length },
          { key: "hypotheses", label: "Hypotheses", count: incident.hypotheses.length },
          { key: "timeline", label: "Timeline", count: incident.timeline.length },
          { key: "investigation", label: "AI investigation", count: investigation?.tool_call_count },
          { key: "remediation", label: "Remediation & verification", count: incident.remediation_runs.length },
          { key: "similar", label: "Similar history", count: similar.data?.neighbours.length },
          { key: "postmortem", label: "Postmortem" },
        ]}
      />

      {tab === "summary" && (
        <div className="grid side">
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <Card title="Conclusion" subtitle="Only stated once evidence supports it">
              {investigation?.root_cause_category ? (
                <>
                  <div className="btn-row" style={{ marginBottom: 10 }}>
                    <Badge tone="accent">{humanise(investigation.root_cause_category)}</Badge>
                    <Badge tone={toneForStatus(investigation.status)}>{humanise(investigation.status)}</Badge>
                  </div>
                  <p style={{ marginTop: 0 }}>{investigation.root_cause_statement}</p>
                  {investigation.alternatives_ruled_out?.length > 0 && (
                    <div className="small muted">
                      <span className="stat-label">Alternatives ruled out</span>
                      <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                        {investigation.alternatives_ruled_out.map((item, index) => (
                          <li key={index}>{item}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                </>
              ) : (
                <Empty title="Root cause not established" hint="Run an investigation to collect evidence and rank hypotheses." />
              )}
            </Card>

            <Card title="Detection" subtitle="What the detector saw, and why it fired" >
              <div className="grid split" style={{ gap: 14 }}>
                <KeyValue
                  data={incident.detection_meta as Record<string, unknown> | null}
                  skip={["verdicts"]}
                />
                <div>
                  <div className="stat-label">Baseline at detection</div>
                  <BarList
                    data={Object.entries(incident.baseline ?? {})
                      .filter(([key]) => !["connections", "pool_size"].includes(key))
                      .slice(0, 8)
                      .map(([key, value]) => ({ label: humanise(key), value: Number(value), tone: "neutral" }))}
                    format={(value) => value.toFixed(3)}
                  />
                </div>
              </div>
            </Card>

            <Card title="Detector verdicts" subtitle="Each detector votes independently; a quorum decides" flush>
              {incident.detection_meta?.verdicts ? (
                <div className="list">
                  {Object.entries(incident.detection_meta.verdicts as Record<string, any>).map(([metric, verdict]) => (
                    <Collapsible
                      key={metric}
                      defaultOpen
                      summary={
                        <>
                          <div className="list-title">
                            {humanise(metric)}{" "}
                            <Badge tone={verdict.anomalous ? "bad" : "good"}>
                              {verdict.anomalous ? "anomalous" : "normal"}
                            </Badge>
                          </div>
                          <div className="list-meta">
                            <span>
                              {verdict.baseline} → {verdict.current}
                            </span>
                            <span>{formatPercent((verdict.relative_change ?? 0) * 100)} change</span>
                            <span>{verdict.direction}</span>
                          </div>
                        </>
                      }
                    >
                      <div className="small muted">{verdict.reason}</div>
                      <div style={{ marginTop: 10 }} className="bars">
                        {(verdict.detectors ?? []).map((detector: any) => (
                          <div className="bar-row" key={detector.detector}>
                            <span className="bar-label">{humanise(detector.detector)}</span>
                            <span className="bar-track" title={detector.message}>
                              <span
                                className="bar-fill"
                                style={{
                                  width: `${Math.min(100, (detector.score ?? 0) * 100)}%`,
                                  background: detector.breached ? "var(--bad)" : "var(--neutral)",
                                }}
                              />
                            </span>
                            <span className="bar-value">{detector.breached ? "breached" : "within"}</span>
                          </div>
                        ))}
                      </div>
                    </Collapsible>
                  ))}
                </div>
              ) : (
                <Empty title="No detector metadata recorded" />
              )}
            </Card>
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <Card title="Incident record">
              <KeyValue data={{ ...incident, evidence: undefined, hypotheses: undefined, timeline: undefined, investigations: undefined, remediation_runs: undefined, verifications: undefined, memory_references: undefined, postmortem: undefined, fingerprint: undefined, baseline: undefined, detection_meta: undefined, impact: undefined }} />
            </Card>

            <Card title="Fingerprint" subtitle="The structured signature used for recall">
              {incident.fingerprint ? (
                <KeyValue
                  data={incident.fingerprint as unknown as Record<string, unknown>}
                  skip={["metric_pattern"]}
                />
              ) : (
                <Empty title="No fingerprint yet" />
              )}
              {incident.fingerprint?.metric_pattern && (
                <div style={{ marginTop: 12 }}>
                  <div className="stat-label">Metric pattern</div>
                  <BarList
                    data={Object.entries(incident.fingerprint.metric_pattern).map(([key, value]) => ({
                      label: humanise(key),
                      value: value,
                      tone: "accent",
                    }))}
                    format={(value) => value.toFixed(3)}
                  />
                </div>
              )}
            </Card>

            <Card title="Memory references" subtitle="Exactly what this incident wrote to memory" flush>
              {incident.memory_references.length === 0 ? (
                <Empty title="Nothing retained yet" hint="Memory is written after the outcome is verified." />
              ) : (
                <div className="list">
                  {incident.memory_references.map((reference) => (
                    <div className="list-row" key={reference.id}>
                      <div className="list-main">
                        <div className="btn-row" style={{ gap: 6 }}>
                          <Badge tone="info">{humanise(reference.scope)}</Badge>
                          <span className="chip">{reference.backend}</span>
                          {reference.recall_count > 0 && (
                            <Badge tone="accent">recalled {reference.recall_count}×</Badge>
                          )}
                        </div>
                        <pre className="code" style={{ marginTop: 6 }}>
                          {reference.preview}
                        </pre>
                        <div className="list-meta">
                          <span className="mono">{reference.memory_id}</span>
                          <span>{formatDateTime(reference.retained_at)}</span>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </Card>
          </div>
        </div>
      )}

      {tab === "evidence" && (
        <Card title="Evidence" subtitle="Collected facts, each traceable to a source" flush>
          {incident.evidence.length === 0 ? (
            <Empty title="No evidence collected yet" />
          ) : (
            <div className="list">
              {incident.evidence.map((item) => (
                <Collapsible
                  key={item.id}
                  summary={
                    <>
                      <div className="list-title">{item.summary}</div>
                      <div className="list-meta">
                        <Badge tone="neutral">{humanise(item.kind)}</Badge>
                        <span>{item.source}</span>
                        <span>strength {item.strength}</span>
                        {item.retrieval && <span>retrieved via {item.retrieval}</span>}
                      </div>
                    </>
                  }
                  right={<span className="faint small">{formatDateTime(item.collected_at)}</span>}
                >
                  <KeyValue data={item.detail} />
                </Collapsible>
              ))}
            </div>
          )}
        </Card>
      )}

      {tab === "hypotheses" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          {incident.hypotheses.length === 0 ? (
            <Card title="Hypotheses">
              <Empty title="No hypotheses ranked yet" />
            </Card>
          ) : (
            incident.hypotheses.map((hypothesis) => (
              <Card
                key={hypothesis.id}
                title={`${hypothesis.code} · ${humanise(hypothesis.category)}`}
                subtitle={`Rank ${hypothesis.ranking}`}
                right={<Badge tone={toneForStatus(hypothesis.status)}>{humanise(hypothesis.status)}</Badge>}
              >
                <p style={{ marginTop: 0 }}>{hypothesis.statement}</p>
                <div className="grid cols-3" style={{ gap: 12 }}>
                  <div>
                    <div className="stat-label">Supporting</div>
                    {hypothesis.supporting.length === 0 ? (
                      <div className="faint small">none</div>
                    ) : (
                      <ul className="small muted" style={{ margin: "4px 0 0", paddingLeft: 16 }}>
                        {hypothesis.supporting.map((item, index) => (
                          <li key={index}>{item}</li>
                        ))}
                      </ul>
                    )}
                  </div>
                  <div>
                    <div className="stat-label">Contradicting</div>
                    {hypothesis.contradicting.length === 0 ? (
                      <div className="faint small">none</div>
                    ) : (
                      <ul className="small muted" style={{ margin: "4px 0 0", paddingLeft: 16 }}>
                        {hypothesis.contradicting.map((item, index) => (
                          <li key={index}>{item}</li>
                        ))}
                      </ul>
                    )}
                  </div>
                  <div>
                    <div className="stat-label">Missing evidence</div>
                    {hypothesis.missing.length === 0 ? (
                      <div className="faint small">none</div>
                    ) : (
                      <ul className="small muted" style={{ margin: "4px 0 0", paddingLeft: 16 }}>
                        {hypothesis.missing.map((item, index) => (
                          <li key={index}>{item}</li>
                        ))}
                      </ul>
                    )}
                  </div>
                </div>
                {hypothesis.tests.length > 0 && (
                  <div style={{ marginTop: 12 }}>
                    <div className="stat-label">Tests applied</div>
                    <div className="list" style={{ marginTop: 6 }}>
                      {hypothesis.tests.map((test, index) => (
                        <div className="list-row" key={index}>
                          <div className="list-main">
                            <div className="list-title" style={{ fontSize: 12.5 }}>
                              {test.name} <Badge tone={toneForStatus(test.verdict)}>{humanise(test.verdict)}</Badge>
                            </div>
                            <div className="list-meta">{test.explanation}</div>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </Card>
            ))
          )}
        </div>
      )}

      {tab === "timeline" && (
        <Card title="Timeline" subtitle="Every state change, in order" flush>
          {incident.timeline.length === 0 ? (
            <Empty title="No timeline events" />
          ) : (
            <div className="card-body">
              <div className="timeline">
                {incident.timeline.map((event) => (
                  <div key={event.id} className={`timeline-item ${toneForStatus(event.kind)}`}>
                    <div className="timeline-head">
                      <span className="timeline-time">{formatDateTime(event.ts)}</span>
                      <span className="timeline-title">{event.title}</span>
                      <Badge tone="neutral">{humanise(event.kind)}</Badge>
                      {event.actor && <span className="faint small">{event.actor}</span>}
                    </div>
                    <div className="timeline-desc">{event.description}</div>
                    {event.payload && Object.keys(event.payload).length > 0 && (
                      <details style={{ marginTop: 6 }}>
                        <summary className="faint small" style={{ cursor: "pointer" }}>
                          payload
                        </summary>
                        <pre className="code scroll" style={{ marginTop: 6 }}>
                          {prettyJson(event.payload)}
                        </pre>
                      </details>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </Card>
      )}

      {tab === "investigation" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          {!investigation ? (
            <Card title="AI investigation">
              <Empty
                title="No investigation recorded"
                hint="Run the AI investigation to see the tool calls, decisions and memory recall."
              />
            </Card>
          ) : (
            <>
              <div className="grid stats">
                <Stat label="Mode" value={investigation.mode} hint={investigation.model ?? "no model configured"} />
                <Stat label="Tool calls" value={investigation.tool_call_count} hint={`${investigation.round_count} rounds`} />
                <Stat label="Memory hits" value={investigation.memory_hits} hint={investigation.memory_recall_used ? "prior experience used" : "no useful precedent"} tone={investigation.memory_recall_used ? "good" : "neutral"} />
                <Stat label="Duration" value={`${(investigation.duration_ms / 1000).toFixed(1)}s`} />
                <Stat
                  label="Tokens"
                  value={`${investigation.prompt_tokens ?? 0}/${investigation.completion_tokens ?? 0}`}
                  hint="prompt / completion"
                />
                <Stat label="Autonomy" value={`L${investigation.autonomy_level}`} />
              </div>

              {investigation.summary && (
                <Card title="Summary">
                  <p style={{ margin: 0 }}>{investigation.summary}</p>
                </Card>
              )}

              <Card title="Recommendation" subtitle="The agent proposes; policy and a human decide">
                <div className="btn-row" style={{ marginBottom: 10 }}>
                  <Badge tone="accent">{investigation.recommended_remediation ?? "none"}</Badge>
                </div>
                <p style={{ margin: 0 }}>{investigation.recommendation_reason}</p>
              </Card>

              <Card title="Decisions" subtitle="Every bounded decision point, with its rationale" flush>
                {investigation.decisions.length === 0 ? (
                  <Empty title="No decisions recorded" />
                ) : (
                  <div className="list">
                    {investigation.decisions.map((decision) => (
                      <div className="list-row" key={decision.id}>
                        <div className="list-main">
                          <div className="btn-row" style={{ gap: 6 }}>
                            <Badge tone="info">{humanise(decision.stage)}</Badge>
                            {decision.status_label && (
                              <Badge tone={toneForStatus(decision.status_label)}>
                                {humanise(decision.status_label)}
                              </Badge>
                            )}
                          </div>
                          <div className="strong small" style={{ marginTop: 4 }}>
                            {decision.decision}
                          </div>
                          <div className="faint small">{decision.rationale}</div>
                          {decision.memory_ids.length > 0 && (
                            <div className="list-meta">
                              <span className="chip">{decision.memory_ids.length} memory citations</span>
                            </div>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </Card>

              <Card
                title="Tool calls"
                subtitle="Typed, bounded, audited — the agent cannot invent a capability"
                flush
              >
                <div className="list">
                  {investigation.tool_calls.map((call) => (
                    <Collapsible
                      key={call.seq}
                      summary={
                        <>
                          <div className="list-title mono" style={{ fontSize: 12.5 }}>
                            {call.tool}
                          </div>
                          <div className="list-meta">
                            <span className="mono">#{call.seq}</span>
                            <span>round {call.round}</span>
                            <span>{call.duration_ms} ms</span>
                            {call.decision && <span className="faint">{call.decision}</span>}
                          </div>
                        </>
                      }
                      right={
                        <Badge tone={call.ok ? "good" : "bad"}>{call.ok ? "ok" : call.error_code ?? "error"}</Badge>
                      }
                    >
                      <div className="grid cols-2" style={{ gap: 10 }}>
                        <div>
                          <div className="stat-label">Arguments</div>
                          <pre className="code scroll">{prettyJson(call.arguments)}</pre>
                        </div>
                        <div>
                          <div className="stat-label">Result</div>
                          <pre className="code scroll">{prettyJson(call.result ?? call.error_message)}</pre>
                        </div>
                      </div>
                    </Collapsible>
                  ))}
                </div>
              </Card>
            </>
          )}
        </div>
      )}

      {tab === "remediation" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <Card
            title="Propose a remediation"
            subtitle="Any registered action can be proposed; policy decides whether it may run"
          >
            <div className="form-row">
              <label className="field" style={{ flex: 2 }}>
                Action
                <select value={actionCode} onChange={(event) => setActionCode(event.target.value)}>
                  <option value="">Select an action…</option>
                  {actions.map((action) => (
                    <option key={action.code} value={action.code}>
                      {action.name} ({action.code}) · risk {action.risk_level}
                      {action.required_approval ? " · approval required" : ""}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field" style={{ flex: 3 }}>
                Rationale
                <input
                  value={rationale}
                  placeholder="Why this action is appropriate"
                  onChange={(event) => setRationale(event.target.value)}
                />
              </label>
              <button
                className="btn primary"
                disabled={!actionCode || propose.running || !can("propose_remediation")}
                onClick={async () => {
                  await propose.run(actionCode, rationale);
                  afterMutation();
                }}
              >
                {propose.running ? <span className="spinner" /> : null}
                Propose
              </button>
            </div>
            {investigation?.recommended_remediation && (
              <div className="small faint" style={{ marginTop: 8 }}>
                The AI recommended <span className="mono">{investigation.recommended_remediation}</span>. You are free to
                choose otherwise — a failed attempt is recorded and remembered, which is the point.
              </div>
            )}
          </Card>

          {incident.remediation_runs.length === 0 ? (
            <Card title="Attempts">
              <Empty title="No remediation attempted yet" />
            </Card>
          ) : (
            incident.remediation_runs.map((run) => {
              const verification = incident.verifications.find((item) => item.remediation_run_id === run.id);
              return (
                <Card
                  key={run.id}
                  className={`step-card ${run.outcome === "failed" ? "bad" : run.outcome === "succeeded" ? "good" : "warn"}`}
                  title={`Attempt ${run.attempt} · ${run.action_name}`}
                  subtitle={`${run.action_code} · risk ${run.risk_level} · proposed by ${run.proposed_by}`}
                  right={
                    <div className="btn-row">
                      <Badge tone={toneForStatus(run.status)}>{humanise(run.status)}</Badge>
                      {run.outcome && <Badge tone={toneForVerdict(run.outcome === "succeeded" ? "verified_success" : "verification_failed")}>{humanise(run.outcome)}</Badge>}
                    </div>
                  }
                >
                  {run.rationale && <p style={{ marginTop: 0 }}>{run.rationale}</p>}

                  {run.blocked_reasons && run.blocked_reasons.length > 0 && (
                    <Banner tone="warn">
                      <div className="strong">Gated by policy</div>
                      <ul style={{ margin: "4px 0 0", paddingLeft: 16 }}>
                        {run.blocked_reasons.map((reason, index) => (
                          <li key={index} className="mono small">
                            {reason}
                          </li>
                        ))}
                      </ul>
                    </Banner>
                  )}

                  {run.safety_checks && (
                    <details style={{ marginTop: 10 }}>
                      <summary className="faint small" style={{ cursor: "pointer" }}>
                        Safety gate checks ({Array.isArray(run.safety_checks) ? run.safety_checks.length : "see detail"})
                      </summary>
                      <pre className="code scroll" style={{ marginTop: 6 }}>
                        {prettyJson(run.safety_checks)}
                      </pre>
                    </details>
                  )}

                  <div className="btn-row" style={{ marginTop: 12 }}>
                    {!run.approved_at && (
                      <button
                        className="btn"
                        disabled={approve.running || !can("approve_remediation")}
                        onClick={async () => {
                          await approve.run(run.id);
                          afterMutation();
                        }}
                      >
                        {approve.running ? <span className="spinner" /> : "✓"} Approve
                      </button>
                    )}
                    {run.approved_at && !run.executed_at && (
                      <button
                        className="btn primary"
                        disabled={executeRun.running || !can("execute_remediation")}
                        onClick={async () => {
                          await executeRun.run(run.id);
                          afterMutation();
                        }}
                      >
                        {executeRun.running ? <span className="spinner" /> : "▶"} Execute and verify
                      </button>
                    )}
                    {run.approved_at && (
                      <span className="chip">approved by {run.approved_by}</span>
                    )}
                    {run.executed_at && <span className="chip">executed {formatRelative(run.executed_at, now)}</span>}
                  </div>

                  {verification && (
                    <div style={{ marginTop: 16 }}>
                      <div className="btn-row" style={{ marginBottom: 8 }}>
                        <Badge tone={toneForVerdict(verification.verdict)}>
                          {humanise(verification.verdict)}
                        </Badge>
                        {verification.improvement_pct !== null && (
                          <span className="chip">{formatPercent(verification.improvement_pct)} improvement</span>
                        )}
                        <span className="chip">settle {formatDuration(verification.settle_seconds)}</span>
                      </div>
                      <p className="small muted" style={{ marginTop: 0 }}>
                        {verification.verdict_reason}
                      </p>
                      <div className="table-wrap">
                        <table className="data">
                          <thead>
                            <tr>
                              <th>Check</th>
                              <th>Metric</th>
                              <th className="num">Before</th>
                              <th className="num">After</th>
                              <th className="num">Baseline</th>
                              <th>Result</th>
                            </tr>
                          </thead>
                          <tbody>
                            {verification.checks.map((check, index) => (
                              <tr key={index}>
                                <td>
                                  <span className="strong">{humanise(check.check_name)}</span>
                                  {check.is_primary && (
                                    <>
                                      {" "}
                                      <Badge tone="accent">primary</Badge>
                                    </>
                                  )}
                                  <div className="faint small">{check.detail}</div>
                                </td>
                                <td className="mono small">{check.metric}</td>
                                <td className="num">{check.before ?? "—"}</td>
                                <td className="num">{check.after ?? "—"}</td>
                                <td className="num">{check.baseline ?? "—"}</td>
                                <td>
                                  <Badge tone={check.passed ? "good" : "bad"}>
                                    {check.passed === null ? "n/a" : check.passed ? "passed" : "failed"}
                                  </Badge>
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  )}
                </Card>
              );
            })
          )}

          {latestRun && latestVerification && latestVerification.before && latestVerification.after && (
            <Card title="Before and after" subtitle="Measured from generated telemetry, not hand-written numbers">
              <div className="grid cols-2" style={{ gap: 18 }}>
                <div>
                  <div className="stat-label">Before ({formatDateTime(latestVerification.started_at)})</div>
                  <BarList
                    data={Object.entries(latestVerification.before)
                      .filter(([, value]) => typeof value === "number")
                      .slice(0, 9)
                      .map(([key, value]) => ({ label: humanise(key), value: Number(value), tone: "bad" }))}
                    format={(value) => value.toFixed(2)}
                  />
                </div>
                <div>
                  <div className="stat-label">After ({formatDateTime(latestVerification.completed_at)})</div>
                  <BarList
                    data={Object.entries(latestVerification.after)
                      .filter(([, value]) => typeof value === "number")
                      .slice(0, 9)
                      .map(([key, value]) => ({ label: humanise(key), value: Number(value), tone: "good" }))}
                    format={(value) => value.toFixed(2)}
                  />
                </div>
              </div>
            </Card>
          )}
        </div>
      )}

      {tab === "similar" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <Card
            title="Why these incidents were recalled"
            subtitle="A weighted, per-feature comparison — every number can be argued with"
          >
            <p className="small muted" style={{ marginTop: 0 }}>
              Similarity is a deterministic blend of seven signals. The bars show how much of the final
              score each feature contributed for each neighbour, so a reviewer can see that two
              incidents match on error signatures rather than merely on service name.
            </p>
          </Card>

          {similar.loading && !similar.data ? (
            <Loading />
          ) : (similar.data?.neighbours.length ?? 0) === 0 ? (
            <Card title="Similar history">
              <Empty
                title="No comparable history"
                hint="Nothing in the corpus is close enough to cite. That is a valid, honest answer."
              />
            </Card>
          ) : (
            similar.data!.neighbours.map((neighbour) => (
              <Card
                key={neighbour.incident_id}
                title={
                  <Link to={`/incidents/${neighbour.incident_id}`}>
                    #{neighbour.incident_id} · {neighbour.title}
                  </Link>
                }
                subtitle={`${neighbour.service} · ${neighbour.severity} · ${formatDateTime(neighbour.detected_at)}`}
              >
                <SimilarityAttribution breakdown={neighbour.similarity} />
              </Card>
            ))
          )}
        </div>
      )}

      {tab === "postmortem" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          {!incident.postmortem ? (
            <Card title="Postmortem">
              <Empty
                title="No postmortem for this incident"
                hint="A postmortem is authored once recovery has been verified. Until then there is nothing honest to write."
              />
            </Card>
          ) : (
            <Card
              title="Postmortem"
              subtitle={`authored by ${incident.postmortem.authored_by ?? "unknown"} · ${formatDateTime(incident.postmortem.created_at)}`}
              right={
                <div className="btn-row">
                  <Badge tone={toneForStatus(incident.postmortem.status)}>{humanise(incident.postmortem.status)}</Badge>
                  {!incident.postmortem.approved_at && can("finalize_postmortem") && (
                    <button
                      className="btn small"
                      onClick={async () => {
                        await api.approvePostmortem(incident.postmortem!.id);
                        detail.reload();
                      }}
                    >
                      Approve
                    </button>
                  )}
                </div>
              }
            >
              {(
                [
                  ["Summary", incident.postmortem.summary],
                  ["Impact", incident.postmortem.impact],
                  ["Detection", incident.postmortem.detection],
                  ["Root cause", incident.postmortem.root_cause],
                  ["Successful remediation", incident.postmortem.successful_remediation],
                  ["Verification", incident.postmortem.verification],
                  ["Deployment relationship", incident.postmortem.deployment_relationship],
                ] as [string, string | null][]
              ).map(([label, text]) => (
                <div key={label} style={{ marginBottom: 14 }}>
                  <div className="stat-label">{label}</div>
                  <p className="small" style={{ margin: "3px 0 0", whiteSpace: "pre-wrap" }}>
                    {text ?? "—"}
                  </p>
                </div>
              ))}

              {incident.postmortem.failed_attempts.length > 0 && (
                <div style={{ marginBottom: 14 }}>
                  <div className="stat-label">Failed attempts (the part most postmortems omit)</div>
                  {incident.postmortem.failed_attempts.map((attempt, index) => (
                    <div className="banner bad" key={index} style={{ marginTop: 6 }}>
                      <span>✕</span>
                      <Value value={attempt} />
                    </div>
                  ))}
                </div>
              )}

              <div className="grid cols-2" style={{ gap: 16 }}>
                <div>
                  <div className="stat-label">Lessons</div>
                  <ul className="small muted" style={{ margin: "4px 0 0", paddingLeft: 16 }}>
                    {incident.postmortem.lessons.map((lesson, index) => (
                      <li key={index}>{lesson}</li>
                    ))}
                  </ul>
                </div>
                <div>
                  <div className="stat-label">Preventive actions</div>
                  <ul className="small muted" style={{ margin: "4px 0 0", paddingLeft: 16 }}>
                    {incident.postmortem.preventive_actions.map((action, index) => (
                      <li key={index}>{action}</li>
                    ))}
                  </ul>
                </div>
              </div>
            </Card>
          )}
        </div>
      )}
    </>
  );
}
