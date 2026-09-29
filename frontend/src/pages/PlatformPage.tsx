import { useState } from "react";
import { api } from "../api";
import { useAuth } from "../auth";
import { Badge, Banner, Card, Empty, ErrorBanner, KeyValue, Loading, Stat, Tabs } from "../components/ui";
import { formatDateTime, formatRelative, humanise, toneForStatus } from "../lib/format";
import { useAction, useAsync, useTicker } from "../lib/hooks";

export default function PlatformPage() {
  const { can } = useAuth();
  const now = useTicker(5000);
  const [tab, setTab] = useState<"simulator" | "deployments" | "audit" | "integrations">("simulator");

  const sim = useAsync(() => api.simState(), [], 5000);
  const incidents = useAsync(() => api.incidents({ limit: 100 }), [], 20000);
  const scenarios = useAsync(() => api.scenarios(), [], 60000);
  const deployments = useAsync(() => api.deployments(60), [], 30000);
  const audit = useAsync(() => api.audit(150), [], 30000);
  const integrations = useAsync(() => api.integrations(), [], 60000);
  const services = useAsync(() => api.services(), [], 20000);

  const [scenario, setScenario] = useState("connection_exhaustion");
  const [service, setService] = useState("payment-service");
  const [environment, setEnvironment] = useState("production");
  const [seconds, setSeconds] = useState("600");

  const advance = useAction((value: number) => api.advance(value));
  const inject = useAction(() =>
    api.injectFault({ scenario, service, environment }),
  );
  const pause = useAction(() => api.pause());
  const resume = useAction(() => api.resume());

  const canSimulate = can("simulate_fault");

  return (
    <>
      <div className="grid stats">
        <Stat
          label="Simulated clock"
          value={sim.data ? formatDateTime(sim.data.sim_time).slice(-8) : "—"}
          hint={sim.data ? `tick ${sim.data.tick_count}` : undefined}
        />
        <Stat label="Simulator" value={sim.data?.running ? "running" : "paused"} tone={sim.data?.running ? "good" : "warn"} />
        <Stat label="Managed services" value={Object.keys(sim.data?.services ?? {}).length} />
        <Stat label="Incidents on record" value={incidents.data?.count ?? 0} />
        <Stat label="Audit entries" value={audit.data?.count ?? 0} hint="every write is attributable" />
      </div>

      <Tabs
        active={tab}
        onChange={setTab}
        tabs={[
          { key: "simulator", label: "Simulator" },
          { key: "deployments", label: "Deployments", count: deployments.data?.deployments.length },
          { key: "audit", label: "Audit trail", count: audit.data?.entries.length },
          { key: "integrations", label: "Integrations" },
        ]}
      />

      {tab === "simulator" && (
        <div className="grid side">
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <Card
              title="Stage an incident"
              subtitle="Inject a fault, advance the clock, and let detection open the incident on its own"
            >
              <div className="form-row">
                <label className="field" style={{ flex: 3 }}>
                  Scenario
                  <select value={scenario} onChange={(event) => setScenario(event.target.value)}>
                    {(scenarios.data?.scenarios ?? []).map((item) => (
                      <option key={item.key} value={item.key}>
                        {humanise(item.key)} · {item.root_cause_category}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field" style={{ flex: 2 }}>
                  Service
                  <select value={service} onChange={(event) => setService(event.target.value)}>
                    {Object.keys(sim.data?.services ?? {}).map((name) => (
                      <option key={name} value={name}>
                        {name}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field" style={{ flex: 1, minWidth: 120 }}>
                  Environment
                  <select value={environment} onChange={(event) => setEnvironment(event.target.value)}>
                    <option value="production">production</option>
                    <option value="staging">staging</option>
                    <option value="development">development</option>
                  </select>
                </label>
                <button
                  className="btn danger"
                  disabled={!canSimulate || inject.running}
                  onClick={() => void inject.run()}
                >
                  {inject.running ? <span className="spinner" /> : "⚠"} Inject fault
                </button>
              </div>

              {scenarios.data && (
                <div className="small muted" style={{ marginTop: 10 }}>
                  {scenarios.data.scenarios.find((item) => item.key === scenario)?.description}
                </div>
              )}

              <div className="btn-row" style={{ marginTop: 16 }}>
                <input
                  value={seconds}
                  onChange={(event) => setSeconds(event.target.value)}
                  style={{ width: 100 }}
                  inputMode="numeric"
                />
                <button
                  className="btn"
                  disabled={!canSimulate || advance.running}
                  onClick={() => void advance.run(Number(seconds) || 600)}
                >
                  {advance.running ? <span className="spinner" /> : "▶"} Advance clock
                </button>
                <button
                  className="btn small"
                  disabled={!canSimulate || pause.running}
                  onClick={() => {
                    void pause.run();
                    sim.reload();
                  }}
                >
                  Pause
                </button>
                <button
                  className="btn small"
                  disabled={!canSimulate || resume.running}
                  onClick={() => {
                    void resume.run();
                    sim.reload();
                  }}
                >
                  Resume
                </button>
              </div>

              {!canSimulate && (
                <div className="small faint" style={{ marginTop: 10 }}>
                  Injecting faults requires the <span className="mono">simulate_fault</span> permission,
                  which only <span className="mono">admin@acme.test</span> holds. Sign in as admin to
                  drive the simulator.
                </div>
              )}

              {advance.error ? <ErrorBanner error={advance.error} /> : null}
              {inject.error ? <ErrorBanner error={inject.error} /> : null}
              {advance.result ? (
                <pre className="code scroll" style={{ marginTop: 12 }}>
                  {JSON.stringify(advance.result, null, 2)}
                </pre>
              ) : null}
            </Card>

            <Card title="Service estate" subtitle="Live state straight out of the simulator" flush>
              <div className="table-wrap">
                <table className="data">
                  <thead>
                    <tr>
                      <th>Service</th>
                      <th>Tier</th>
                      <th>Owner</th>
                      <th className="num">Connections</th>
                      <th className="num">Pool</th>
                      <th className="num">Latency</th>
                      <th className="num">Errors</th>
                      <th className="num">Heap</th>
                      <th className="num">Disk</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(services.data?.services ?? []).map((item) => {
                      const gauge = (sim.data?.services ?? {})[item.name] ?? {};
                      return (
                        <tr key={item.id}>
                          <td>
                            <span className="strong">{item.name}</span>
                            {item.active_fault && (
                              <>
                                {" "}
                                <Badge tone="bad">faulted</Badge>
                              </>
                            )}
                          </td>
                          <td>{item.tier}</td>
                          <td className="small">{humanise(item.owner_team)}</td>
                          <td className="num">{gauge.connections?.toFixed(0) ?? "—"}</td>
                          <td className="num">{gauge.pool_size?.toFixed(0) ?? "—"}</td>
                          <td className="num">{gauge.latency_ms?.toFixed(0) ?? "—"} ms</td>
                          <td className="num">{((gauge.error_rate ?? 0) * 100).toFixed(2)}%</td>
                          <td className="num">{gauge.mem_pct?.toFixed(0) ?? "—"}%</td>
                          <td className="num">{gauge.disk_pct?.toFixed(0) ?? "—"}%</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </Card>
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <Card title="Available scenarios" subtitle="Causally built, not scripted outcomes" flush>
              <div className="list scroll-y">
                {(scenarios.data?.scenarios ?? []).map((item) => (
                  <div className="list-row" key={item.key}>
                    <div className="list-main">
                      <div className="btn-row" style={{ gap: 6 }}>
                        <span className="list-title" style={{ fontSize: 12.5 }}>
                          {humanise(item.key)}
                        </span>
                        <Badge tone="warn">{item.severity}</Badge>
                      </div>
                      <div className="small muted">{item.description}</div>
                      <div className="list-meta">
                        <span>root cause: {item.root_cause_category}</span>
                      </div>
                      <div className="btn-row" style={{ marginTop: 6 }}>
                        <span className="faint small">canonical fix:</span>
                        {item.canonical_actions.map((action) => (
                          <Badge tone="good" key={action}>
                            {action}
                          </Badge>
                        ))}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </Card>

            <Card title="Active faults" subtitle="Injected faults currently driving telemetry" flush>
              {(services.data?.services ?? []).filter((item) => item.active_fault).length === 0 ? (
                <Empty
                  title="No faults injected"
                  hint="Inject one above and advance the clock; the detector will open an incident once the deviation is unambiguous."
                />
              ) : (
                <div className="list">
                  {(services.data?.services ?? [])
                    .filter((item) => item.active_fault)
                    .map((item) => (
                      <div className="list-row" key={item.id}>
                        <div className="list-main">
                          <div className="list-title" style={{ fontSize: 12.5 }}>
                            {item.name}
                          </div>
                          <div className="list-meta">
                            {(item.active_fault as Record<string, unknown>) &&
                              Object.entries(item.active_fault as Record<string, unknown>)
                                .slice(0, 6)
                                .map(([key, value]) => (
                                  <span className="chip" key={key}>
                                    {humanise(key)}: {String(value)}
                                  </span>
                                ))}
                          </div>
                        </div>
                        <div className="list-side">
                          <Badge tone="bad">faulted</Badge>
                        </div>
                      </div>
                    ))}
                </div>
              )}
            </Card>
          </div>
        </div>
      )}

      {tab === "deployments" && (
        <Card title="Change feed" subtitle="Deployments are correlated with incidents, never blamed automatically" flush>
          {(deployments.data?.deployments.length ?? 0) === 0 ? (
            <Empty title="No deployments recorded" />
          ) : (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Version</th>
                    <th>Service</th>
                    <th>Environment</th>
                    <th>Change</th>
                    <th className="num">Risk</th>
                    <th>Started</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {deployments.data!.deployments.map((deployment) => (
                    <tr key={deployment.id}>
                      <td className="mono">{deployment.version}</td>
                      <td>
                        {deployment.service}
                        {deployment.is_suspected_cause && (
                          <>
                            {" "}
                            <Badge tone="bad">suspected cause</Badge>
                          </>
                        )}
                      </td>
                      <td>{deployment.environment}</td>
                      <td>
                        <span className="small">{humanise(deployment.change_class)}</span>
                        <div className="faint small">{deployment.commit_message?.slice(0, 80)}</div>
                      </td>
                      <td className="num">
                        {deployment.risk_score === null ? "—" : deployment.risk_score.toFixed(2)}
                      </td>
                      <td className="nowrap small">
                        {formatRelative(deployment.started_at, now)}
                        <div className="faint">{deployment.author}</div>
                      </td>
                      <td>
                        <Badge tone={toneForStatus(deployment.status)}>{humanise(deployment.status)}</Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}

      {tab === "audit" && (
        <Card
          title="Audit trail"
          subtitle="Who did what, with what result, and why — including refused actions"
          flush
        >
          {audit.loading && !audit.data ? (
            <Loading />
          ) : (audit.data?.entries.length ?? 0) === 0 ? (
            <Empty title="No audit entries" />
          ) : (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>Actor</th>
                    <th>Action</th>
                    <th>Target</th>
                    <th>Result</th>
                    <th>Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {audit.data!.entries.map((entry) => (
                    <tr key={entry.id}>
                      <td className="nowrap small">{formatDateTime(entry.created_at)}</td>
                      <td>
                        <span className="small">{entry.actor}</span>
                        <div className="faint small">{entry.actor_role}</div>
                      </td>
                      <td className="mono small">{entry.action}</td>
                      <td className="mono small">
                        {entry.target_type}
                        {entry.target_id ? `#${entry.target_id}` : ""}
                      </td>
                      <td>
                        <Badge tone={toneForStatus(entry.result)}>{humanise(entry.result)}</Badge>
                      </td>
                      <td className="small muted">{entry.reason ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}

      {tab === "integrations" && (
        <div className="grid cols-2" style={{ gap: 16 }}>
          <Card title="Configured integrations" subtitle="Secrets are never sent to the browser" flush>
            <div className="list">
              {((integrations.data?.integrations as unknown[]) ?? []).map((raw, index) => {
                const item = raw as Record<string, any>;
                return (
                  <div className="list-row" key={index}>
                    <div className="list-main">
                      <div className="btn-row" style={{ gap: 6 }}>
                        <span className="list-title" style={{ fontSize: 12.5 }}>
                          {item.name}
                        </span>
                        <Badge tone={item.status === "connected" ? "good" : "neutral"}>{humanise(item.status)}</Badge>
                        <Badge tone={item.has_secret ? "good" : "neutral"}>
                          {item.has_secret ? "secret present" : "no secret"}
                        </Badge>
                      </div>
                      <div className="list-meta">
                        <span className="mono">{item.kind}</span>
                        {item.base_url && <span className="mono">{item.base_url}</span>}
                      </div>
                      {item.last_error && <div className="banner bad">{item.last_error}</div>}
                    </div>
                  </div>
                );
              })}
            </div>
          </Card>

          <Card title="Runtime posture" subtitle="What the platform is actually using right now">
            <KeyValue data={(integrations.data?.runtime as Record<string, unknown>) ?? null} />
            <Banner tone="info">
              <div>
                The browser is told whether a secret exists, never its value:{" "}
                <span className="mono">
                  {JSON.stringify(integrations.data?.secrets_exposed_to_browser ?? [])}
                </span>
              </div>
            </Banner>
          </Card>
        </div>
      )}
    </>
  );
}
