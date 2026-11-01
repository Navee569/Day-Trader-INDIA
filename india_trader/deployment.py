"""Linux deployment loops and a persistent, paper-only operations database.

The validated JSON ledgers remain authoritative. SQLite records worker health and
the last checked copy of each ledger for explicit, offline recovery only.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import time
from threading import Event
from urllib.request import urlopen

from .web_api import create_app, ensure_ui_token


ROLES = {
    "scanner": ("market-engine", "market-engine.json", 300),
    "committee": ("ai-committee", "ai-committee.json", 240),
    "paper": ("paper-lifecycle", "paper-lifecycle.json", 120),
}
SECRET_FILES = {
    "UPSTOX_ANALYTICS_TOKEN": "upstox_analytics_token",
    "FYERS_APP_ID": "fyers_app_id",
    "FYERS_ACCESS_TOKEN": "fyers_access_token",
}
STALE_SECONDS = 420


class DeploymentError(ValueError):
    pass


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def _database_path(root):
    return Path(root) / "trader/state/operations.sqlite3"


def connect(root):
    path = _database_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS worker_status (
            role TEXT PRIMARY KEY, at TEXT NOT NULL, last_success_at TEXT,
            status TEXT NOT NULL, exit_code INTEGER NOT NULL,
            consecutive_failures INTEGER NOT NULL, state_sha256 TEXT
        );
        CREATE TABLE IF NOT EXISTS worker_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL,
            at TEXT NOT NULL, status TEXT NOT NULL, exit_code INTEGER NOT NULL,
            duration_ms INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS worker_events_at ON worker_events(at);
        CREATE TABLE IF NOT EXISTS state_snapshots (
            role TEXT PRIMARY KEY, at TEXT NOT NULL, sha256 TEXT NOT NULL,
            content BLOB NOT NULL
        );
    """)
    return connection


def database_check(root):
    with closing(connect(root)) as db:
        result = db.execute("PRAGMA quick_check").fetchone()
        if result != ("ok",):
            raise DeploymentError("operations database integrity check failed")
    return True


def _secret_environment(base=None, directory="/run/secrets"):
    result = dict(os.environ if base is None else base)
    secret_dir = Path(directory)
    for variable, filename in SECRET_FILES.items():
        path = secret_dir / filename
        if path.is_file():
            if path.stat().st_size > 4096:
                raise DeploymentError("provider secret exceeds size limit")
            value = path.read_text(encoding="utf-8").strip()
            if not value or "\n" in value or "\r" in value:
                raise DeploymentError("provider secret is empty or malformed")
            result[variable] = value
    return result


def _checked_state(root, role):
    filename = ROLES[role][1]
    path = Path(root) / "trader/state" / filename
    if not path.is_file():
        return None
    if path.stat().st_size > 5_000_000:
        raise DeploymentError("worker state exceeds size limit")
    content = path.read_bytes()
    doc = json.loads(content)
    if not isinstance(doc, dict) or doc.get("orders_possible") is not False:
        raise DeploymentError("worker state is not paper-only")
    if role == "paper":
        from .paper_lifecycle import _state, load_config
        root = Path(root)
        setting = root / "trader/settings/paper_lifecycle.json"
        cfg = load_config(setting if setting.is_file() else
                          root / "examples/paper_lifecycle_config.json")
        _state(path, cfg)  # Full cash, fills and holdings reconciliation.
    return content


