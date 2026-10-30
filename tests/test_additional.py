import unittest
from india_trader.core import orb_signal, plan
from india_trader.backtest import backtest_orb
from tests.test_core import day


class TestAdditional(unittest.TestCase):
    def test_stale_feed_blocks_existing_trigger(self):
        result=orb_signal(day(),as_of='2026-10-28T10:00:00+05:30')
        self.assertEqual(result['status'],'STALE_DATA')
    def test_after_market_historical_not_stale(self):
        result=orb_signal(day(),as_of='2026-10-28T16:00:00+05:30')
        self.assertEqual(result['status'],'TRIGGERED_LONG')
    def test_only_forming_open_bar_not_signal(self):
        rows=day()
        result=orb_signal(rows,as_of='2026-10-28T09:16:00+05:30')
        self.assertEqual(result['status'],'INCOMPLETE_OPEN')
    def test_strict_plan_slippage_bounds(self):
        with self.assertRaises(ValueError):plan(100,95,slippage_bps=501,tick_size="0.01")
    def test_zero_volume_bar_blocks_orb(self):
        bars=day();bars[3]['volume']=0
        self.assertEqual(orb_signal(bars,as_of='2026-10-28T09:40:00+05:30')['status'],'ZERO_VOLUME_BAR')
    def test_zero_volume_bar_excludes_backtest(self):
        bars=day(full=True);bars[3]['volume']=0
        self.assertEqual(backtest_orb(bars)['sessions'][0]['reason'],'ZERO_VOLUME_BAR')
    def test_missing_volume_does_not_claim_verified(self):
        bars=day();bars[0]['volume']=None
        r=orb_signal(bars,as_of='2026-10-28T09:40:00+05:30')
        self.assertEqual(r['status'],'TRIGGERED_LONG')
        self.assertTrue(r['volume_missing_in_some_bars'])
    def test_plan_tick_must_be_explicit(self):
        with self.assertRaisesRegex(ValueError, "tick is mandatory"):
            plan(100,95)
    def test_backtest_cutoff_excludes_after_1515(self):
        rows=day(full=True)
        # After 15:15 can include CAS/non-standard bars; replay must ignore them.
        rows[72].update(open=100,high=99999,low=0.1,close=100)
        a=backtest_orb(rows)
        self.assertEqual(a['simulated_trades'],1)
        self.assertEqual(a['sessions'][0]['reason'],'STOP')
    def test_two_full_sessions(self):
        a=day('2026-10-27',full=True,trigger=False)
        b=day('2026-10-28',full=True,trigger=True)
        result=backtest_orb(a+b)
        self.assertEqual(result['data_sessions'],2)
        self.assertEqual(result['simulated_trades'],1)
    def test_replay_does_not_retroactively_buy_signal_close(self):
        r=backtest_orb(day(full=True))['sessions'][0]
        self.assertNotEqual(r['signal_bar_start'],r['entry_time'])

    def test_published_nse_holiday_cannot_become_a_replay_trade(self):
        result=backtest_orb(day('2026-10-20',full=True))
        self.assertEqual(result['simulated_trades'],0)
        self.assertEqual(result['sessions'][0]['reason'],'NSE_2026_PUBLISHED_HOLIDAY')

    def test_boolean_reward_multiple_is_not_a_strategy_parameter(self):
        with self.assertRaises(ValueError):
            backtest_orb(day(full=True),reward_r=True)

if __name__=='__main__':unittest.main()
