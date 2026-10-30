import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from india_trader.__main__ import main
from india_trader.core import save_json
from india_trader.broker_sandbox import (SANDBOX_PLACE_URL, SANDBOX_CANCEL_URL,
                                         order_payload, place_sandbox, cancel_sandbox,
                                         verify_instrument)
from india_trader.paper_broker import (new_ledger, submit, fill_entry, propose_flatten,
                                       approve_flatten, begin_sandbox_send,
                                       record_sandbox_result, begin_sandbox_cancel,
                                       record_sandbox_cancel, close_position,
                                       deny_sandbox_permission, reconcile_sandbox, ticket_view)
from india_trader.paper_orders import size_paper_order, risk_review


PLAN = {"mode": "PAPER_PLAN_ONLY", "status": "PLANNED", "ticker": "RELIANCE.NS",
        "entry_reference": 103, "stop": 100, "target": 109,
        "trigger_at": "2026-10-28T09:35:00+05:30", "flat_by_ist": "15:10"}
KEY = "NSE_EQ|INE002A01018"  # illustrative instrument key; not looked up or validated
MASTER_ROW = {"segment": "NSE_EQ", "exchange": "NSE", "trading_symbol": "RELIANCE",
              "instrument_type": "EQ", "instrument_key": KEY, "lot_size": 1,
              "isin": "INE002A01018", "freeze_quantity": 100000}


def approved():
    order = size_paper_order(PLAN)
    review = risk_review(order, "2026-10-28T09:36:00+05:30")
    return submit(new_ledger(), order, review, "2026-10-28T09:37:00+05:30")


class Response:
    def __init__(self, body):
        self.stream = io.BytesIO(body)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.stream.close()

    def read(self, limit):
        return self.stream.read(limit)


class Opener:
    def __init__(self, body):
        self.body = body
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        assert timeout == 15
        return Response(self.body)


