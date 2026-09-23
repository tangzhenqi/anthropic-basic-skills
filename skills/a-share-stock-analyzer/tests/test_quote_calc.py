"""quote.py 纯算术工具测试: 历史分位、近N日收益、均线多空排列、均值。
止损/目标价位与情绪温度计的'锚'都建在这几只函数上, 不能静默漂。无网络。"""
import unittest

import _path  # noqa: F401
import quote


class TestPctl(unittest.TestCase):
    def test_basic(self):
        # today=5 高于 [1,2,3,4] 中全部 4 个, 含自身共 5 个 -> 4/5=80%
        self.assertEqual(quote._pctl([1, 2, 3, 4, 5], 5), 80.0)

    def test_lowest(self):
        self.assertEqual(quote._pctl([1, 2, 3, 4, 5], 1), 0.0)

    def test_empty_or_none(self):
        self.assertIsNone(quote._pctl([], 5))
        self.assertIsNone(quote._pctl([1, 2], None))


class TestRet(unittest.TestCase):
    def test_basic_return(self):
        # 近2日: 今收 110 / 2日前 100 - 1 = 10%
        self.assertEqual(quote._ret([100, 105, 110], 2), 10.0)

    def test_insufficient_data(self):
        self.assertIsNone(quote._ret([100, 110], 5))

    def test_zero_base(self):
        self.assertIsNone(quote._ret([0, 110], 1))


class TestMaTrend(unittest.TestCase):
    def test_bull_alignment(self):
        d, why = quote.ma_trend(110, 100, 90)
        self.assertEqual(d, "偏多")

    def test_bear_alignment(self):
        d, why = quote.ma_trend(90, 100, 110)
        self.assertEqual(d, "偏空")

    def test_mixed_is_range(self):
        d, why = quote.ma_trend(100, 110, 90)  # 价在两均线之间
        self.assertNotIn(d, ("偏多", "偏空"))

    def test_missing_data(self):
        d, why = quote.ma_trend(None, 100, 90)
        self.assertIsNone(d)


class TestAvg(unittest.TestCase):
    def test_full(self):
        self.assertEqual(quote._avg([2, 4, 6]), 4)

    def test_last_n(self):
        self.assertEqual(quote._avg([1, 2, 3, 10, 10], 2), 10)

    def test_empty(self):
        self.assertIsNone(quote._avg([]))


if __name__ == "__main__":
    unittest.main()


class TestKlineFailure(unittest.TestCase):
    def test_hint_none_when_ok(self):
        self.assertIsNone(quote.kline_failure_hint({"ok": True}))

    def test_hint_names_throttle_and_reason(self):
        h = quote.kline_failure_hint({"ok": False, "reason": "K线数据不足(新股/停牌/接口空)"})
        self.assertIn("限流", h)
        self.assertIn("重跑", h)
        self.assertIn("接口空", h)  # 原始原因要带出来
        self.assertIn("不是行情信号", h)

    def test_hint_on_missing_kline(self):
        self.assertIn("原因未知", quote.kline_failure_hint(None))

    def test_retry_refetches_once(self):
        calls = []
        orig = quote.fetch_kline
        quote.fetch_kline = lambda secid: calls.append(secid) or {"ok": True, "ma20": 1.0}
        try:
            r = quote.kline_retry({"secid": "0.000066", "kline": {"ok": False}}, wait=0)
        finally:
            quote.fetch_kline = orig
        self.assertEqual(calls, ["0.000066"])
        self.assertTrue(r["kline"]["ok"])

    def test_retry_skips_when_ok(self):
        orig = quote.fetch_kline
        quote.fetch_kline = lambda secid: self.fail("不应重抓")
        try:
            r = quote.kline_retry({"secid": "0.000066", "kline": {"ok": True}}, wait=0)
        finally:
            quote.fetch_kline = orig
        self.assertTrue(r["kline"]["ok"])

    def test_retry_exception_kept_as_failure(self):
        orig = quote.fetch_kline

        def boom(secid):
            raise TimeoutError("timed out")
        quote.fetch_kline = boom
        try:
            r = quote.kline_retry({"secid": "0.000066", "kline": {"ok": False}}, wait=0)
        finally:
            quote.fetch_kline = orig
        self.assertFalse(r["kline"]["ok"])
        self.assertIn("timed out", r["kline"]["reason"])
