"""Offline Stage 4 committee contract tests. No model or broker calls."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from india_trader.ai_committee import AICommittee, load_config
from india_trader.core import IST


NOW = datetime(2026, 10, 28, 9, 41, tzinfo=IST)
SIGNAL_ID = "2026-10-28|RELIANCE.NS|orb|2026-10-28T09:35:00+05:30"


def signal(**overrides):
    result = {"id": SIGNAL_ID, "ticker": "RELIANCE.NS", "setup": "orb",
              "trigger_at": "2026-10-28T09:40:00+05:30",
              "recorded_at": "2026-10-28T09:41:00+05:30",
              "one_minute_last_bar": "2026-10-28T09:39:00+05:30",
              "one_minute_last_close": 103.0,
              "five_minute_last_close": 103.0,
              "opening_range_high": 100.6,
              "quote_at": "2026-10-28T09:40:58+05:30", "quote_price": 103.0,
              "market_status_verified": True,
              "exchange": "NSE", "instrument_id": "NSE:RELIANCE-EQ",
              "instrument_id_type": "NSE_EQ_SYMBOL",
              "five_minute_bar_complete": True, "data_gaps": 0,
              "volume_quality": "POSITIVE", "source_5m": "UPSTOX",
              "source_1m": "FYERS", "quote_source": "UPSTOX",
              "metrics": {"day_volume": 15000, "turnover_inr": 1500000,
                          "range_pct": 2.0, "vwap": 102.0, "spread_pct": 0.2,
                          "ma20": None, "rsi14": None, "atr14": None},
              "status": "AWAITING_AI_REVIEW", "orders_possible": False}
    result.update(overrides)
    return result


class FakeAdapter:
    def __init__(self, outcome="SELECT", bad_judge=False, fail=None, invalid_first=False):
        self.outcome, self.bad_judge, self.fail = outcome, bad_judge, fail
        self.invalid_first = invalid_first
        self.calls = []

    def invoke(self, spec, prompt, schema, max_answer_chars):
        from india_trader.ai_provider import AIProviderError
        payload = json.loads(prompt.split("\n", 1)[1])
        self.calls.append((spec["id"], payload["phase"], payload["payload"]))
        if self.fail == spec["id"]:
            raise AIProviderError("AI_TIMEOUT")
        evidence = (payload["payload"]["evidence"] if payload["phase"] == "final_judgment"
                    else payload["payload"]["evidence"] if payload["phase"].startswith("debate")
                    else payload["payload"])
        candidates = evidence["candidates"]
        if spec["id"] == "AI4":
            answer = {"overall_reasoning": "Only the supplied market evidence was considered.",
                      "decisions": [{"candidate_id": c["candidate_id"],
                                     "outcome": self.outcome,
                                     "rank": 2 if self.bad_judge else
                                             i if self.outcome in ("SELECT", "MONITOR") else 0,
                                     "score": 60, "reasoning": "Bounded paper research assessment.",
                                     "evidence_ids": ["MARKET_" + c["candidate_id"]],
                                     "risk_concerns": ["No executable fill is established."],
                                     "entry_conditions": ["Check next completed bar"],
                                     "valid_for_seconds": 180} for i, c in enumerate(candidates, 1)]}
        else:
            answer = {"analyst_id": spec["id"], "phase": payload["phase"],
                      "assessments": [{"candidate_id": c["candidate_id"],
                                       "stance": "UNCERTAIN", "reasoning": "Evidence is limited.",
                                       "evidence_ids": ["MARKET_" + c["candidate_id"]],
                                       "risk_concerns": ["No verified news supplied."],
                                       "challenges": [], "confidence": 0.5}
                                      for c in candidates]}
            if self.invalid_first and len(self.calls) == 1:
                answer["assessments"][0]["evidence_ids"] = ["MADE_UP_EVIDENCE"]
        return {"answer": answer, "usage": {"input_tokens": 100,
                "output_tokens": 100, "source": "LOCAL_ESTIMATE"}}


class CommitteeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.engine_path = root / "market-engine.json"
        self.state_path = root / "committee.json"
        self.config = load_config()
        self._write_engine([signal()])

    def _write_engine(self, signals):
        self.engine_path.write_text(json.dumps({"day": NOW.date().isoformat(),
                                               "signals": signals,
                                               "orders_possible": False}), encoding="utf-8")

    def _committee(self, adapter=None, config=None):
        return AICommittee(config or self.config, self.state_path,
                           adapter or FakeAdapter(), sleep=lambda _: None)

    def test_single_analyst_then_independent_ai4_no_artificial_debate(self):
        adapter = FakeAdapter()
        committee = self._committee(adapter)
        result = committee.review(self.engine_path, now=NOW)
        self.assertEqual(result["status"], "DECISIONS_RECORDED")
        self.assertEqual([(id_, phase) for id_, phase, _ in adapter.calls],
                         [("AI1", "independent"), ("AI4", "final_judgment")])
        self.assertEqual(result["debate_rounds"], 0)
        decision = result["decisions"][0]
        self.assertEqual(decision["outcome"], "SELECT")
        self.assertEqual(decision["instrument_id"], "NSE:RELIANCE-EQ")
        self.assertEqual(decision["rank"], 1)
        self.assertFalse(decision["orders_possible"])
        self.assertIn("Independent paper capital and risk review before any simulated order",
                      decision["entry_conditions"])
        self.assertEqual(decision["valid_until"], "2026-10-28T09:44:00+05:30")
        again = self._committee(FakeAdapter()).review(self.engine_path, now=NOW)
        self.assertEqual(again["status"], "NO_ELIGIBLE_CANDIDATES")

    def test_two_analysts_receive_same_evidence_then_debate_and_judge(self):
        config = deepcopy(self.config)
        config["analysts"][1]["enabled"] = True
        adapter = FakeAdapter()
        result = self._committee(adapter, config).review(self.engine_path, now=NOW)
        self.assertEqual(result["debate_rounds"], 1)
        self.assertEqual([(id_, phase) for id_, phase, _ in adapter.calls],
                         [("AI1", "independent"), ("AI2", "independent"),
                          ("AI1", "debate_1"), ("AI2", "debate_1"),
                          ("AI4", "final_judgment")])
        self.assertEqual(adapter.calls[0][2], adapter.calls[1][2])
        self.assertEqual(len(result["decisions"][0]["debate"]), 2)

    def test_three_analysts_and_two_bounded_debate_rounds(self):
        config = deepcopy(self.config)
        config["analysts"][1]["enabled"] = True
        config["analysts"][2]["enabled"] = True
        config["max_debate_rounds"] = 2
        adapter = FakeAdapter()
        result = self._committee(adapter, config).review(self.engine_path, now=NOW)
        self.assertEqual(result["status"], "DECISIONS_RECORDED")
        self.assertEqual(result["analyst_count"], 3)
        self.assertEqual(result["debate_rounds"], 2)
        self.assertEqual(len(adapter.calls), 10)
        self.assertEqual(adapter.calls[-1][0], "AI4")

    def test_judge_ranks_multiple_candidates(self):
        second = signal(id="2026-10-28|TCS.NS|orb|2026-10-28T09:35:00+05:30",
                        ticker="TCS.NS", instrument_id="NSE:TCS-EQ")
        self._write_engine([signal(), second])
        result = self._committee().review(self.engine_path, now=NOW)
        self.assertEqual(result["status"], "DECISIONS_RECORDED")
        self.assertEqual([d["rank"] for d in result["decisions"]], [1, 2])
        self.assertEqual({d["ticker"] for d in result["decisions"]},
                         {"RELIANCE.NS", "TCS.NS"})

    def test_judge_can_reject_every_candidate(self):
        result = self._committee(FakeAdapter(outcome="NO_TRADE")).review(
            self.engine_path, now=NOW)
        self.assertEqual(result["decisions"][0]["outcome"], "NO_TRADE")
        self.assertEqual(result["decisions"][0]["rank"], 0)

    def test_processed_id_remains_once_only_after_decision_archive_rolls_off(self):
        first = self._committee().review(self.engine_path, now=NOW)
        self.assertEqual(first["status"], "DECISIONS_RECORDED")
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["processed_ids"], [SIGNAL_ID])
        saved["decisions"] = []
        self.state_path.write_text(json.dumps(saved), encoding="utf-8")
        adapter = FakeAdapter()
        second = self._committee(adapter).review(self.engine_path, now=NOW)
        self.assertEqual(second["status"], "NO_ELIGIBLE_CANDIDATES")
        self.assertEqual(adapter.calls, [])

    def test_invalid_judge_or_unavailable_model_blocks_selection(self):
        for adapter in (FakeAdapter(bad_judge=True), FakeAdapter(fail="AI4")):
            with self.subTest(adapter=adapter):
                self.state_path.unlink(missing_ok=True)
                result = self._committee(adapter).review(self.engine_path, now=NOW)
                self.assertEqual(result["status"], "AI_UNAVAILABLE_NO_SELECTION")
                self.assertEqual(result["decisions"], [])
                saved = json.loads(self.state_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["decisions"], [])

    def test_invalid_analyst_evidence_is_retried_only_within_limit(self):
        config = deepcopy(self.config)
        config["analysts"][0]["max_retries"] = 1
        adapter = FakeAdapter(invalid_first=True)
        result = self._committee(adapter, config).review(self.engine_path, now=NOW)
        self.assertEqual(result["status"], "DECISIONS_RECORDED")
        self.assertEqual(len(adapter.calls), 3)

    def test_stale_or_nonbroker_signal_never_reaches_ai(self):
        for bad in (signal(source_1m="YAHOO"),
                    signal(instrument_id="NSE:OTHER-EQ"),
                    signal(data_gaps=1), signal(metrics={"spread_pct": None}),
                    signal(quote_price=110.0),
                    signal(one_minute_last_close=99.0),
                    signal(trigger_at="2026-10-28T09:30:00+05:30")):
            with self.subTest(bad=bad.get("id")):
                self.state_path.unlink(missing_ok=True)
                self._write_engine([bad])
                adapter = FakeAdapter()
                result = self._committee(adapter).review(self.engine_path, now=NOW)
                self.assertEqual(result["status"], "NO_ELIGIBLE_CANDIDATES")
                self.assertEqual(adapter.calls, [])

    def test_closed_session_and_missing_engine_do_not_call_model(self):
        adapter = FakeAdapter()
        committee = self._committee(adapter)
        late = signal(id="2026-10-28|RELIANCE.NS|orb|2026-10-28T15:05:00+05:30",
                      trigger_at="2026-10-28T15:10:00+05:30",
                      recorded_at="2026-10-28T15:10:30+05:30",
                      one_minute_last_bar="2026-10-28T15:09:00+05:30",
                      quote_at="2026-10-28T15:10:29+05:30")
        self._write_engine([late])
        result = committee.review(self.engine_path, now=NOW.replace(hour=15, minute=11))
        self.assertEqual(result["status"], "SESSION_CLOSED_NO_SELECTION")
        self.assertEqual(adapter.calls, [])
        self.engine_path.unlink()
        result = committee.review(self.engine_path, now=NOW)
        self.assertEqual(result["status"], "WAIT_ENGINE")

    def test_usage_budget_blocks_judge_and_records_no_selection(self):
        config = deepcopy(self.config)
        config["analysts"][1]["enabled"] = True
        config["max_calls_per_day"] = 2
        adapter = FakeAdapter()
        result = self._committee(adapter, config).review(self.engine_path, now=NOW)
        self.assertEqual(result["status"], "AI_UNAVAILABLE_NO_SELECTION")
        self.assertEqual(result["reason"], "AI_USAGE_BUDGET")
        self.assertEqual(len(adapter.calls), 2)

    def test_check_validates_without_model_call(self):
        adapter = FakeAdapter()
        result = self._committee(adapter).review(self.engine_path, now=NOW, execute=False)
        self.assertEqual(result["status"], "INPUT_VALIDATED_NO_MODEL_CALL")
        self.assertEqual(adapter.calls, [])


if __name__ == "__main__":
    unittest.main()
