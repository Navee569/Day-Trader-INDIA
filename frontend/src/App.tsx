import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { overview, saveSettings, streamSnapshots } from './api';
import type { AIConfig, Candidate, Decision, MarketConfig, NotificationConfig, PaperConfig, Position, Snapshot } from './types';

type Tab = 'dashboard' | 'scanner' | 'debate' | 'trading' | 'settings';
const tabs: { id: Tab; label: string; icon: string }[] = [
  { id: 'dashboard', label: 'Dashboard', icon: '◫' },
  { id: 'scanner', label: 'Scanner', icon: '⌁' },
  { id: 'debate', label: 'AI Debate', icon: '✦' },
  { id: 'trading', label: 'Trading', icon: '▣' },
  { id: 'settings', label: 'Settings', icon: '⚙' },
];

const inr = (value: number | null | undefined, digits = 2) => value == null || !Number.isFinite(value)
  ? '—' : new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value);
const num = (value: number | null | undefined) => value == null || !Number.isFinite(value)
  ? '—' : new Intl.NumberFormat('en-IN').format(value);
const when = (value?: string | null) => {
  if (!value) return 'Not yet';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? 'Unknown time' : new Intl.DateTimeFormat('en-IN', {
    dateStyle: 'medium', timeStyle: 'short', timeZone: 'Asia/Kolkata',
  }).format(date);
};
const signed = (value: number | null | undefined) => value == null ? '—' : `${value > 0 ? '+' : ''}${inr(value)}`;
const tone = (value?: string | null) => {
  if (!value) return 'muted';
  if (/SELECT|FILLED|BALANCED|RUNNING|OPEN|TRIGGERED|VERIFIED|PASS/.test(value)) return 'positive';
  if (/REJECT|BLOCK|ERROR|UNAVAILABLE|STALE|CANCELLED|CLOSED/.test(value)) return 'negative';
  return 'warning';
};

function Pill({ value, label }: { value?: string | null; label?: string }) {
  return <span className={`pill ${tone(value)}`}>{label || value || 'Unknown'}</span>;
}

function Empty({ title, text }: { title: string; text: string }) {
  return <div className="empty"><span className="empty-symbol">◇</span><strong>{title}</strong><p>{text}</p></div>;
}

function Metric({ label, value, note, accent }: { label: string; value: string; note: string; accent?: string }) {
  return <div className={`metric ${accent || ''}`}><span>{label}</span><strong>{value}</strong><small>{note}</small></div>;
}

function SectionTitle({ eyebrow, title, right }: { eyebrow: string; title: string; right?: string }) {
  return <div className="section-heading"><div><span className="eyebrow">{eyebrow}</span><h2>{title}</h2></div>{right && <span className="section-right">{right}</span>}</div>;
}

function CandidateRow({ row, decision }: { row: Candidate; decision?: Decision }) {
  const metrics = row.metrics || {};
  return <article className="candidate-row">
    <div className="candidate-main"><div className="ticker-icon">{row.ticker.slice(0, 2)}</div><div>
      <strong>{row.ticker.replace('.NS', '')}</strong><small>NSE cash equity · {row.setup || 'Opening range'}</small>
    </div></div>
    <div className="candidate-facts"><span><b>{inr(metrics.vwap)}</b><small>VWAP</small></span>
      <span><b>{num(metrics.day_volume)}</b><small>Volume</small></span>
      <span><b>{metrics.spread_pct == null ? '—' : `${metrics.spread_pct.toFixed(2)}%`}</b><small>Spread</small></span></div>
    <div className="candidate-end"><Pill value={decision?.outcome || row.status} />
      <small>{row.last_verified_at ? when(row.last_verified_at) : 'Not live verified'}</small></div>
  </article>;
}

