import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { BarList, StackedStrip } from "../components/charts";
import {
  Badge,
  Banner,
  Card,
  Collapsible,
  Empty,
  ErrorBanner,
  Loading,
  Stat,
  Tabs,
} from "../components/ui";
import PageIntro from "../components/PageIntro";
import { formatDateTime, formatRelative, humanise } from "../lib/format";
import { useAction, useAsync, useTicker } from "../lib/hooks";
import type { MemoryItem } from "../types";

const TYPE_TONE: Record<string, "info" | "accent" | "good"> = {
  world: "info",
  experience: "accent",
  observation: "good",
};

function MemoryItemRow({ item }: { item: MemoryItem }) {
  return (
    <Collapsible
      summary={
        <>
          <div className="list-title" style={{ fontSize: 12.5 }}>
            {item.text}
          </div>
          <div className="list-meta">
            <Badge tone={TYPE_TONE[item.type] ?? "neutral"}>{item.type}</Badge>
            {item.context && <span>{item.context}</span>}
            {item.proof_count > 0 && <span className="mono">proof ×{item.proof_count}</span>}
            {item.score !== null && item.score !== undefined && (
              <span className="mono">score {item.score.toFixed(3)}</span>
            )}
          </div>
        </>
      }
      right={
        item.retrieval.length > 0 ? (
          <div className="btn-row" style={{ gap: 5 }}>
            {item.retrieval.map((arm) => (
              <span className="chip" key={arm}>
                {arm}
              </span>
            ))}
          </div>
        ) : undefined
      }
    >
      <div className="grid cols-2" style={{ gap: 12 }}>
        <div>
          <div className="stat-label">Identity</div>
          <div className="small">
            <div className="mono">{item.id}</div>
            {item.document_id && <div className="mono faint">{item.document_id}</div>}
            {item.mentioned_at && <div className="faint">mentioned {formatDateTime(item.mentioned_at)}</div>}
          </div>
          {item.entities.length > 0 && (
            <div className="btn-row" style={{ gap: 5, marginTop: 8 }}>
              {item.entities.map((entity, index) => (
                <span className="chip" key={index}>
                  {entity}
                </span>
              ))}
            </div>
          )}
        </div>
        <div>
          {item.source_fact_ids.length > 0 && (
            <>
              <div className="stat-label">Grounded in</div>
              <div className="small mono faint">{item.source_fact_ids.length} source fact(s)</div>
            </>
          )}
          {item.metadata && Object.keys(item.metadata).length > 0 && (
            <>
              <div className="stat-label" style={{ marginTop: 8 }}>
                Metadata
              </div>
              <pre className="code scroll">{JSON.stringify(item.metadata, null, 2)}</pre>
            </>
          )}
        </div>
      </div>
    </Collapsible>
  );
}

