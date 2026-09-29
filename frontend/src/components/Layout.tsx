import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { formatClock, humanise } from "../lib/format";
import { useAsync, useTicker } from "../lib/hooks";
import ErrorBoundary from "./ErrorBoundary";
import { Badge } from "./ui";

/**
 * Six destinations, not eleven. Each one answers a question an on-call engineer would actually
 * ask, and `hint` says which question out loud so nobody has to click around to find out what
 * a page is for. Views that only made sense next to another one (an investigation and its
 * incident, a postmortem and the pattern it explains) are tabs inside a single destination.
 */
const NAV: { group: string; items: { to: string; label: string; icon: string; hint: string }[] }[] = [
  {
    group: "Start here",
    items: [
      {
        to: "/start",
        label: "How this works",
        icon: "❖",
        hint: "Plain-English explanation, a four-minute demo script, and what every word means",
      },
    ],
  },
  {
    group: "Respond",
    items: [
      {
        to: "/",
        label: "Overview",
        icon: "◉",
        hint: "Live state of the estate: open incidents, service health, and how much is remembered",
      },
      {
        to: "/incidents",
        label: "Incidents",
        icon: "⚠",
        hint: "Open incidents and the AI's investigation record for each one",
      },
    ],
  },
  {
    group: "Learn",
    items: [
      {
        to: "/demo",
        label: "Learning Loop",
        icon: "↻",
        hint: "Run the whole story end to end: detect, investigate, fail, learn, remember, succeed",
      },
      {
        to: "/memory",
        label: "Organizational Memory",
        icon: "◈",
        hint: "What the Hindsight bank remembers, and what recall returns for a live question",
      },
      {
        to: "/learning",
        label: "Reliability & Postmortems",
        icon: "∿",
        hint: "Recurring failure patterns and the written postmortems that explain them",
      },
    ],
  },
  {
    group: "Operate",
    items: [
      {
        to: "/environment",
        label: "Environment & Actions",
        icon: "⌗",
        hint: "The services being watched, the actions the agent may propose, and the audit trail",
      },
    ],
  },
];

export default function Layout() {
  const { session, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  useTicker(1000);

  const health = useAsync(() => api.health(), [], 10000);
  const sim = useAsync(() => api.simState(), [], 5000);
  const incidents = useAsync(() => api.incidents({ status: "open" }), [], 15000);

  const memoryBackend = health.data?.checks.memory;
  // Tri-state on purpose. `undefined` means "nobody has asked yet", which is not the same
  // answer as `false` ("asked, and there is no LLM key").
  const llmConfigured: boolean | undefined = health.data?.checks.llm.configured;
  const openCount = incidents.data?.count ?? 0;

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">
            <span className="brand-logo">OM</span>
            <div>
              <div className="brand-name">OpsMemory AI</div>
              <div className="brand-tag">Incident memory</div>
            </div>
          </div>
        </div>

        <nav className="nav">
          {NAV.map((group) => (
            <div key={group.group}>
              <div className="nav-group">{group.group}</div>
              {group.items.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.to === "/"}
                  title={item.hint}
                  className={({ isActive }) => `nav-link ${isActive ? "active" : ""}`}
                >
                  <span className="nav-icon">{item.icon}</span>
                  <span className="nav-text">
                    <span className="label">{item.label}</span>
                    <span className="nav-hint">{item.hint}</span>
                  </span>
                  {item.to === "/incidents" && openCount > 0 && <span className="nav-count">{openCount}</span>}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>

        <div className="sidebar-foot">
          <div>
            <span className="faint">Reasoning</span>{" "}
            <span className="strong">{health.data ? health.data.checks.llm.mode : "…"}</span>
          </div>
          <div>
            <span className="faint">Memory</span>{" "}
            <span className="strong">{memoryBackend ? memoryBackend.active_backend : "…"}</span>
          </div>
          <div>
            <span className="faint">Bank</span>{" "}
            <span className="strong">{memoryBackend?.health?.bank_id ?? "…"}</span>
          </div>
          <div>
            <span className="faint">Documents</span>{" "}
            <span className="strong">
              {memoryBackend?.health
                ? memoryBackend.health.counts_available === false ||
                  memoryBackend.health.documents === undefined
                  ? "not reported"
                  : memoryBackend.health.documents
                : "…"}
            </span>
          </div>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <div>
            <div className="topbar-title">Acme Payments · payments-platform</div>
            <div className="topbar-sub">
              Simulated clock {sim.data ? formatClock(sim.data.sim_time) : "—"}
              {sim.data && ` · tick ${sim.data.tick_count}`}
              {sim.data && ` · ${sim.data.running ? "running" : "paused"}`}
            </div>
          </div>
          <div className="topbar-right">
            <Badge
              tone={memoryBackend ? (memoryBackend.degraded ? "warn" : "good") : "neutral"}
              title={
                memoryBackend
                  ? memoryBackend.degraded
                    ? (memoryBackend.reasons ?? []).join("; ") || "memory degraded"
                    : `bank ${memoryBackend.active_backend}`
                  : health.error
                    ? "could not reach the API to read the memory backend"
                    : "asking the API which memory backend is active"
              }
            >
              <span className="dot" />
              {memoryBackend
                ? `Memory · ${memoryBackend.active_backend}${memoryBackend.degraded ? " (degraded)" : ""}`
                : "Memory · checking…"}
            </Badge>
            {/* An unknown state must never render as a confident claim. Saying
                "deterministic" before the health response arrives tells the reader the AI is
                switched off, when in fact nobody has asked the question yet. */}
            <Badge
              tone={llmConfigured === true ? "good" : "neutral"}
              title={
                llmConfigured === true
                  ? `reasoning via ${health.data?.checks.llm.model ?? "a live model"}`
                  : llmConfigured === false
                    ? "no LLM key configured — the deterministic evidence-driven reasoner is running"
                    : health.error
                      ? "could not reach the API to determine the reasoning backend"
                      : "asking the API which reasoning backend is active"
              }
            >
              {llmConfigured === true
                ? "LLM · live"
                : llmConfigured === false
                  ? "LLM · deterministic"
                  : "LLM · checking…"}
            </Badge>
            {openCount > 0 && <Badge tone="warn">{openCount} open</Badge>}
            <span className="chip" title={session?.email}>
              {session ? humanise(session.role) : "—"}
            </span>
            <button
              className="btn ghost small"
              onClick={() => {
                logout();
                navigate("/login");
              }}
            >
              Sign out
            </button>
          </div>
        </header>

        <main className="content">
          {/* Remounting on path change clears a previous page's error state. */}
          <ErrorBoundary key={location.pathname}>
            <Outlet />
          </ErrorBoundary>
        </main>
      </div>
    </div>
  );
}
