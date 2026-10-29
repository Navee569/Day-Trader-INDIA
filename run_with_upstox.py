"""Prompt for a read-only Upstox Analytics Token without saving or echoing it."""
import getpass
import os
import subprocess
import sys


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python run_with_upstox.py <india_trader command and options>")
        return 2
    token = getpass.getpass("Upstox read-only Analytics Token (input hidden): ").strip()
    if not token:
        print("No token entered. Nothing was run.")
        return 2
    env = os.environ.copy()
    env["UPSTOX_ANALYTICS_TOKEN"] = token
    token = ""
    return subprocess.run([sys.executable, "-m", "india_trader", *sys.argv[1:]], env=env,
                          check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
