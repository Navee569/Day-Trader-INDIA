"""Repository examples remain consumable by the India paper workflows."""
import json
from pathlib import Path
import unittest

from india_trader.alerts import _rules
from india_trader.budget_manager import parse_weights


ROOT = Path(__file__).resolve().parents[1]


class ParityExamplesTests(unittest.TestCase):
    def test_original_style_alert_rule_example(self):
        rules = _rules(ROOT / "examples" / "ALERTS_INDIA.json")["rules"]
        self.assertEqual({r["type"] for r in rules},
                         {"opening_bell", "stop", "target", "time", "pct_move"})

    def test_original_style_weight_target_example(self):
        raw = json.loads((ROOT / "examples" / "TARGETS_INDIA.json").read_text())
        rows = parse_weights(raw)
        self.assertEqual(len(rows), 3)
        self.assertEqual(sum(r["weight"] for r in rows), 100)


if __name__ == "__main__":
    unittest.main()
