"""t_plan.py 做T预案测试: 费用、差价、挂单价挂靠、闸门(方向/防守线/接飞刀)、盘中状态判定。
挂单价直接变成用户的委托, 不能静默漂。无网络。"""
import unittest
from datetime import datetime

import _path  # noqa: F401
import t_plan
from quote import CST

# 实战样本(中国长城 2026-09-23 盘中)
ANCHORS = [("MA20", 14.44), ("MA60", 15.13), ("20日前高", 16.32), ("20日前低", 13.63),
           ("60日前高", 20.40), ("60日前低", 13.11)]
PREV = {"date": "2026-09-22", "high": 16.32, "low": 15.60, "vwap": 16.09}


def _plan(price, state="live", **kw):
    base = dict(vwap=15.50, day_lo=15.23, day_hi=15.72, prev=PREV)
    base.update(kw)
    return t_plan.plan(price, ANCHORS, 2.86, 4000, 2000, state, **base)


class TestFees(unittest.TestCase):
    def test_round_trip(self):
        # 2000股 卖16/买15.7: 佣金 8+7.85, 印花 16, 过户 0.634
        self.assertAlmostEqual(t_plan.fees(2000, 16.0, 15.7), 8 + 7.85 + 16 + 0.634, places=3)

    def test_min_commission(self):
        # 100股 @10: 佣金按最低 5 元计(双边 10), 印花 0.5, 过户 0.02
        self.assertAlmostEqual(t_plan.fees(100, 10, 10), 10.52, places=3)

    def test_min_spread_covers_fees(self):
        ms = t_plan.min_spread(2000, 15.0)
        self.assertAlmostEqual(ms * 2000, 3 * t_plan.fees(2000, 15.0, 15.0))

    def test_target_spread(self):
        self.assertAlmostEqual(t_plan.target_spread(15.0, 2.0, 2000), 15.0 * 0.02 * 0.6)
        self.assertIsNone(t_plan.target_spread(15.0, None, 2000))

    def test_tiny_lot_spread_floored_by_fees(self):
        # 100股低价低波动: 最低佣金摊薄差, 差价须抬到覆盖费用
        self.assertGreater(t_plan.target_spread(3.0, 0.5, 100), 3.0 * 0.005 * 0.6)


class TestIntradayLegs(unittest.TestCase):
    def test_fresh_low_not_entry_support(self):
        # 现价贴今日新低 → 今日低点不算支撑; MA60 在一日振幅外 → 不给正T, 不接飞刀
        o = _plan(15.25)
        self.assertIsNone(o["intraday"]["forward"])
        self.assertIn("不卖", o["action"])

    def test_rebound_off_low_allows_forward(self):
        # 离开今日低点已有距离 → 低点成为经检验支撑, 可挂正T
        o = _plan(15.40)
        f = o["intraday"]["forward"]
        self.assertIsNotNone(f)
        self.assertEqual(f["fail_basis"], "今日低点")
        self.assertLess(f["first_px"], 15.40)
        self.assertGreater(f["second_px"], f["first_px"])

    def test_reverse_sell_above_buy(self):
        r = _plan(15.40)["intraday"]["reverse"]
        self.assertGreater(r["first_px"], r["second_px"])
        self.assertGreater(r["spread"], 0)
        self.assertTrue(r["viable"])
        self.assertAlmostEqual(r["net"], 2000 * r["spread"] - r["fee"])

    def test_reverse_capped_to_reachable(self):
        # 阻力须在'今日低点×(1+一日振幅)'之内
        it = _plan(15.40)["intraday"]
        self.assertLessEqual(it["reverse"]["first_px"], it["r_cap"])

    def test_top_of_range_prefers_reverse(self):
        o = _plan(15.66)
        self.assertIn("倒T", o["action"])


