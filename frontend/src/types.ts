/** Response types mirroring `app/api/serializers.py`. Nothing here is invented: every field
 *  was taken from a real response captured from the running API. */

export type Role = "viewer" | "analyst" | "sre" | "admin";

export interface Session {
  user_id: number;
  email: string;
  role: Role;
  organization_id: number;
  is_anonymous: boolean;
  permissions: string[];
}

export interface MemoryBackend {
  active_backend: string;
  configured_backend: string;
  degraded: boolean;
  reasons: string[];
  health: MemoryHealth | null;
}

export interface MemoryStats {
  backend: string;
  bank_id: string;
  /** Counts are optional: a backend that cannot count its bank omits them entirely. */
  counts_available?: boolean;
  facts?: number;
  observations?: number;
  documents?: number;
  entities?: number;
  bank_size_capped?: boolean;
  facts_by_type?: Record<string, number>;
  facts_by_scope?: Record<string, number>;
  persisted_to?: string | null;
}

/** `/api/memory/status` returns the stats plus a health verdict. */
export interface MemoryHealth extends MemoryStats {
  healthy: boolean;
  detail: string;
}

export interface MemoryStatus {
  status: MemoryBackend;
  health: MemoryHealth | null;
  stats: MemoryStats | null;
  configured: {
    hindsight_base_url: boolean;
    bank_id: string;
    recall_budget: string;
    required: boolean;
  };
}

export interface Health {
  status: string;
  sim_time: string;
  checks: {
    database: { ok: boolean; detail: string };
    memory: MemoryBackend;
    llm: { configured: boolean; model: string | null; mode: string };
  };
  states: string[];
}

export type Severity = "critical" | "high" | "medium" | "low";

export interface IncidentSummary {
  id: number;
  title: string;
  service: string;
  service_id: number;
  environment: string;
  environment_id: number;
  severity: Severity | string;
  status: string;
  root_cause_category: string | null;
  root_cause_summary: string | null;
  symptom: string | null;
  detected_at: string;
  resolved_at: string | null;
  verified_at: string | null;
  mttr_seconds: number | null;
  is_historical: boolean;
  suspected_deployment_id: number | null;
  has_fingerprint: boolean;
}

export interface Fingerprint {
  service: string;
  service_family: string;
  environment: string;
  environment_kind: string;
  severity: string;
  root_cause_category: string | null;
  error_signatures: string[];
  symptom: string | null;
  metric_pattern: Record<string, number>;
  deployment_related: boolean;
  deployment_change_classes: string[];
  symptoms: string[];
  time_bucket: string;
}

/** Per-feature attribution of a similarity score, as produced by `compare_fingerprints`.
 *  `contributions` is the audit trail: every feature's raw score, weight and weighted share. */
export interface SimilarityBreakdown {
  score: number;
  label: string;
  matched: string[];
  partial: string[];
  differences: string[];
  contributions: {
    feature: string;
    label: string;
    raw_score: number;
    weight: number;
    weighted: number;
    direction: "match" | "partial" | "mismatch" | string;
    detail: string;
  }[];
  method: string;
}

export interface SimilarNeighbour {
  incident_id: number;
  title: string;
  service: string;
  severity: string;
  root_cause_category: string | null;
  detected_at: string;
  similarity: SimilarityBreakdown;
}

export interface IncidentEvidence {
  id: number;
  kind: string;
  summary: string;
  detail: Record<string, unknown> | null;
  source: string;
  strength: string;
  retrieval: string | null;
  collected_at: string;
}

export interface HypothesisTest {
  name: string;
  verdict: string;
  explanation: string;
  result: Record<string, unknown> | null;
}

export interface Hypothesis {
  id: number;
  code: string;
  statement: string;
  category: string;
  status: string;
  ranking: number;
  supporting: string[];
  contradicting: string[];
  missing: string[];
  tests: HypothesisTest[];
}

export interface TimelineEvent {
  id: number;
  ts: string;
  kind: string;
  title: string;
  description: string;
  actor: string | null;
  source: string;
  payload: Record<string, unknown> | null;
}

export interface ToolCall {
  seq: number;
  round: number;
  tool: string;
  arguments: Record<string, unknown> | null;
  result: Record<string, unknown> | null;
  ok: boolean;
  error_code: string | null;
  error_message: string | null;
  duration_ms: number;
  decision: string | null;
}

export interface AiDecision {
  id: number;
  stage: string;
  decision: string;
  rationale: string;
  evidence_ids: number[];
  memory_ids: string[];
  status_label: string | null;
  created_at: string;
}

