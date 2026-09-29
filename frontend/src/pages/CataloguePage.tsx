import { Link } from "react-router-dom";
import { api } from "../api";
import { Badge, Banner, Card, Collapsible, Empty, ErrorBanner, Loading, Stat } from "../components/ui";
import { humanise, toneForStatus } from "../lib/format";
import { useAsync } from "../lib/hooks";

const RISK_TONE: Record<string, "good" | "warn" | "bad"> = {
  low: "good",
  medium: "warn",
  high: "bad",
  critical: "bad",
};

export default function CataloguePage() {
  const registry = useAsync(() => api.registry(), [], 60000);

  if (registry.loading && !registry.data) return <Loading label="Loading the action catalogue…" />;
  if (registry.error && !registry.data) return <ErrorBanner error={registry.error} onRetry={registry.reload} />;

  const actions = registry.data?.actions ?? [];
  const needsApproval = actions.filter((action) => action.required_approval).length;
  const shellFree = actions.every((action) => !action.executes_shell);
  const withVerification = actions.filter((action) => action.verification_workflow).length;

  return (
    <>
      <Banner tone="good">
        <div className="strong">The agent cannot invent a fix</div>
        <div>
          Remediation is a closed registry. The model may only choose from the actions below, each of
          which declares its risk, the environments it may touch, who must approve it, its timeout,
          and — crucially — how its effect is verified.
        </div>
      </Banner>

      <div className="grid stats">
        <Stat label="Registered actions" value={actions.length} />
        <Stat label="Require human approval" value={needsApproval} hint={`${actions.length - needsApproval} may run automatically`} tone="warn" />
        <Stat label="With a verification workflow" value={withVerification} hint="how success is judged, declared up front" tone="good" />
        <Stat
          label="Execute shell commands"
          value={actions.filter((action) => action.executes_shell).length}
          tone={shellFree ? "good" : "bad"}
          hint={shellFree ? "none — the registry is data, not command execution" : "review required"}
        />
      </div>

      <Card title="Action catalogue" subtitle={registry.data?.note} flush>
        {actions.length === 0 ? (
          <Empty title="The registry is empty" hint="Seed the catalogue to populate it." />
        ) : (
          <div className="list">
            {actions.map((action) => (
              <Collapsible
                key={action.code}
                summary={
                  <>
                    <div className="btn-row" style={{ gap: 6 }}>
                      <span className="list-title">{action.name}</span>
                      <Badge tone={RISK_TONE[action.risk_level] ?? "neutral"}>risk {action.risk_level}</Badge>
                      {action.required_approval ? (
                        <Badge tone="warn">approval required</Badge>
                      ) : (
                        <Badge tone="good">may be automatic</Badge>
                      )}
                      {action.is_enabled ? (
                        <Badge tone="neutral">enabled</Badge>
                      ) : (
                        <Badge tone="bad">disabled</Badge>
                      )}
                      {action.executes_shell && <Badge tone="bad">executes shell</Badge>}
                    </div>
                    <div className="list-meta">
                      <span className="mono">{action.code}</span>
                      <span>environments: {action.allowed_environments.join(", ")}</span>
                    </div>
                  </>
                }
                right={
                  <div className="btn-row">
                    <span className="chip">{action.historical_success} verified</span>
                    <span className="chip">{action.historical_failure} failed</span>
                  </div>
                }
              >
                <p className="small" style={{ marginTop: 0 }}>
                  {action.description}
                </p>

                <div className="grid cols-4" style={{ gap: 12 }}>
                  <div>
                    <div className="stat-label">Applicable root causes</div>
                    <div className="btn-row" style={{ gap: 5, marginTop: 4 }}>
                      {action.applicable_categories.map((category) => (
                        <span className="chip" key={category}>
                          {humanise(category)}
                        </span>
                      ))}
                    </div>
                  </div>
                  <div>
                    <div className="stat-label">Timeout / retries</div>
                    <div className="small muted">
                      {action.timeout_seconds}s · {action.max_retries} retr{action.max_retries === 1 ? "y" : "ies"}
                    </div>
                  </div>
                  <div>
                    <div className="stat-label">Execution</div>
                    <pre className="code">{JSON.stringify(action.execution_workflow, null, 2)}</pre>
                  </div>
                  <div>
                    <div className="stat-label">Verification</div>
                    <pre className="code">{JSON.stringify(action.verification_workflow, null, 2)}</pre>
                  </div>
                </div>

                {action.rollback_workflow && (
                  <div style={{ marginTop: 12 }}>
                    <div className="stat-label">Rollback</div>
                    <pre className="code">{JSON.stringify(action.rollback_workflow, null, 2)}</pre>
                  </div>
                )}
              </Collapsible>
            ))}
          </div>
        )}
      </Card>

      <div className="grid cols-2" style={{ gap: 16 }}>
        <Card title="How approval works">
          <ol className="small muted" style={{ paddingLeft: 18, margin: 0, lineHeight: 1.9 }}>
            <li>The agent proposes an action with a rationale and the memory it relied on.</li>
            <li>A deterministic safety gate checks the action is registered, permitted in this environment, applicable to the concluded root cause, and within the risk policy for the autonomy level.</li>
            <li>If approval is required, the run is parked as <span className="mono">pending approval</span> — it has not touched anything.</li>
            <li>Only after a human approves does the action execute, and then verification runs with the settle window the action itself declares.</li>
            <li>If verification fails, the failure is recorded, retained in memory, and the agent re-investigates rather than declaring victory.</li>
          </ol>
        </Card>

        <Card title="Why the settle window matters">
          <p className="small muted" style={{ marginTop: 0 }}>
            Restarting a process clears a saturated connection pool instantly, which looks like a
            perfect fix. If the underlying leak is still present, the pool refills during the settle
            window. Verification therefore waits, then re-measures, and the honest verdict is{" "}
            <span className="strong">verification failed</span>. That is the difference between an
            agent that reports success and one that checks.
          </p>
          <div className="btn-row" style={{ marginTop: 10 }}>
            <Badge tone={toneForStatus("verified_success")}>verified success</Badge>
            <Badge tone={toneForStatus("verification_failed")}>verification failed</Badge>
            <Badge tone={toneForStatus("pending")}>pending approval</Badge>
          </div>
          <div className="btn-row" style={{ marginTop: 12 }}>
            <Link className="btn small" to="/demo">
              Watch a failure get caught
            </Link>
          </div>
        </Card>
      </div>
    </>
  );
}
