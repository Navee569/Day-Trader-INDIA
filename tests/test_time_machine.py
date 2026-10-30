"""Regression tests for the paper-only NSE Time Machine workflow."""
import contextlib
from datetime import date, datetime, time, timedelta
import io
import json
from pathlib import Path
import tempfile
import unittest

from india_trader.core import IST
from india_trader.market_scanner import trading_day
from india_trader import time_machine as m


PLAN = {"ticker": "RELIANCE.NS", "date": "2026-10-28", "entry_reference": 100.5,
        "stop": 99.5, "target": 102.5, "entry_time_ist": "09:40", "entry_bar": 4,
        "interval": "5m", "setup": "orb", "opening_range": {"minutes": 15},
        "stop_rule": "or-mid", "flat_by_ist": "15:10", "entry_confirmed": True,
        "tick_size_assumed": 0.05}
NOW = datetime(2026, 10, 28, 15, 30, tzinfo=IST)


def fixture(session_count=22, gap=None, interval="5m"):
    days = [date(2026, 10, 28)]
    cursor = days[0]
    while len(days) <= session_count:
        cursor -= timedelta(days=1)
        if trading_day(cursor)[0]:
            days.append(cursor)
    days.reverse()
    stamps, opens, highs, lows, closes, volumes = [], [], [], [], [], []
    for day in days:
        for i in range(72):
            if (day.isoformat(), i) == gap:
                continue
            start = datetime.combine(day, time(9, 15), tzinfo=IST) + timedelta(minutes=5 * i)
            close = 100 if i < 3 else 100.5 if i == 3 else 100.5 + (i - 4) * 0.01
            stamps.append(int(start.timestamp()))
            opens.append(round(close - 0.05, 2))
            highs.append(round(close + 0.2, 2))
            lows.append(round(close - 0.2, 2))
            closes.append(round(close, 2))
            volumes.append(1000)
    doc = {"chart": {"result": [{"meta": {"symbol": "RELIANCE.NS", "currency": "INR",
           "dataGranularity": interval, "regularMarketTime": stamps[-1] + 300},
           "timestamp": stamps, "indicators": {"quote": [{"open": opens, "high": highs,
           "low": lows, "close": closes, "volume": volumes}]}}]}}
    return json.dumps(doc).encode(), days