export interface Investigation {
  id: number;
  incident_id: number;
  status: string;
  mode: string;
  model: string | null;
  autonomy_level: number;
  round_count: number;
  started_at: string;
  completed_at: string | null;
  duration_ms: number;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  degraded: string | null;
  summary: string | null;
  root_cause_category: string | null;
  root_cause_statement: string | null;
  alternatives_ruled_out: string[];
  recommended_remediation: string | null;
  recommendation_reason: string | null;
  memory_recall_used: boolean;
  memory_hits: number;
  tool_call_count: number;
  tool_calls: ToolCall[];
  decisions: AiDecision[];
}

/** Row from `GET /api/investigations`: an investigation plus the incident it belongs to. */
export interface InvestigationListItem extends Investigation {
  incident: IncidentSummary | null;
}

export interface VerificationCheck {
  check_name: string;
  metric: string;
  before: number | null;
  after: number | null;
  baseline: number | null;
  unit: string | null;
  passed: boolean | null;
  is_primary: boolean;
  detail: string;
}

export interface Verification {
  id: number;
  remediation_run_id: number;
  status: string;
  verdict: string;
  verdict_reason: string;
  settle_seconds: number | null;
  started_at: string;
  completed_at: string | null;
  improvement_pct: number | null;
  /** Gauge snapshot plus a nested `snapshot` copy of the healthy state. */
  before: Record<string, any> | null;
  after: Record<string, any> | null;
  checks: VerificationCheck[];
}

export interface RemediationRun {
  id: number;
  incident_id: number;
  action_code: string;
  action_name: string;
  risk_level: string;
  autonomy_level: number;
  status: string;
  attempt: number;
  proposed_by: string;
  proposed_at: string;
  rationale: string | null;
  parameters: Record<string, unknown> | null;
  blocked_reasons: string[] | null;
  safety_checks: Record<string, unknown> | null;
  approved_by: string | null;
  approved_at: string | null;
  executed_at: string | null;
  result: Record<string, unknown> | null;
  outcome: string | null;
}

export interface MemoryReference {
  id: number;
  incident_id: number;
  service: string;
  scope: string;
  bank_id: string;
  memory_id: string | null;
  document_id: string | null;
  backend: string;
  retained_by: string | null;
  retained_at: string;
  recall_count: number;
  preview: string;
}

