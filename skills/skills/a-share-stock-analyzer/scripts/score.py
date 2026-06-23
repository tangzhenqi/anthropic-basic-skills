#!/usr/bin/env python3
"""A股四维综合评分计算器 —— 把 SKILL Step 3 的算术固化成确定性脚本。

为什么要有它:
  Step 3 的"带符号分 × 权重 → 综合分 → 方向映射 → 估值护栏±1档 → ST封顶 →
  某维不计分时按比例重新归一化"全靠模型脑内算, 归一化和调档容易出错、口径会漂。
  本脚本只做算术与映射(确定性), 模型只负责"给每维定方向+强度"(判断)。

输入(模型判断后填):
  - 每维一个带符号分(方向×强度, 范围 -5..+5): 看多为正, 看空为负, 中性为 0。
    强度绝对值=1 视为"信号微弱→不计分"(SKILL rubric), 自动剔除并把权重按比例分摊。
    显式不计分(数据不足/停牌/次新)用 na。
  - 行业权重模板(Step 3.1): 默认/周期/科技成长/消费医药/金融, 或 --weights 自定义。
  - 估值护栏(Step 3.3): --val-pctl 传 valuation.py 的主分位; >=90 下调一档, <=10 上调一档。
  - --major-risk: 重大单点风险(停牌/业绩暴雷/监管处罚)下调一档。
  - --st: ST/*ST 标的, 看多侧封顶为"中性偏多"(不得给偏多/强烈看多)。

用法:
    python3 score.py --template 消费医药 --funds +4 --sentiment 0 --intl -2 --econ +3
    python3 score.py --template 科技成长 --funds +3 --sentiment +2 --intl na --econ +2 --val-pctl 95
    python3 score.py --weights 30,20,20,30 --funds -4 --sentiment -3 --intl -2 --econ -3 --st
    python3 score.py --json ...        # JSON 输出(供 analyze.py / 自动化消费)

退出码: 0 正常; 2 输入非法(如四维全 na)。
"""

import sys
import json
import argparse

DIMS = ("funds", "sentiment", "intl", "econ")
DIM_CN = {"funds": "资金流量", "sentiment": "市场情绪", "intl": "国际形势", "econ": "经济形势"}

# Step 3.1 行业权重模板(资金, 情绪, 国际, 经济), 每行合计 100
TEMPLATES = {
    "默认": (30, 20, 20, 30),
    "周期": (25, 15, 30, 30),
    "科技成长": (25, 20, 30, 25),
    "消费医药": (25, 20, 15, 40),
    "金融": (30, 15, 15, 40),
}
TEMPLATE_ALIASES = {
    "均衡": "默认", "default": "默认", "balanced": "默认",
    "周期股": "周期", "cyclical": "周期",
    "科技": "科技成长", "成长": "科技成长", "tech": "科技成长", "growth": "科技成长",
    "消费": "消费医药", "医药": "消费医药", "consumer": "消费医药",
    "银行": "金融", "券商": "金融", "保险": "金融", "finance": "金融",
}

# 综合分 → 方向(7 档, 升序)。带符号分区间 [lo, hi) 用 >= 一致切分。
LABELS = ["强烈看空", "偏空", "中性偏空", "中性", "中性偏多", "偏多", "强烈看多"]
BULL_CAP_LEVEL = 4  # ST/*ST 看多侧封顶 = "中性偏多"


def map_level(score):
    """带符号综合分(-5..+5) → 方向档位索引(0..6)。"""
    if score >= 3.0:
        return 6
    if score >= 1.5:
        return 5
    if score >= 0.5:
        return 4
    if score >= -0.5:
        return 3
    if score >= -1.5:
        return 2
    if score >= -3.0:
        return 1
    return 0


def parse_dim(s):
    """解析单维输入 -> (signed_or_None, note)。
    na/none/skip/'-' -> 不计分; 整数 -5..5; |强度|=1 自动转不计分(rubric)。"""
    t = s.strip().lower()
    if t in ("na", "none", "skip", "-", ""):
        return None, "不计分(显式: 数据不足/停牌/次新)"
    try:
        v = int(s)
    except ValueError:
        raise ValueError(f"非法维度分 {s!r}(应为 -5..5 的整数或 na)")
    if not -5 <= v <= 5:
        raise ValueError(f"维度分越界 {v}(应在 -5..+5)")
    if abs(v) == 1:
        return None, "强度1→不计分(信号微弱, rubric 要求)"
    return v, None


def resolve_template(name):
    if not name:
        return "默认", TEMPLATES["默认"]
    key = TEMPLATE_ALIASES.get(name.strip().lower(), name.strip())
    if key in TEMPLATES:
        return key, TEMPLATES[key]
    raise ValueError(f"未知行业模板 {name!r}; 可选: {list(TEMPLATES)} 或 --weights a,b,c,d")


