import { useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { ApiError } from "../api";
import { humanise, looksLikeDate, prettyJson, titleCase, type Tone } from "../lib/format";

export function Card({
  title,
  subtitle,
  right,
  children,
  className = "",
  flush = false,
}: {
  title?: ReactNode;
  subtitle?: ReactNode;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
  flush?: boolean;
}) {
  return (
    <section className={`card ${className}`}>
      {(title || right) && (
        <header className="card-head">
          {title && (
            <div>
              <h3>{title}</h3>
              {subtitle && <div className="sub">{subtitle}</div>}
            </div>
          )}
          {right && <div className="card-head-right">{right}</div>}
        </header>
      )}
      <div className={`card-body ${flush ? "flush" : ""}`}>{children}</div>
    </section>
  );
}

export function Badge({
  tone = "neutral",
  children,
  title,
}: {
  tone?: Tone;
  children: ReactNode;
  title?: string;
}) {
  return (
    <span className={`badge ${tone}`} title={title}>
      {children}
    </span>
  );
}

export function StatusBadge({ value, tone }: { value: string; tone?: Tone }) {
  return (
    <Badge tone={tone ?? "neutral"} title={value}>
      {humanise(value)}
    </Badge>
  );
}

export function Stat({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: Tone;
}) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value" style={tone ? { color: `var(--${tone})` } : undefined}>
        {value}
      </div>
      {hint && <div className="stat-hint">{hint}</div>}
    </div>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="empty">
      <span className="spinner" /> <span style={{ marginLeft: 8 }}>{label}</span>
    </div>
  );
}

export function Empty({ title, hint }: { title: string; hint?: ReactNode }) {
  return (
    <div className="empty">
      <div className="empty-strong">{title}</div>
      {hint && <div>{hint}</div>}
    </div>
  );
}

export function ErrorBanner({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  if (!error) return null;
  const apiError = error instanceof ApiError ? error : null;
  const message = error instanceof Error ? error.message : String(error);
  return (
    <div className="banner bad">
      <span>⚠</span>
      <div style={{ flex: 1 }}>
        <div className="strong">{apiError ? apiError.code : "Request failed"}</div>
        <div>{message}</div>
        {apiError?.detail && <pre className="code" style={{ marginTop: 6 }}>{prettyJson(apiError.detail)}</pre>}
      </div>
      {onRetry && (
        <button className="btn small" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

export function Banner({ tone = "info", children }: { tone?: Tone; children: ReactNode }) {
  return (
    <div className={`banner ${tone}`}>
      <span>{tone === "good" ? "✓" : tone === "warn" ? "!" : tone === "bad" ? "⚠" : "ℹ"}</span>
      <div style={{ flex: 1 }}>{children}</div>
    </div>
  );
}

/** Renders a value without guessing: nested objects become key/value lists or JSON. */
export function Value({ value }: { value: unknown }): ReactNode {
  if (value === null || value === undefined) return <span className="faint">—</span>;
  if (typeof value === "boolean") return <span className="mono">{value ? "true" : "false"}</span>;
  if (typeof value === "number") return <span className="mono">{String(value)}</span>;
  if (typeof value === "string") {
    if (looksLikeDate(value)) return <span className="mono small">{new Date(value).toLocaleString()}</span>;
    if (value.includes("\n")) return <pre className="code">{value}</pre>;
    return <span>{value}</span>;
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="faint">none</span>;
    if (value.every((item) => typeof item === "string" || typeof item === "number")) {
      return (
        <div className="btn-row" style={{ gap: 5 }}>
          {value.map((item, index) => (
            <span className="chip" key={index}>
              {String(item)}
            </span>
          ))}
        </div>
      );
    }
    return <pre className="code">{prettyJson(value)}</pre>;
  }
  return <pre className="code">{prettyJson(value)}</pre>;
}

export function KeyValue({ data, skip }: { data: Record<string, unknown> | null | undefined; skip?: string[] }) {
  if (!data) return <Empty title="Nothing recorded" />;
  const entries = Object.entries(data).filter(([key]) => !(skip ?? []).includes(key));
  if (entries.length === 0) return <Empty title="Nothing recorded" />;
  return (
    <dl className="kv">
      {entries.map(([key, value]) => (
        <div key={key} style={{ display: "contents" }}>
          <dt>{humanise(key)}</dt>
          <dd>
            <Value value={value} />
          </dd>
        </div>
      ))}
    </dl>
  );
}

export function Tabs<T extends string>({
  tabs,
  active,
  onChange,
}: {
  tabs: { key: T; label: string; count?: number }[];
  active: T;
  onChange: (key: T) => void;
}) {
  return (
    <div className="tabs">
      {tabs.map((tab) => (
        <button
          key={tab.key}
          className={`tab ${tab.key === active ? "active" : ""}`}
          onClick={() => onChange(tab.key)}
        >
          {tab.label}
          {tab.count !== undefined && <span className="faint"> · {tab.count}</span>}
        </button>
      ))}
    </div>
  );
}

export function Collapsible({
  summary,
  children,
  right,
  defaultOpen = false,
}: {
  summary: ReactNode;
  children: ReactNode;
  right?: ReactNode;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div style={{ borderBottom: "1px solid rgba(31,42,60,0.6)" }}>
      <div
        className="list-row clickable"
        onClick={() => setOpen((value) => !value)}
        style={{ alignItems: "center" }}
      >
        <span className="faint mono" style={{ width: 12 }}>
          {open ? "▾" : "▸"}
        </span>
        <div className="list-main">{summary}</div>
        {right && <div className="list-side">{right}</div>}
      </div>
      {open && <div style={{ padding: "0 14px 14px 40px" }}>{children}</div>}
    </div>
  );
}

export function LoopDiagram({ phases, reached }: { phases: { key: string; label: string }[]; reached: string[] }) {
  return (
    <div className="loop">
      {phases.map((phase) => (
        <span key={phase.key} className={`loop-step ${reached.includes(phase.key) ? "done" : ""}`}>
          {phase.label}
        </span>
      ))}
    </div>
  );
}

export function Toolbar({ children }: { children: ReactNode }) {
  return <div className="btn-row">{children}</div>;
}

export function Pill({ label, value }: { label: string; value: ReactNode }) {
  return (
    <span className="chip">
      <span className="faint">{label}</span>
      <span className="strong">{value}</span>
    </span>
  );
}

export function SectionTitle({ children, sub }: { children: ReactNode; sub?: ReactNode }) {
  return (
    <div>
      <h2 style={{ fontSize: 17 }}>{children}</h2>
      {sub && <div className="faint small">{sub}</div>}
    </div>
  );
}

export function ProgressBar({ value, max = 1, tone = "accent" }: { value: number; max?: number; tone?: Tone }) {
  const pct = max <= 0 ? 0 : Math.max(0, Math.min(1, value / max));
  const style: CSSProperties = { width: `${pct * 100}%`, background: `var(--${tone})` };
  return (
    <div className="progress">
      <span style={style} />
    </div>
  );
}

export function Tag({ children }: { children: ReactNode }) {
  return <span className="badge plain">{titleCase(String(children))}</span>;
}
