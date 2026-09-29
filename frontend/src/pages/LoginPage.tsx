import { useState } from "react";
import type { FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../auth";
import { Banner, ErrorBanner } from "../components/ui";

// Scopes mirror PERMISSIONS in app/core/security.py exactly. The console never offers an
// action the API will refuse, so the hinted scope has to match the real policy.
const SEEDED_ROLES = [
  {
    role: "admin",
    email: "admin@acme.test",
    scope: "simulate, reset, manage",
    note: "use this one for the demo",
  },
  { role: "sre", email: "sre@acme.test", scope: "approve and execute fixes", note: "" },
  { role: "analyst", email: "analyst@acme.test", scope: "investigate, propose fixes", note: "" },
  { role: "viewer", email: "viewer@acme.test", scope: "read only", note: "" },
];

export default function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();
  // Pre-filled as admin on purpose: running the learning loop injects its own faults, which
  // only the admin role may do. Landing on a role whose buttons are disabled is a confusing
  // first impression, so the demo account is the default.
  const [email, setEmail] = useState("admin@acme.test");
  const [password, setPassword] = useState("password123");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email, password);
      navigate("/");
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-page">
      <div className="login-card">
        <div className="brand-mark" style={{ marginBottom: 16 }}>
          <span className="brand-logo" style={{ width: 38, height: 38, fontSize: 15 }}>
            OM
          </span>
          <div>
            <div className="brand-name" style={{ fontSize: 17 }}>
              OpsMemory AI
            </div>
            <div className="brand-tag">Incident memory console</div>
          </div>
        </div>

        <p className="muted small" style={{ marginTop: 0 }}>
          An SRE agent that remembers what it tried, what failed and what worked — and verifies its
          own recovery before it claims success.
        </p>

        <form onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <label className="field">
            Email
            <input value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="username" />
          </label>
          <label className="field">
            Password
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete="current-password"
            />
          </label>
          <button className="btn primary" type="submit" disabled={busy}>
            {busy ? <span className="spinner" /> : null}
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>

        <div style={{ marginTop: 16 }}>
          <div className="stat-label">Seeded roles (password <span className="mono">password123</span>)</div>
          <div className="login-roles">
            {SEEDED_ROLES.map((item) => (
              <button
                key={item.role}
                type="button"
                className="role-btn"
                onClick={() => {
                  setEmail(item.email);
                  setPassword("password123");
                }}
              >
                <strong>{item.role}</strong>
                {item.scope}
                {item.note && <em className="role-note">{item.note}</em>}
              </button>
            ))}
          </div>
        </div>

        {error ? (
          <div style={{ marginTop: 14 }}>
            <ErrorBanner error={error} />
          </div>
        ) : null}

        <div style={{ marginTop: 14 }}>
          <Banner tone="info">
            This console runs with or without external services. With an OpenAI-compatible key and
            a Hindsight key it uses live reasoning and the real memory service; with no keys it
            falls back to a deterministic reasoner and an in-process store that keeps
            Hindsight&apos;s semantics (typed facts, consolidated observations, proof counts).
            Either way the loop completes — the badges after you sign in tell you which one you
            got.
          </Banner>
        </div>
      </div>
    </div>
  );
}
