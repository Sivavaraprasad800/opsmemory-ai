import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { Badge, Card, Empty, ErrorBanner, Loading, Stat } from "../components/ui";
import { formatDuration, formatRelative, humanise, toneForSeverity, toneForStatus } from "../lib/format";
import { useAsync, useTicker } from "../lib/hooks";

const STATUS_FILTERS = ["all", "open", "investigating", "verified", "closed"];

export default function IncidentsPage() {
  const now = useTicker(10000);
  const navigate = useNavigate();
  const [status, setStatus] = useState("all");
  const [search, setSearch] = useState("");

  const incidents = useAsync(() => api.incidents({ limit: 300 }), [], 20000);

  const rows = useMemo(() => {
    const list = incidents.data?.incidents ?? [];
    return list
      .filter((incident) => (status === "all" ? true : incident.status === status))
      .filter((incident) => {
        if (!search.trim()) return true;
        const needle = search.trim().toLowerCase();
        return (
          incident.title.toLowerCase().includes(needle) ||
          incident.service.toLowerCase().includes(needle) ||
          String(incident.id) === needle ||
          (incident.root_cause_category ?? "").toLowerCase().includes(needle)
        );
      })
      .sort((a, b) => b.detected_at.localeCompare(a.detected_at));
  }, [incidents.data, status, search]);

  const all = incidents.data?.incidents ?? [];
  const open = all.filter((incident) => incident.status !== "closed" && incident.status !== "verified").length;
  const verified = all.filter((incident) => incident.status === "verified").length;
  const withMemory = all.filter((incident) => incident.root_cause_category).length;

  if (incidents.loading && !incidents.data) return <Loading label="Loading incidents…" />;
  if (incidents.error && !incidents.data) return <ErrorBanner error={incidents.error} onRetry={incidents.reload} />;

  return (
    <>
      <div className="grid stats">
        <Stat label="Incidents on record" value={all.length} hint="live and historical" />
        <Stat label="Open" value={open} tone={open > 0 ? "warn" : "good"} />
        <Stat label="Verified resolved" value={verified} tone="good" hint="recovery independently confirmed" />
        <Stat label="With a root cause" value={withMemory} hint="conclusions the agent is willing to stand behind" />
      </div>

      <Card
        title="Incidents"
        subtitle="Detected by the anomaly committee, not typed in by hand"
        right={
          <div className="btn-row">
            {STATUS_FILTERS.map((filter) => (
              <button
                key={filter}
                className={`btn small ${status === filter ? "primary" : ""}`}
                onClick={() => setStatus(filter)}
              >
                {humanise(filter)}
              </button>
            ))}
          </div>
        }
        flush
      >
        <div style={{ padding: "12px 14px", borderBottom: "1px solid var(--border)" }}>
          <input
            placeholder="Filter by title, service, root cause or incident id…"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        </div>

        {rows.length === 0 ? (
          <Empty
            title="No incidents match"
            hint="Clear the filter, or open the Learning Loop page to run a full incident end to end."
          />
        ) : (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>#</th>
                  <th>Incident</th>
                  <th>Service</th>
                  <th>Env</th>
                  <th>Severity</th>
                  <th>Status</th>
                  <th>Root cause</th>
                  <th className="num">MTTR</th>
                  <th>Detected</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((incident) => (
                  <tr
                    key={incident.id}
                    className="clickable"
                    onClick={() => navigate(`/incidents/${incident.id}`)}
                  >
                    <td className="mono">{incident.id}</td>
                    <td>
                      <div className="strong">{incident.title}</div>
                      <div className="faint small">{incident.symptom?.slice(0, 110)}</div>
                    </td>
                    <td>
                      {incident.service}
                      {incident.is_historical && (
                        <>
                          {" "}
                          <Badge tone="neutral">historical</Badge>
                        </>
                      )}
                    </td>
                    <td>{incident.environment}</td>
                    <td>
                      <Badge tone={toneForSeverity(incident.severity)}>{incident.severity}</Badge>
                    </td>
                    <td>
                      <Badge tone={toneForStatus(incident.status)}>{humanise(incident.status)}</Badge>
                    </td>
                    <td>
                      {incident.root_cause_category ? (
                        <span>
                          {humanise(incident.root_cause_category)}
                          <div className="faint small">{incident.root_cause_summary?.slice(0, 90)}</div>
                        </span>
                      ) : (
                        <span className="faint">not established</span>
                      )}
                    </td>
                    <td className="num">{formatDuration(incident.mttr_seconds)}</td>
                    <td className="nowrap small">
                      {formatRelative(incident.detected_at, now)}
                      <div className="faint">{incident.detected_at.slice(11, 19)}</div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="Where these came from">
        <p className="small muted" style={{ margin: 0 }}>
          Every row above is opened by the detection pipeline: four independent detectors (robust
          z-score, rolling-median deviation, EWMA residual and CUSUM change-point) vote on two
          windows of real telemetry, a quorum plus an operational floor decides, and a two-pass
          debounce stops a single noisy tick from paging anyone. The root cause column stays
          <span className="strong"> not established</span> until the agent produces evidence a human
          could re-check.
        </p>
        <div className="btn-row" style={{ marginTop: 12 }}>
          <Link className="btn small" to="/environment?tab=simulator">
            Open the simulator
          </Link>
          <Link className="btn small" to="/demo">
            Run the two-incident learning loop
          </Link>
        </div>
      </Card>
    </>
  );
}