def _record(root, role, status, exit_code, duration_ms, content=None):
    at = _stamp()
    digest = hashlib.sha256(content).hexdigest() if content is not None else None
    with closing(connect(root)) as db, db:
        previous = db.execute("SELECT last_success_at, consecutive_failures, state_sha256 "
                              "FROM worker_status WHERE role=?", (role,)).fetchone()
        healthy = exit_code == 0 or role == "committee" and exit_code == 1 and \
            status == "AI_UNAVAILABLE_NO_SELECTION"
        last_success = at if healthy else (previous[0] if previous else None)
        failures = 0 if healthy else (previous[1] if previous else 0) + 1
        state_digest = digest or (previous[2] if previous else None)
        db.execute("INSERT INTO worker_status VALUES (?,?,?,?,?,?,?) "
                   "ON CONFLICT(role) DO UPDATE SET at=excluded.at, "
                   "last_success_at=excluded.last_success_at, status=excluded.status, "
                   "exit_code=excluded.exit_code, "
                   "consecutive_failures=excluded.consecutive_failures, "
                   "state_sha256=excluded.state_sha256",
                   (role, at, last_success, status, exit_code, failures, state_digest))
        db.execute("INSERT INTO worker_events(role,at,status,exit_code,duration_ms) "
                   "VALUES (?,?,?,?,?)", (role, at, status, exit_code, duration_ms))
        if content is not None and healthy:
            db.execute("INSERT INTO state_snapshots VALUES (?,?,?,?) "
                       "ON CONFLICT(role) DO UPDATE SET at=excluded.at, "
                       "sha256=excluded.sha256, content=excluded.content",
                       (role, at, digest, content))
        # The ISO timestamps are UTC; a bounded event history limits disk use.
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        db.execute("DELETE FROM worker_events WHERE at < ?", (cutoff,))
    return {"role": role, "at": at, "status": status, "exit_code": exit_code,
            "consecutive_failures": failures, "state_sha256": state_digest}


def _last_json(output):
    if not output or len(output) > 2_000_000:
        return None
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return None


def run_cycle(root, role, runner=subprocess.run, secret_dir="/run/secrets"):
    if role not in ROLES:
        raise DeploymentError("unknown worker role")
    root = Path(root)
    command_name, _, timeout = ROLES[role]
    started = time.monotonic()
    environment = _secret_environment(directory=secret_dir) if role in ("scanner", "paper") else dict(os.environ)
    status, code = "PROCESS_FAILED", 2
    content = None
    try:
        if role == "scanner" and not (root / "trader/universe.txt").is_file():
            bootstrap = runner([sys.executable, "-m", "india_trader", "scanner", "universe"],
                               cwd=root, env=environment, capture_output=True, text=True,
                               timeout=180, check=False)
            if bootstrap.returncode != 0:
                status = "UNIVERSE_UNAVAILABLE"
                raise DeploymentError(status)
        completed = runner([sys.executable, "-m", "india_trader", command_name, "once"],
                           cwd=root, env=environment, capture_output=True, text=True,
                           timeout=timeout, check=False)
        code = completed.returncode
        result = _last_json(completed.stdout)
        raw_status = result.get("status") if result else None
        status = raw_status if isinstance(raw_status, str) and re.fullmatch(
            r"[A-Z][A-Z0-9_]{0,63}", raw_status) else "PROCESS_FAILED"
        if role == "paper" and code == 0 and result and \
                result.get("mode") == "PAPER_ONLY" and result.get("orders_possible") is False:
            status = ("EXIT_DATA_UNAVAILABLE" if result.get("exit_data_unavailable")
                      else "CYCLE_OK")
        if code == 0 and status == "PROCESS_FAILED":
            status, code = "INVALID_RESPONSE", 2
        if code == 0 or role == "committee" and code == 1 and \
                status == "AI_UNAVAILABLE_NO_SELECTION":
            content = _checked_state(root, role)
            if content is None:
                status, code = "STATE_MISSING", 2
    except subprocess.TimeoutExpired:
        status, code = "TIMEOUT", 2
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        if status != "UNIVERSE_UNAVAILABLE":
            status, code = "STATE_OR_PROCESS_ERROR", 2
    elapsed = round((time.monotonic() - started) * 1000)
    recorded = _record(root, role, status, code, elapsed, content)
    print(json.dumps({key: value for key, value in recorded.items()
                      if key != "state_sha256"}, separators=(",", ":")), flush=True)
    return recorded


