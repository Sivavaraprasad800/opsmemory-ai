import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { BarList } from "../components/charts";
import { Badge, Card, Empty, ErrorBanner, Loading, Stat } from "../components/ui";
import { formatDateTime, formatRelative, humanise } from "../lib/format";
import { useAsync, useTicker } from "../lib/hooks";

const FILTERS: { key: string; label: string; params: { memory_used?: boolean } }[] = [
  { key: "all", label: "All runs", params: {} },
  { key: "memory", label: "With prior experience", params: { memory_used: true } },
  { key: "cold", label: "Cold start", params: { memory_used: false } },
];

export default function InvestigationsPage() {
  const now = useTicker(10000);
  const [filter, setFilter] = useState("all");
  const active = FILTERS.find((item) => item.key === filter) ?? FILTERS[0];

  const runs = useAsync(() => api.investigations({ limit: 200, ...active.params }), [filter], 20000);

  if (runs.loading && !runs.data) return <Loading label="Loading investigations…" />;
  if (runs.error && !runs.data) return <ErrorBanner error={runs.error} onRetry={runs.reload} />;

  const list = runs.data?.investigations ?? [];
  const withMemory = list.filter((run) => run.memory_recall_used);
  const avgTools =
    list.length === 0 ? 0 : list.reduce((sum, run) => sum + run.tool_call_count, 0) / list.length;
  const avgToolsWithMemory =
    withMemory.length === 0
      ? 0
      : withMemory.reduce((sum, run) => sum + run.tool_call_count, 0) / withMemory.length;
  const coldRuns = list.filter((run) => !run.memory_recall_used);
  const avgToolsCold =
    coldRuns.length === 0 ? 0 : coldRuns.reduce((sum, run) => sum + run.tool_call_count, 0) / coldRuns.length;

  return (
    <>
      <div className="grid stats">
        <Stat label="Reasoning runs" value={list.length} hint="shown" />
        <Stat
          label="Ran with prior experience"
          value={withMemory.length}
          tone="good"
          hint={`${list.length === 0 ? 0 : Math.round((withMemory.length / list.length) * 100)}% of runs`}
        />
        <Stat label="Avg tool calls" value={avgTools.toFixed(1)} hint="per investigation" />
        <Stat
          label="Avg with memory"
          value={avgToolsWithMemory.toFixed(1)}
          tone="accent"
          hint={avgToolsCold > 0 ? `vs ${avgToolsCold.toFixed(1)} cold` : "no cold runs to compare"}
        />
      </div>

      {withMemory.length > 0 && coldRuns.length > 0 && (
        <Card
          title="Does memory change the work?"
          subtitle="Average tool calls per investigation, split by whether precedent existed"
        >
          <BarList
            data={[
              { label: "with precedent", value: avgToolsWithMemory, tone: "good" },
              { label: "cold start", value: avgToolsCold, tone: "warn" },
            ]}
            format={(value) => value.toFixed(1)}
          />
          <p className="small faint" style={{ marginTop: 10, marginBottom: 0 }}>
            Tool calls are a proxy, not a verdict: fewer calls is only better if the conclusion is
            still correct and independently verified. Every run below links to its own evidence.
          </p>
        </Card>
      )}

      <Card
        title="AI investigations"
        subtitle="Each run is a bounded sequence of typed tool calls against real evidence"
        right={
          <div className="btn-row">
            {FILTERS.map((item) => (
              <button
                key={item.key}
                className={`btn small ${filter === item.key ? "primary" : ""}`}
                onClick={() => setFilter(item.key)}
              >
                {item.label}
              </button>
            ))}
          </div>
        }
        flush
      >
        {list.length === 0 ? (
          <Empty
            title="No investigations recorded"
            hint="Run one from an incident page, or run the full learning loop."
          />
        ) : (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Incident</th>
                  <th>Conclusion</th>
                  <th>Mode</th>
                  <th className="num">Tools</th>
                  <th className="num">Rounds</th>
                  <th className="num">Memory</th>
                  <th>Recommended</th>
                  <th>Started</th>
                </tr>
              </thead>
              <tbody>
                {list.map((run) => (
                  <tr key={run.id}>
                    <td>
                      <Link to={`/incidents/${run.incident_id}?tab=investigation`}>
                        #{run.incident_id} {run.incident?.service ?? ""}
                      </Link>
                      <div className="faint small">{run.incident?.title?.slice(0, 60)}</div>
                    </td>
                    <td>
                      {run.root_cause_category ? (
                        <>
                          <span className="strong">{humanise(run.root_cause_category)}</span>
                          <div className="faint small">{run.root_cause_statement?.slice(0, 90)}</div>
                        </>
                      ) : (
                        <span className="faint">not established</span>
                      )}
                    </td>
                    <td>
                      <Badge tone={run.mode === "llm" ? "good" : "neutral"}>{run.mode}</Badge>
                      {run.degraded && (
                        <>
                          {" "}
                          <Badge tone="warn">degraded</Badge>
                        </>
                      )}
                    </td>
                    <td className="num">{run.tool_call_count}</td>
                    <td className="num">{run.round_count}</td>
                    <td className="num">
                      {run.memory_recall_used ? (
                        <Badge tone="good">{run.memory_hits}</Badge>
                      ) : (
                        <span className="faint">0</span>
                      )}
                    </td>
                    <td className="mono small">{run.recommended_remediation ?? "—"}</td>
                    <td className="nowrap small">
                      {formatRelative(run.started_at, now)}
                      <div className="faint">{formatDateTime(run.started_at)}</div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="How a run is bounded">
        <p className="small muted" style={{ margin: 0 }}>
          The agent cannot do anything the platform did not give it a tool for: roughly twenty typed
          tools cover metrics, logs, deployments, similarity, memory recall, the remediation
          registry and verification. Every call is recorded with its arguments, result, duration and
          the decision it belonged to. The model decides hypotheses, the conclusion and the choice of
          action; it never decides metric values, whether a fix worked, or what enters memory.
        </p>
        <div className="btn-row" style={{ marginTop: 12 }}>
          <Link className="btn small" to="/environment?tab=services">
            Inspect the action catalogue
          </Link>
          <Link className="btn small" to="/memory">
            Open the memory bank
          </Link>
        </div>
      </Card>
    </>
  );
}