function Dashboard({ data }: { data: Snapshot }) {
  const recent = data.trading.daily.slice(-7);
  const maxAbs = Math.max(1, ...recent.map((day) => Math.abs(day.realized_net_pnl_inr)));
  const latestDecisions = new Map(data.committee.decisions.map((item) => [item.ticker, item]));
  return <>
    <section className="hero card"><div><span className="eyebrow">YOUR PAPER DESK</span>
      <h1>A clear view of every paper decision.</h1>
      <p>Track market coverage, AI review, and simulated positions in one place. Figures are hypothetical.</p>
      <div className="hero-tags"><Pill value={data.market.status} label={`NSE ${data.market.status}`} />
        <span className="hero-tag">Paper trading only</span><span className="hero-tag">Asia/Kolkata</span></div>
    </div><div className="hero-orbit" aria-hidden="true"><div className="orbit-ring"><span>₹</span></div></div></section>
    <div className="metric-grid">
      <Metric label="Portfolio value" value={inr(data.summary.portfolio_value_inr)}
        note={data.summary.ledger_started ? (data.summary.portfolio_value_inr == null ? 'Fresh quote needed' : 'Marked paper account') : 'Paper ledger not started'} accent="teal" />
      <Metric label="Today’s realized P&L" value={signed(data.summary.today_realized_pnl_inr)} note="Closed paper trades today" accent={data.summary.today_realized_pnl_inr < 0 ? 'rose' : 'teal'} />
      <Metric label="Total realized P&L" value={signed(data.summary.total_realized_pnl_inr)} note="After modeled costs" />
      <Metric label="Open positions" value={num(data.summary.open_positions)} note={`${data.trading.balance?.reserved_inr != null ? inr(data.trading.balance.reserved_inr) : '—'} reserved`} />
    </div>
    <div className="two-col">
      <section className="card panel"><SectionTitle eyebrow="SYSTEM PULSE" title="What’s running" right={when(data.as_of)} />
        <div className="health-grid"><div><span>Market scanner</span><Pill value={data.health.scanner} /></div>
          <div><span>AI committee</span><Pill value={data.health.committee} /></div>
          <div><span>Paper monitor</span><Pill value={data.health.paper} /></div>
          <div><span>Active data source</span><strong>{data.providers.active || 'No live source'}</strong></div></div>
        {data.settings.notifications?.show_feed_warnings !== false && data.health.errors.length > 0 &&
          <div className="alert-box">{data.health.errors.join(' · ')}</div>}
      </section>
      <section className="card panel"><SectionTitle eyebrow="PAPER PERFORMANCE" title="Recent realized P&L" right="Past active days" />
        {recent.length ? <div className="bars" aria-label="Daily realized paper profit and loss">{recent.map((day) =>
          <div className="bar-slot" key={day.date}><div className="bar-track"><div className={`bar ${day.realized_net_pnl_inr < 0 ? 'loss' : ''}`}
            style={{ height: `${Math.max(5, Math.abs(day.realized_net_pnl_inr) / maxAbs * 100)}%` }} /></div>
            <small>{day.date.slice(5)}</small></div>)}</div> : <Empty title="No paper history yet" text="Daily results appear after the simulator records activity." />}
      </section>
    </div>
    <section className="card panel"><SectionTitle eyebrow="MARKET WATCH" title="Latest candidates" right={`${data.scanner.coverage.candidate_pool ?? data.scanner.candidates.length} in pool`} />
      {data.scanner.candidates.length ? data.scanner.candidates.slice(0, 4).map((row) =>
        <CandidateRow key={row.ticker} row={row} decision={latestDecisions.get(row.ticker)} />) :
        <Empty title="Waiting for a normal market session" text="The scanner will show candidates when it is running and a source supplies usable data." />}
    </section>
  </>;
}