export interface Postmortem {
  id: number;
  incident_id: number;
  status: string;
  summary: string | null;
  impact: string | null;
  detection: string | null;
  root_cause: string | null;
  timeline: Record<string, unknown>[];
  evidence: Record<string, unknown>[];
  hypotheses: Record<string, unknown>[];
  failed_attempts: Record<string, unknown>[];
  successful_remediation: string | null;
  verification: string | null;
  deployment_relationship: string | null;
  lessons: string[];
  preventive_actions: string[];
  authored_by: string | null;
  approved_by: string | null;
  approved_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface IncidentDetail extends IncidentSummary {
  fingerprint: Fingerprint | null;
  baseline: Record<string, number> | null;
  impact: Record<string, unknown> | null;
  detection_meta: Record<string, unknown> | null;
  evidence: IncidentEvidence[];
  hypotheses: Hypothesis[];
  timeline: TimelineEvent[];
  investigations: Investigation[];
  remediation_runs: RemediationRun[];
  verifications: Verification[];
  memory_references: MemoryReference[];
  postmortem: Postmortem | null;
}

export interface MemoryItem {
  id: string;
  text: string;
  type: "world" | "experience" | "observation" | string;
  score: number | null;
  context: string | null;
  metadata: Record<string, unknown> | null;
  tags: string[];
  entities: string[];
  occurred_start: string | null;
  occurred_end: string | null;
  mentioned_at: string | null;
  document_id: string | null;
  proof_count: number;
  source_fact_ids: string[];
  retrieval: string[];
}

export interface RecallResponse {
  query: string;
  backend: string;
  degraded: boolean;
  note: string;
  /** How many candidates each retrieval arm contributed before fusion: semantic, keyword,
   *  graph and temporal. Useful for showing that recall is genuinely multi-arm. */
  strategy_hits: Record<string, number>;
  hits: MemoryItem[];
}

export interface LearningEvent {
  id: number;
  incident_id: number | null;
  kind: string;
  summary: string;
  payload: Record<string, unknown> | null;
  memory_ids: string[];
  created_at: string;
}

export interface PatternInsight {
  category: string;
  occurrences: number;
  window_days: number;
  services: string[];
  incident_ids: number[];
  failed_actions: string[];
  successful_actions: string[];
  deployment_related: number;
  median_mttr_seconds: number | null;
  evidence_strength: string;
  statement: string;
  retention_advice: string;
}

export interface PeriodAnalysis {
  window_days: number;
  window_start: string;
  incidents: number;
  open_incidents: number;
  by_root_cause: Record<string, number>;
  by_severity: Record<string, number>;
  median_mttr_seconds: number | null;
  remediation_attempts: number;
  failed_remediation_actions: string[];
  successful_remediation_actions: string[];
  verifications: number;
  verified_success: number;
  verification_failed: number;
  remediation_success_rate: number | null;
  deployments: number;
  deployment_related_incidents: number;
}

export interface ActionReliability {
  action_code: string;
  succeeded: number;
  failed: number;
  partial: number;
  applied: number;
  total: number;
  success_rate: number | null;
  reliability_label: string;
}

export interface Fragility {
  service_id: number;
  service: string;
  tier: string;
  incidents: number;
  mean_mttr_seconds: number | null;
}

export interface RemediationCatalogueAction {
  id: number;
  code: string;
  name: string;
  description: string;
  risk_level: string;
  required_approval: boolean;
  allowed_environments: string[];
  applicable_categories: string[];
  timeout_seconds: number;
  max_retries: number;
  is_enabled: boolean;
  executes_shell: boolean;
  historical_success: number;
  historical_failure: number;
  execution_workflow: Record<string, unknown> | null;
  verification_workflow: Record<string, unknown> | null;
  rollback_workflow: Record<string, unknown> | null;
}

export interface ServiceBrief {
  id: number;
  name: string;
  tier: string;
  kind: string;
  owner_team: string;
  fragility_score: number;
  gauges: Record<string, number> | null;
  description?: string | null;
  language?: string | null;
  active_fault?: Record<string, unknown> | null;
  open_incidents?: number;
}

export interface Deployment {
  id: number;
  service: string;
  environment: string;
  version: string;
  commit_sha: string | null;
  commit_message: string | null;
  author: string | null;
  status: string;
  strategy: string;
  started_at: string;
  completed_at: string | null;
  is_suspected_cause: boolean;
  risk_score: number | null;
  change_class: string | null;
}

export interface SimState {
  sim_time: string;
  running: boolean;
  tick_count: number;
  services: Record<string, Record<string, number>>;
}

export interface AuditEntry {
  id: number;
  created_at: string;
  actor: string;
  actor_role: string;
  action: string;
  target_type: string;
  target_id: string | null;
  result: string;
  reason: string | null;
  detail: Record<string, unknown> | null;
  ip_address: string | null;
}

export interface Scenario {
  key: string;
  kind: string;
  root_cause_category: string;
  severity: string;
  description: string;
  canonical_actions: string[];
  symptoms: string[];
}

export interface LearningActivityEvent {
  id: number;
  kind: string;
  summary: string;
  incident_id: number | null;
  created_at: string;
}

export interface Overview {
  sim_time: string;
  running: boolean;
  status: {
    active_incidents: number;
    severity_counts: Record<string, number>;
    memory: MemoryBackend;
    reasoning_mode: string;
    autonomy_level: number;
  };
  active_incidents: IncidentSummary[];
  recent_incidents: IncidentSummary[];
  investigations: Investigation[];
  deployments: Deployment[];
  top_pattern: PatternInsight | null;
  learning_activity: LearningActivityEvent[];
  services: ServiceBrief[];
  active_faults: Record<string, Record<string, unknown>>;
}

/** The narrative produced by `POST /api/demo/learning-loop`. */
export interface DemoStep {
  index: number;
  phase: string;
  title: string;
  narrative: string;
  facts: Record<string, any>;
  evidence_refs: string[];
}

export interface LearningDelta {
  memory_before: { documents: number; facts: number; observations: number; backend: string };
  memory_after_incident_1: { documents: number; facts: number; observations: number; backend: string };
  memory_after_incident_2: { documents: number; facts: number; observations: number; backend: string };
  incident_1_tool_calls: number;
  incident_2_tool_calls: number;
  incident_1_recommended: string | null;
  incident_2_recommended: string | null;
  failed_action_avoided_in_incident_2: boolean;
  incident_2_recall_hits: number;
}

export interface LearningLoopResult {
  scenario: string;
  service: string;
  sim_time: string;
  steps: DemoStep[];
  learning_delta: LearningDelta;
  learning_events: {
    id: number;
    kind: string;
    summary: string;
    incident_id: number | null;
    created_at: string;
  }[];
}
