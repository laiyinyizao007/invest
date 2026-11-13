// 由 docs/web/status-api.openapi.json 生成,请勿手改;契约变更后重新生成。
// 前端必须容忍未知字段,并把未知的 state / level 取值按 "unknown" 处理。

/** 总体级别。unknown = 没有足够信息判断(数据缺失/过期),必须与 ok 区别显示。 */
export type Level = "ok" | "warn" | "critical" | "unknown";

/** 区块数据状态。前端遇到未列出的取值必须按 unknown 渲染,不得当作 ok。 */
export type SectionState = "ok" | "stale" | "missing" | "corrupt" | "unknown";

/** severe=严重, warning=警告(与无人值守日志口径一致) */
export type Severity = "severe" | "warning";

export interface SectionBase {
  state: SectionState;
  as_of?: string | null;
  age_seconds?: number | null;
  stale_after_seconds?: number | null;
  message?: string | null;
  level: "ok" | "warn" | "critical" | "unknown";
}

export interface ApiError {
  error: string;
  code: "unauthorized" | "forbidden" | "not_found" | "bad_request" | "rate_limited" | "csrf" | "conflict" | "unavailable" | "internal";
}

export interface Environment {
  label: "simnow" | "guoxin-test" | "live" | "unknown";
  display: string;
}

export interface Meta {
  version: string;
  phase: number;
  server_time: string;
  server_timezone: string;
  exchange_timezone: "Asia/Shanghai";
  environment: Environment;
  polling_hint_seconds: number;
  capabilities: {
    live: boolean;
    equity: boolean;
    control: boolean;
    stream: boolean;
    research_detail: boolean;
    data_views: boolean;
    nodes: boolean;
    evolution: boolean;
  };
}

export interface Reason {
  section: string;
  level: Level;
  text: string;
}

export interface Brief {
  state: SectionState;
  level: Level;
  headline: string;
}

export interface Overview {
  server_time: string;
  level: Level;
  reasons: Array<Reason>;
  sections: {
    trading: Brief;
    alerts: Brief;
    days: Brief;
    recording: Brief;
    research: Brief;
    evolution?: EvolutionSection;
  };
}

export interface Heartbeat {
  process: "running" | "stopped" | "unknown";
  last_beat_at: string | null;
  age_seconds: number | null;
}

export interface Guard {
  account_masked: string;
  day: string;
  day_start_equity: number;
  last_equity: number;
  peak_equity: number;
  daily_pnl: number;
  drawdown_from_peak: number;
  daily_locked_day: string | null;
  hard_locked: boolean;
  hard_reason: string | null;
  limits: {
    daily_loss_yuan: number | null;
    hard_drawdown_yuan: number | null;
  };
  as_of: string | null;
  age_seconds: number | null;
}

export interface TradingData {
  heartbeat: Heartbeat;
  guard: Guard | null;
  pause: {
    paused: boolean;
    since: string | null;
  };
  live_admission: {
    approved: boolean;
  };
}

export interface EventRow {
  day: string;
  level: Severity;
  name: string;
  count: number;
}

export interface AlertsData {
  outbox: {
    pending: number;
    oldest_age_seconds: number | null;
  };
  recent: Array<EventRow>;
}

export interface DayRow {
  day: string;
  started: number;
  trades: number;
  clean: boolean;
  has_severe: boolean;
  events: Array<{
    level: Severity;
    name: string;
    count: number;
  }>;
  first_ts: string | null;
  last_ts: string | null;
  heartbeat: string | null;
  trusted: boolean;
  tail_uncertain: boolean;
}

export interface DaysData {
  days: Array<DayRow>;
  checklist: Array<{
    text: string;
    checked: boolean | null;
  }>;
  longest_clean_streak: number;
  required_streak: number;
  missing_weekdays: Array<string>;
  streak_warning_count: number;
}

