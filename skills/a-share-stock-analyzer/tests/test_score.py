"""score.py 确定性内核测试: 维度解析、模板解析、方向映射、综合分/归一化/护栏/封顶。

这些是报告结论的算术地基(权重重归一化、估值护栏±1档、ST封顶), 改一行就可能静默漂口径,
而 selftest.py 只验联网接口、不验这些纯算术。故离线断言锁住口径。"""
import unittest

import _path  # noqa: F401 - 注入 scripts 到 sys.path
import score


class TestParseDim(unittest.TestCase):
    def test_na_variants_are_not_scored(self):
        for s in ("na", "NA", "none", "skip", "-", ""):
            v, note = score.parse_dim(s)
            self.assertIsNone(v, f"{s!r} 应判不计分")
            self.assertIn("不计分", note)

    def test_strength_one_auto_demoted(self):
        # |强度|=1 按 rubric 自动转不计分
        for s in ("1", "+1", "-1"):
            v, note = score.parse_dim(s)
            self.assertIsNone(v)
            self.assertIn("不计分", note)

    def test_valid_signed_scores(self):
        self.assertEqual(score.parse_dim("+4"), (4, None))
        self.assertEqual(score.parse_dim("-3"), (-3, None))
        self.assertEqual(score.parse_dim("0"), (0, None))
        self.assertEqual(score.parse_dim("5")[0], 5)

    def test_out_of_range_raises(self):
        for s in ("6", "-6", "10"):
            with self.assertRaises(ValueError):
                score.parse_dim(s)

    def test_non_integer_raises(self):
        with self.assertRaises(ValueError):
            score.parse_dim("abc")


class TestResolveTemplate(unittest.TestCase):
    def test_default_when_empty(self):
        name, w = score.resolve_template(None)
        self.assertEqual(name, "默认")
        self.assertEqual(sum(w), 100)

    def test_aliases(self):
        self.assertEqual(score.resolve_template("成长")[0], "科技成长")
        self.assertEqual(score.resolve_template("银行")[0], "金融")
        self.assertEqual(score.resolve_template("medicine") if False else score.resolve_template("医药")[0], "消费医药")

    def test_all_templates_sum_100(self):
        for name, w in score.TEMPLATES.items():
            self.assertEqual(sum(w), 100, f"模板 {name} 权重未合计 100")

    def test_unknown_raises(self):
        with self.assertRaises(ValueError):
            score.resolve_template("不存在的模板")


class TestMapLevel(unittest.TestCase):
    def test_boundaries(self):
        # 升序档位边界 (>= 切分): -3,-1.5,-0.5,0.5,1.5,3
        self.assertEqual(score.map_level(3.0), 6)   # 强烈看多
        self.assertEqual(score.map_level(2.99), 5)  # 偏多
        self.assertEqual(score.map_level(1.5), 5)
        self.assertEqual(score.map_level(0.5), 4)   # 中性偏多
        self.assertEqual(score.map_level(0.0), 3)   # 中性
        self.assertEqual(score.map_level(-0.5), 3)
        self.assertEqual(score.map_level(-0.51), 2)  # 中性偏空
        self.assertEqual(score.map_level(-1.5), 2)
        self.assertEqual(score.map_level(-1.51), 1)  # 偏空
        self.assertEqual(score.map_level(-3.0), 1)
        self.assertEqual(score.map_level(-3.01), 0)  # 强烈看空


class TestCompute(unittest.TestCase):
    W = (30, 20, 20, 30)  # 默认模板

    def test_all_na_raises(self):
        with self.assertRaises(ValueError):
            score.compute({"funds": None, "sentiment": None, "intl": None, "econ": None}, self.W)

    def test_simple_weighted(self):
        # 全部计分: 0.3*4 + 0.2*0 + 0.2*(-2) + 0.3*2 = 1.2 - 0.4 + 0.6 = 1.4
        r = score.compute({"funds": 4, "sentiment": 0, "intl": -2, "econ": 2}, self.W)
        self.assertAlmostEqual(r["composite"], 1.4, places=3)
        self.assertEqual(r["final_direction"], "中性偏多")  # 0.5~1.5

    def test_renormalize_when_one_na(self):
        # intl 不计分 -> 权重 30/20/_/30 = 80, 重新归一化
        r = score.compute({"funds": 4, "sentiment": 0, "intl": None, "econ": 2}, self.W)
        # eff: funds 30/80=.375, sent 20/80=.25, econ 30/80=.375
        expect = 0.375 * 4 + 0.25 * 0 + 0.375 * 2  # = 1.5 + 0.75 = 2.25
        self.assertAlmostEqual(r["composite"], round(expect, 3), places=3)
        # 不计分维度有效权重为 None
        intl_row = next(x for x in r["rows"] if x["dim"] == "intl")
        self.assertIsNone(intl_row["eff_w"])
        # 有效权重合计应为 100
        eff_sum = sum(x["eff_w"] for x in r["rows"] if x["eff_w"] is not None)
        self.assertAlmostEqual(eff_sum, 100.0, places=0)

    def test_guardrail_high_pctl_downgrades(self):
        base = score.compute({"funds": 4, "sentiment": 4, "intl": 4, "econ": 4}, self.W)
        capped = score.compute({"funds": 4, "sentiment": 4, "intl": 4, "econ": 4},
                               self.W, val_pctl=95)
        self.assertEqual(capped["final_level"], base["final_level"] - 1)
        self.assertTrue(any("下调" in a for a in capped["adjustments"]))

    def test_guardrail_low_pctl_upgrades(self):
        base = score.compute({"funds": 2, "sentiment": 0, "intl": 0, "econ": 2}, self.W)
        up = score.compute({"funds": 2, "sentiment": 0, "intl": 0, "econ": 2},
                           self.W, val_pctl=5)
        self.assertEqual(up["final_level"], min(6, base["final_level"] + 1))

    def test_guardrail_mid_no_change(self):
        r = score.compute({"funds": 2, "sentiment": 0, "intl": 0, "econ": 2}, self.W, val_pctl=50)
        self.assertEqual(r["final_level"], r["base_level"])
        self.assertIn("不调档", r["guardrail"])

    def test_major_risk_downgrades(self):
        base = score.compute({"funds": 4, "sentiment": 4, "intl": 4, "econ": 4}, self.W)
        risk = score.compute({"funds": 4, "sentiment": 4, "intl": 4, "econ": 4},
                             self.W, major_risk=True)
        self.assertEqual(risk["final_level"], base["final_level"] - 1)

    def test_st_caps_bull_side(self):
        # 强烈看多 -> ST 封顶为"中性偏多"(level 4)
        r = score.compute({"funds": 5, "sentiment": 5, "intl": 5, "econ": 5}, self.W, st=True)
        self.assertEqual(r["final_level"], score.BULL_CAP_LEVEL)
        self.assertEqual(r["final_direction"], "中性偏多")
        self.assertTrue(r["st_capped"])

    def test_st_does_not_lift_bear_side(self):
        # 看空侧 ST 不影响
        r = score.compute({"funds": -4, "sentiment": -4, "intl": -4, "econ": -4}, self.W, st=True)
        self.assertFalse(r["st_capped"])
        self.assertEqual(r["final_direction"], "强烈看空")

    def test_level_clamped(self):
        # 低分位上调但已封顶在 6
        r = score.compute({"funds": 5, "sentiment": 5, "intl": 5, "econ": 5}, self.W, val_pctl=5)
        self.assertLessEqual(r["final_level"], 6)


if __name__ == "__main__":
    unittest.main()
