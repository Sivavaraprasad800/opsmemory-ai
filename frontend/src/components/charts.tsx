import type { ReactNode } from "react";
import { compactNumber } from "../lib/format";
import type { SimilarityBreakdown } from "../types";

const TONE_COLOURS: Record<string, string> = {
  good: "#34d399",
  warn: "#fbbf24",
  bad: "#f87171",
  info: "#a78bfa",
  accent: "#38bdf8",
  neutral: "#94a3b8",
};

export function toneColour(tone: string): string {
  return TONE_COLOURS[tone] ?? TONE_COLOURS.neutral;
}

export interface Series {
  name: string;
  colour: string;
  points: { label: string; value: number }[];
}

/**
 * A compact multi-series line chart. Written in SVG rather than pulled from a charting
 * library: the project has no runtime chart dependency, and the axes needed here are few.
 */
export function LineChart({
  series,
  height = 200,
  yFormat = compactNumber,
  yMin,
  yMax,
  showValues = true,
}: {
  series: Series[];
  height?: number;
  yFormat?: (value: number) => string;
  yMin?: number;
  yMax?: number;
  showValues?: boolean;
}) {
  const width = 720;
  const pad = { top: 18, right: 18, bottom: 30, left: 46 };
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;

  const allValues = series.flatMap((item) => item.points.map((point) => point.value));
  if (allValues.length === 0) return <div className="empty">No data points</div>;

  const dataMax = yMax ?? Math.max(...allValues);
  const dataMin = yMin ?? Math.min(...allValues, 0);
  const top = dataMax === dataMin ? dataMax + 1 : dataMax;
  const bottom = dataMin;
  const labels = series[0]?.points.map((point) => point.label) ?? [];

  const xAt = (index: number) =>
    pad.left + (labels.length <= 1 ? innerW / 2 : (index / (labels.length - 1)) * innerW);
  const yAt = (value: number) =>
    pad.top + innerH - ((value - bottom) / Math.max(top - bottom, 1e-9)) * innerH;

  const ticks = 4;
  const tickValues = Array.from({ length: ticks + 1 }, (_, index) => bottom + ((top - bottom) * index) / ticks);

  return (
    <svg className="chart" viewBox={`0 0 ${width} ${height}`} role="img">
      {tickValues.map((value, index) => (
        <g key={index}>
          <line className="chart-grid" x1={pad.left} x2={width - pad.right} y1={yAt(value)} y2={yAt(value)} />
          <text className="chart-label" x={pad.left - 8} y={yAt(value) + 3.5} textAnchor="end">
            {yFormat(value)}
          </text>
        </g>
      ))}

      {labels.map((label, index) => (
        <text key={index} className="chart-label" x={xAt(index)} y={height - 10} textAnchor="middle">
          {label}
        </text>
      ))}

      {series.map((item) => {
        if (item.points.length === 0) return null;
        const path = item.points.map((point, index) => `${index === 0 ? "M" : "L"}${xAt(index)},${yAt(point.value)}`).join(" ");
        const area = `${path} L${xAt(item.points.length - 1)},${yAt(bottom)} L${xAt(0)},${yAt(bottom)} Z`;
        return (
          <g key={item.name}>
            <path d={area} fill={item.colour} className="chart-area" />
            <path d={path} stroke={item.colour} className="chart-series" />
            {item.points.map((point, index) => (
              <g key={index}>
                <circle cx={xAt(index)} cy={yAt(point.value)} r={4} fill={item.colour} className="chart-point" />
                {showValues && (
                  <text
                    className="chart-label"
                    x={xAt(index)}
                    y={yAt(point.value) - 10}
                    textAnchor="middle"
                    style={{ fill: item.colour }}
                  >
                    {yFormat(point.value)}
                  </text>
                )}
              </g>
            ))}
          </g>
        );
      })}
    </svg>
  );
}

export function ChartLegend({ series }: { series: Series[] }) {
  return (
    <div className="legend">
      {series.map((item) => (
        <span className="legend-item" key={item.name}>
          <span className="legend-swatch" style={{ background: item.colour }} />
          {item.name}
        </span>
      ))}
    </div>
  );
}

export interface BarDatum {
  label: string;
  value: number;
  tone?: string;
  hint?: ReactNode;
}

export function BarList({
  data,
  format = compactNumber,
  maxOverride,
  columns,
}: {
  data: BarDatum[];
  format?: (value: number) => string;
  maxOverride?: number;
  columns?: string;
}) {
  if (data.length === 0) return <div className="empty">Nothing to chart yet</div>;
  const max = maxOverride ?? Math.max(...data.map((item) => item.value), 1);
  return (
    <div className="bars" style={columns ? { gridTemplateColumns: columns } : undefined}>
      {data.map((item) => (
        <div className="bar-row" key={item.label} title={typeof item.hint === "string" ? item.hint : undefined}>
          <span className="bar-label">{item.label}</span>
          <span className="bar-track">
            <span
              className="bar-fill"
              style={{
                width: `${Math.max(2, (item.value / max) * 100)}%`,
                background: toneColour(item.tone ?? "accent"),
              }}
            />
          </span>
          <span className="bar-value">{format(item.value)}</span>
        </div>
      ))}
    </div>
  );
}