def compute(signed, weights, val_pctl=None, major_risk=False, st=False):
    """核心计算。signed: {dim: int|None}; weights: 四维基础权重(合计100)。
    返回综合分、归一化后有效权重、方向档位与各项调档。"""
    scored = [d for d in DIMS if signed.get(d) is not None]
    if not scored:
        raise ValueError("四维全部不计分, 无法给出方向(请至少保留一维)")
    base_w = dict(zip(DIMS, weights))
    w_sum = sum(base_w[d] for d in scored)  # 不计分维度的权重按比例分摊给其余

    rows = []
    composite = 0.0
    for d in DIMS:
        sv = signed.get(d)
        if sv is None:
            rows.append({"dim": d, "signed": None, "base_w": base_w[d],
                         "eff_w": None, "contrib": None})
            continue
        eff = base_w[d] / w_sum  # 0..1
        contrib = eff * sv
        composite += contrib
        rows.append({"dim": d, "signed": sv, "base_w": base_w[d],
                     "eff_w": round(eff * 100, 1), "contrib": round(contrib, 3)})

    composite = round(composite, 3)
    base_level = map_level(composite)
    level = base_level
    adjustments = []

    # 估值护栏(±1 档)
    guardrail = None
    if val_pctl is not None:
        if val_pctl >= 90:
            level -= 1
            guardrail = f"估值极高分位({val_pctl:g}%>90)→下调一档"
            adjustments.append(guardrail)
        elif val_pctl <= 10:
            level += 1
            guardrail = f"估值极低分位({val_pctl:g}%<10)→上调一档"
            adjustments.append(guardrail)
        else:
            guardrail = f"估值分位({val_pctl:g}%)处中间, 不调档"

    # 重大单点风险(−1 档)
    if major_risk:
        level -= 1
        adjustments.append("重大单点风险→下调一档")

    level = max(0, min(6, level))  # 钳到合法档位

    # ST/*ST 看多侧封顶为"中性偏多"
    st_capped = False
    if st and level > BULL_CAP_LEVEL:
        level = BULL_CAP_LEVEL
        st_capped = True
        adjustments.append("ST/*ST→看多侧封顶为'中性偏多'")

    return {
        "composite": composite,
        "base_level": base_level,
        "base_direction": LABELS[base_level],
        "final_level": level,
        "final_direction": LABELS[level],
        "rows": rows,
        "scored_dims": scored,
        "adjustments": adjustments,
        "guardrail": guardrail,
        "major_risk": bool(major_risk),
        "st_capped": st_capped,
    }


def print_human(res, template_name):
    print(f"行业模板: {template_name}  (权重已对不计分维度重新归一化)")
    print("-" * 64)
    print(f"{'维度':<10}{'带符号分':>8}{'基础权重':>10}{'有效权重':>10}{'加权分':>10}")
    for r in res["rows"]:
        cn = DIM_CN[r["dim"]]
        if r["signed"] is None:
            print(f"{cn:<10}{'不计分':>8}{r['base_w']:>9}%{'—':>10}{'—':>10}")
        else:
            print(f"{cn:<10}{r['signed']:>+8}{r['base_w']:>9}%{r['eff_w']:>9}%{r['contrib']:>+10.3f}")
    print("-" * 64)
    print(f"综合分: {res['composite']:+.3f}  →  基础方向: {res['base_direction']}")
    for a in res["adjustments"]:
        print(f"  调档: {a}")
    if res["guardrail"] and not res["adjustments"]:
        print(f"  护栏: {res['guardrail']}")
    if res["final_direction"] != res["base_direction"]:
        print(f"最终方向: {res['final_direction']}  (经调档)")
    else:
        print(f"最终方向: {res['final_direction']}")
    print()
    print("可直接粘进报告的信号矩阵(填入各维关键发现即可):")
    print("| 维度 | 当前信号 | 方向 | 强度 | 带符号分 | 有效权重 | 加权分 |")
    print("|------|---------|------|------|--------|--------|------|")
    for r in res["rows"]:
        cn = DIM_CN[r["dim"]]
        if r["signed"] is None:
            print(f"| {cn} | [关键发现] | — | 不计分 | — | — | — |")
        else:
            d = "+" if r["signed"] > 0 else ("−" if r["signed"] < 0 else "0")
            print(f"| {cn} | [关键发现] | {d} | {abs(r['signed'])} | {r['signed']:+d} "
                  f"| {r['eff_w']:g}% | {r['contrib']:+.3f} |")
    print(f"\n综合分 {res['composite']:+.3f}, 方向「{res['final_direction']}」"
          f"（行业模板 {template_name}"
          + (f"; {'; '.join(res['adjustments'])}" if res['adjustments'] else "; 未触发护栏/封顶") + "）")


def main():
    ap = argparse.ArgumentParser(description="A股四维综合评分计算器(Step 3 算术固化)")
    ap.add_argument("--template", help="行业权重模板: 默认/周期/科技成长/消费医药/金融")
    ap.add_argument("--weights", help="自定义四维权重 资金,情绪,国际,经济 (合计100), 覆盖 --template")
    for d in DIMS:
        ap.add_argument(f"--{d}", required=True,
                        help=f"{DIM_CN[d]}带符号分 -5..5(看多+/看空-/中性0), 或 na 不计分")
    ap.add_argument("--val-pctl", type=float, default=None,
                    help="估值主分位(valuation.py 的 guardrail_pctl); >=90下调/<=10上调一档")
    ap.add_argument("--major-risk", action="store_true", help="重大单点风险, 下调一档")
    ap.add_argument("--st", action="store_true", help="ST/*ST, 看多侧封顶为中性偏多")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()

    try:
        if args.weights:
            parts = [float(x) for x in args.weights.split(",")]
            if len(parts) != 4:
                raise ValueError("--weights 需 4 个数: 资金,情绪,国际,经济")
            template_name, weights = f"自定义{tuple(parts)}", tuple(parts)
        else:
            template_name, weights = resolve_template(args.template)

        signed, notes = {}, {}
        for d in DIMS:
            v, note = parse_dim(getattr(args, d))
            signed[d] = v
            if note:
                notes[d] = note

        res = compute(signed, weights, val_pctl=args.val_pctl,
                      major_risk=args.major_risk, st=args.st)
    except ValueError as e:
        print(f"✗ 输入错误: {e}", file=sys.stderr)
        sys.exit(2)

    res["template"] = template_name
    res["dim_notes"] = notes

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"抓取/计算无网络依赖, 纯算术。")
        print("=" * 64)
        for d, n in notes.items():
            print(f"  注: {DIM_CN[d]} {n}")
        print_human(res, template_name)
    sys.exit(0)


if __name__ == "__main__":
    main()