def status(root):
    with closing(connect(root)) as db:
        rows = db.execute("SELECT role,at,last_success_at,status,exit_code,"
                          "consecutive_failures,state_sha256 FROM worker_status").fetchall()
    return {role: {"at": at, "last_success_at": success, "status": state,
                   "exit_code": code, "consecutive_failures": failures,
                   "state_sha256": digest}
            for role, at, success, state, code, failures, digest in rows}


def worker_health(root, role, now=None):
    if role not in ROLES:
        raise DeploymentError("unknown worker role")
    row = status(root).get(role)
    if not row:
        raise DeploymentError("worker has not completed a cycle")
    clock = datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)
    age = (clock - datetime.fromisoformat(row["at"])).total_seconds()
    if age < -30 or age > STALE_SECONDS or row["consecutive_failures"] > 0 or \
            role == "paper" and row["status"] == "EXIT_DATA_UNAVAILABLE":
        raise DeploymentError("worker is stale or failing")
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m india_trader deploy",
                                     description="Private Linux paper deployment")
    commands = parser.add_subparsers(dest="command", required=True)
    worker = commands.add_parser("worker")
    worker.add_argument("role", choices=tuple(ROLES))
    worker.add_argument("--max-cycles", type=int)
    database = commands.add_parser("database")
    database.add_argument("--once", action="store_true")
    commands.add_parser("web")
    health = commands.add_parser("health")
    health.add_argument("role", choices=("database", "backend", *ROLES))
    commands.add_parser("status")
    backup = commands.add_parser("backup")
    backup.add_argument("--out", required=True)
    backup.add_argument("--workers-stopped", action="store_true", required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--from", dest="archive", required=True)
    restore.add_argument("--workers-stopped", action="store_true", required=True)
    restore.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    root = Path.cwd()
    stop_requested = Event()
    if args.command in ("worker", "database"):
        signal.signal(signal.SIGTERM, lambda _signal, _frame: stop_requested.set())
    try:
        if args.command == "database":
            while not stop_requested.is_set():
                database_check(root)
                if args.once:
                    return 0
                stop_requested.wait(60)
            return 0
        if args.command == "worker":
            if args.max_cycles is not None and args.max_cycles < 1:
                raise DeploymentError("max-cycles must be positive")
            count = 0
            while not stop_requested.is_set():
                result = run_cycle(root, args.role)
                count += 1
                if args.max_cycles is not None and count >= args.max_cycles:
                    return 0 if result["consecutive_failures"] == 0 else 2
                interval = 30
                if args.role == "committee":
                    from .ai_committee import load_config
                    interval = load_config()["poll_seconds"]
                elif args.role == "paper":
                    from .paper_lifecycle import load_config
                    interval = load_config()["poll_seconds"]
                stop_requested.wait(interval)
            return 0
        if args.command == "web":
            if os.environ.get("DAY_TRADER_CONTAINER") != "1":
                raise DeploymentError("container-only backend listener")
            import uvicorn
            token, source = ensure_ui_token(root)
            print(json.dumps({"service": "backend", "token_location": source,
                              "paper_only": True}), flush=True)
            uvicorn.run(create_app(root, token, root / "nonexistent-frontend-dist"),
                        host="0.0.0.0", port=8765, access_log=False,
                        log_level="warning")
            return 0
        if args.command == "health":
            if args.role == "database":
                database_check(root)
            elif args.role == "backend":
                with urlopen("http://127.0.0.1:8765/healthz", timeout=5) as response:
                    if response.status != 200:
                        raise DeploymentError("backend health endpoint failed")
            else:
                worker_health(root, args.role)
            print("healthy")
            return 0
        if args.command == "status":
            print(json.dumps(status(root), indent=2))
            return 0
        from .deployment_backup import backup_archive, restore_archive
        if args.command == "backup":
            print(backup_archive(root, args.out))
        elif args.command == "restore":
            print(restore_archive(root, args.archive, force=args.force))
        return 0
    except KeyboardInterrupt:
        return 0
    except (DeploymentError, OSError, ValueError, sqlite3.DatabaseError) as exc:
        print("deploy: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