class SandboxTests(unittest.TestCase):
    def test_cli_second_gate_and_persisted_accepted_result(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger_file = Path(folder) / "ledger.json"
            master_file = Path(folder) / "bod.json"
            save_json(ledger_file, approved())
            save_json(master_file, [MASTER_ROW])
            cmd = ["paper-broker", "sandbox-place", "--ledger", str(ledger_file),
                   "--id", "1", "--instrument-key", KEY, "--instrument-master", str(master_file),
                   "--as-of", "2026-10-28T09:38:00+05:30"]
            with patch.dict(os.environ, {"UPSTOX_SANDBOX_TOKEN": "test-secret"}), \
                 patch("builtins.input", return_value="no"), \
                 patch("india_trader.__main__.place_sandbox") as post, \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(cmd), 2)
                post.assert_not_called()
            from india_trader.paper_broker import load_ledger
            self.assertEqual(load_ledger(ledger_file)["orders"][0]["status"], "SANDBOX_DENIED")
            save_json(ledger_file, approved())  # independent new scenario for the success path
            with patch.dict(os.environ, {"UPSTOX_SANDBOX_TOKEN": "test-secret"}), \
                 patch("builtins.input", return_value="ALLOW ONCE"), \
                 patch("india_trader.__main__.place_sandbox", return_value="12345") as post, \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(cmd), 0)
                post.assert_called_once()
            saved = load_ledger(ledger_file)
            self.assertEqual(saved["orders"][0]["status"], "SANDBOX_ACCEPTED")
            self.assertNotIn("test-secret", ledger_file.read_text())
            self.assertFalse(saved["positions"])

    def test_cli_timeout_records_unknown_before_return(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger_file = Path(folder) / "ledger.json"
            master_file = Path(folder) / "bod.json"
            save_json(ledger_file, approved())
            save_json(master_file, [MASTER_ROW])
            cmd = ["paper-broker", "sandbox-place", "--ledger", str(ledger_file),
                   "--id", "1", "--instrument-key", KEY, "--instrument-master", str(master_file),
                   "--as-of", "2026-10-28T09:38:00+05:30"]
            with patch.dict(os.environ, {"UPSTOX_SANDBOX_TOKEN": "test-secret"}), \
                 patch("builtins.input", return_value="ALLOW ONCE"), \
                 patch("india_trader.__main__.place_sandbox", side_effect=ValueError("timeout")), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(cmd), 2)
                self.assertEqual(main(cmd), 2)
            from india_trader.paper_broker import load_ledger
            self.assertEqual(load_ledger(ledger_file)["orders"][0]["status"], "SANDBOX_UNKNOWN")

    def test_upstox_sandbox_only_payload_and_response(self):
        ledger = approved()
        payload = order_payload(ledger["orders"][0], KEY)
        self.assertEqual(payload["transaction_type"], "BUY")
        self.assertEqual(payload["instrument_token"], KEY)
        self.assertEqual(payload["product"], "D")
        self.assertEqual(payload["market_protection"], -1)
        with self.assertRaises(ValueError):
            order_payload(ledger["orders"][0], "BSE_EQ|INE002A01018")
        with self.assertRaisesRegex(ValueError, "Invalid sandbox payload"):
            place_sandbox({**payload, "transaction_type": "SELL_SHORT"}, "dummy-secret", opener=Opener(b'{}'))
        with self.assertRaisesRegex(ValueError, "Invalid sandbox payload"):
            place_sandbox({**payload, "extra": "unreviewed"}, "dummy-secret", opener=Opener(b'{}'))
        opener = Opener(b'{"status":"success","data":{"order_ids":["12345"]}}')
        self.assertEqual(place_sandbox(payload, "dummy-secret", opener=opener), "12345")
        req = opener.requests[0]
        self.assertEqual(req.full_url, SANDBOX_PLACE_URL)
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), "Bearer dummy-secret")
        self.assertNotIn(b"dummy-secret", req.data)
        self.assertNotIn("api-hft.upstox.com", SANDBOX_PLACE_URL)
        with tempfile.TemporaryDirectory() as folder:
            master = Path(folder) / "bod.json"
            save_json(master, [MASTER_ROW])
            self.assertEqual(verify_instrument(str(master), "RELIANCE.NS", KEY, 140)["segment"], "NSE_EQ")
            save_json(master, [dict(MASTER_ROW, trading_symbol="OTHER")])
            with self.assertRaisesRegex(ValueError, "does not match"):
                verify_instrument(str(master), "RELIANCE.NS", KEY, 140)
            save_json(master, [dict(MASTER_ROW, isin="INE000000000")])
            with self.assertRaisesRegex(ValueError, "does not match"):
                verify_instrument(str(master), "RELIANCE.NS", KEY, 140)

    def test_malformed_instrument_master_fails_with_controlled_error(self):
        with tempfile.TemporaryDirectory() as folder:
            master = Path(folder) / "bod.json"
            save_json(master, [MASTER_ROW])
            for key, qty in (("bad", 1), (KEY, True), (KEY, 0)):
                with self.assertRaises(ValueError):
                    verify_instrument(str(master), "RELIANCE.NS", key, qty)
            for changed in (dict(MASTER_ROW, lot_size=True),
                            dict(MASTER_ROW, freeze_quantity="many"),
                            dict(MASTER_ROW, freeze_quantity=1)):
                save_json(master, [changed])
                with self.assertRaisesRegex(ValueError, "does not match"):
                    verify_instrument(str(master), "RELIANCE.NS", KEY, 140)
            compressed = Path(folder) / "broken.gz"
            compressed.write_bytes(b"not a gzip stream")
            with self.assertRaisesRegex(ValueError, "Invalid compressed"):
                verify_instrument(str(compressed), "RELIANCE.NS", KEY, 140)

    def test_send_requires_fresh_approval_and_cannot_retry_unknown(self):
        ledger = approved()
        with self.assertRaisesRegex(ValueError, "fresh same-day"):
            begin_sandbox_send(ledger, 1, KEY, "2026-10-28T09:43:00+05:30")
        attempt = begin_sandbox_send(ledger, 1, KEY, "2026-10-28T09:38:00+05:30")
        self.assertEqual(attempt["orders"][0]["status"], "SANDBOX_SEND_ATTEMPTED")
        with self.assertRaises(ValueError):
            begin_sandbox_send(attempt, 1, KEY, "2026-10-28T09:38:00+05:30")
        unknown = record_sandbox_result(attempt, 1, None, "2026-10-28T09:38:01+05:30")
        self.assertEqual(unknown["orders"][0]["status"], "SANDBOX_UNKNOWN")
        with self.assertRaises(ValueError):
            begin_sandbox_send(unknown, 1, KEY, "2026-10-28T09:38:02+05:30")
        reconciled = reconcile_sandbox(unknown, 1, "rejected")
        self.assertEqual(reconciled["orders"][0]["status"], "SANDBOX_REJECTED")
        with self.assertRaises(ValueError):
            reconcile_sandbox(reconciled, 1, "accepted", "12345")
        accepted = reconcile_sandbox(unknown, 1, "accepted", "12345")
        self.assertEqual(accepted["orders"][0]["sandbox_order_id"], "12345")
        self.assertEqual(ticket_view(accepted)["tickets"][0]["status"], "SANDBOX_ACCEPTED")
        recovered = reconcile_sandbox(attempt, 1, "accepted", "12345")
        self.assertEqual(recovered["orders"][0]["status"], "SANDBOX_ACCEPTED")

    def test_sandbox_cancel_is_individual_and_ack_is_not_a_fill(self):
        attempt = begin_sandbox_send(approved(), 1, KEY, "2026-10-28T09:38:00+05:30")
        accepted = record_sandbox_result(attempt, 1, "12345", "2026-10-28T09:38:01+05:30")
        self.assertFalse(accepted["positions"])
        cancel_attempt = begin_sandbox_cancel(accepted, 1, "2026-10-28T09:39:00+05:30")
        self.assertEqual(cancel_attempt["orders"][0]["status"], "SANDBOX_CANCEL_ATTEMPTED")
        opener = Opener(b'{"status":"success","data":{"order_id":"12345"}}')
        self.assertEqual(cancel_sandbox("12345", "dummy-secret", opener=opener), "12345")
        self.assertEqual(opener.requests[0].full_url, SANDBOX_CANCEL_URL + "?order_id=12345")
        self.assertEqual(opener.requests[0].get_method(), "DELETE")
        done = record_sandbox_cancel(cancel_attempt, 1, True)
        self.assertEqual(done["orders"][0]["status"], "SANDBOX_CANCEL_ACK")
        self.assertFalse(done["positions"])
        recovered = reconcile_sandbox(cancel_attempt, 1, "canceled")
        self.assertEqual(recovered["orders"][0]["status"], "SANDBOX_CANCELED")

    def test_unexpected_sandbox_id_is_not_accepted(self):
        payload = order_payload(approved()["orders"][0], KEY)
        with self.assertRaises(ValueError):
            place_sandbox(payload, "dummy-secret",
                          opener=Opener(b'{"status":"success","data":{"order_ids":["bad_id"]}}'))

    def test_partial_sandbox_fill_requires_cancel_and_reconciliation(self):
        attempt = begin_sandbox_send(approved(), 1, KEY, "2026-10-28T09:38:00+05:30")
        accepted = record_sandbox_result(attempt, 1, "12345", "2026-10-28T09:38:01+05:30")
        partial = fill_entry(accepted, 1, 2, "103", "2026-10-28T09:39:00+05:30")
        self.assertEqual(partial["orders"][0]["status"], "SANDBOX_PARTIALLY_FILLED")
        with self.assertRaisesRegex(ValueError, "outstanding sandbox entry"):
            close_position(partial, "RELIANCE.NS", 2, "109")
        pending = begin_sandbox_cancel(partial, 1)
        ack = record_sandbox_cancel(pending, 1, True)
        reconciled = reconcile_sandbox(ack, 1, "canceled")
        self.assertEqual(reconciled["orders"][0]["status"], "OPEN")
        self.assertEqual(reconciled["orders"][0]["quantity"], 2)
        closed = close_position(reconciled, "RELIANCE.NS", 2, "109")
        self.assertFalse(closed["positions"])

    def test_flatten_exact_ticket_and_one_approval(self):
        ledger = fill_entry(approved(), 1, 2, "103", "2026-10-28T09:38:00+05:30")
        from india_trader.paper_broker import cancel
        ledger = cancel(ledger, 1)
        ticket = propose_flatten(ledger, "RELIANCE.NS", "2026-10-28T15:02:00+05:30")
        with self.assertRaisesRegex(ValueError, "pending"):
            propose_flatten(ticket, "RELIANCE.NS", "2026-10-28T15:02:00+05:30")
        with self.assertRaisesRegex(ValueError, "stale"):
            approve_flatten(ticket, 2, "2026-10-28T15:08:00+05:30")
        approved_exit = approve_flatten(ticket, 2, "2026-10-28T15:03:00+05:30")
        self.assertEqual(order_payload(approved_exit["orders"][1], KEY)["transaction_type"], "SELL")
        with self.assertRaisesRegex(ValueError, "exact exit ID"):
            close_position(approved_exit, "RELIANCE.NS", 2, "109")
        with self.assertRaisesRegex(ValueError, "Exact sell-to-close"):
            close_position(approved_exit, "RELIANCE.NS", 1, "109", exit_id=2)
        closed = close_position(approved_exit, "RELIANCE.NS", 2, "109",
                                "2026-10-28T15:04:00+05:30", "FLAT_BY", 2)
        self.assertFalse(closed["positions"])
        self.assertEqual(closed["orders"][1]["status"], "EXIT_CLOSED")

    def test_sandbox_order_ids_accept_documented_hyphen_shape(self):
        attempt = begin_sandbox_send(approved(), 1, KEY, "2026-10-28T09:38:00+05:30")
        accepted = record_sandbox_result(attempt, 1, "abc-123")
        self.assertEqual(begin_sandbox_cancel(accepted, 1)["orders"][0]["status"], "SANDBOX_CANCEL_ATTEMPTED")

    def test_changed_approved_ledger_row_cannot_be_sent_or_simulated(self):
        ledger = approved()
        ledger['orders'][0]['quantity'] += 1000
        with self.assertRaisesRegex(ValueError, 'Approved paper ticket changed'):
            begin_sandbox_send(ledger, 1, KEY, '2026-10-28T09:38:00+05:30')
        with self.assertRaisesRegex(ValueError, 'Approved paper ticket changed'):
            fill_entry(ledger, 1, 1, '103', '2026-10-28T09:38:00+05:30')

    def test_changed_approved_exit_cannot_close_paper_position(self):
        from india_trader.paper_broker import cancel
        paper = cancel(fill_entry(approved(), 1, 2, '103', '2026-10-28T09:38:00+05:30'), 1)
        exit_order = approve_flatten(propose_flatten(paper, 'RELIANCE.NS', '2026-10-28T15:02:00+05:30'),
                                     2, '2026-10-28T15:03:00+05:30')
        exit_order['orders'][1]['ticker'] = 'OTHER.NS'
        with self.assertRaisesRegex(ValueError, 'Approved paper ticket changed'):
            begin_sandbox_send(exit_order, 2, KEY, '2026-10-28T15:03:01+05:30')
        exit_order['orders'][1]['ticker'] = 'RELIANCE.NS'
        exit_order['orders'][1]['quantity'] = 1
        with self.assertRaisesRegex(ValueError, 'Approved paper ticket changed'):
            close_position(exit_order, 'RELIANCE.NS', 1, '109',
                           '2026-10-28T15:04:00+05:30', exit_id=2)

    def test_pending_sandbox_exit_cancel_cannot_spawn_second_sell(self):
        from india_trader.paper_broker import cancel
        paper = cancel(fill_entry(approved(), 1, 2, "103", "2026-10-28T09:38:00+05:30"), 1)
        exit_ticket = approve_flatten(propose_flatten(paper, "RELIANCE.NS", "2026-10-28T15:02:00+05:30"),
                                      2, "2026-10-28T15:03:00+05:30")
        sent = begin_sandbox_send(exit_ticket, 2, KEY, "2026-10-28T15:03:01+05:30")
        accepted = record_sandbox_result(sent, 2, "abc-123")
        waiting = begin_sandbox_cancel(accepted, 2)
        for state in (waiting, record_sandbox_cancel(waiting, 2, True),
                      record_sandbox_cancel(waiting, 2, False)):
            with self.assertRaisesRegex(ValueError, "already pending"):
                propose_flatten(state, "RELIANCE.NS")


if __name__ == "__main__":
    unittest.main()
