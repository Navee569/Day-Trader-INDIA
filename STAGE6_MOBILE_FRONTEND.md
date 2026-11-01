# Stage 6 — Mobile dashboard for the local paper desk

This working copy uses the `codex/stage2-provider-integration` branch. The original `Day-Trader-INDIA` checkout is unchanged. Stage 6 was checked before the later Stage 7 branch commit.

The responsive React and TypeScript frontend has five screens: Dashboard, Market Scanner, AI Debate, Trading, and Settings. It reads the scanner, committee, and autonomous paper ledger through a FastAPI service. The service requires a strong local access key for every data and settings request, serves updates every five seconds, and binds to loopback only. Browser notifications are optional and work only while the page is open. The market, AI, and paper workers remain separate foreground processes; closing the dashboard does not stop them.

The Dashboard shows the paper account, realized P&L, open positions, recent results, market session, active verified feed, and process freshness. Scanner shows coverage, candidate metrics, sources, and verified handoffs. AI Debate shows the four agent configurations, decisions, analyst arguments, risks, and expiry. Trading shows positions, fresh marks, virtual order states, closed trades, and daily cash/cost summaries. Settings validates and saves local scanner, AI, paper, and browser notification preferences. The initial paper capital is locked once its ledger exists. Broker tokens are never accepted by the dashboard; provider status is shown without exposing credential values. Worker settings take effect after the corresponding process is restarted.

## Local setup

```text
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-web.txt
cd frontend
npm install
npm run build
cd ..
.venv\Scripts\python -m india_trader web serve
```

Open `http://127.0.0.1:8765` on this computer. The access key is generated at `trader/state/ui-access-token.txt`; the server prints only the file path. The file and saved settings are in ignored local directories. Do not share this key or publish this local server. [Stage 7](STAGE7_SERVER_DEPLOYMENT.md) adds the private deployment path. The UI has no live order route.

The frontend is a viewer and configuration surface. To generate new scanner/committee/paper events, separately run the relevant Stage 3–5 processes with a supported fresh read-only data provider during a normal market session. Missing or stale feeds produce empty or unavailable states; the UI does not invent prices, decisions, or P&L.

## Validation

The local React build passed (`npm run build`). All 290 Python tests passed at the Stage 6 checkpoint with the optional web packages installed, including six dashboard/API checks. Python syntax compilation and `git diff --check` passed. A local server returned the built page, rejected unauthenticated API access with HTTP 401, and returned a paper-only overview with no health errors when authenticated. Desktop and 390-pixel phone layouts, navigation, and the Settings screen were inspected in a browser. No remote phone connection or live market feed was validated at that checkpoint; the later Linux acceptance run remains Stage 8 work.
