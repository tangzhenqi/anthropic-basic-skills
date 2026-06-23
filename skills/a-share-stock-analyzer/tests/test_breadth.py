"""breadth.py 纯逻辑测试: 涨停/跌停家数 -> 打板情绪分档(era-robust, 不依赖成交额绝对值)。
情绪分档进入维度二, 判错会让'市场偏暖/恐慌'结论失真。无网络。"""
import unittest

import _path  # noqa: F401
import breadth


class TestReadLimitSentiment(unittest.TestCase):
    def test_warm_when_many_up_few_down(self):
        s = breadth.read_limit_sentiment(50, 3)
        self.assertIsNotNone(s)
        self.assertIn("偏暖", s)
        self.assertIn("🔥", s)

    def test_warm_threshold_boundary(self):
        # 涨停恰到 ZT_WARM 且 >= 2*跌停 -> 偏暖
        s = breadth.read_limit_sentiment(breadth.ZT_WARM, 1)
        self.assertIn("偏暖", s)

    def test_not_warm_if_up_below_threshold(self):
        # 涨停虽多于跌停, 但未达 ZT_WARM -> 不判偏暖
        s = breadth.read_limit_sentiment(breadth.ZT_WARM - 1, 0)
        self.assertNotIn("偏暖", s)
        self.assertIn("中性", s)

    def test_panic_when_many_down(self):
        s = breadth.read_limit_sentiment(5, 20)
        self.assertIn("恐慌", s)
        self.assertIn("❄️", s)

    def test_panic_threshold_boundary(self):
        # 跌停达 DT_PANIC 且 >= 涨停 -> 恐慌
        s = breadth.read_limit_sentiment(DT := breadth.DT_PANIC, breadth.DT_PANIC)
        self.assertIn("恐慌", s)

    def test_neutral_mixed(self):
        s = breadth.read_limit_sentiment(10, 8)
        self.assertIn("中性", s)
        self.assertIn("🌡️", s)

    def test_net_count_in_message(self):
        s = breadth.read_limit_sentiment(40, 5)
        self.assertIn("+35", s)  # 净涨停 = 40-5

    def test_missing_data_returns_none(self):
        self.assertIsNone(breadth.read_limit_sentiment(None, 3))
        self.assertIsNone(breadth.read_limit_sentiment(50, None))


if __name__ == "__main__":
    unittest.main()