function Scanner({ data }: { data: Snapshot }) {
  const coverage = data.scanner.coverage;
  const latestDecisions = new Map(data.committee.decisions.map((item) => [item.ticker, item]));
  return <>
    <SectionTitle eyebrow="MARKET SCANNER" title="Find, verify, then review" right={`Updated ${when(data.scanner.updated_at)}`} />
    <div className="metric-grid compact">
      <Metric label="Universe" value={num(Number(coverage.universe ?? 0))} note="Supported NSE EQ symbols" />
      <Metric label="Broad checked" value={num(Number(coverage.latest_broad_scanned ?? 0))} note="Latest broad scan" accent="teal" />
      <Metric label="Live verified" value={num(Number(coverage.reliable_live_now ?? 0))} note="Fresh broker data now" />
      <Metric label="Not proven live" value={num(Number(coverage.not_proven_reliable_live ?? 0))} note="Coverage gap, not a signal" accent="rose" />
    </div>
    <section className="card panel"><SectionTitle eyebrow="SHORTLIST" title="Candidates under review" right={`${data.scanner.candidates.length} shown`} />
      {data.scanner.candidates.length ? data.scanner.candidates.map((row) =>
        <CandidateRow key={row.ticker} row={row} decision={latestDecisions.get(row.ticker)} />) :
        <Empty title="No current shortlist" text="Closed markets and unverified feeds can leave this empty. That is an expected safety result." />}
    </section>
    <section className="card panel"><SectionTitle eyebrow="VERIFIED HANDOFFS" title="Recent signals" />
      {data.scanner.signals.length ? <div className="table-scroll"><table><thead><tr><th>Stock</th><th>State</th><th>Breakout</th><th>Quote</th><th>Sources</th><th>Time</th></tr></thead><tbody>
        {data.scanner.signals.map((row) => <tr key={row.id}><td><strong>{row.ticker}</strong></td><td><Pill value={row.status} /></td>
          <td>{inr(row.opening_range_high)}</td><td>{inr(row.quote_price)}</td>
          <td>{row.source_1m || '—'} / {row.source_5m || '—'}</td><td>{when(row.recorded_at)}</td></tr>)}</tbody></table></div> :
        <Empty title="No verified handoffs" text="A research chart alone cannot create a paper entry." />}
    </section>
  </>;
}

function Debate({ data }: { data: Snapshot }) {
  return <>
    <SectionTitle eyebrow="AI COMMITTEE" title="Evidence, arguments, decision" right={`${data.committee.analyst_count} analyst${data.committee.analyst_count === 1 ? '' : 's'} + AI4`} />
    <div className="agent-grid">{data.committee.agents.map((agent) => <div className="agent-card card" key={agent.id}>
      <div className="agent-top"><span className="agent-number">{agent.id}</span><Pill value={agent.enabled ? 'RUNNING' : 'DISABLED'} label={agent.enabled ? 'Enabled' : 'Off'} /></div>
      <strong>{agent.role.replaceAll('_', ' ')}</strong><small>{agent.provider} · {agent.model || 'Default model'}</small>
    </div>)}</div>
    <section className="card panel"><SectionTitle eyebrow="FINAL JUDGMENTS" title="Ranked paper decisions" right={`Updated ${when(data.committee.updated_at)}`} />
      {data.committee.status === 'AI_UNAVAILABLE_NO_SELECTION' && <div className="alert-box">AI unavailable: no new paper selections. {data.committee.last_reason || ''}</div>}
      {data.committee.decisions.length ? data.committee.decisions.map((decision) =>
        <article className="decision" key={decision.signal_id}>
          <div className="decision-top"><div><span className="eyebrow">{decision.rank ? `RANK ${decision.rank}` : 'NO RANK'} · {decision.instrument_id}</span>
            <h3>{decision.ticker.replace('.NS', '')}</h3></div><div className="decision-result"><Pill value={decision.outcome} />
            <span>{decision.score == null ? '—' : `${decision.score}/100`}</span></div></div>
          <p>{decision.reasoning || 'No final reasoning recorded.'}</p>
          <div className="decision-meta"><span>Evidence {when(decision.evidence_at)}</span><span>Expires {when(decision.valid_until)}</span></div>
          <details><summary>Read analyst debate and risks</summary>
            <div className="analysis-grid">{decision.analyses?.map((item) => <div className="analysis" key={item.analyst_id}>
              <strong>{item.analyst_id} · {item.assessment.stance || 'UNCERTAIN'}</strong><p>{item.assessment.reasoning}</p>
              {!!item.assessment.risk_concerns?.length && <small>Risks: {item.assessment.risk_concerns.join(' · ')}</small>}</div>)}</div>
            {!!decision.debate?.length && <div className="debate-block"><strong>Debate</strong>{decision.debate.map((item, index) =>
              <p key={`${item.analyst_id}-${index}`}><b>{item.analyst_id}:</b> {item.assessment.reasoning} {item.assessment.challenges?.join(' · ')}</p>)}</div>}
            {!!decision.risk_concerns?.length && <p><b>AI4 risks:</b> {decision.risk_concerns.join(' · ')}</p>}
            {!!decision.entry_conditions?.length && <p><b>Conditions:</b> {decision.entry_conditions.join(' · ')}</p>}
            <small>Evidence IDs: {decision.evidence_ids?.join(', ') || '—'}</small>
          </details>
        </article>) : <Empty title="No AI judgment yet" text="The committee waits for a fresh, verified Stage 3 handoff during a normal session." />}
    </section>
  </>;
}