export interface RecordingData {
  wrapper_running: boolean;
  status: string;
  profile: string | null;
  symbols: Array<string>;
  started_at: string | null;
  heartbeat_at: string | null;
  last_tick: string | null;
  last_bar: string | null;
  last_error: string | null;
  data_warning: string | null;
}

export interface RunBrief {
  run_id: string;
  kind: "backtest" | "gate" | "evaluate";
  status: string | null;
  passed: boolean | null;
  strategy_file: string | null;
  headline: string;
}

export interface ResearchData {
  quota: {
    used: number;
    limit: number;
    window_hours: number;
    next_release_in_seconds: number | null;
  };
  approvals_count: number;
  pending: Array<{
    name: string;
    sha256: string;
    approved: boolean;
  }>;
  recent_runs: Array<RunBrief>;
}

export interface RunDetail {
  run_id: string;
  kind: "backtest" | "gate" | "evaluate";
  status?: string | null;
  passed?: boolean | null;
  strategy_file?: string | null;
  strategy_sha256?: string | null;
  report?: string | null;
  failures?: Array<string>;
  params?: Record<string, unknown> | null;
  metrics?: Record<string, unknown> | null;
  diagnosis?: Record<string, unknown> | null;
  selection?: Record<string, unknown> | null;
  windows?: unknown[] | null;
  error?: Record<string, unknown> | null;
  stats?: Record<string, unknown> | null;
}

export interface DocData {
  key: "unattended-log" | "simnow-drills";
  title: string;
  markdown: string;
  truncated: boolean;
}

export interface LiveData {
  account: {
    balance: number;
    available: number;
    margin: number;
    frozen: number;
    currency: "CNY";
  } | null;
  positions: Array<{
    vt_symbol: string;
    direction: "long" | "short" | "net";
    volume: number;
    avg_price: number;
    pnl: number;
  }>;
  open_orders: Array<{
    vt_orderid: string;
    vt_symbol: string;
    direction: "long" | "short" | "net";
    offset: string;
    price: number;
    volume: number;
    traded: number;
    status: string;
    time: string;
  }>;
  trades_today: Array<{
    vt_tradeid: string;
    vt_symbol: string;
    direction: "long" | "short" | "net";
    offset: string;
    price: number;
    volume: number;
    time: string;
  }>;
  ticks: Array<{
    vt_symbol: string;
    last_price: number;
    bid_price_1: number | null;
    ask_price_1: number | null;
    volume: number | null;
    time: string;
  }>;
}

export interface EquityData {
  interval: "1m" | "5m" | "1h" | "1d";
  points: Array<{
    t: string;
    equity: number;
  }>;
  day_start_equity: number | null;
}

export interface PauseRequest {
  confirm: "PAUSE";
  reason: string;
}

export interface ResumeRequest {
  confirm: "RESUME";
  reason: string;
}

export interface ControlResult {
  paused: boolean;
  since: string | null;
  audit_id: number;
}

export interface AuditEntry {
  id: number;
  at: string;
  actor: string;
  action: "pause" | "resume";
  reason: string | null;
  result: "ok" | "rejected";
  remote_addr: string;
}

export interface AuditData {
  entries: Array<AuditEntry>;
}

export interface CandidateView {
  name: string;
  sha256: string;
  approved: boolean;
  screening: {
    passed: boolean;
    findings: Array<string>;
  };
  lines: Array<{
    n: number;
    text: string;
  }>;
  review_command: string;
}

export interface RunList {
  runs: Array<RunBrief>;
  next_cursor: string | null;
}

export interface DataSegments {
  segments: Array<{
    key: string;
    label: string;
    kind: "train" | "validation" | "test";
    from: string;
    to: string;
    rows: number | null;
    test_consumed: boolean | null;
  }>;
}

export interface DrillsData {
  scenarios: Array<{
    name: string;
    title: string;
    result: "pass" | "fail" | "skipped" | "unknown";
    summary: string;
    at: string | null;
  }>;
  source_doc: string;
}

