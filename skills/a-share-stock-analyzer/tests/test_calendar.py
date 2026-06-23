"""trading_calendar.py 测试: 交易日判定、最近/上一交易日、权威区间、到期天数。
freshness 直接决定一条数据'能否写入报告', 日历判错 = 误用滞后数据或误弃合法数据。无网络。"""
import unittest
from datetime import date, datetime

import _path  # noqa: F401
import trading_calendar as cal


class TestIsTradingDay(unittest.TestCase):
    def test_normal_weekday(self):
        # 2025-09-30 周二, 非节假日 -> 交易日
        self.assertTrue(cal.is_trading_day(date(2025, 9, 30)))

    def test_weekend(self):
        self.assertFalse(cal.is_trading_day(date(2025, 10, 4)))  # 周六

    def test_holiday(self):
        self.assertFalse(cal.is_trading_day(date(2025, 10, 1)))  # 国庆
        self.assertFalse(cal.is_trading_day(date(2024, 2, 12)))  # 春节

    def test_out_of_range_returns_none(self):
        self.assertIsNone(cal.is_trading_day(date(2030, 1, 1)))


class TestRecentTradingDay(unittest.TestCase):
    def test_during_holiday_goes_back(self):
        # 国庆周六 10-04, 往回最近交易日是 09-30(10-01~03 节假日, 10-04 周六)
        self.assertEqual(cal.recent_trading_day(datetime(2025, 10, 4, 12, 0, tzinfo=cal.CST)),
                         date(2025, 9, 30))

    def test_trading_day_returns_self(self):
        self.assertEqual(cal.recent_trading_day(datetime(2025, 9, 30, 15, 0, tzinfo=cal.CST)),
                         date(2025, 9, 30))

    def test_out_of_range_none(self):
        self.assertIsNone(cal.recent_trading_day(datetime(2030, 1, 1, tzinfo=cal.CST)))


class TestPrevTradingDay(unittest.TestCase):
    def test_excludes_self(self):
        # 严格早于 10-01 的最近交易日是 09-30
        self.assertEqual(cal.prev_trading_day(date(2025, 10, 1)), date(2025, 9, 30))

    def test_from_trading_day(self):
        # 09-30 周二的前一交易日是 09-29 周一
        self.assertEqual(cal.prev_trading_day(date(2025, 9, 30)), date(2025, 9, 29))


class TestInRangeAndExpiry(unittest.TestCase):
    def test_in_range(self):
        self.assertTrue(cal.in_range(date(2026, 6, 1)))
        self.assertFalse(cal.in_range(date(2027, 1, 1)))

    def test_days_until_expiry_positive(self):
        d = cal.days_until_expiry(datetime(2026, 11, 1, tzinfo=cal.CST))
        self.assertEqual(d, (cal.CALENDAR_VALID_THROUGH - date(2026, 11, 1)).days)
        self.assertGreater(d, 0)

    def test_days_until_expiry_expired(self):
        d = cal.days_until_expiry(datetime(2027, 2, 1, tzinfo=cal.CST))
        self.assertLess(d, 0)

    def test_valid_through_year_end(self):
        # 维护约定: CALENDAR_VALID_THROUGH 应落在某年 12-31
        self.assertEqual((cal.CALENDAR_VALID_THROUGH.month, cal.CALENDAR_VALID_THROUGH.day), (12, 31))


if __name__ == "__main__":
    unittest.main()