function PositionCard({ position }: { position: Position }) {
  return <article className="position-card"><div className="position-top"><div><strong>{position.ticker.replace('.NS', '')}</strong>
    <small>{position.quantity} shares · opened {when(position.opened_at)}</small></div><Pill value={position.mark_fresh ? 'VERIFIED' : 'STALE'} label={position.mark_fresh ? 'Fresh mark' : 'Mark unavailable'} /></div>
    <div className="position-prices"><span><small>Entry</small><b>{inr(position.average_cost_inr)}</b></span>
      <span><small>Current</small><b>{inr(position.mark_price_inr)}</b></span>
      <span><small>Stop / trail</small><b>{inr(position.trailing_stop_inr ?? position.stop_inr)}</b></span>
      <span><small>Target</small><b>{inr(position.target_inr)}</b></span></div></article>;
}

function Trading({ data }: { data: Snapshot }) {
  const balance = data.trading.balance;
  return <>
    <SectionTitle eyebrow="PAPER TRADING" title="Portfolio and activity" right="Local simulation only" />
    <div className="metric-grid compact">
      <Metric label="Cash" value={inr(balance?.cash_inr)} note="Paper balance" accent="teal" />
      <Metric label="Available" value={inr(balance?.available_cash_inr)} note="After pending reservations" />
      <Metric label="Reserved" value={inr(balance?.reserved_inr)} note="Pending paper buys" />
      <Metric label="Unrealized" value={signed(data.summary.unrealized_pnl_inr)} note={data.summary.unrealized_status.replaceAll('_', ' ').toLowerCase()} />
    </div>
    <section className="card panel"><SectionTitle eyebrow="OPEN PAPER POSITIONS" title="What is being monitored" />
      {data.trading.positions.length ? <div className="position-grid">{data.trading.positions.map((p) => <PositionCard key={p.ticker} position={p} />)}</div> :
        <Empty title="No open paper positions" text="A verified AI selection must still pass a new bar, capital, and risk checks before a simulated fill." />}
    </section>
    <div className="two-col">
      <section className="card panel"><SectionTitle eyebrow="ORDER QUEUE" title="Virtual orders" />
        {data.trading.orders.length ? <div className="stack-list">{[...data.trading.orders].reverse().slice(0, 10).map((o) =>
          <div className="list-row" key={o.id}><div><strong>{o.side} {o.ticker}</strong><small>{o.filled_quantity}/{o.quantity} shares · {when(o.submitted_at)}</small></div><Pill value={o.status} /></div>)}</div> :
          <Empty title="No orders recorded" text="Simulated orders and rejections will appear here." />}
      </section>
      <section className="card panel"><SectionTitle eyebrow="CLOSED TRADES" title="Recent history" />
        {data.trading.trades.length ? <div className="stack-list">{[...data.trading.trades].reverse().slice(0, 10).map((t, index) =>
          <div className="list-row" key={`${t.closed_at}-${index}`}><div><strong>{t.ticker} · {t.quantity} shares</strong><small>{t.reason.replaceAll('_', ' ')} · {when(t.closed_at)}</small></div>
            <b className={t.net_pnl_inr < 0 ? 'text-loss' : 'text-gain'}>{signed(t.net_pnl_inr)}</b></div>)}</div> :
          <Empty title="No closed trades" text="Net paper P&L appears after a simulated sell." />}
      </section>
    </div>
    <section className="card panel"><SectionTitle eyebrow="DAILY SUMMARY" title="Cash and modeled costs" />
      {data.trading.daily.length ? <div className="table-scroll"><table><thead><tr><th>Day</th><th>Realized P&L</th><th>Charges</th><th>Fills</th><th>Ending cash</th><th>Equity</th></tr></thead><tbody>
        {[...data.trading.daily].reverse().map((day) => <tr key={day.date}><td>{day.date}</td>
          <td className={day.realized_net_pnl_inr < 0 ? 'text-loss' : 'text-gain'}>{signed(day.realized_net_pnl_inr)}</td>
          <td>{inr(day.total_modeled_charges_inr)}</td><td>{day.simulated_fills}</td>
          <td>{inr(day.ending_cash_inr)}</td><td>{inr(day.end_of_day_equity_inr)}</td></tr>)}</tbody></table></div> :
        <Empty title="No daily activity" text="The account will add a dated summary while the paper controller runs." />}
    </section>
  </>;
}

