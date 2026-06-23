"""review.py 命中判定测试: 看多/看空/中性 × 带宽边界。复盘命中率的判定口径不能漂。"""
import unittest

import _path  # noqa: F401
import review


class TestJudge(unittest.TestCase):
    BAND = 3.0

    def test_bull_hit_requires_above_band(self):
        self.assertEqual(review.judge(1, 5.0, self.BAND), "hit")    # 涨超带宽
        self.assertEqual(review.judge(1, 3.0, self.BAND), "miss")   # 恰好等于带宽不算(严格 >)
        self.assertEqual(review.judge(1, 1.0, self.BAND), "miss")   # 涨幅在噪声区
        self.assertEqual(review.judge(1, -5.0, self.BAND), "miss")  # 看多却跌

    def test_bear_hit_requires_below_neg_band(self):
        self.assertEqual(review.judge(-1, -5.0, self.BAND), "hit")
        self.assertEqual(review.judge(-1, -3.0, self.BAND), "miss")  # 严格 <
        self.assertEqual(review.judge(-1, -1.0, self.BAND), "miss")
        self.assertEqual(review.judge(-1, 5.0, self.BAND), "miss")

    def test_neutral_hit_within_band(self):
        self.assertEqual(review.judge(0, 0.0, self.BAND), "hit")
        self.assertEqual(review.judge(0, 3.0, self.BAND), "hit")    # 边界含等于
        self.assertEqual(review.judge(0, -3.0, self.BAND), "hit")
        self.assertEqual(review.judge(0, 4.0, self.BAND), "miss")
        self.assertEqual(review.judge(0, -4.0, self.BAND), "miss")

    def test_custom_band(self):
        self.assertEqual(review.judge(1, 2.0, 1.0), "hit")   # 带宽1时2%算命中
        self.assertEqual(review.judge(1, 2.0, 5.0), "miss")  # 带宽5时2%在噪声区


if __name__ == "__main__":
    unittest.main()
