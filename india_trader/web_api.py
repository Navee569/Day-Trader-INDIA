"""Authenticated FastAPI viewer and settings API for the paper dashboard."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import sys

from .web_dashboard import snapshot
from .web_settings import FILES, SettingsError, save_settings


def ensure_ui_token(root):
    supplied = os.environ.get("DAY_TRADER_UI_TOKEN", "").strip()
    if supplied:
        if len(supplied) < 24:
            raise ValueError("DAY_TRADER_UI_TOKEN must contain at least 24 characters")
        return supplied, "process environment"
    path = Path(root) / "trader/state/ui-access-token.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(secrets.token_urlsafe(36) + "\n")
            if os.name != "nt":
                path.chmod(0o600)
        except FileExistsError:
            pass
    token = path.read_text(encoding="utf-8").strip()
    if len(token) < 24:
        raise ValueError("UI access token file is invalid")
    return token, str(path)


def create_app(root=None, token=None, frontend_dist=None):
    """Build the UI server. A token is mandatory even on localhost."""
    try:
        from fastapi import Depends, FastAPI, Header, HTTPException, Request
        from fastapi.responses import HTMLResponse, StreamingResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:
        raise RuntimeError("install requirements-web.txt before starting the dashboard") from exc
    root = Path(root or Path.cwd()).resolve()
    expected = token or os.environ.get("DAY_TRADER_UI_TOKEN")
    if not expected or len(expected) < 24:
        raise ValueError("a strong local dashboard access token is required")
    dist = Path(frontend_dist or root / "frontend/dist")
    app = FastAPI(title="Day Trader India paper dashboard", docs_url=None,
                  redoc_url=None, openapi_url=None)

    def authorize(authorization: str | None = Header(default=None)):
        if not authorization or not authorization.startswith("Bearer ") or \
                not secrets.compare_digest(authorization[7:], expected):
            raise HTTPException(status_code=401, detail="Dashboard access required")
        return True

    @app.middleware("http")
    async def safe_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/overview", dependencies=[Depends(authorize)])
    def overview():
        return snapshot(root)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok", "paper_only": True}

    @app.get("/api/stream", dependencies=[Depends(authorize)])
    async def stream(request: Request):
        async def events():
            while not await request.is_disconnected():
                try:
                    payload = json.dumps(snapshot(root), allow_nan=False, separators=(",", ":"))
                    yield f"event: snapshot\ndata: {payload}\n\n"
                except (OSError, ValueError, TypeError) as exc:
                    yield "event: error\ndata: " + json.dumps(
                        {"message": "dashboard state unavailable", "type": type(exc).__name__}) + "\n\n"
                await asyncio.sleep(5)
        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store",
                                          "X-Accel-Buffering": "no"})

    @app.put("/api/settings/{section}", dependencies=[Depends(authorize)])
    async def update_settings(section: str, request: Request):
        if section not in FILES:
            raise HTTPException(status_code=404, detail="Unknown settings section")
        raw = await request.body()
        if len(raw) > 64_000:
            raise HTTPException(status_code=413, detail="Settings payload too large")
        try:
            payload = json.loads(raw)
            return save_settings(root, section, payload)
        except (SettingsError, ValueError, TypeError, KeyError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if dist.is_dir():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")
    else:
        @app.get("/", response_class=HTMLResponse)
        def not_built():
            return ("<html><body><h1>Dashboard build needed</h1>"
                    "<p>Run npm install and npm run build inside frontend, then restart this server.</p>"
                    "</body></html>")
    return app


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m india_trader web",
                                     description="Local authenticated paper dashboard")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)
    try:
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("Stage 6 dashboard binds to loopback only; remote hosting is Stage 7")
        if not 1024 <= args.port <= 65535:
            raise ValueError("port must be between 1024 and 65535")
        root = Path.cwd()
        token, source = ensure_ui_token(root)
        app = create_app(root, token)
        import uvicorn
        print("Paper dashboard access token: " + source, flush=True)
        print(f"Open http://127.0.0.1:{args.port} on this computer", flush=True)
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning",
                    access_log=False)
        return 0
    except (RuntimeError, ValueError, OSError, ImportError) as exc:
        print("web: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
