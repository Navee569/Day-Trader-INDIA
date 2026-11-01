# Stage 7 — Private Linux paper deployment

This stage packages the paper-only scanner, AI committee, independent paper monitor, authenticated API, mobile frontend, and SQLite operations database as persistent Docker Compose services. The original `Day-Trader-INDIA` checkout is unchanged. The branch is `codex/stage2-provider-integration`.

The existing atomic JSON files remain the **authoritative paper ledger**. SQLite stores worker health, a bounded 30-day run journal, and the last validated copy of each state file. It is not a live broker database. A corrupt ledger stops that worker's health check; it is never silently replaced with an older snapshot. The paper monitor runs in its own container, so an AI outage does not stop its protective checks. A market-data outage can still leave an open paper position unmarked or unclosed; do not treat a process restart as a simulated fill.

## What you need

Use a Linux host that stays on, with Docker Engine and Docker Compose installed. This Windows computer currently has neither Docker nor WSL, so the containers were **not run here**. A private [Tailscale](https://tailscale.com/docs/solutions/host-private-server-docker) account is needed for phone access. You may start the stack without a broker data account, but it will not make fresh broker-verified paper entries. AI4 requires a Codex CLI ChatGPT login; [OpenAI's headless login guidance](https://learn.chatgpt.com/docs/auth) recommends device-code login when available. Do not copy or commit `auth.json`, broker tokens, or the dashboard key.

Docker, Tailscale, model access, a data-provider account, and a continuously running host can have limits or costs depending on the chosen plans. This setup does not establish a profitable strategy.

## Start on Linux

From this repository directory:

```bash
cp .env.example .env
mkdir -p backups
sudo chown 10001:10001 backups
chmod 700 backups
docker compose up -d --build
docker compose ps
```

The frontend binds **only** to `127.0.0.1:8787` by default; the API, workers, database file, and Codex login have no published ports. On the Linux host, open `http://127.0.0.1:8787`. Get the randomly generated dashboard key with `docker compose exec backend cat /app/trader/state/ui-access-token.txt`, then paste it into the local dashboard. Keep that key private. The frontend has no live-order action.

For AI4, run `docker compose exec committee codex login --device-auth` and complete the sign-in yourself using the URL and code shown by Codex. If device-code login is unavailable, follow the [official OpenAI alternatives](https://learn.chatgpt.com/docs/auth). The login cache lives in the private `codex_home` Docker volume, separate from the paper data and backups. Until AI is authenticated, the committee records no selection and the paper entry gate remains closed. The other workers continue.

The scanner obtains the official NSE EQ symbol list when it starts and no list exists in its persistent volume. If that fetch fails, it records `UNIVERSE_UNAVAILABLE` and retries without inventing broad-market coverage. You can inspect the exact worker statuses with `docker compose exec backend python -m india_trader deploy status`.

### Optional read-only market data

The default stack accepts no credentials. Create only the secret files for the provider you actually have, outside Git:

```bash
mkdir -p secrets
chmod 700 secrets
# Use your local editor to create secrets/upstox_analytics_token, or
# secrets/fyers_app_id and secrets/fyers_access_token.
sudo chgrp 10001 secrets/*
chmod 640 secrets/*
```

For Upstox, start with `docker compose -f compose.yaml -f compose.upstox.yaml up -d --build`. For FYERS, use `compose.fyers.yaml`. You may include both overrides when both accounts are available. Compose mounts each secret as a file **only** into the scanner and paper containers; their runner reads it into the child process environment and never includes the value in health records or logs. The host file's group and `0640` mode let the container's numeric user 10001 read the bind-mounted secret while keeping it private to that group. [Docker's Compose reference](https://docs.docker.com/reference/compose-file/services/) says UID/GID remapping is ignored for file-backed secrets, so verify ownership if you use rootless Docker or a different user mapping. Token expiry or a provider outage must appear as unavailable data, not as a synthetic market price. Do not paste tokens into chat, `.env`, Compose YAML, or Git.

### Phone access

Install and sign in to Tailscale on the **Linux host and your phone**. On the host, run `tailscale serve --bg 8787`, then `tailscale serve status`. Open the printed `https://...ts.net/` address on the phone and enter the dashboard key. [Tailscale Serve](https://tailscale.com/docs/solutions/host-private-server-docker) provides tailnet-only HTTPS access to the loopback port. Restrict the tailnet's access rules to your own devices. Do **not** enable Tailscale Funnel, publish port 8787 on a public interface, or open router ports. Browser notifications require the page to stay open; the paper workers do not.

## Logs, health, and restarts

The services use `restart: unless-stopped`, individual health checks, and Docker JSON logs rotated at 10 MB × 5 files. Check `docker compose ps`, `docker compose logs --tail=100 scanner committee paper backend`, and `docker compose exec backend python -m india_trader deploy status`. The `database` service checks SQLite integrity; the worker checks require a recent successful cycle. `AI_UNAVAILABLE_NO_SELECTION` is a healthy but degraded committee state. Broker credentials, prompts, raw model output, and dashboard keys are not written by the Stage 7 runner to its logs or SQLite journal.

Workers use one bounded `once` cycle at a time and finish the active cycle after a normal stop signal. Their state survives container replacement in the `trader_data` volume. Changes saved in the dashboard are read on the next cycle; restart the affected worker after changing settings if you want an immediate clean start. The Compose health check is a liveness and state-integrity signal, not proof of exchange coverage, correct simulated fills, or investment performance.

## Backup and recovery

Back up after stopping the writers so the JSON files and database represent one quiet point in time:

```bash
docker compose stop scanner committee paper backend database
docker compose --profile tools run --rm --no-deps maintenance backup \
  --out /backup/paper-YYYYMMDD.tar.gz --workers-stopped
docker compose up -d
```

Replace `YYYYMMDD` with the date and keep the resulting `backups/` archive off the host as well. It contains the paper ledger and may reveal your simulated activity. The archive is checksummed and includes a consistent SQLite backup. It intentionally excludes broker secrets, the Codex login cache, and the browser access key.

To recover from a **trusted** backup, stop all services and restore while offline:

```bash
docker compose down
docker compose --profile tools run --rm --no-deps maintenance restore \
  --from /backup/paper-YYYYMMDD.tar.gz --workers-stopped --force
docker compose up -d
```

Restore verifies the archive member list, hashes, JSON paper-only markers, SQLite integrity, and paper cash/fill reconciliation before writing. Existing target files and database journals are copied to `trader/pre-restore-...` in the persistent volume. That safety copy should be moved off-host too. Restore can roll the paper ledger back to an older moment; review it before resuming, especially if a position was open. Never use a restored file to claim a real exchange fill. If the UI key is missing, the backend generates a new one; Codex and provider access may need to be set up again.

## Validation boundary

On this Windows computer, **296 Python tests passed**, including six focused deployment checks for worker snapshots, degraded AI behavior, stale/failing health, local secret loading, backup/restore, archive tampering, and Compose port/secret boundaries. Python syntax compilation, `git diff --check`, and the frontend `npm run build` passed. The Compose YAML parsed with PyYAML. A real closed-market paper worker cycle returned `CYCLE_OK`, and its health check passed. Docker image builds, Compose startup, Tailscale access, real provider feeds, and crash recovery on a Linux host were **not run** because Docker and WSL are absent here; those require the Stage 8 acceptance run on actual infrastructure.
