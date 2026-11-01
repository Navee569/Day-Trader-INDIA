export type Status = string | null | undefined;

export interface MarketMetrics {
  day_volume?: number;
  turnover_inr?: number;
  range_pct?: number;
  vwap?: number;
  spread_pct?: number | null;
  ma20?: number | null;
  rsi14?: number | null;
  atr14?: number | null;
}

export interface Candidate {
  ticker: string;
  status?: string;
  setup?: string;
  metrics?: MarketMetrics;
  checks?: string[];
  last_verified_at?: string;
  five_minute_provider?: string;
  one_minute_provider?: string;
  quote_provider?: string;
}

export interface Signal {
  id: string;
  ticker: string;
  status: string;
  trigger_at?: string;
  recorded_at?: string;
  quote_price?: number;
  opening_range_high?: number;
  source_5m?: string;
  source_1m?: string;
  metrics?: MarketMetrics;
}

export interface Assessment {
  stance?: string;
  reasoning?: string;
  risk_concerns?: string[];
  challenges?: string[];
  confidence?: number;
}

export interface AnalystRecord {
  analyst_id: string;
  phase?: string;
  assessment: Assessment;
}

export interface Decision {
  signal_id: string;
  ticker: string;
  instrument_id?: string;
  rank?: number;
  score?: number;
  outcome?: string;
  decided_at?: string;
  valid_until?: string;
  evidence_at?: string;
  evidence_ids?: string[];
  reasoning?: string;
  overall_reasoning?: string;
  risk_concerns?: string[];
  entry_conditions?: string[];
  analyses?: AnalystRecord[];
  debate?: AnalystRecord[];
}

export interface Position {
  ticker: string;
  instrument_id?: string;
  quantity: number;
  average_cost_inr: number;
  cost_basis_inr?: number;
  stop_inr?: number;
  trailing_stop_inr?: number;
  target_inr?: number;
  mark_price_inr?: number | null;
  mark_at?: string;
  mark_fresh?: boolean;
  opened_at?: string;
}

export interface PaperOrder {
  id: string;
  ticker: string;
  side: string;
  quantity: number;
  filled_quantity: number;
  status: string;
  reserved_inr?: number;
  submitted_at?: string;
  reasons?: string[];
  reason?: string;
}

export interface Trade {
  ticker: string;
  quantity: number;
  entry_price_inr: number;
  exit_price_inr: number;
  net_pnl_inr: number;
  reason: string;
  closed_at: string;
}

export interface Daily {
  date: string;
  starting_cash_inr: number;
  ending_cash_inr: number;
  realized_net_pnl_inr: number;
  total_modeled_charges_inr: number;
  simulated_fills: number;
  open_positions_at_day_end: number;
  end_of_day_equity_inr: number | null;
}

export interface Balance {
  status: string;
  cash_inr: number;
  reserved_inr: number;
  available_cash_inr: number;
  open_positions: number;
}

export interface AgentConfig {
  id: string;
  provider: string;
  model: string | null;
  role: string;
  enabled: boolean;
  instructions: string;
  timeout_seconds: number;
  max_retries: number;
  max_prompt_chars: number;
  max_answer_chars: number;
}

export interface AIConfig {
  analysts: AgentConfig[];
  judge: AgentConfig;
  max_debate_rounds: number;
  max_candidates_per_cycle: number;
  max_calls_per_day: number;
  max_estimated_tokens_per_day: number;
  max_evidence_age_seconds: number;
  max_decision_valid_seconds: number;
  poll_seconds: number;
}

export interface PaperConfig {
  mode: 'PAPER_ONLY';
  initial_capital_inr: number;
  max_open_positions: number;
  max_portfolio_exposure_pct: number;
  max_daily_realized_loss_inr: number;
  max_spread_pct: number;
  max_quote_age_seconds: number;
  max_entry_move_pct: number;
  max_volume_participation_pct: number;
  slippage_bps_per_side: number;
  modeled_cost_bps_per_side: number;
  fixed_fee_inr_per_fill: number;
  risk_fee_buffer_inr: number;
  reward_risk: number;
  trailing_pct: number;
  flat_by_ist: string;
  poll_seconds: number;
  risk_limits: Record<string, number | string | string[]>;
  cost_schedule: Record<string, string | number>;
}

export interface MarketConfig {
  broad_batch_size: number;
  detailed_batch_size: number;
  max_candidates: number;
  candidate_ttl_minutes: number;
  min_price: number;
  min_broad_volume: number;
  min_turnover_inr: number;
  min_range_pct: number;
  max_range_pct: number;
  max_spread_pct: number;
  max_signal_age_minutes: number;
}

export interface NotificationConfig {
  browser_events: boolean;
  show_feed_warnings: boolean;
}

export interface Settings {
  ai: AIConfig | null;
  paper: PaperConfig | null;
  market: MarketConfig | null;
  notifications: NotificationConfig | null;
}

export interface Snapshot {
  as_of: string;
  paper_only: true;
  market: { status: string; reason?: string | null; timezone: string; session: string };
  summary: {
    portfolio_value_inr: number | null;
    ledger_started: boolean;
    today_realized_pnl_inr: number;
    total_realized_pnl_inr: number;
    unrealized_pnl_inr: number | null;
    unrealized_status: string;
    open_positions: number;
  };
  scanner: {
    status: string;
    updated_at?: string;
    coverage: Record<string, number | string | null>;
    candidates: Candidate[];
    signals: Signal[];
  };
  committee: {
    status: string;
    updated_at?: string;
    analyst_count: number;
    agents: AgentConfig[];
    decisions: Decision[];
    last_reason?: string;
    usage: Record<string, number>;
  };
  trading: {
    positions: Position[];
    orders: PaperOrder[];
    trades: Trade[];
    daily: Daily[];
    balance: Balance | null;
    last_cycle: Record<string, unknown>;
  };
  providers: {
    active: string | null;
    upstox_configured: boolean;
    fyers_configured: boolean;
    yahoo_research_available: boolean;
  };
  settings: Settings;
  health: { scanner: string; committee: string; paper: string; errors: string[] };
}
