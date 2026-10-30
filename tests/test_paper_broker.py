import tempfile
import copy
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from india_trader.__main__ import main
from india_trader.core import save_json
from india_trader.paper_orders import risk_review, size_paper_order
from india_trader.paper_broker import (new_ledger, submit, cancel, fill_entry,
                                       close_position, load_ledger, save_ledger, ledger_lock)


PLAN = {"mode": "PAPER_PLAN_ONLY", "status": "PLANNED", "ticker": "RELIANCE.NS",
        "entry_reference": 103, "stop": 100, "target": 109,
        "trigger_at": "2026-10-28T09:35:00+05:30", "flat_by_ist": "15:10",
        "data_origin": "synthetic_test"}
APPROVAL = "2026-10-28T09:37:00+05:30"


def proposal():
    order = size_paper_order(PLAN)
    review = risk_review(order, "2026-10-28T09:36:00+05:30")
    return order, review


class PaperBrokerTests(unittest.TestCase):
    def test_cli_requires_exact_confirmation_and_writes_ledger(self):
        order, review = proposal()
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            order_file, risk_file, ledger_file = (base / name for name in ("order.json", "risk.json", "ledger.json"))
            save_json(order_file, order)
            save_json(risk_file, review)
            self.assertEqual(main(["paper-broker", "init", "--ledger", str(ledger_file)]), 0)
            command = ["paper-broker", "submit", "--ledger", str(ledger_file),
                       "--order", str(order_file), "--risk", str(risk_file), "--as-of", APPROVAL]
            with patch("builtins.input", return_value="yes"):
                self.assertEqual(main(command), 2)
            self.assertEqual(len(load_ledger(ledger_file)["orders"]), 0)
            with patch("builtins.input", return_value="YES"):
                self.assertEqual(main(command), 0)
            self.assertEqual(len(load_ledger(ledger_file)["orders"]), 1)
            fill = ["paper-broker", "fill", "--ledger", str(ledger_file),
                    "--id", "1", "--qty", "1", "--price", "103",
                    "--as-of", "2026-10-28T09:38:00+05:30"]
            with patch("builtins.input", return_value="no"):
                self.assertEqual(main(fill), 2)
            self.assertFalse(load_ledger(ledger_file)["positions"])
            with patch("builtins.input", return_value="SIMULATE"):
                self.assertEqual(main(fill), 0)
            self.assertEqual(load_ledger(ledger_file)["positions"]["RELIANCE.NS"]["quantity"], 1)

    def test_approval_fill_close_and_persistence(self):
        order, review = proposal()
        original = new_ledger()
        ledger = submit(original, order, review, APPROVAL)
        self.assertFalse(original["orders"])
        self.assertEqual(ledger["orders"][0]["status"], "APPROVED_UNFILLED")
        self.assertEqual(ledger["cash_inr"], "100000")
        order_id = ledger["orders"][0]["id"]
        ledger = fill_entry(ledger, order_id, 2, "103", "2026-10-28T09:38:00+05:30")
        self.assertEqual(ledger["orders"][0]["status"], "PARTIALLY_FILLED")
        ledger = cancel(ledger, order_id, "2026-10-28T09:39:00+05:30")
        self.assertEqual(ledger["orders"][0]["status"], "OPEN")
        ledger = close_position(ledger, "RELIANCE.NS", 2, "109", "2026-10-28T15:05:00+05:30", "TARGET")
        self.assertEqual(ledger["cash_inr"], "100012")
        self.assertFalse(ledger["positions"])
        self.assertEqual(ledger["orders"][0]["status"], "CLOSED")
        self.assertEqual(ledger["events"][-1]["realized_pnl_inr"], "12")
        self.assertEqual(ledger["events"][-1]["kind"], "SIMULATED_USER_INPUT")
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "broker.json"
            save_ledger(file, ledger)
            self.assertEqual(load_ledger(file), ledger)

    def test_fails_closed_on_duplicate_stale_and_changed_orders(self):
        order, review = proposal()
        ledger = submit(new_ledger(), order, review, APPROVAL)
        with self.assertRaisesRegex(ValueError, "already submitted"):
            submit(ledger, order, review, APPROVAL)
        with self.assertRaisesRegex(ValueError, "fresh same-day"):
            submit(new_ledger(), order, review, "2026-10-28T09:43:00+05:30")
        changed = dict(order, quantity=order["quantity"] + 1)
        with self.assertRaisesRegex(ValueError, "changed"):
            submit(new_ledger(), changed, review, APPROVAL)

    def test_cash_risk_and_oversell_limits(self):
        order, review = proposal()
        ledger = submit(new_ledger(), order, review, APPROVAL)
        order_id = ledger["orders"][0]["id"]
        with self.assertRaisesRegex(ValueError, "approved notional or loss"):
            fill_entry(ledger, order_id, 1, "150", "2026-10-28T09:38:00+05:30")
        with self.assertRaisesRegex(ValueError, "same-day"):
            fill_entry(ledger, order_id, 1, "103", "2026-10-29T09:38:00+05:30")
        with self.assertRaisesRegex(ValueError, "flat-by"):
            fill_entry(ledger, order_id, 1, "103", "2026-10-28T15:11:00+05:30")
        with self.assertRaisesRegex(ValueError, "more than the open"):
            close_position(ledger, "RELIANCE.NS", 1, "109")
        ledger = fill_entry(ledger, order_id, 1, "103", "2026-10-28T09:38:00+05:30")
        with self.assertRaisesRegex(ValueError, "more than the open"):
            close_position(ledger, "RELIANCE.NS", 2, "109")
        self.assertEqual(ledger["positions"]["RELIANCE.NS"]["quantity"], 1)

    def test_partial_entry_must_be_canceled_before_a_paper_exit(self):
        ledger = submit(new_ledger(), *proposal(), APPROVAL)
        ledger = fill_entry(ledger, 1, 1, "103", "2026-10-28T09:38:00+05:30")
        with self.assertRaisesRegex(ValueError, "outstanding entry remainder"):
            close_position(ledger, "RELIANCE.NS", 1, "109")
        ledger = cancel(ledger, 1)
        ledger = close_position(ledger, "RELIANCE.NS", 1, "109")
        self.assertFalse(ledger["positions"])

    def test_cli_waits_for_other_process_holding_ledger_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ledger.json"
            save_ledger(path, new_ledger())
            with ledger_lock(path):
                proc = subprocess.Popen([sys.executable, "-m", "india_trader", "paper-broker",
                                         "show", "--ledger", str(path)],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    time.sleep(0.25)
                    self.assertIsNone(proc.poll(), "CLI read escaped the ledger lock")
                except BaseException:
                    proc.kill()
                    proc.communicate()
                    raise
            out, err = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0, err)
            self.assertIn('"cash_inr": "100000"', out)

    def test_corrupt_fills_and_duplicate_order_ids_are_rejected_on_load(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / 'ledger.json'
            order, review = proposal()
            good = fill_entry(submit(new_ledger(), order, review, APPROVAL), 1, 1, '103',
                              '2026-10-28T09:38:00+05:30')
            for mutate in (lambda x: x['orders'][0].update(filled_quantity=0),
                           lambda x: x['orders'].append(copy.deepcopy(x['orders'][0])),
                           lambda x: x['positions']['RELIANCE.NS'].update(quantity=-1)):
                bad = copy.deepcopy(good)
                mutate(bad)
                with self.assertRaises(ValueError):
                    save_ledger(file, bad)
                file.write_text(json.dumps(bad))
                with self.assertRaises(ValueError):
                    load_ledger(file)


if __name__ == "__main__":
    unittest.main()
