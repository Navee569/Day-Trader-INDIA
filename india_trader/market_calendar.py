"""Conservative NSE EQ regular-session calendar snapshot for paper entry gates."""
from __future__ import annotations

from datetime import date

# NSE equity holiday page checked 2026-10-30. Special Sunday sessions, Muhurat,
# ad hoc closures, and future-year calendars are deliberately not treated as
# normal continuous sessions.
NSE_2026_CLOSED = frozenset(date.fromisoformat(day) for day in (
    "2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26",
    "2026-03-31", "2026-04-03", "2026-04-14", "2026-05-01",
    "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02",
    "2026-10-20", "2026-11-10", "2026-11-24", "2026-12-25",
))


def regular_nse_entry_day(day: date) -> bool:
    """Only known ordinary NSE equity weekdays; unknown years fail closed."""
    return day.year == 2026 and day.weekday() < 5 and day not in NSE_2026_CLOSED
