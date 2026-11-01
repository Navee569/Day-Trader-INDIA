# Stage 4 — Multi-AI paper decision engine

Working copy: `C:\Users\prave\Documents\Codex\Day-Trader-INDIA-Stage2`
Branch: `codex/stage2-provider-integration`
Original `Day-Trader-INDIA` checkout: unchanged
Stage checkpoint: uncommitted; included in the later Stage 7 branch commit.

## Operation

The Stage 3 market engine writes fresh `AWAITING_AI_REVIEW` signals to `trader/state/market-engine.json`. Stage 4 revalidates the NSE symbol and instrument ID, completed one- and five-minute broker candles, timestamped quote, opening-range breakout, volume, turnover, range, spread, market status, source provenance and age before calling a model. Yahoo or daily Bhavcopy research candidates cannot pass this gate. `check` performs this validation without a model call. `once` performs one review; `run` polls in the foreground.

The example configuration enables AI1 technical analysis and AI4 independent final judgment. AI2 news/market and AI3 risk critic can be enabled for two or three analysts. All enabled analysts receive the same evidence independently. With two or three analysts, up to two configured debate rounds let them challenge one another. A single analyst skips debate. AI4 receives the evidence and all analyses, independently records `SELECT`, `MONITOR`, `REJECT` or `NO_TRADE` for every candidate, and can reject all. It ranks selected/monitored candidates, including a research score. A selection means only **monitor a possible paper entry**. This stage neither starts Stage 5 entry monitoring nor sends a paper or real order.

Decision records include the symbol, instrument ID, score/rank, outcome, market evidence IDs and timestamps, analyst arguments, debate, final reasoning, risk concerns, entry conditions, decision time and expiry. The persisted state is `trader/state/ai-committee.json`. The last 100 full decisions are retained, with processed signal IDs kept for the full trading day to prevent repeat evaluation after records roll off. Decisions expire by their configured lifetime, the underlying signal's evidence age, or the 15:10 IST last-new-signal cutoff, whichever comes first. The next stage must independently check freshness, paper capital, strategy and risk before any simulated order.

## Model connection

AI4 uses the locally installed official Codex CLI and requires **ChatGPT login mode**, checked with `codex login status`. An API-key login is rejected to avoid silently switching to separately billed access. Each call is a new noninteractive `codex exec` session in a temporary folder with `--sandbox read-only`, `--ephemeral`, `--ignore-user-config`, `--output-schema` and `--json`. The process receives a limited environment; broker tokens and API keys are excluded. The adapter rejects reported tool activity and validates the returned JSON fields, evidence IDs, outcomes, ranks and numeric ranges. It records CLI-reported token usage when available, otherwise a local estimate. Per-agent timeouts, retries, prompt/answer size limits, daily call limits and estimated daily token limits stop new selections when exceeded. CLI read-only mode and output review do not prove a model never read another file; this is why no broker credentials are supplied to the subprocess and the model cannot submit an order.

Optional analyst providers `ollama` and `lmstudio` use the official CLI's local-provider flags. They need a separately running local model service; this integration was not exercised against such a service. AI4 remains Codex CLI in all configurations.

AI2 can analyze an optional JSON context file whose items have exactly `ticker`, `kind`, `observed_at`, `source_url` and `summary`. Each must be dated within one day and have an HTTPS source URL. These entries are labeled `USER_SUPPLIED_UNVERIFIED`; the committee does **not** fetch or verify the linked story, earnings filing or broader market context. Missing context is explicit to the models. Existing separate news/earnings tools are not automatically connected to Stage 4.

## Commands

```text
python -m india_trader market-engine once --symbols RELIANCE,TCS
python -m india_trader ai-committee check
python -m india_trader ai-committee once
python -m india_trader ai-committee run --max-cycles 10
python -m india_trader ai-committee status
```

Use `--config`, `--engine-state`, `--state` and optionally `--context-file` to change paths. Configure only local file paths; never paste credentials into config, context or chat. The process will normally report `WAIT_ENGINE` or `WAIT_ENGINE_CURRENT_SESSION` outside a running, current Stage 3 session. AI or auth failure results in `AI_UNAVAILABLE_NO_SELECTION`. Existing paper positions are outside this component and their protection is not tied to this model process.

## Validation

On Windows/Python 3.11.2, all **264 offline tests passed**. Syntax compilation and `git diff --check` passed. The `ai-committee check` and `status` command smoke test returned `WAIT_ENGINE` with no orders possible, as expected without a current Stage 3 handoff. Tests cover one/two/three analysts, independent AI4, bounded debates, all-reject, ranking, once-only records after archival, malformed output, retries, usage limit, stale/unverified signals, closed sessions and mocked CLI commands. The CLI's installed help and ChatGPT login mode were checked locally. No actual Codex model call, Upstox/FYERS live handoff, local Ollama/LM Studio service, open-market session or Stage 5 automated paper trade has been validated. Tests use synthetic prices and mocked model results; they establish control flow, not profitable trading.

The official CLI supports [noninteractive execution](https://learn.chatgpt.com/docs/non-interactive-mode) and [ChatGPT login](https://learn.chatgpt.com/docs/auth). Its usage and model availability remain subject to the user's account and service at execution time.