function NumberField({ label, value, onChange, min = 0, max, step = 1, disabled = false }: {
  label: string; value: number; onChange: (value: number) => void; min?: number; max?: number; step?: number; disabled?: boolean;
}) {
  return <label className="field"><span>{label}</span><input type="number" value={value} min={min} max={max} step={step}
    disabled={disabled} onChange={(event) => onChange(Number(event.target.value))} /></label>;
}

function SettingsScreen({ data, token }: { data: Snapshot; token: string }) {
  const [ai, setAi] = useState<AIConfig | null>(null);
  const [paper, setPaper] = useState<PaperConfig | null>(null);
  const [market, setMarket] = useState<MarketConfig | null>(null);
  const [notifications, setNotifications] = useState<NotificationConfig | null>(null);
  const [message, setMessage] = useState('');
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    if (ai === null && data.settings.ai) setAi(structuredClone(data.settings.ai));
    if (paper === null && data.settings.paper) setPaper(structuredClone(data.settings.paper));
    if (market === null && data.settings.market) setMarket(structuredClone(data.settings.market));
    if (notifications === null && data.settings.notifications) setNotifications(structuredClone(data.settings.notifications));
  }, [data.settings, ai, paper, market, notifications]);
  async function save(section: string, payload: unknown) {
    setSaving(true); setMessage('');
    try {
      if (section === 'notifications' && notifications?.browser_events && 'Notification' in window && Notification.permission === 'default') {
        await Notification.requestPermission();
      }
      setMessage(await saveSettings(token, section, payload));
    } catch (error) { setMessage(error instanceof Error ? error.message : 'Could not save settings'); }
    finally { setSaving(false); }
  }
  const updateAnalyst = (index: number, patch: Partial<AIConfig['analysts'][number]>) => setAi((previous) => previous && ({
    ...previous, analysts: previous.analysts.map((agent, i) => i === index ? { ...agent, ...patch } : agent),
  }));
  const updatePaper = (patch: Partial<PaperConfig>) => setPaper((previous) => previous && ({ ...previous, ...patch }));
  const updateRisk = (key: string, value: number) => setPaper((previous) => previous && ({
    ...previous, risk_limits: { ...previous.risk_limits, [key]: value },
  }));
  return <>
    <SectionTitle eyebrow="SETTINGS" title="Tune your paper desk" right="Changes apply after worker restart" />
    {message && <div className="notice" role="status">{message}</div>}
    <section className="card panel"><SectionTitle eyebrow="DATA ACCESS" title="Provider and credential status" />
      <div className="provider-grid"><div><strong>Upstox</strong><Pill value={data.providers.upstox_configured ? 'VERIFIED' : 'UNAVAILABLE'} label={data.providers.upstox_configured ? 'Token configured' : 'Not configured'} /></div>
        <div><strong>FYERS</strong><Pill value={data.providers.fyers_configured ? 'VERIFIED' : 'UNAVAILABLE'} label={data.providers.fyers_configured ? 'Account configured' : 'Not configured'} /></div>
        <div><strong>Yahoo</strong><Pill value="MONITOR" label="Research fallback" /></div></div>
      <p className="hint">Broker data credentials are entered on this computer through local environment settings. This dashboard never accepts or displays their values. Without fresh broker data, the paper entry gate stays closed.</p>
    </section>
    {market && <section className="card panel"><SectionTitle eyebrow="SCANNER" title="Coverage and quality filters" />
      <div className="form-grid"><NumberField label="Broad symbols per batch" value={market.broad_batch_size} min={1} max={50} onChange={(value) => setMarket({ ...market, broad_batch_size: value })} />
        <NumberField label="Detailed checks per cycle" value={market.detailed_batch_size} min={1} max={10} onChange={(value) => setMarket({ ...market, detailed_batch_size: value })} />
        <NumberField label="Candidate pool limit" value={market.max_candidates} min={1} max={50} onChange={(value) => setMarket({ ...market, max_candidates: value })} />
        <NumberField label="Maximum spread %" value={market.max_spread_pct} min={0.01} max={1} step={0.01} onChange={(value) => setMarket({ ...market, max_spread_pct: value })} />
        <NumberField label="Minimum turnover ₹" value={market.min_turnover_inr} min={0} step={100000} onChange={(value) => setMarket({ ...market, min_turnover_inr: value })} />
        <NumberField label="Candidate expiry minutes" value={market.candidate_ttl_minutes} min={1} max={120} onChange={(value) => setMarket({ ...market, candidate_ttl_minutes: value })} /></div>
      <button className="button primary" disabled={saving} onClick={() => save('market', market)}>Save scanner settings</button></section>}
    {ai && <section className="card panel"><SectionTitle eyebrow="AI COMMITTEE" title="Analysts and judge" />
      <div className="form-grid"><NumberField label="Debate rounds" value={ai.max_debate_rounds} min={0} max={2} onChange={(value) => setAi({ ...ai, max_debate_rounds: value })} />
        <NumberField label="Daily AI call limit" value={ai.max_calls_per_day} min={2} max={200} onChange={(value) => setAi({ ...ai, max_calls_per_day: value })} /></div>
      <div className="analyst-settings">{ai.analysts.map((agent, index) => <div className="analyst-setting" key={agent.id}>
        <div className="setting-top"><strong>{agent.id} · {agent.role.replaceAll('_', ' ')}</strong><label className="toggle"><input type="checkbox" checked={agent.enabled} onChange={(event) => updateAnalyst(index, { enabled: event.target.checked })} /><span>Enabled</span></label></div>
        <div className="form-grid"><label className="field"><span>Provider</span><select value={agent.provider} onChange={(event) => updateAnalyst(index, { provider: event.target.value })}>
          <option value="codex_cli">Codex CLI</option><option value="ollama">Ollama local</option><option value="lmstudio">LM Studio local</option></select></label>
          <label className="field"><span>Model (blank = default)</span><input value={agent.model || ''} onChange={(event) => updateAnalyst(index, { model: event.target.value || null })} /></label></div>
        <label className="field"><span>Instructions</span><textarea rows={2} value={agent.instructions} onChange={(event) => updateAnalyst(index, { instructions: event.target.value })} /></label>
      </div>)}</div>
      <p className="hint">AI4 stays enabled as the independent final judge. A selection is research only; the separate risk gate controls paper entry.</p>
      <button className="button primary" disabled={saving} onClick={() => save('ai', ai)}>Save AI settings</button></section>}
    {paper && <section className="card panel"><SectionTitle eyebrow="VIRTUAL PORTFOLIO" title="Capital, risk and costs" />
      <div className="form-grid"><NumberField label="Starting paper capital ₹" value={paper.initial_capital_inr} min={1000} step={1000} disabled={data.summary.ledger_started} onChange={(value) => updatePaper({ initial_capital_inr: value })} />
        <NumberField label="Max open positions" value={paper.max_open_positions} min={1} max={20} onChange={(value) => updatePaper({ max_open_positions: value })} />
        <NumberField label="Max loss per trade ₹" value={Number(paper.risk_limits.max_loss_per_trade_inr)} min={1} step={50} onChange={(value) => updateRisk('max_loss_per_trade_inr', value)} />
        <NumberField label="Daily loss cap ₹" value={Number(paper.risk_limits.max_daily_loss_inr)} min={1} step={100} onChange={(value) => updateRisk('max_daily_loss_inr', value)} />
        <NumberField label="Portfolio exposure %" value={paper.max_portfolio_exposure_pct} min={1} max={100} onChange={(value) => updatePaper({ max_portfolio_exposure_pct: value })} />
        <NumberField label="Trailing stop %" value={paper.trailing_pct} min={0} max={10} step={0.1} onChange={(value) => updatePaper({ trailing_pct: value })} />
        <NumberField label="Maximum quote age seconds" value={paper.max_quote_age_seconds} min={1} max={30} onChange={(value) => updatePaper({ max_quote_age_seconds: value })} />
        <NumberField label="Volume participation %" value={paper.max_volume_participation_pct} min={0.01} max={25} step={0.5} onChange={(value) => updatePaper({ max_volume_participation_pct: value })} /></div>
      <details className="cost-settings"><summary>Modeled India charges</summary><p className="hint">These are configurable paper assumptions, not a broker contract note.</p>
        <div className="form-grid">{Object.entries(paper.cost_schedule).filter(([key]) => key !== 'market').map(([key, value]) =>
          <NumberField key={key} label={key.replaceAll('_', ' ')} value={Number(value)} min={0} max={1000} step={0.001}
            onChange={(next) => setPaper({ ...paper, cost_schedule: { ...paper.cost_schedule, [key]: next } })} />)}</div></details>
      {data.summary.ledger_started && <p className="hint">Starting capital is locked once a paper ledger exists. Other changes apply after restarting the paper worker.</p>}
      <button className="button primary" disabled={saving} onClick={() => save('paper', paper)}>Save paper settings</button></section>}
    {notifications && <section className="card panel"><SectionTitle eyebrow="NOTIFICATIONS" title="While this dashboard is open" />
      <label className="check-row"><input type="checkbox" checked={notifications.browser_events} onChange={(event) => setNotifications({ ...notifications, browser_events: event.target.checked })} />
        <span><strong>Browser paper-fill alerts</strong><small>Only while this page is connected and browser permission is granted.</small></span></label>
      <label className="check-row"><input type="checkbox" checked={notifications.show_feed_warnings} onChange={(event) => setNotifications({ ...notifications, show_feed_warnings: event.target.checked })} />
        <span><strong>Show feed warnings</strong><small>Keep data outages visible in the dashboard.</small></span></label>
      <button className="button primary" disabled={saving} onClick={() => save('notifications', notifications)}>Save preferences</button></section>}
  </>;
}

