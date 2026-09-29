import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { BarList, ChartLegend, LineChart } from "../components/charts";
import { Badge, Banner, Card, ErrorBanner, KeyValue, Stat } from "../components/ui";
import PageIntro from "../components/PageIntro";
import { formatDateTime, formatPercent, humanise, toneForVerdict } from "../lib/format";
import { useAction } from "../lib/hooks";
import type { DemoStep, LearningLoopResult } from "../types";

const PHASE_TONE: Record<string, string> = {
  incident_1_detected: "warn",
  incident_1_investigated: "info",
  failed_attempt: "bad",
  incident_1_resolved: "good",
  incident_2_resolved: "accent",
};

function StepFacts({ step }: { step: DemoStep }) {
  const facts = step.facts as Record<string, any>;
  const remediation = facts.remediation as Record<string, any> | undefined;
  const verification = facts.verification as Record<string, any> | undefined;
  const memory = facts.memory as Record<string, any> | undefined;
  const choice = (remediation?.choice ?? {}) as Record<string, any>;
  const recall = (memory?.recall ?? {}) as Record<string, any>;

  // Keys already surfaced as headline stats; the collapsed payload shows everything else.
  const consumed = new Set<string>();
  if (remediation) consumed.add("remediation");
  if (verification) consumed.add("verification");
  if (recall.memory_count !== undefined) consumed.add("memory");

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {(remediation || verification || recall.memory_count !== undefined) && (
        <div className="grid stats">
          {remediation && (
            <Stat
              label="Action chosen"
              value={choice.action_code ?? remediation.action_code ?? "—"}
              hint={choice.avoided_actions?.length ? `avoided ${choice.avoided_actions.length} action(s)` : undefined}
              tone="accent"
            />
          )}
          {verification && (
            <Stat
              label="Verification"
              value={humanise(verification.verdict ?? verification.outcome ?? "—")}
              tone={toneForVerdict(String(verification.verdict ?? ""))}
              hint={
                verification.improvement_pct !== undefined && verification.improvement_pct !== null
                  ? `${formatPercent(verification.improvement_pct)} improvement`
                  : verification.verdict_reason?.slice(0, 70)
              }
            />
          )}
          {recall.memory_count !== undefined && (
            <Stat label="Memory recalled" value={recall.memory_count} hint="items retrieved from the bank" />
          )}
        </div>
      )}

      {choice.avoided_actions?.length > 0 && (
        <Banner tone="warn">
          <div className="strong">Actions avoided because memory recorded their failure</div>
          <div className="btn-row" style={{ marginTop: 6 }}>
            {choice.avoided_actions.map((item: unknown, index: number) => (
              <span className="chip" key={index}>
                {typeof item === "string" ? item : JSON.stringify(item)}
              </span>
            ))}
          </div>
        </Banner>
      )}

      {verification?.verdict_reason && (
        <div>
          <div className="stat-label">Why that verdict</div>
          <p className="small muted" style={{ margin: "3px 0 0" }}>
            {verification.verdict_reason}
          </p>
        </div>
      )}

      <details>
        <summary className="faint small" style={{ cursor: "pointer" }}>
          Full step payload
        </summary>
        <div style={{ marginTop: 10 }}>
          <KeyValue
            data={Object.fromEntries(
              Object.entries(facts).filter(([key]) => !consumed.has(key)),
            )}
          />
        </div>
      </details>
    </div>
  );
}

