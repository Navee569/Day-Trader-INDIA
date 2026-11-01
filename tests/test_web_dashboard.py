"""Offline contract checks for the local paper dashboard."""
from __future__ import annotations

from datetime import datetime
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from india_trader.core import IST
from india_trader.web_dashboard import snapshot
from india_trader.web_settings import SettingsError, load_settings, save_settings


class DashboardFixture(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="stage6-ui-", dir=Path.cwd())
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        (self.root / "examples").mkdir()
        source = Path(__file__).resolve().parent.parent / "examples"
        for name in ("ai_committee_config.json", "paper_lifecycle_config.json"):
            shutil.copyfile(source / name, self.root / "examples" / name)


class DashboardTests(DashboardFixture):
    def test_empty_snapshot_is_paper_only_and_does_not_invent_a_portfolio(self):
        value = snapshot(self.root, datetime(2026, 11, 1, 12, tzinfo=IST))
        self.assertIs(value["paper_only"], True)
        self.assertEqual(value["market"]["status"], "CLOSED")
        self.assertIsNone(value["summary"]["portfolio_value_inr"])
        self.assertFalse(value["summary"]["ledger_started"])
        self.assertEqual(value["trading"]["orders"], [])
        self.assertEqual(value["health"]["errors"], [])
        self.assertNotIn("access_token", json.dumps(value).lower())

    def test_untrusted_non_paper_state_is_rejected(self):
        state = self.root / "trader/state/ai-committee.json"
        state.parent.mkdir(parents=True)
        state.write_text('{"orders_possible":true,"decisions":[{"outcome":"SELECT"}]}', encoding="utf-8")
        value = snapshot(self.root, datetime(2026, 11, 1, 12, tzinfo=IST))
        self.assertEqual(value["committee"]["decisions"], [])
        self.assertTrue(any("AI state" in error for error in value["health"]["errors"]))

    def test_settings_persist_and_reject_credentials(self):
        market = load_settings(self.root, "market")
        market["min_price"] = 25.0
        result = save_settings(self.root, "market", market)
        self.assertTrue(result["restart_workers"])
        self.assertEqual(load_settings(self.root, "market")["min_price"], 25.0)
        market["upstox_access_token"] = "should-not-persist"
        with self.assertRaises(ValueError):
            save_settings(self.root, "market", market)
        self.assertNotIn("should-not-persist", (self.root / "trader/settings/market_engine.json").read_text())

    def test_initial_capital_locks_when_ledger_exists(self):
        ledger = self.root / "trader/state/paper-lifecycle.json"
        ledger.parent.mkdir(parents=True)
        ledger.write_text('{"initial_capital_inr":100000}', encoding="utf-8")
        paper = load_settings(self.root, "paper")
        paper["initial_capital_inr"] = 200000
        with self.assertRaisesRegex(SettingsError, "locked"):
            save_settings(self.root, "paper", paper)


@unittest.skipUnless(importlib.util.find_spec("fastapi") and
                     importlib.util.find_spec("httpx"), "optional web packages not installed")
class ApiTests(DashboardFixture):
    def test_authentication_and_paper_only_overview(self):
        from fastapi.testclient import TestClient
        from india_trader.web_api import create_app

        token = "stage-six-local-test-access-key-12345"
        app = create_app(root=self.root, token=token, frontend_dist=self.root / "missing")
        with TestClient(app) as client:
            self.assertEqual(client.get("/healthz").json(),
                             {"status": "ok", "paper_only": True})
            self.assertEqual(client.get("/api/overview").status_code, 401)
            response = client.get("/api/overview", headers={"Authorization": "Bearer " + token})
            self.assertEqual(response.status_code, 200)
            self.assertIs(response.json()["paper_only"], True)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertNotIn(token, response.text)

    def test_settings_endpoint_rejects_bad_sections_and_payloads(self):
        from fastapi.testclient import TestClient
        from india_trader.web_api import create_app

        token = "stage-six-local-test-access-key-12345"
        app = create_app(root=self.root, token=token, frontend_dist=self.root / "missing")
        headers = {"Authorization": "Bearer " + token}
        with TestClient(app) as client:
            self.assertEqual(client.put("/api/settings/broker", headers=headers, json={}).status_code, 404)
            self.assertEqual(client.put("/api/settings/notifications", headers=headers,
                                        json={"browser_events": "yes", "show_feed_warnings": True}).status_code, 400)
            response = client.put("/api/settings/notifications", headers=headers,
                                  json={"browser_events": True, "show_feed_warnings": True})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["restart_workers"])


if __name__ == "__main__":
    unittest.main()
