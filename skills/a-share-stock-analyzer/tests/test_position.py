"""position.py 止损挂靠与新钱视角测试: 止损须'结构位 + 盖得住正常波动', 加仓评估须相对买价重挂。
价位直接影响用户止损/加仓决策, 不能静默漂。无网络。"""
import unittest

import _path  # noqa: F401
import position

# 实战样本(中国长城 2026-09-23 盘中): MA60 距现价 1.76%, 近20日均振幅 2.78%
ANCHORS = [("MA20", 14.44), ("MA60", 15.13), ("20日前高", 16.32), ("20日前低", 13.63),
           ("60日前高", 20.40), ("60日前低", 13.11)]


class TestStopBuffer(unittest.TestCase):
    def test_amplitude_widens_buffer(self):
        self.assertAlmostEqual(position.stop_buffer_pct(2.78), 2.78)

    def test_floor_for_low_volatility(self):
        # 低振幅票不低于固定下限
        self.assertEqual(position.stop_buffer_pct(0.8), position.ANCHOR_BUFFER_PCT)

    def test_missing_amplitude_falls_back(self):
        self.assertEqual(position.stop_buffer_pct(None), position.ANCHOR_BUFFER_PCT)

    def test_multiplier(self):
        self.assertAlmostEqual(position.stop_buffer_pct(2.0, mult=1.5), 3.0)


class TestSuggestStop(unittest.TestCase):
    def test_near_anchor_widened_not_skipped(self):
        # MA60 在 2.78% 缓冲内 → 以 MA60 为依据下移到 现价×(1-2.78%), 不跳到 MA20
        stop, basis, widened = position.suggest_stop(15.40, ANCHORS, 2.78)
        self.assertTrue(widened)
        self.assertEqual(basis, "MA60")
        self.assertEqual(stop, 14.97)
        self.assertLess(stop, 15.13)

    def test_far_anchor_used_directly(self):
        # 最近支撑已在缓冲外 → 直接挂靠
        stop, basis, widened = position.suggest_stop(15.40, ANCHORS, 1.5)
        self.assertEqual((stop, basis, widened), (15.13, "MA60", False))

    def test_widened_stop_stays_outside_buffer(self):
        # 向下取到分: 止损距现价不得因四舍五入落回缓冲内
        for price in (15.35, 15.40, 15.4765, 14.60):
            stop, _, widened = position.suggest_stop(price, ANCHORS, 2.78)
            if widened:
                self.assertGreaterEqual((price - stop) / price * 100, 2.78 - 1e-9)

    def test_lowest_near_anchor_is_basis(self):
        anchors = [("A", 9.95), ("B", 9.85), ("C", 9.0)]
        stop, basis, widened = position.suggest_stop(10.0, anchors, 3.0)
        self.assertEqual((stop, basis, widened), (9.7, "B", True))

    def test_no_anchor_below(self):
        self.assertEqual(position.suggest_stop(10.0, [("MA20", 11.0)], 2.0), (None, None, False))


class TestBuild(unittest.TestCase):
    def _build(self, **kw):
        return position.build("sz000066", 4000, 19.8, 15.40, ANCHORS, True,
                              amp_avg20=2.78, **kw)

    def test_auto_stop_uses_amplitude(self):
        o = self._build()
        self.assertEqual(o["stop"], 14.97)
        self.assertTrue(o["stop_widened"])
        self.assertFalse(o["rr_distorted"])

    def test_tight_manual_stop_flagged(self):
        # 手填止损在正常波动内 → R:R 标失真
        o = self._build(stop=15.20)
        self.assertTrue(o["rr_distorted"])

    def test_new_money_rebased_to_add_price(self):
        # 加仓价 14.60 低于现价口径止损 → 新钱止损须相对买价重挂(MA20 下移), 目标取买价上方最近阻力
        o = self._build(add_shares=2000, add_price=14.60)
        a = o["add"]
        self.assertLess(a["nm_stop"], 14.60)
        self.assertEqual(a["nm_stop_basis"], "MA20")
        self.assertTrue(a["nm_stop_widened"])
        self.assertEqual(a["nm_target"], 15.13)
        self.assertLess(a["nm_stop_pnl"], 0)
        self.assertGreater(a["nm_target_pnl"], 0)
        self.assertIsNotNone(a["nm_rr"])

    def test_manual_stop_above_add_price_invalid(self):
        o = self._build(stop=15.0, add_shares=2000, add_price=14.60)
        a = o["add"]
        self.assertTrue(a["nm_stop_invalid"])
        self.assertIsNone(a["nm_stop_pnl"])
        self.assertIsNone(a["nm_rr"])

    def test_no_kline_leaves_levels_empty(self):
        o = position.build("sz000066", 4000, 19.8, 15.40, ANCHORS, False, amp_avg20=2.78)
        self.assertIsNone(o["stop"])
        self.assertIsNone(o["target"])


if __name__ == "__main__":
    unittest.main()