class TimeMachineTests(unittest.TestCase):
    def test_replay_is_seeded_and_excludes_plan_day(self):
        raw, days = fixture()
        plan = m.normalize_plan(PLAN)
        a = m.run_machine(raw, plan, NOW, replays=40, seed=19)
        b = m.run_machine(raw, plan, NOW, replays=40, seed=19)
        self.assertEqual(a["mc"], b["mc"])
        self.assertEqual(a["mc"]["pool_sessions"], 22)
        self.assertEqual(a["mc"]["pool_to"], days[-2].isoformat())
        self.assertEqual(a["mc"]["bars_left"], 66)
        self.assertAlmostEqual(sum(a["mc"]["p_" + x] for x in ("target", "stop", "neither")), 1)
        self.assertEqual(len(a["mc"]["bands"]), 67)
        self.assertEqual(len(a["mc"]["hist"]["bins"]), 24)
        self.assertEqual(a["history"]["sessions"], 22)
        self.assertEqual(a["mode"], "PAPER_RESEARCH_ONLY")
        self.assertFalse(a["orders_possible"])

    def test_ambiguity_is_stop_first_even_when_target_touched(self):
        pool = [[(1, 1.1, 0.9, 1)] for _ in range(72)]
        result = m.simulate(pool, 100, 99, 101, 4, 5, replays=25, window=0)
        self.assertEqual(result["p_stop"], 1)
        self.assertEqual(result["p_target"], 0)
        self.assertEqual(result["avg_R"], -1)
        short = m.simulate(pool, 100, 101, 99, 4, 5, replays=25, window=0, side="sell")
        self.assertEqual(short["p_stop"], 1)
        self.assertEqual(short["p_target"], 0)

    def test_missing_session_is_excluded_and_minimum_enforced(self):
        _, days = fixture(20)
        # A gap in any of the actual prior sessions leaves fewer than 20 full days.
        prior = days[-2].isoformat()
        raw, _ = fixture(20, gap=(prior, 10))
        with self.assertRaisesRegex(m.MachineError, "at least 20"):
            m.run_machine(raw, m.normalize_plan(PLAN), NOW, replays=10)

    def test_reject_one_minute_and_future_or_incomplete_entry(self):
        bad = dict(PLAN, interval="1m")
        with self.assertRaisesRegex(m.MachineError, "five-minute"):
            m.normalize_plan(bad)
        raw, _ = fixture()
        early = datetime(2026, 10, 28, 9, 39, tzinfo=IST)
        with self.assertRaisesRegex(m.MachineError, "entry close"):
            m.run_machine(raw, m.normalize_plan(PLAN), early, replays=10)
        with self.assertRaisesRegex(m.MachineError, "assumed tick"):
            m.normalize_plan(dict(PLAN, tick_size_assumed=-0.05))

    def test_history_orb_trigger_and_stop_first(self):
        bars = [{"o": 100, "h": 101, "l": 99, "c": 100, "v": 1} for _ in range(72)]
        bars[3] = {"o": 101, "h": 102, "l": 101, "c": 101.5, "v": 1}
        bars[4] = {"o": 101.5, "h": 105, "l": 98, "c": 102, "v": 1}
        rule = {"or_minutes": 15, "stop_rule": "or-low", "stop_pct": 1,
                "target_r": 1, "tick_assumed": 0.05}
        result = m.replay_history([("2026-10-27", bars)], rule, 70)
        self.assertEqual(result["triggered"], 1)
        self.assertEqual(result["stop"], 1)
        self.assertEqual(result["target"], 0)
        short_bars = [{"o": 100, "h": 101, "l": 99, "c": 100, "v": 1} for _ in range(72)]
        short_bars[3] = {"o": 99, "h": 99, "l": 98, "c": 98.5, "v": 1}
        short_bars[4] = {"o": 98.5, "h": 102, "l": 95, "c": 98, "v": 1}
        short_rule = dict(rule, stop_rule="or-high", side="sell")
        short = m.replay_history([("2026-10-27", short_bars)], short_rule, 70)
        self.assertEqual(short["stop"], 1)
        self.assertEqual(short["target"], 0)

    def test_manual_sell_plan_and_auto_range_stop_detection(self):
        sell = m.normalize_plan(dict(PLAN, side="sell", entry_reference=100,
                                     stop=101, target=98, tick_size_assumed=0.05, stop_rule="auto"))
        self.assertEqual(sell["risk_per_share"], 1)
        self.assertEqual(sell["target_r"], 2)
        self.assertEqual(m.history_rule(sell)["stop_rule"], "pct")
        self.assertEqual(m.history_rule(dict(sell, stop_rule="or-high"))["stop_rule"], "or-high")
        buy = m.normalize_plan(dict(PLAN, entry_reference=100.5, stop=100,
                                    target=101.5, stop_rule="auto"))
        first = {i: {"h": 100.2, "l": 99.8} for i in range(3)}
        self.assertEqual(m.history_rule(buy, first)["stop_rule"], "or-mid")

    def test_cli_saved_result_chart_and_manual_flags(self):
        raw, _ = fixture()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            chart = root / "chart.json"
            saved = root / "machine.json"
            chart.write_bytes(raw)
            args = ["run", "--symbol", "RELIANCE", "--date", "2026-10-28",
                    "--entry", "100.5", "--stop", "99.5", "--target", "102.5",
                    "--entry-time", "09:40", "--offline", str(chart),
                    "--now", NOW.isoformat(), "--replays", "20", "--write", str(saved)]
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = m.main(args)
            self.assertEqual(code, 0)
            self.assertIn("20 seeded replays", output.getvalue())
            self.assertEqual(json.loads(saved.read_text())["mc"]["replays"], 20)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = m.main(["chart", "--from", str(saved)])
            self.assertEqual(code, 0)
            self.assertIn("ASCII fan chart", output.getvalue())
            short_args = args.copy()
            short_args[short_args.index("--entry") + 1] = "100.5"
            short_args[short_args.index("--stop") + 1] = "101.5"
            short_args[short_args.index("--target") + 1] = "99.5"
            short_args += ["--side", "sell"]
            with contextlib.redirect_stdout(io.StringIO()):
                code = m.main(short_args)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(saved.read_text())["plan"]["side"], "sell")

    def test_cli_invalid_chart_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as folder:
            chart = Path(folder) / "bad.json"
            chart.write_text("{}")
            args = ["run", "--symbol", "RELIANCE", "--date", "2026-10-28",
                    "--entry", "100.5", "--stop", "99.5", "--target", "102.5",
                    "--entry-time", "09:40", "--offline", str(chart), "--now", NOW.isoformat()]
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                code = m.main(args)
            self.assertEqual(code, 1)
            self.assertIn("invalid chart", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
