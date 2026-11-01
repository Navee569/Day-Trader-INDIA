"""Offline public cash-market calendar parsing and fail-closed checks."""
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from india_trader import calendar_source as c
from india_trader.core import IST


def event(day, typ, closed=(), opened=(), description="Holiday"):
    return {"date": day, "holiday_type": typ, "description": description,
            "closed_exchanges": list(closed),
            "open_exchanges": [{"exchange": exchange,
                                "start_time": int(datetime.fromisoformat(day + "T" + start + ":00+05:30").timestamp() * 1000),
                                "end_time": int(datetime.fromisoformat(day + "T" + end + ":00+05:30").timestamp() * 1000)}
                               for exchange, start, end in opened]}


class CalendarSourceTests(unittest.TestCase):
    def test_normal_holiday_and_special_by_exchange(self):
        now = datetime(2026, 11, 1, 12, tzinfo=IST)
        payload = {"status": "success", "data": [
            event("2026-11-10", "TRADING_HOLIDAY", ("NSE", "BSE")),
            event("2026-11-08", "SPECIAL_TIMING", (),
                  (("NSE", "18:00", "19:00"), ("BSE", "18:00", "19:00")), "Muhurat") ]}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, cache = root / "response.json", root / "cache.json"
            source.write_text(json.dumps(payload))
            c.refresh(cache, source, now)
            self.assertEqual(c.status(date(2026, 11, 10), "NSE", cache, now)["state"], "CLOSED")
            self.assertEqual(c.status(date(2026, 11, 8), "BSE", cache, now)["state"], "SPECIAL")
            self.assertEqual(c.status(date(2026, 11, 2), "NSE", cache, now)["state"], "NORMAL")
            self.assertEqual(c.status(date(2026, 11, 7), "NSE", cache, now)["state"], "CLOSED")
            self.assertEqual(c.status(date(2026, 11, 2), "NSE", cache, now + timedelta(days=8))["state"], "UNKNOWN")

    def test_invalid_calendar_rejected(self):
        with self.assertRaises(c.CalendarError):
            c.parse_holidays(b'{"status":"success","data":[]}', 2026)
        wrong = {"status": "success", "data": [event("2027-01-01", "TRADING_HOLIDAY", ("NSE",))]}
        with self.assertRaises(c.CalendarError):
            c.parse_holidays(json.dumps(wrong), 2026)


if __name__ == "__main__":
    unittest.main()