export interface RecordingQuality {
  symbols: Array<{
    vt_symbol: string;
    days: Array<{
      day: string;
      expected_bars: number;
      actual_bars: number;
      complete: boolean;
    }>;
  }>;
}

export interface ConfigView {
  risk: {
    capital_yuan: number | null;
    per_trade_loss_yuan: number | null;
    daily_limit_yuan: number | null;
    hard_limit_yuan: number | null;
  };
  symbols: {
    live: string | null;
    history: string | null;
  };
  thresholds: {
    heartbeat_stale_seconds: number;
    live_snapshot_stale_seconds: number | null;
  };
}

export interface NodesData {
  nodes: Array<{
    id: string;
    label: string;
    role: "trader" | "recorder" | "research";
    environment: Environment;
    level: Level;
    last_seen: string | null;
    reachable: boolean;
  }>;
}

export interface WhoAmI {
  user: string;
  role: "viewer" | "operator" | "admin";
}

/** 进化战役列表(**裸数据**,与 curve/log/heatmap 同族端点一致)。state/level/message 放在数据里,不套 Section 外壳。 */
export interface EvolutionList {
  campaigns: Array<EvolutionCampaign>;
  total: number;
  checked: number;
  unscanned: number;
  state: "ok" | "missing" | "unknown";
  level: "ok" | "warn" | "critical" | "unknown";
  message: string | null;
}

export interface EvolutionCampaign {
  campaign_id: string;
  generations: number;
  latest_generation: number | null;
  records: number;
  updated_at: string | null;
  best_sharpe: number | null;
  state: "ok" | "missing" | "corrupt" | "stale";
  message: string | null;
}

/** 进化曲线:每代的种群统计。**由服务端算好,前端不要重算**。 */
export interface EvolutionCurve {
  campaign_id: string;
  generations: number;
  latest_generation: number | null;
  points: Array<EvolutionPoint>;
  flags: Array<EvolutionFlag>;
}

export interface EvolutionPoint {
  generation: number;
  count: number;
  elite_sharpe: number | null;
  mean_sharpe: number | null;
  p25_sharpe: number | null;
  p75_sharpe: number | null;
  best_total_return: number | null;
  worst_max_drawdown: number | null;
  cells: Array<string>;
  mean_total_return?: number | null;
  mean_max_drawdown?: number | null;
}

/** 服务端的提示标志。**是提示,不是结论**;文案已写明依据。 */
export interface EvolutionFlag {
  kind: "stagnant" | "overfit_suspect";
  level: "warn" | "info";
  text: string;
}

/** 变异日志:每一代的记录。reflection 是大模型写的自由文本,**仅供审阅,没有进入模型输入**。 */
export interface EvolutionLog {
  campaign_id: string;
  records: Array<EvolutionRecord>;
  next_cursor: string | null;
}

export interface EvolutionRecord {
  generation: number;
  candidate_sha256: string;
  parent_sha256: string | null;
  run_id: string | null;
  status: string | null;
  sharpe: number | null;
  total_return: number | null;
  max_drawdown: number | null;
  dsr: number | null;
  n_trials: number | null;
  fail_reason: string | null;
  cell: string | null;
  diagnosis: Record<string, unknown> | null;
  reflection: string | null;
  created_at: string | null;
}

/** 参数热力图(三维网格的二维切片)。分数来自**选参窗口**,不是样本外表现。 */
export interface Heatmap {
  run_id: string;
  axes: {
    x: {
      name: string;
      values: Array<number>;
    };
    y: {
      name: string;
      values: Array<number>;
    };
    z: {
      name: string;
      values: Array<number>;
      selected: number;
    };
  };
  grid: Array<Array<number | null>>;
  best_cell: Array<number> | null;
  on_plateau: boolean | null;
  valid_cells: number;
  n_cells: number;
  source_window: Array<string> | null;
  note: string | null;
}