class TestGates(unittest.TestCase):
    def test_bearish_blocks_forward(self):
        o = _plan(15.40, direction="偏空")
        self.assertTrue(o["intraday"].get("forward_blocked"))
        self.assertNotIn("正T", o["action"])

    def test_no_cash_blocks_forward(self):
        self.assertTrue(_plan(15.40, has_cash=False)["intraday"].get("forward_blocked"))

    def test_near_preset_stop_blocks_forward(self):
        o = _plan(15.40, stop=15.10)  # 距 1.95% < 均振幅 2.86%
        self.assertTrue(o["near_defense"])
        self.assertTrue(o["intraday"].get("forward_blocked"))

    def test_broken_stop_no_t(self):
        o = _plan(15.40, stop=15.50)
        self.assertTrue(o["broken_defense"])
        self.assertEqual(o["action"], "不做T")

    def test_auto_stop_is_reference_only(self):
        o = _plan(15.40)
        self.assertFalse(o["near_defense"])
        self.assertIn("参考", o["defense_basis"])

    def test_late_session_no_new_t(self):
        self.assertEqual(_plan(15.40, state="late")["action"], "不开新T")

    def test_no_amplitude_no_levels(self):
        # K线确实不足(上市首日/长期停牌): 不给价位
        o = t_plan.plan(15.40, ANCHORS, None, 4000, 2000, "live", prev=PREV, vwap=15.5)
        self.assertEqual(o["action"], "不做T")
        self.assertIsNone(o["intraday"])
        self.assertEqual(o["warnings"], [])

    def test_kline_throttled_is_not_a_no_t_signal(self):
        # 限流导致日K缺失: 动作必须是'重跑', 不能是像行情结论的'不做T'
        hint = "日K接口未取到(...), 多为东财 push2his 限流"
        o = t_plan.plan(15.40, [(n, None) for n, _ in ANCHORS], None, 4000, 2000, "live",
                        vwap=15.5, day_lo=15.23, day_hi=15.72, kline_hint=hint)
        self.assertEqual(o["action"], "数据未取全, 请重跑")
        self.assertIn(hint, o["warnings"])
        self.assertIsNone(o["intraday"])

    def test_missing_intraday_quote_warned(self):
        o = _plan(15.40, vwap=None, day_lo=None, day_hi=None)
        self.assertTrue(any("今日高/低/均价未取到" in w for w in o["warnings"]))

    def test_off_session_no_intraday_warning(self):
        o = _plan(15.25, state="off", vwap=None, day_lo=None, day_hi=None)
        self.assertEqual(o["warnings"], [])


class TestSwingAndOff(unittest.TestCase):
    def test_swing_sell_high_buy_low(self):
        sw = _plan(15.25)["swing"]
        self.assertEqual(sw["first_basis"], "20日前高略下方")
        self.assertEqual(sw["second_basis"], "MA20略上方")
        self.assertGreater(sw["spread"], 1.0)
        self.assertGreater(sw["net"], 0)

    def test_cost_reduction(self):
        sw = _plan(15.25, cost=19.8)["swing"]
        self.assertAlmostEqual(sw["new_cost"], 19.8 - sw["net"] / 4000)

    def test_off_session_ignores_today_levels(self):
        # 盘外: 不用今日高低/均价(那是未完成K或已成'昨日'), 可及范围以收盘价±一日振幅
        o = _plan(15.25, state="off", vwap=None, day_lo=None, day_hi=None)
        names = {o["intraday"]["reverse"]["first_basis"]} if o["intraday"]["reverse"] else set()
        self.assertFalse(any("今日" in n for n in names))
        self.assertIn("下一交易日", " ".join(o["why"]))


class TestSession(unittest.TestCase):
    def _ts(self, h, m, d=23):
        return datetime(2026, 9, d, h, m, tzinfo=CST)

    def test_states(self):
        self.assertEqual(t_plan.session_state(self._ts(10, 0), self._ts(10, 0)), "live")
        self.assertEqual(t_plan.session_state(self._ts(14, 55), self._ts(14, 55)), "late")
        self.assertEqual(t_plan.session_state(self._ts(15, 5), self._ts(15, 0)), "off")
        self.assertEqual(t_plan.session_state(self._ts(9, 0), self._ts(9, 0)), "off")

    def test_stale_quote_is_off(self):
        # 交易日盘中却只拿到昨日行情 → 不按盘中处理
        self.assertEqual(t_plan.session_state(self._ts(10, 0), self._ts(15, 0, d=22)), "off")

    def test_weekend_off(self):
        sat = datetime(2026, 9, 26, 10, 0, tzinfo=CST)
        self.assertEqual(t_plan.session_state(sat, sat), "off")

    def test_prev_bar(self):
        bars = [{"date": "2026-09-22", "high": 16.32}, {"date": "2026-09-23", "high": 15.72}]
        now = self._ts(10, 0)
        self.assertEqual(t_plan.prev_bar(bars, now, "live")["date"], "2026-09-22")
        self.assertEqual(t_plan.prev_bar(bars, self._ts(15, 30), "off")["date"], "2026-09-23")


if __name__ == "__main__":
    unittest.main()