export default function App() {
  const [access, setAccess] = useState('');
  const [token, setToken] = useState('');
  const [data, setData] = useState<Snapshot | null>(null);
  const [tab, setTab] = useState<Tab>('dashboard');
  const [loginError, setLoginError] = useState('');
  const [connecting, setConnecting] = useState(false);
  const [live, setLive] = useState('Connecting');
  const lastFillAt = useRef<string | null>(null);

  async function signIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setConnecting(true); setLoginError('');
    try {
      const initial = await overview(access.trim());
      lastFillAt.current = typeof initial.trading.last_cycle.at === 'string' ? initial.trading.last_cycle.at : null;
      setData(initial); setToken(access.trim()); setAccess('');
    } catch (error) { setLoginError(error instanceof Error ? error.message : 'Could not connect'); }
    finally { setConnecting(false); }
  }
  useEffect(() => {
    if (!token) return;
    const controller = new AbortController();
    let stopped = false;
    async function connect() {
      while (!stopped) {
        try {
          setLive('Connecting');
          await streamSnapshots(token, (next) => {
            setLive('Live');
            const at = next.trading.last_cycle.at;
            const fills = next.trading.last_cycle.fills;
            if (typeof at === 'string' && at !== lastFillAt.current && Array.isArray(fills) && fills.length &&
                next.settings.notifications?.browser_events && 'Notification' in window && Notification.permission === 'granted') {
              new Notification('Paper fill recorded', { body: `${fills.length} simulated fill event${fills.length === 1 ? '' : 's'}. Open the Trading tab for details.` });
            }
            if (typeof at === 'string') lastFillAt.current = at;
            setData(next);
          }, controller.signal);
        } catch { if (!stopped) setLive('Reconnecting'); }
        if (!stopped) await new Promise((resolve) => setTimeout(resolve, 2500));
      }
    }
    void connect();
    return () => { stopped = true; controller.abort(); };
  }, [token]);

  if (!token || !data) return <div className="login-shell"><div className="login-glow" /><div className="login-card card">
    <div className="brand-mark">₹<span className="brand-pulse" /></div><span className="eyebrow">DAY TRADER INDIA</span>
    <h1>Your private paper desk.</h1><p>See what the market scanner, AI committee, and virtual portfolio are doing—without placing a real trade.</p>
    <form onSubmit={signIn}><label className="field"><span>Dashboard access key</span><input type="password" autoComplete="off" value={access}
      onChange={(event) => setAccess(event.target.value)} placeholder="Paste the key from your computer" required /></label>
      {loginError && <div className="alert-box" role="alert">{loginError}</div>}
      <button className="button primary full" disabled={connecting}>{connecting ? 'Connecting…' : 'Open dashboard'}</button></form>
    <small>Find the key at trader/state/ui-access-token.txt on the computer running the dashboard. It stays in this browser tab’s memory only.</small>
  </div></div>;

  const currentTab = tabs.find((item) => item.id === tab)!;
  return <div className="app-shell"><aside className="sidebar"><div className="brand"><span className="brand-mark">₹<span className="brand-pulse" /></span><div><strong>Day Trader India</strong><small>PAPER DESK</small></div></div>
    <nav aria-label="Primary navigation">{tabs.map((item) => <button key={item.id} className={`nav-item ${tab === item.id ? 'active' : ''}`}
      aria-current={tab === item.id ? 'page' : undefined} onClick={() => setTab(item.id)}><span>{item.icon}</span>{item.label}</button>)}</nav>
    <div className="sidebar-foot"><span className="sidebar-dot" />Local paper simulation<br /><small>No real order connection</small></div></aside>
    <div className="main-shell"><header className="topbar"><div className="mobile-brand"><span className="brand-mark">₹</span><strong>Day Trader India</strong></div>
      <div className="breadcrumb">WORKSPACE <span>/</span> {currentTab.label.toUpperCase()}</div><div className="top-actions">
        <span className={`live-indicator ${live === 'Live' ? '' : 'offline'}`}><i />{live}</span>
        <button className="lock-button" aria-label="Lock dashboard" title="Lock dashboard" onClick={() => { setToken(''); setData(null); }}>Lock</button>
      </div></header>
      <main className="content"><div className="page-intro"><div><span className="eyebrow">NSE CASH EQUITIES · PAPER ONLY</span>
        <h1>{currentTab.label}</h1></div><div className="asof">Updated {when(data.as_of)}</div></div>
        {tab === 'dashboard' && <Dashboard data={data} />}
        {tab === 'scanner' && <Scanner data={data} />}
        {tab === 'debate' && <Debate data={data} />}
        {tab === 'trading' && <Trading data={data} />}
        {tab === 'settings' && <SettingsScreen data={data} token={token} />}
        <footer className="page-foot">Paper research and simulation only. No live broker orders. Data and AI decisions can be unavailable or stale.</footer>
      </main></div>
    <nav className="bottom-nav" aria-label="Mobile navigation">{tabs.map((item) => <button key={item.id} className={tab === item.id ? 'active' : ''}
      aria-label={item.label} aria-current={tab === item.id ? 'page' : undefined} onClick={() => setTab(item.id)}><span>{item.icon}</span><small>{item.label}</small></button>)}</nav>
  </div>;
}
