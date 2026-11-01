"""Offline Stage 7 persistence, recovery and process-boundary checks."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

from india_trader.ai_committee import _blank_state
from india_trader.deployment import (DeploymentError, _secret_environment,
                                     database_check, run_cycle, status, worker_health)
from india_trader.deployment_backup import backup_archive, restore_archive
from india_trader.market_engine import _empty
from india_trader.paper_lifecycle import _blank, load_config


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="stage7-", dir=Path.cwd())
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        (self.root / "examples").mkdir()
        source = Path(__file__).resolve().parent.parent / "examples"
        for name in ("ai_committee_config.json", "paper_lifecycle_config.json"):
            shutil.copy2(source / name, self.root / "examples" / name)

    def _state(self, name, data):
        path = self.root / "trader/state" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, allow_nan=False), encoding="utf-8")
        return path

    def test_worker_records_checked_paper_snapshot_and_health(self):
        paper = _blank(load_config(self.root / "examples/paper_lifecycle_config.json"))
        state = self._state("paper-lifecycle.json", paper)
        fake = lambda *a, **k: subprocess.CompletedProcess(
            a[0], 0, '{"mode":"PAPER_ONLY","orders_possible":false,"fills":[]}\n', '')
        result = run_cycle(self.root, "paper", runner=fake, secret_dir=self.root / "no-secrets")
        self.assertEqual(result["status"], "CYCLE_OK")
        self.assertEqual(result["consecutive_failures"], 0)
        self.assertEqual(worker_health(self.root, "paper")["status"], "CYCLE_OK")
        self.assertTrue(result["state_sha256"])
        self.assertTrue(database_check(self.root))
        unavailable = lambda *a, **k: subprocess.CompletedProcess(
            a[0], 0, '{"mode":"PAPER_ONLY","orders_possible":false,"exit_data_unavailable":["RELIANCE.NS"]}\n', '')
        degraded = run_cycle(self.root, "paper", runner=unavailable,
                             secret_dir=self.root / "no-secrets")
        self.assertEqual(degraded["status"], "EXIT_DATA_UNAVAILABLE")
        with self.assertRaisesRegex(DeploymentError, "stale or failing"):
            worker_health(self.root, "paper")
        state.write_text('{"orders_possible":true}', encoding="utf-8")
        bad = run_cycle(self.root, "paper", runner=fake, secret_dir=self.root / "no-secrets")
        self.assertEqual(bad["consecutive_failures"], 1)
        with self.assertRaisesRegex(DeploymentError, "stale or failing"):
            worker_health(self.root, "paper")

    def test_committee_unavailability_keeps_health_without_selection(self):
        self._state("ai-committee.json", _blank_state("2026-11-01"))
        fake = lambda *a, **k: subprocess.CompletedProcess(
            a[0], 1, '{"status":"AI_UNAVAILABLE_NO_SELECTION"}\n', '')
        result = run_cycle(self.root, "committee", runner=fake)
        self.assertEqual(result["consecutive_failures"], 0)
        self.assertEqual(worker_health(self.root, "committee")["exit_code"], 1)
        old = datetime.now(timezone.utc) + timedelta(hours=1)
        with self.assertRaisesRegex(DeploymentError, "stale or failing"):
            worker_health(self.root, "committee", old)

    def test_secret_files_are_loaded_locally_without_entering_status(self):
        folder = self.root / "secrets"
        folder.mkdir()
        (folder / "upstox_analytics_token").write_text("read-only-example\n", encoding="utf-8")
        values = _secret_environment(base={"PATH": "test"}, directory=folder)
        self.assertEqual(values["UPSTOX_ANALYTICS_TOKEN"], "read-only-example")
        self.assertNotIn("read-only-example", json.dumps(status(self.root)))

    def test_backup_restore_preserves_paper_and_database_and_rejects_tampering(self):
        self._state("market-engine.json", _empty("2026-11-01"))
        self._state("ai-committee.json", _blank_state("2026-11-01"))
        state = self._state("paper-lifecycle.json", _blank(
            load_config(self.root / "examples/paper_lifecycle_config.json")))
        database_check(self.root)
        archive = self.root / "backups" / "paper.tar.gz"
        created = backup_archive(self.root, archive)
        self.assertGreater(created["files"], 1)
        with self.assertRaisesRegex(DeploymentError, "already exists"):
            backup_archive(self.root, archive)
        state.write_text('{"orders_possible":true}', encoding="utf-8")
        with self.assertRaisesRegex(DeploymentError, "overwrite"):
            restore_archive(self.root, archive)
        result = restore_archive(self.root, archive, force=True)
        self.assertGreater(result["restored"], 1)
        self.assertTrue(result["safety_copy"])
        self.assertIs(json.loads(state.read_text())["orders_possible"], False)
        self.assertTrue(database_check(self.root))

        unsafe = self.root / "backups" / "unsafe.tar.gz"
        with tarfile.open(unsafe, "w:gz") as tar:
            data = b"wrong"
            member = tarfile.TarInfo("../escape")
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
        with self.assertRaisesRegex(DeploymentError, "unsafe entries"):
            restore_archive(self.root, unsafe, force=True)

        changed = self.root / "backups" / "changed.tar.gz"
        with tarfile.open(archive, "r:gz") as source, tarfile.open(changed, "w:gz") as target:
            for member in source.getmembers():
                data = source.extractfile(member).read()
                if member.name == "trader/state/paper-lifecycle.json":
                    data += b"\n"
                copy = tarfile.TarInfo(member.name)
                copy.size = len(data)
                target.addfile(copy, io.BytesIO(data))
        with self.assertRaisesRegex(DeploymentError, "checksum mismatch"):
            restore_archive(self.root, changed, force=True)


@unittest.skipUnless(importlib.util.find_spec("yaml"), "PyYAML is an optional Compose check")
class ComposeConfigurationTests(unittest.TestCase):
    def test_only_frontend_has_a_loopback_published_port(self):
        import yaml

        root = Path(__file__).resolve().parent.parent
        compose = yaml.safe_load((root / "compose.yaml").read_text(encoding="utf-8"))
        services = compose["services"]
        self.assertEqual(set(services), {"database", "scanner", "committee", "paper",
                                         "backend", "frontend", "maintenance"})
        self.assertEqual(services["frontend"]["ports"],
                         ["127.0.0.1:${DAY_TRADER_PORT:-8787}:8080"])
        self.assertTrue(all(not service.get("ports") for name, service in services.items()
                            if name != "frontend"))
        self.assertEqual(set(compose["volumes"]), {"trader_data", "codex_home"})
        self.assertEqual(services["committee"]["volumes"],
                         ["trader_data:/app/trader", "codex_home:/data/codex"])
        self.assertTrue(all(services[name]["restart"] == "unless-stopped"
                            for name in services if name != "maintenance"))

    def test_optional_provider_secrets_are_worker_only(self):
        import yaml

        root = Path(__file__).resolve().parent.parent
        for filename in ("compose.upstox.yaml", "compose.fyers.yaml"):
            config = yaml.safe_load((root / filename).read_text(encoding="utf-8"))
            self.assertEqual(set(config["services"]), {"scanner", "paper"})
            self.assertTrue(all(value["file"].startswith("./secrets/")
                                for value in config["secrets"].values()))


if __name__ == "__main__":
    unittest.main()