/** 候选源码(只读)。按哈希找,不按名字。 */
export interface CandidateCode {
  sha256: string;
  found: boolean;
  language: string;
  lines: Array<{
    n: number;
    text: string;
  }>;
}

/** 两个候选之间的统一 diff(服务端算)。 */
export interface CandidateDiff {
  from_sha256: string;
  to_sha256: string;
  diff: string;
  available: boolean;
}

/** 总览里的进化区块。**只有 evolution.enabled 为真时才出现**(阶段①默认关着)。 */
export interface EvolutionSection {
  state: string;
  level: string;
  headline: string;
}

/** 区块外壳:先看 state,再决定是否渲染 data。 */
export type Section<T> = Omit<SectionBase, never> & { data: T | null };

/** 契约里的全部接口(阶段与最低角色)。API 客户端只允许请求这里列出的路径。 */
export const ENDPOINTS = {
  getMeta: { method: "GET", path: "/api/meta", phase: 1, role: "viewer" },
  getOverview: { method: "GET", path: "/api/overview", phase: 1, role: "viewer" },
  getTrading: { method: "GET", path: "/api/trading", phase: 1, role: "viewer" },
  getAlerts: { method: "GET", path: "/api/alerts", phase: 1, role: "viewer" },
  getDays: { method: "GET", path: "/api/days", phase: 1, role: "viewer" },
  getRecording: { method: "GET", path: "/api/recording", phase: 1, role: "viewer" },
  getResearch: { method: "GET", path: "/api/research", phase: 1, role: "viewer" },
  getRun: { method: "GET", path: "/api/research/runs/{run_id}", phase: 1, role: "viewer" },
  getDoc: { method: "GET", path: "/api/docs/{key}", phase: 1, role: "viewer" },
  getLive: { method: "GET", path: "/api/live", phase: 2, role: "viewer" },
  getEquity: { method: "GET", path: "/api/equity", phase: 2, role: "viewer" },
  pauseOpening: { method: "POST", path: "/api/control/pause", phase: 3, role: "operator" },
  resumeOpening: { method: "POST", path: "/api/control/resume", phase: 3, role: "operator" },
  getAudit: { method: "GET", path: "/api/audit", phase: 3, role: "operator" },
  getCandidate: { method: "GET", path: "/api/research/candidates/{name}", phase: 4, role: "viewer" },
  listRuns: { method: "GET", path: "/api/research/runs", phase: 4, role: "viewer" },
  getDataSegments: { method: "GET", path: "/api/data/segments", phase: 4, role: "viewer" },
  getDrills: { method: "GET", path: "/api/drills", phase: 4, role: "viewer" },
  getRecordingQuality: { method: "GET", path: "/api/recording/quality", phase: 4, role: "viewer" },
  getConfig: { method: "GET", path: "/api/config", phase: 4, role: "viewer" },
  getNodes: { method: "GET", path: "/api/nodes", phase: 5, role: "viewer" },
  whoami: { method: "GET", path: "/api/whoami", phase: 5, role: "viewer" },
  listEvolution: { method: "GET", path: "/api/research/evolution", phase: 4, role: "viewer" },
  getEvolutionCurve: { method: "GET", path: "/api/research/evolution/{campaign_id}/curve", phase: 4, role: "viewer" },
  getEvolutionLog: { method: "GET", path: "/api/research/evolution/{campaign_id}/log", phase: 4, role: "viewer" },
  getRunHeatmap: { method: "GET", path: "/api/research/runs/{run_id}/heatmap", phase: 4, role: "viewer" },
  getCandidateCode: { method: "GET", path: "/api/research/candidates/{sha256}/code", phase: 4, role: "viewer" },
  getCandidateDiff: { method: "GET", path: "/api/research/candidates/{sha256}/diff", phase: 4, role: "viewer" },
} as const;
export type EndpointName = keyof typeof ENDPOINTS;
