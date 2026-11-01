"""Checked offline backups of Stage 3–7 paper data.

Do not run restore while any deployment service is writing the shared volume.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile

from .deployment import DeploymentError, _database_path, connect


FILES = (
    "trader/universe.txt",
    "trader/state/market-engine.json",
    "trader/state/ai-committee.json",
    "trader/state/paper-lifecycle.json",
    "trader/settings/market_engine.json",
    "trader/settings/ai_committee.json",
    "trader/settings/paper_lifecycle.json",
    "trader/settings/notifications.json",
    "trader/state/operations.sqlite3",
)
LIMIT = 20_000_000


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _verify_data(root, files):
    root = Path(root)
    scratch = root / "trader"
    scratch.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        if name not in FILES or len(data) > LIMIT:
            raise DeploymentError("backup has an unexpected or oversized file")
        if name.endswith(".json"):
            try:
                doc = json.loads(data)
            except (ValueError, UnicodeError) as exc:
                raise DeploymentError("backup contains invalid JSON") from exc
            if not isinstance(doc, dict):
                raise DeploymentError("backup JSON must be an object")
            if name.startswith("trader/state/") and doc.get("orders_possible") is not False:
                raise DeploymentError("backup state is not paper-only")
            if name == "trader/settings/paper_lifecycle.json" and doc.get("mode") != "PAPER_ONLY":
                raise DeploymentError("backup paper settings are not paper-only")
        elif name == "trader/state/operations.sqlite3":
            with tempfile.TemporaryDirectory(prefix="backup-verify-", dir=scratch) as folder:
                path = Path(folder) / "operations.sqlite3"
                path.write_bytes(data)
                try:
                    with closing(sqlite3.connect(path)) as db:
                        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                            raise DeploymentError("backup database failed integrity check")
                        for role, digest, content in db.execute(
                                "SELECT role, sha256, content FROM state_snapshots"):
                            if role not in ("scanner", "committee", "paper") or \
                                    _digest(content) != digest or \
                                    json.loads(content).get("orders_possible") is not False:
                                raise DeploymentError("backup has an invalid state snapshot")
                except sqlite3.DatabaseError as exc:
                    raise DeploymentError("backup database is invalid") from exc
    if "trader/settings/market_engine.json" in files:
        from .market_engine import _settings
        settings = json.loads(files["trader/settings/market_engine.json"])
        if set(settings) != set(_settings()):
            raise DeploymentError("market settings schema is invalid")
        _settings(**settings)
    if "trader/settings/notifications.json" in files:
        prefs = json.loads(files["trader/settings/notifications.json"])
        if set(prefs) != {"browser_events", "show_feed_warnings"} or \
                any(type(value) is not bool for value in prefs.values()):
            raise DeploymentError("notification settings schema is invalid")
    for name, loader in (("trader/settings/ai_committee.json", "ai"),
                         ("trader/settings/paper_lifecycle.json", "paper")):
        if name in files:
            with tempfile.TemporaryDirectory(prefix="config-verify-", dir=scratch) as folder:
                config = Path(folder) / "config.json"
                config.write_bytes(files[name])
                if loader == "ai":
                    from .ai_committee import load_config
                else:
                    from .paper_lifecycle import load_config
                load_config(config)
    if "trader/state/market-engine.json" in files:
        with tempfile.TemporaryDirectory(prefix="scanner-verify-", dir=scratch) as folder:
            state = Path(folder) / "market-engine.json"
            state.write_bytes(files["trader/state/market-engine.json"])
            from .market_engine import _read_state
            _read_state(state)
    if "trader/state/ai-committee.json" in files:
        with tempfile.TemporaryDirectory(prefix="committee-verify-", dir=scratch) as folder:
            state = Path(folder) / "ai-committee.json"
            state.write_bytes(files["trader/state/ai-committee.json"])
            from .ai_committee import _state
            _state(state, json.loads(files["trader/state/ai-committee.json"]).get("day"))
    if "trader/state/paper-lifecycle.json" in files:
        with tempfile.TemporaryDirectory(prefix="paper-verify-", dir=scratch) as folder:
            folder = Path(folder)
            state = folder / "paper-lifecycle.json"
            state.write_bytes(files["trader/state/paper-lifecycle.json"])
            config = folder / "paper_lifecycle.json"
            config.write_bytes(files.get("trader/settings/paper_lifecycle.json",
                                         (root / "examples/paper_lifecycle_config.json").read_bytes()))
            from .paper_lifecycle import _state, load_config
            _state(state, load_config(config))  # Reconcile fills, cash and positions.


def backup_archive(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    if output.exists():
        raise DeploymentError("backup already exists; choose a new filename")
    output.parent.mkdir(parents=True, exist_ok=True)
    files = {}
    for name in FILES:
        if name.endswith("operations.sqlite3"):
            continue
        path = root / name
        if not path.is_file():
            continue
        if path.is_symlink() or path.stat().st_size > LIMIT:
            raise DeploymentError("backup source is a link or exceeds size limit")
        files[name] = path.read_bytes()
    with tempfile.TemporaryDirectory(prefix="db-backup-", dir=output.parent) as folder:
        snapshot = Path(folder) / "operations.sqlite3"
        source = connect(root)
        try:
            destination = sqlite3.connect(snapshot)
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
        files["trader/state/operations.sqlite3"] = snapshot.read_bytes()
    _verify_data(root, files)
    manifest = {"version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                "paper_only": True,
                "files": {name: _digest(data) for name, data in sorted(files.items())}}
    with output.open("xb") as stream:
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            payloads = {"manifest.json": json.dumps(manifest, sort_keys=True).encode("utf-8"),
                        **files}
            for name, data in payloads.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o600
                archive.addfile(info, io.BytesIO(data))
    if os.name != "nt":
        output.chmod(0o600)
    return {"archive": str(output), "files": len(files),
            "sha256": _digest(output.read_bytes())}


def _read_archive(path):
    content = {}
    with tarfile.open(path, mode="r:gz") as archive:
        members = archive.getmembers()
        if len(members) > len(FILES) + 1:
            raise DeploymentError("backup has too many entries")
        for member in members:
            if not member.isfile() or member.name not in (*FILES, "manifest.json") or \
                    member.name in content or member.size > LIMIT:
                raise DeploymentError("backup has unsafe entries")
            stream = archive.extractfile(member)
            if stream is None:
                raise DeploymentError("backup entry is unreadable")
            data = stream.read(LIMIT + 1)
            if len(data) != member.size:
                raise DeploymentError("backup entry length mismatch")
            content[member.name] = data
    try:
        manifest = json.loads(content.pop("manifest.json"))
    except (KeyError, ValueError, UnicodeError) as exc:
        raise DeploymentError("backup manifest missing or invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != 1 or \
            manifest.get("paper_only") is not True or not isinstance(manifest.get("files"), dict) or \
            set(manifest["files"]) != set(content):
        raise DeploymentError("backup manifest does not match contents")
    for name, data in content.items():
        if manifest["files"][name] != _digest(data):
            raise DeploymentError("backup checksum mismatch")
    return content


def restore_archive(root, archive, force=False):
    root = Path(root).resolve()
    files = _read_archive(archive)
    _verify_data(root, files)
    existing = [root / name for name in files if (root / name).exists()]
    journals = [Path(str(_database_path(root)) + suffix) for suffix in ("-wal", "-shm")]
    journals = [path for path in journals if path.exists()]
    if (existing or journals) and not force:
        raise DeploymentError("restore would overwrite data; use --force after stopping all services")
    safety_copy = None
    if existing:
        safety_copy = root / "trader" / ("pre-restore-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        for path in existing:
            if path.is_symlink() or not path.is_file():
                raise DeploymentError("restore target is not a regular file")
            destination = safety_copy / path.relative_to(root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
    for stale in journals:
        if stale.is_symlink() or not stale.is_file():
            raise DeploymentError("database journal is not a regular file")
        if safety_copy is None:
            safety_copy = root / "trader" / ("pre-restore-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        destination = safety_copy / stale.relative_to(root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(stale, destination)
        stale.unlink()
    for name, data in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".restore-tmp")
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    return {"restored": len(files), "safety_copy": str(safety_copy) if safety_copy else None,
            "broker_credentials_restored": False, "codex_login_restored": False}