export default function DemoPage() {
  const { can } = useAuth();
  const [result, setResult] = useState<LearningLoopResult | null>(null);

  const loop = useAction(() => api.runLearningLoop({ include_failed_attempt: true }));
  const clean = useAction(() => api.cleanRoom());
  const history = useAction(() => api.seedHistory(60, 45));

  const delta = result?.learning_delta;

  const curve = delta
    ? [
        { label: "cold start", value: delta.memory_before.documents },
        { label: "after incident 1", value: delta.memory_after_incident_1.documents },
        { label: "after incident 2", value: delta.memory_after_incident_2.documents },
      ]
    : [];

  const factsCurve = delta
    ? [
        { label: "cold start", value: delta.memory_before.facts },
        { label: "after incident 1", value: delta.memory_after_incident_1.facts },
        { label: "after incident 2", value: delta.memory_after_incident_2.facts },
      ]
    : [];

  return (
    <>
      <PageIntro
        icon="↻"
        title="The learning loop"
        what="One button that runs the whole story: something breaks, it is investigated, a fix fails, the failure is remembered, and a second similar incident is solved using that memory."
        why="This is the claim of the product made checkable in a single request. Every step below is produced by real telemetry and a real AI investigation, not a scripted animation — which is also why it takes a couple of minutes."
        lookFor={
          <>
            Watch for the <strong>failed verification</strong> in the middle of the run. That failure is
            the product: without it there is nothing to learn. Then watch the second incident skip the
            fix that already failed.
          </>
        }
      />

      <div className="grid side-left">
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <Card title="Run it yourself" subtitle="Two incidents, one failure, one lesson">
            <p className="small muted" style={{ marginTop: 0 }}>
              This drives the whole loop against the live simulator: it injects a real connection
              leak, investigates it with no precedent, watches a human apply a plausible-but-wrong
              fix, records the failure, re-investigates, verifies a working fix — then raises a
              second, similar incident on a different environment and lets memory do the work.
            </p>
            <div className="btn-row">
              <button
                className="btn primary"
                disabled={loop.running || !can("simulate_fault")}
                onClick={async () => {
                  const value = await loop.run();
                  if (value) setResult(value);
                }}
              >
                {loop.running ? <span className="spinner" /> : "↻"}
                {loop.running ? "Running the loop…" : "Run the complete learning loop"}
              </button>
            </div>
            {!can("simulate_fault") && (
              <Banner tone="warn">
                <div>
                  The loop injects its own faults, which needs the{" "}
                  <span className="mono">simulate_fault</span> permission held only by{" "}
                  <span className="mono">admin@acme.test</span>. Sign in as admin to run it.
                </div>
              </Banner>
            )}
            {loop.running && (
              <div className="small faint" style={{ marginTop: 8 }}>
                Advancing simulated time, detecting, investigating and verifying. This takes about
                two minutes — it is generating real telemetry and running real AI investigations,
                not replaying a recording. The page updates when the run finishes.
              </div>
            )}

            <hr style={{ border: "none", borderTop: "1px solid var(--border)", margin: "16px 0" }} />

            <div className="btn-row">
              <button
                className="btn small"
                disabled={history.running || !can("reset_environment")}
                onClick={() => void history.run()}
              >
                {history.running ? <span className="spinner" /> : "▤"} Seed 60-incident history
              </button>
              <button
                className="btn small"
                disabled={clean.running || !can("reset_environment")}
                onClick={() => {
                  void clean.run();
                  setResult(null);
                }}
              >
                {clean.running ? <span className="spinner" /> : "⌫"} Clean room
              </button>
            </div>
            <div className="small faint" style={{ marginTop: 8 }}>
              Seeding history gives the corpus something to recall from. The clean room wipes
              incidents, memory and attempts but keeps the catalogue.
            </div>

            {(history.result || clean.result) && (
              <pre className="code scroll" style={{ marginTop: 10 }}>
                {JSON.stringify(history.result ?? clean.result, null, 2)}
              </pre>
            )}
          </Card>

          {delta && (
            <Card title="The learning delta" subtitle="What changed between the two incidents">
              <div className="grid cols-2" style={{ gap: 18 }}>
                <div>
                  <div className="stat-label">Retained experience documents</div>
                  <LineChart
                    series={[{ name: "documents", colour: "#38bdf8", points: curve }]}
                    height={200}
                  />
                  <ChartLegend
                    series={[{ name: "retained experience documents", colour: "#38bdf8", points: curve }]}
                  />
                </div>
                <div>
                  <div className="stat-label">Atomic facts in the bank</div>
                  <LineChart
                    series={[{ name: "facts", colour: "#a78bfa", points: factsCurve }]}
                    height={200}
                  />
                  <ChartLegend series={[{ name: "atomic facts", colour: "#a78bfa", points: factsCurve }]} />
                </div>
              </div>
            </Card>
          )}

          {result && (
            <Card title="Narrative" subtitle={`scenario ${result.scenario} on ${result.service}`} flush>
              <div className="list">
                {result.steps.map((step) => (
                  <div
                    key={step.index}
                    className={`card step-card ${PHASE_TONE[step.phase] ?? "warn"}`}
                    style={{ margin: 14, borderRadius: 10 }}
                  >
                    <div className="card-head">
                      <div>
                        <h3>
                          {step.index}. {step.title}
                        </h3>
                        <div className="sub">
                          <Badge tone={(PHASE_TONE[step.phase] ?? "neutral") as never}>{humanise(step.phase)}</Badge>
                          <span style={{ marginLeft: 8 }} className="mono">
                            {step.evidence_refs.join(" · ")}
                          </span>
                        </div>
                      </div>
                    </div>
                    <div className="card-body">
                      <p style={{ marginTop: 0, whiteSpace: "pre-wrap" }}>{step.narrative}</p>
                      <StepFacts step={step} />
                    </div>
                  </div>
                ))}
              </div>
            </Card>
          )}
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          {loop.error ? <ErrorBanner error={loop.error} /> : null}

          {delta && (
            <>
              <Card title="Did it actually learn?" subtitle="The claim, measured">
                <div className="grid" style={{ gap: 10 }}>
                  <Stat
                    label="Tool calls, first attempt"
                    value={delta.incident_1_tool_calls}
                    hint="with no useful precedent"
                  />
                  <Stat
                    label="Tool calls, second incident"
                    value={delta.incident_2_tool_calls}
                    hint="with organizational memory"
                  />
                  <Stat
                    label="Memory items recalled on incident 2"
                    value={delta.incident_2_recall_hits}
                    tone="accent"
                  />
                  <Stat
                    label="Failed action avoided"
                    value={delta.failed_action_avoided_in_incident_2 ? "yes" : "no"}
                    tone={delta.failed_action_avoided_in_incident_2 ? "good" : "warn"}
                    hint="the restart that failed on incident 1"
                  />
                </div>
              </Card>

              <Card title="Outcome">
                <Banner tone={delta.failed_action_avoided_in_incident_2 ? "good" : "warn"}>
                  <div className="strong">
                    {delta.failed_action_avoided_in_incident_2
                      ? "Organizational memory changed the decision"
                      : "Memory did not change the decision this run"}
                  </div>
                  <div>
                    The second incident was resolved with{" "}
                    <span className="mono">{delta.incident_2_recommended}</span>, and the agent
                    explicitly avoided the action that had already failed — not because it was told
                    to, but because the failure was in the record.
                  </div>
                </Banner>
                <div style={{ marginTop: 12 }}>
                  <BarList
                    data={[
                      { label: "incident 1 tools", value: delta.incident_1_tool_calls, tone: "warn" },
                      { label: "incident 2 tools", value: delta.incident_2_tool_calls, tone: "good" },
                      { label: "recall hits", value: delta.incident_2_recall_hits, tone: "accent" },
                    ]}
                    format={(value) => String(value)}
                  />
                </div>
              </Card>

              <Card title="Retained memory" subtitle="Documents, facts and observations" flush>
                <div className="list">
                  {(["memory_before", "memory_after_incident_1", "memory_after_incident_2"] as const).map(
                    (key) => {
                      const stats = delta[key];
                      return (
                        <div className="list-row" key={key}>
                          <div className="list-main">
                            <div className="list-title" style={{ fontSize: 12.5 }}>
                              {humanise(key)}
                            </div>
                            <div className="list-meta">
                              <span className="mono">{stats.documents} docs</span>
                              <span className="mono">{stats.facts} facts</span>
                              <span className="mono">{stats.observations} obs</span>
                            </div>
                          </div>
                          <div className="list-side">
                            <Badge tone="neutral">{stats.backend}</Badge>
                          </div>
                        </div>
                      );
                    },
                  )}
                </div>
              </Card>

              <Card title="Learning ledger" subtitle="What was written and why" flush>
                <div className="list scroll-y">
                  {result?.learning_events.map((event) => (
                    <div className="list-row" key={event.id}>
                      <div className="list-main">
                        <div className="btn-row" style={{ gap: 6 }}>
                          <Badge tone="info">{event.kind}</Badge>
                          {event.incident_id !== null && (
                            <Link to={`/incidents/${event.incident_id}`} className="mono small">
                              #{event.incident_id}
                            </Link>
                          )}
                        </div>
                        <div className="small" style={{ marginTop: 3 }}>
                          {event.summary}
                        </div>
                        <div className="faint small">{formatDateTime(event.created_at)}</div>
                      </div>
                    </div>
                  ))}
                </div>
              </Card>
            </>
          )}

          {!result && !loop.running && (
            <Card title="What you are about to see">
              <ol className="small muted" style={{ paddingLeft: 18, margin: 0, lineHeight: 1.8 }}>
                <li>A connection leak is injected on <span className="mono">payment-service</span> in production, with a matching deployment in the change feed.</li>
                <li>The detector opens an incident from telemetry alone — no human typed it in.</li>
                <li>The agent investigates with <span className="strong">no useful precedent</span> and ranks hypotheses with evidence.</li>
                <li>A human applies <span className="mono">restart_service</span>. It clears the pool, but the leak is still live, so <span className="strong">verification fails</span> inside the settle window.</li>
                <li>That failure is retained as organizational experience.</li>
                <li>The agent re-investigates, cites the failed attempt, and applies a different action that verifies.</li>
                <li>A second, similar incident arrives in staging — and memory changes the outcome.</li>
              </ol>
            </Card>
          )}
        </div>
      </div>
    </>
  );
}
