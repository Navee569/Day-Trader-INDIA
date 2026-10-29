import unittest

from india_trader.core import DataError
from india_trader.universe import parse_nse_equities


HEADER = b"SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE\n"


class NSEUniverseTests(unittest.TestCase):
    def test_eq_only_and_symbol_mapping(self):
        data = HEADER + (b"RELIANCE,Reliance Industries,EQ,01-JAN-2000,10,1,INE002A01018,10\n"
                         b"ETF,Example Fund,ETF,01-JAN-2000,10,1,INE000A01001,10\n")
        self.assertEqual(parse_nse_equities(data), [{"ticker": "RELIANCE.NS", "name": "Reliance Industries", "isin": "INE002A01018"}])

    def test_duplicate_symbol_rejected(self):
        line = b"RELIANCE,Reliance Industries,EQ,01-JAN-2000,10,1,INE002A01018,10\n"
        with self.assertRaises(DataError):
            parse_nse_equities(HEADER + line + line)

    def test_bad_schema_rejected(self):
        with self.assertRaises(DataError):
            parse_nse_equities(b"ticker,name\nRELIANCE,Reliance\n")
