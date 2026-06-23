"""events.py 纯逻辑测试: 日期分类、距今天数、临近事件提炼、业绩预告时效门。
事件'未来 vs 已发生'判错会让解禁抛压/除权日提示失真; 陈旧预告若当前瞻信号会误导。无网络。"""
import unittest
from datetime import date

import _path  # noqa: F401
import events


TODAY = date(2026, 6, 12)


class TestClassify(unittest.TestCase):
    def test_future_is_upcoming(self):
        self.assertEqual(events.classify("2026-08-01 00:00:00", TODAY), "upcoming")

    def test_today_is_upcoming(self):
        self.assertEqual(events.classify("2026-06-12 00:00:00", TODAY), "upcoming")

    def test_past(self):
        self.assertEqual(events.classify("2025-01-01 00:00:00", TODAY), "past")

    def test_invalid(self):
        self.assertIsNone(events.classify(None, TODAY))
        self.assertIsNone(events.classify("", TODAY))


class TestDaysUntil(unittest.TestCase):
    def test_positive(self):
        self.assertEqual(events.days_until("2026-06-22 00:00:00", TODAY), 10)

    def test_negative(self):
        self.assertEqual(events.days_until("2026-06-02 00:00:00", TODAY), -10)

    def test_invalid(self):
        self.assertIsNone(events.days_until("bad", TODAY))


class TestHighlights(unittest.TestCase):
    def test_big_lockup_soon_flagged(self):
        res = {"lockup": {"upcoming": [
            {"date": "2026-07-01", "days_until": 19, "free_ratio": 8.0,
             "market_cap": 5e9, "type": "首发原股东限售"}]}}
        hl = events.highlights(res, TODAY)
        self.assertTrue(any("解禁" in h and "大比例抛压" in h for h in hl))

    def test_small_lockup_soon_flagged_without_bigtag(self):
        res = {"lockup": {"upcoming": [
            {"date": "2026-07-01", "days_until": 19, "free_ratio": 0.5,
             "market_cap": 1e8, "type": "定增"}]}}
        hl = events.highlights(res, TODAY)
        self.assertTrue(any("解禁" in h for h in hl))
        self.assertFalse(any("大比例抛压" in h for h in hl))

    def test_far_lockup_not_flagged(self):
        res = {"lockup": {"upcoming": [
            {"date": "2027-01-01", "days_until": 203, "free_ratio": 8.0,
             "market_cap": 5e9, "type": "定增"}]}}
        self.assertEqual(events.highlights(res, TODAY), [])

    def test_dividend_record_soon_flagged(self):
        res = {"dividend": {"upcoming": [
            {"record_date": "2026-06-20", "record_days_until": 8,
             "ex_date": "2026-06-21", "ex_days_until": 9, "pretax_per_10": 5.0}]}}
        hl = events.highlights(res, TODAY)
        self.assertTrue(any("股权登记" in h for h in hl))
        self.assertTrue(any("除权除息" in h for h in hl))

    def test_fresh_bull_forecast_flagged(self):
        res = {"forecast": {"latest": {"predict_type": "预增", "forward": True,
                                       "report_date": "2026-03-31",
                                       "amp_lower": 50.0, "amp_upper": 80.0}}}
        hl = events.highlights(res, TODAY)
        self.assertTrue(any(h.startswith("📈") and "预增" in h for h in hl))

    def test_stale_forecast_not_flagged(self):
        # forward=False 的陈旧预告不进高亮
        res = {"forecast": {"latest": {"predict_type": "预增", "forward": False,
                                       "report_date": "2024-12-31",
                                       "amp_lower": 50.0, "amp_upper": 80.0}}}
        self.assertEqual(events.highlights(res, TODAY), [])

    def test_bear_forecast_uses_down_icon(self):
        res = {"forecast": {"latest": {"predict_type": "首亏", "forward": True,
                                       "report_date": "2026-03-31",
                                       "amp_lower": -200.0, "amp_upper": -150.0}}}
        hl = events.highlights(res, TODAY)
        self.assertTrue(any(h.startswith("📉") for h in hl))

    def test_empty_when_nothing(self):
        self.assertEqual(events.highlights({}, TODAY), [])


if __name__ == "__main__":
    unittest.main()