export function Sparkline({
  values,
  colour = "#38bdf8",
  width = 120,
  height = 28,
}: {
  values: number[];
  colour?: string;
  width?: number;
  height?: number;
}) {
  if (values.length < 2) return <span className="faint small">—</span>;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const points = values
    .map((value, index) => {
      const x = (index / (values.length - 1)) * (width - 2) + 1;
      const y = height - 2 - ((value - min) / span) * (height - 4);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg className="chart" viewBox={`0 0 ${width} ${height}`} width={width} height={height} role="img">
      <polyline points={points} fill="none" stroke={colour} strokeWidth={1.6} strokeLinejoin="round" />
    </svg>
  );
}

/**
 * The explainability view for a similarity score: every feature's weight, its raw score on this
 * pair, and the weighted share it contributed. This is what makes the ranking auditable rather
 * than a number a judge has to take on faith.
 */
export function SimilarityAttribution({ breakdown }: { breakdown: SimilarityBreakdown }) {
  const { score, label, contributions, matched, partial, differences, method } = breakdown;
  const totalContribution = contributions.reduce((sum, item) => sum + item.weighted, 0) || 1;
  const tone = score >= 0.75 ? "good" : score >= 0.5 ? "accent" : score >= 0.3 ? "warn" : "neutral";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div className="btn-row" style={{ justifyContent: "space-between" }}>
        <div className="btn-row">
          <span className={`badge ${tone}`} style={{ fontSize: 13, padding: "3px 11px" }}>
            {score.toFixed(3)} · {label}
          </span>
          <span className="faint small">{method}</span>
        </div>
      </div>

      <div className="attribution">
        {contributions.map((item) => (
          <div className="attribution-row" key={item.feature}>
            <span className="mono" style={{ color: "var(--text)" }}>
              {item.label}
            </span>
            <span className="mono faint small">
              w {item.weight.toFixed(2)} · raw {item.raw_score.toFixed(2)}
            </span>
            <span>
              <span
                className="bar-track"
                style={{ display: "block", marginBottom: 3 }}
                title={`${item.weighted.toFixed(4)} of a ${totalContribution.toFixed(4)} total`}
              >
                <span
                  className="bar-fill"
                  style={{
                    width: `${(item.weighted / totalContribution) * 100}%`,
                    background: toneColour(
                      item.direction === "match" ? "good" : item.direction === "partial" ? "accent" : "bad",
                    ),
                  }}
                />
              </span>
              <span className="detail-text">{item.detail}</span>
            </span>
          </div>
        ))}
      </div>

      {(matched.length > 0 || partial.length > 0 || differences.length > 0) && (
        <div className="grid split" style={{ gap: 10 }}>
          {matched.length > 0 && (
            <div>
              <div className="stat-label">Matched</div>
              <ul className="small muted" style={{ margin: "4px 0 0", paddingLeft: 16 }}>
                {matched.map((item, index) => (
                  <li key={index}>{item}</li>
                ))}
              </ul>
            </div>
          )}
          {partial.length > 0 && (
            <div>
              <div className="stat-label">Partial</div>
              <ul className="small muted" style={{ margin: "4px 0 0", paddingLeft: 16 }}>
                {partial.map((item, index) => (
                  <li key={index}>{item}</li>
                ))}
              </ul>
            </div>
          )}
          {differences.length > 0 && (
            <div>
              <div className="stat-label">Differences</div>
              <ul className="small muted" style={{ margin: "4px 0 0", paddingLeft: 16 }}>
                {differences.map((item, index) => (
                  <li key={index}>{item}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** Horizontal proportional strip, used for severity mixes and verification outcomes. */
export function StackedStrip({ segments }: { segments: { label: string; value: number; tone: string }[] }) {
  const total = segments.reduce((sum, item) => sum + item.value, 0);
  if (total === 0) return <div className="empty small">Nothing recorded</div>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", height: 10, borderRadius: 999, overflow: "hidden", border: "1px solid var(--border)" }}>
        {segments
          .filter((segment) => segment.value > 0)
          .map((segment) => (
            <span
              key={segment.label}
              title={`${segment.label}: ${segment.value}`}
              style={{ width: `${(segment.value / total) * 100}%`, background: toneColour(segment.tone) }}
            />
          ))}
      </div>
      <div className="legend">
        {segments.map((segment) => (
          <span className="legend-item" key={segment.label}>
            <span className="legend-swatch" style={{ background: toneColour(segment.tone) }} />
            {segment.label} <span className="mono">{segment.value}</span>
          </span>
        ))}
      </div>
    </div>
  );
}