export default function MemoryPage() {
  const now = useTicker(5000);
  const [tab, setTab] = useState<"observations" | "items" | "references" | "ledger">("observations");
  const [query, setQuery] = useState(
    "connection pool saturation on payment-service — what worked and what failed before?",
  );
  const [budget, setBudget] = useState<"low" | "mid" | "high">("mid");
  const [types, setTypes] = useState<string[]>(["world", "experience", "observation"]);

  const status = useAsync(() => api.memoryStatus(), [], 20000);
  const observations = useAsync(() => api.memoryItems({ type: "observation", limit: 100 }), [], 30000);
  const experiences = useAsync(() => api.memoryItems({ type: "experience", limit: 100 }), [], 30000);
  const world = useAsync(() => api.memoryItems({ type: "world", limit: 100 }), [], 30000);
  const references = useAsync(() => api.memoryReferences(200), [], 30000);
  const ledger = useAsync(() => api.learningEvents(100), [], 30000);

  const recall = useAction(() => api.recall({ query, types, budget, max_tokens: 2048 }));

  if (status.loading && !status.data) return <Loading label="Loading memory…" />;
  if (status.error && !status.data) return <ErrorBanner error={status.error} onRetry={status.reload} />;

  const memory = status.data!;
  const health = memory.health;

  // An unknown count must never be drawn as a confident zero: a bank that merely did not
  // report its size would otherwise look identical to a bank with nothing in it.
  const countsKnown = health?.counts_available !== false;
  const unknownHint = countsKnown ? null : "backend did not report a count";
  const count = (value: number | undefined) =>
    countsKnown && value !== undefined ? value : "—";

  const allItems = [
    ...(observations.data?.items ?? []),
    ...(experiences.data?.items ?? []),
    ...(world.data?.items ?? []),
  ];

  const items = tab === "observations" ? observations.data?.items ?? [] : allItems;

  return (
    <>
      {memory.status.degraded && (
        <Banner tone="warn">
          <div className="strong">Long-term memory is degraded</div>
          <div>
            {(memory.status.reasons ?? []).join("; ") ||
              "The configured Hindsight backend is unreachable. Retains and recalls are being served by the in-process store so the loop still completes."}
          </div>
        </Banner>
      )}

      <PageIntro
        icon="◈"
        title="Organizational memory"
        what="Everything the assistant has permanently remembered, plus a search box you can query live."
        why="This is what makes the second incident easier than the first. Nothing here is a per-session cache: it is written to the Hindsight memory bank and survives restarts. An empty bank on a fresh start is correct — it fills up as incidents are resolved."
        steps={[
          "Read the counts first: documents are stored lessons, facts are small statements extracted from them.",
          "Open “Observations” — these are beliefs the system formed from many facts, each showing how many facts back it.",
          "Type a real question into the recall console and press search. This is the demo moment.",
        ]}
      />

      <div className="grid stats">
        <Stat label="Active backend" value={memory.status.active_backend} hint={`configured: ${memory.status.configured_backend}`} />
        <Stat label="Bank" value={health?.bank_id ?? memory.configured.bank_id} hint={`recall budget: ${memory.configured.recall_budget}`} />
        <Stat label="Documents" value={count(health?.documents)} hint={unknownHint ?? "retained experiences"} />
        <Stat label="Facts" value={count(health?.facts)} hint={unknownHint ?? "atomic, typed memories"} />
        <Stat label="Observations" value={count(health?.observations)} tone="good" hint={unknownHint ?? "consolidated, evidence-grounded"} />
        <Stat label="Entities" value={count(health?.entities)} hint={unknownHint ?? "graph nodes used for recall"} />
      </div>

      {!countsKnown && (
        <Banner tone="info">
          <div className="strong">This backend did not report its memory counts</div>
          <div>
            The bank is reachable, so retain and recall work normally — it simply did not return
            a size. The counts are shown as “—” rather than 0 so an unknown number is never
            mistaken for an empty memory.
          </div>
        </Banner>
      )}

      <div className="grid side">
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <Card
            title="Recall console"
            subtitle="Hybrid retrieval: semantic + keyword + entity graph + temporal, fused then reranked"
          >
            <div className="form-row">
              <label className="field" style={{ flex: 4 }}>
                Query
                <input value={query} onChange={(event) => setQuery(event.target.value)} />
              </label>
              <label className="field" style={{ flex: 1, minWidth: 110 }}>
                Budget
                <select value={budget} onChange={(event) => setBudget(event.target.value as "low" | "mid" | "high")}>
                  <option value="low">low</option>
                  <option value="mid">mid</option>
                  <option value="high">high</option>
                </select>
              </label>
              <button className="btn primary" disabled={recall.running} onClick={() => void recall.run()}>
                {recall.running ? <span className="spinner" /> : "◈"} Recall
              </button>
            </div>

            <div className="btn-row" style={{ marginTop: 10 }}>
              {(["world", "experience", "observation"] as const).map((type) => (
                <button
                  key={type}
                  className={`btn small ${types.includes(type) ? "primary" : ""}`}
                  onClick={() =>
                    setTypes((current) =>
                      current.includes(type) ? current.filter((value) => value !== type) : [...current, type],
                    )
                  }
                >
                  {type}
                </button>
              ))}
              <span className="faint small">types to search</span>
            </div>

            {recall.error ? (
              <div style={{ marginTop: 12 }}>
                <ErrorBanner error={recall.error} />
              </div>
            ) : null}

            {recall.running && (
              <div className="small faint" style={{ marginTop: 10 }}>
                Running all four retrieval arms in parallel and fusing them…
              </div>
            )}
          </Card>

          {recall.result && (
            <Card
              title="Recall result"
              subtitle={recall.result.note}
              right={
                <div className="btn-row">
                  <Badge tone="accent">{recall.result.hits.length} hits</Badge>
                  {recall.result.degraded && <Badge tone="warn">degraded</Badge>}
                  <span className="chip">{recall.result.backend}</span>
                </div>
              }
              flush
            >
              <div style={{ padding: "12px 14px", borderBottom: "1px solid var(--border)" }}>
                <div className="stat-label">Candidates contributed by each retrieval arm</div>
                <BarList
                  data={Object.entries(recall.result.strategy_hits).map(([arm, count]) => ({
                    label: humanise(arm),
                    value: count,
                    tone: "info",
                  }))}
                  format={(value) => String(value)}
                />
                <div className="small faint" style={{ marginTop: 8 }}>
                  Arms are fused with reciprocal rank fusion, then reranked — so the top hits below
                  are not simply the longest list.
                </div>
              </div>

              {recall.result.hits.length === 0 ? (
                <Empty
                  title="No memory matched"
                  hint="Nothing relevant is retained. The agent treats this as absence of precedent, not as evidence."
                />
              ) : (
                <div className="list scroll-y">
                  {recall.result.hits.map((item) => (
                    <MemoryItemRow key={item.id} item={item} />
                  ))}
                </div>
              )}
            </Card>
          )}

          <Card title="Explore the bank" right={undefined} flush>
            <Tabs
              active={tab}
              onChange={setTab}
              tabs={[
                { key: "observations", label: "Observations", count: observations.data?.items.length },
                { key: "items", label: "All memories", count: allItems.length },
                { key: "references", label: "Retained documents", count: references.data?.references.length },
                { key: "ledger", label: "Learning ledger", count: ledger.data?.events.length },
              ]}
            />

            <div style={{ padding: "12px 14px 0" }} className="small muted">
              {tab === "observations" && (
                <p style={{ margin: 0 }}>
                  Observations are the system&apos;s beliefs. They are consolidated from many
                  underlying facts, carry a proof count and cite their sources, and are{" "}
                  <span className="strong">refined rather than overwritten</span> when new evidence
                  arrives.
                </p>
              )}
              {tab === "items" && (
                <p style={{ margin: 0 }}>
                  Every memory the bank holds, as it is stored — the raw material that recall
                  searches. Documents are the incidents they were extracted from.
                </p>
              )}
              {tab === "references" && (
                <p style={{ margin: 0 }}>
                  This is <span className="strong">this platform&apos;s own receipt book</span>, not
                  the bank: one row per retain, saying who stored what and when. It is deliberately
                  separate from the memory service, and a clean-room reset empties it while the bank
                  keeps its contents. So a 0 here does <em>not</em> mean memory is empty — read the
                  counts above for that.
                </p>
              )}
              {tab === "ledger" && (
                <p style={{ margin: 0 }}>
                  A running audit trail of what the platform did with memory: retains, recalls and
                  reflections, with the backend that served each one. It is evidence of the work, not
                  the memory itself — and like the receipts above, a clean-room reset clears it.
                </p>
              )}
            </div>

            {tab === "references" ? (
              (references.data?.references.length ?? 0) === 0 ? (
                <Empty
                  title="No retention receipts yet"
                  hint="Nothing has been retained since the last reset. Run the learning loop to produce some — the memory bank above is unaffected by this list being empty."
                />
              ) : (
                <div className="list scroll-y tall">
                  {references.data!.references.map((reference) => (
                    <div className="list-row" key={reference.id}>
                      <div className="list-main">
                        <div className="btn-row" style={{ gap: 6 }}>
                          <Badge tone="info">{humanise(reference.scope)}</Badge>
                          <span className="chip">{reference.backend}</span>
                          <span className="faint small">by {reference.retained_by}</span>
                        </div>
                        <pre className="code" style={{ marginTop: 6 }}>
                          {reference.preview}
                        </pre>
                        <div className="list-meta">
                          <Link to={`/incidents/${reference.incident_id}`} className="mono">
                            incident #{reference.incident_id}
                          </Link>
                          <span>{reference.service}</span>
                          <span>{formatRelative(reference.retained_at, now)}</span>
                          {reference.recall_count > 0 && (
                            <Badge tone="accent">recalled {reference.recall_count}×</Badge>
                          )}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )
            ) : tab === "ledger" ? (
              (ledger.data?.events.length ?? 0) === 0 ? (
                <Empty
                  title="No memory activity since the last reset"
                  hint="Retains, recalls and reflections will appear here as the loop runs."
                />
              ) : (
                <div className="list scroll-y tall">
                  {ledger.data!.events.map((event) => (
                    <Collapsible
                      key={event.id}
                      summary={
                        <>
                          <div className="list-title" style={{ fontSize: 12.5 }}>
                            {event.summary}
                          </div>
                          <div className="list-meta">
                            <Badge tone="info">{event.kind}</Badge>
                            {event.incident_id !== null && (
                              <Link to={`/incidents/${event.incident_id}`} className="mono">
                                #{event.incident_id}
                              </Link>
                            )}
                            <span>{formatDateTime(event.created_at)}</span>
                          </div>
                        </>
                      }
                      right={<span className="chip">{event.memory_ids.length} ids</span>}
                    >
                      <pre className="code scroll">{JSON.stringify(event.payload, null, 2)}</pre>
                    </Collapsible>
                  ))}
                </div>
              )
            ) : (items.length ?? 0) === 0 ? (
              <Empty title="No memories of this type yet" />
            ) : (
              <div className="list scroll-y tall">
                {items.map((item) => (
                  <MemoryItemRow key={item.id} item={item} />
                ))}
              </div>
            )}
          </Card>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <Card title="Fact composition" subtitle="Typed memories by kind">
            <BarList
              data={Object.entries(health?.facts_by_type ?? {}).map(([key, value]) => ({
                label: humanise(key),
                value,
                tone: key === "observation" ? "good" : "info",
              }))}
              format={(value) => String(value)}
            />
            <div style={{ marginTop: 14 }}>
              <div className="stat-label">By scope</div>
              <StackedStrip
                segments={Object.entries(health?.facts_by_scope ?? {}).map(([key, value], index) => ({
                  label: humanise(key),
                  value,
                  tone: ["accent", "info", "warn", "good"][index % 4],
                }))}
              />
            </div>
          </Card>

          <Card title="Where it is stored" subtitle="Hindsight semantics, swappable backend">
            <dl className="kv">
              <dt>Active backend</dt>
              <dd>
                <Badge tone={memory.status.active_backend === "hindsight" ? "good" : "info"}>
                  {memory.status.active_backend}
                </Badge>
              </dd>
              <dt>Configured backend</dt>
              <dd className="mono small">{memory.status.configured_backend}</dd>
              <dt>Hindsight reachable</dt>
              <dd>
                <Badge tone={memory.configured.hindsight_base_url ? "good" : "neutral"}>
                  {memory.configured.hindsight_base_url ? "yes" : "not configured"}
                </Badge>
              </dd>
              <dt>Retention required</dt>
              <dd>
                <Badge tone={memory.configured.required ? "warn" : "neutral"}>
                  {memory.configured.required ? "retention must not silently fail" : "best effort"}
                </Badge>
              </dd>
              <dt>Persistence</dt>
              <dd className="mono small">{health?.persisted_to ?? "in-memory only"}</dd>
              <dt>Health</dt>
              <dd>{health?.detail ?? "—"}</dd>
            </dl>
          </Card>

          <Card title="Why this matters">
            <p className="small muted" style={{ margin: 0 }}>
              Memory is not a log. A retained experience is decomposed into typed facts, consolidated
              into observations that cite their sources and carry proof counts, and refined in place
              as new evidence arrives. That is what lets the agent answer &ldquo;we tried this and it
              did not work&rdquo; with a citation instead of a guess.
            </p>
          </Card>
        </div>
      </div>
    </>
  );
}
