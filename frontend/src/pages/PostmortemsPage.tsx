import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { Badge, Card, Empty, ErrorBanner, Loading, Stat, Value } from "../components/ui";
import { formatDateTime, formatRelative, humanise, toneForStatus } from "../lib/format";
import { useAsync, useTicker } from "../lib/hooks";
import type { Postmortem } from "../types";

function PostmortemBody({ postmortem }: { postmortem: Postmortem }) {
  const sections: [string, string | null][] = [
    ["Summary", postmortem.summary],
    ["Impact", postmortem.impact],
    ["Detection", postmortem.detection],
    ["Root cause", postmortem.root_cause],
    ["Successful remediation", postmortem.successful_remediation],
    ["Verification", postmortem.verification],
    ["Deployment relationship", postmortem.deployment_relationship],
  ];

  return (
    <>
      {sections.map(([label, text]) => (
        <div key={label} style={{ marginBottom: 16 }}>
          <div className="stat-label">{label}</div>
          <p className="small" style={{ margin: "3px 0 0", whiteSpace: "pre-wrap", color: "var(--text)" }}>
            {text ?? "—"}
          </p>
        </div>
      ))}

      {postmortem.failed_attempts.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <div className="stat-label">Failed attempts</div>
          {postmortem.failed_attempts.map((attempt, index) => (
            <div className="banner bad" key={index} style={{ marginTop: 6, alignItems: "flex-start" }}>
              <span>✕</span>
              <Value value={attempt} />
            </div>
          ))}
        </div>
      )}

      <div className="grid cols-2" style={{ gap: 18 }}>
        <div>
          <div className="stat-label">Lessons</div>
          {postmortem.lessons.length === 0 ? (
            <div className="faint small">none recorded</div>
          ) : (
            <ul className="small muted" style={{ margin: "4px 0 0", paddingLeft: 16 }}>
              {postmortem.lessons.map((lesson, index) => (
                <li key={index} style={{ marginBottom: 4 }}>
                  {lesson}
                </li>
              ))}
            </ul>
          )}
        </div>
        <div>
          <div className="stat-label">Preventive actions</div>
          {postmortem.preventive_actions.length === 0 ? (
            <div className="faint small">none recorded</div>
          ) : (
            <ul className="small muted" style={{ margin: "4px 0 0", paddingLeft: 16 }}>
              {postmortem.preventive_actions.map((action, index) => (
                <li key={index} style={{ marginBottom: 4 }}>
                  {action}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      {postmortem.evidence.length > 0 && (
        <details style={{ marginTop: 16 }}>
          <summary className="faint small" style={{ cursor: "pointer" }}>
            Evidence referenced ({postmortem.evidence.length})
          </summary>
          <pre className="code scroll" style={{ marginTop: 8 }}>
            {JSON.stringify(postmortem.evidence, null, 2)}
          </pre>
        </details>
      )}
      {postmortem.hypotheses.length > 0 && (
        <details style={{ marginTop: 8 }}>
          <summary className="faint small" style={{ cursor: "pointer" }}>
            Hypotheses as recorded ({postmortem.hypotheses.length})
          </summary>
          <pre className="code scroll" style={{ marginTop: 8 }}>
            {JSON.stringify(postmortem.hypotheses, null, 2)}
          </pre>
        </details>
      )}
    </>
  );
}

function Detail({ id }: { id: number }) {
  const { can } = useAuth();
  const detail = useAsync(() => api.postmortem(id), [id]);

  if (detail.loading && !detail.data) return <Loading />;
  if (detail.error) return <ErrorBanner error={detail.error} onRetry={detail.reload} />;
  if (!detail.data) return <Empty title="Postmortem not found" />;

  const { postmortem, incident } = detail.data;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div className="btn-row" style={{ justifyContent: "space-between" }}>
        <div>
          <Link to="/learning?tab=postmortems" className="small">
            ← Postmortems
          </Link>
          <h1 style={{ fontSize: 19, marginTop: 4 }}>
            {humanise(incident.title)} <span className="faint mono">#{incident.id}</span>
          </h1>
          <div className="faint small">
            {incident.service} · {incident.environment} · detected {formatDateTime(incident.detected_at)}
          </div>
        </div>
        <div className="btn-row">
          <Badge tone={toneForStatus(postmortem.status)}>{humanise(postmortem.status)}</Badge>
          {!postmortem.approved_at && can("finalize_postmortem") && (
            <button
              className="btn"
              onClick={async () => {
                await api.approvePostmortem(postmortem.id);
                detail.reload();
              }}
            >
              ✓ Approve and finalize
            </button>
          )}
          {postmortem.approved_at && (
            <span className="chip">
              approved by {postmortem.approved_by} · {formatDateTime(postmortem.approved_at)}
            </span>
          )}
          <Link className="btn small" to={`/incidents/${incident.id}`}>
            Open incident
          </Link>
        </div>
      </div>

      <Card title="Postmortem" subtitle={`authored by ${postmortem.authored_by ?? "unknown"}`}>
        <PostmortemBody postmortem={postmortem} />
      </Card>
    </div>
  );
}

export default function PostmortemsPage() {
  const params = useParams();
  const now = useTicker(10000);
  const list = useAsync(() => api.postmortems(200), [], 30000);

  if (params.id) return <Detail id={Number(params.id)} />;

  if (list.loading && !list.data) return <Loading label="Loading postmortems…" />;
  if (list.error && !list.data) return <ErrorBanner error={list.error} onRetry={list.reload} />;

  const items = list.data?.postmortems ?? [];
  const approved = items.filter((item) => item.approved_at).length;
  const withFailures = items.filter((item) => item.failed_attempts.length > 0).length;

  return (
    <>
      <div className="grid stats">
        <Stat label="Postmortems" value={items.length} />
        <Stat label="Approved" value={approved} tone="good" hint="signed off by a human" />
        <Stat
          label="Document a failed attempt"
          value={withFailures}
          tone="warn"
          hint="the part most postmortems leave out"
        />
        <Stat
          label="Total lessons recorded"
          value={items.reduce((sum, item) => sum + item.lessons.length, 0)}
        />
      </div>

      <Card
        title="Postmortems"
        subtitle="Authored from verified evidence, including the attempts that failed"
        flush
      >
        {items.length === 0 ? (
          <Empty
            title="No postmortems yet"
            hint="A postmortem is written once recovery has been verified — not while the incident is still open."
          />
        ) : (
          <div className="list">
            {items.map((postmortem) => (
              <Link key={postmortem.id} to={`/postmortems/${postmortem.id}`} className="list-row clickable">
                <div className="list-main">
                  <div className="list-title">
                    {postmortem.summary?.slice(0, 130) ?? `Postmortem for incident #${postmortem.incident_id}`}
                  </div>
                  <div className="list-meta">
                    <span className="mono">#{postmortem.incident_id}</span>
                    <Badge tone={toneForStatus(postmortem.status)}>{humanise(postmortem.status)}</Badge>
                    {postmortem.failed_attempts.length > 0 && (
                      <Badge tone="bad">{postmortem.failed_attempts.length} failed attempt(s)</Badge>
                    )}
                    <span>{postmortem.lessons.length} lessons</span>
                    <span>{formatRelative(postmortem.created_at, now)}</span>
                  </div>
                </div>
                <div className="list-side">
                  {postmortem.approved_at ? (
                    <Badge tone="good">approved</Badge>
                  ) : (
                    <Badge tone="warn">awaiting review</Badge>
                  )}
                </div>
              </Link>
            ))}
          </div>
        )}
      </Card>
    </>
  );
}
