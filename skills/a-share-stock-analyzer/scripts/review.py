#!/usr/bin/env python3
"""复盘: 读 record.py 留下的预测, 拉之后实际涨跌, 统计方向命中率。

为什么要有它:
  留痕(record.py)只是记账; 真正的闭环是回头看"判得准不准"。本脚本对每条历史预测
  抓当前现价, 算研判以来的前向收益, 判断方向是否命中, 给出总体/分方向命中率与
  平均收益 —— 用来校准强度 rubric、发现系统性偏差(如总体偏多但命中率低)。

判定口径:
  - 前向收益 forward_return(%) = (现价 / 研判时基准价 ref_price - 1) * 100。
  - **超额收益 excess(%) = 个股前向收益 − 同期基准指数收益**(基准默认沪深300, 留痕时存的
    bench_ref_price)。skill 全程强调相对强弱, 复盘也按超额口径判命中更自洽: 大盘普涨时
    个股小涨其实是跑输, 原始口径会误判"看多命中", 超额口径不会。原始/超额双口径并列输出。
  - 命中(原始): 看多(+)且收益 > +band; 看空(−)且收益 < −band; 中性(0)且 |收益| <= band。
    超额命中同理, 把"收益"换成"超额收益"。band 默认 3%(可 --band 调), 噪声区算中性。
  - 无 ref_price(留痕时没抓到现价)或现价抓取失败的条目按"无法判定"跳过, 不计入分母;
    无 bench_ref_price(老记录/限流)的条目仅缺超额口径, 原始口径仍照常判。
  - 复盘只对"记录满 --min-days 天"的预测有意义(太新还没走出来), 默认 0 不限制。

用法:
    python3 review.py                    # 复盘全部可判定预测
    python3 review.py --code 600519      # 只看某标的(裸6位/带前缀均可, 自动按6位数字匹配)
    python3 review.py --min-days 5       # 只复盘记录满5天的
    python3 review.py --band 2 --json    # 自定义中性带宽 + JSON
    python3 review.py --code 000066 --current-price 17.77   # 收盘后/限流抓不到现价时手填兜底

退出码: 0 正常(含无可判定记录); 2 日志不存在。
"""

import re
import sys
import json
import argparse
from datetime import datetime

from quote import CST, analyze_one
from record import load_records, LOG_PATH

DIR_BUCKET = {1: "看多", -1: "看空", 0: "中性"}


def _code_core(c):
    """取代码的6位数字核心, 用于宽松匹配。

    record.py 存的是带前缀的 disp('sz000066'), 而 --code 常传裸6位('000066');
    此前按整串精确比较永不相等 → 全部记录被滤掉, 误报'无可判定'。这里统一抽 6 位数字,
    使 '000066'/'sz000066'/'000066.SZ' 等价。"""
    m = re.search(r"(\d{6})", c or "")
    return m.group(1) if m else (c or "")


def _cur_price(code):
    """当前现价(腾讯优先退东财); 抓不到返回 (None, freshness)。"""
    try:
        r = analyze_one(code)
        tx, em = r.get("tencent") or {}, r.get("eastmoney") or {}
        price = (tx.get("price") if tx.get("ok") else None) or (em.get("price") if em.get("ok") else None)
        return price, r.get("freshness")
    except Exception:  # noqa: BLE001
        return None, None


def _days_since(iso):
    try:
        dt = datetime.fromisoformat(iso)
        return (datetime.now(CST) - dt).total_seconds() / 86400.0
    except (ValueError, TypeError):
        return None


def judge(sign, ret, band):
    """方向是否命中。返回 'hit'/'miss'。"""
    if sign > 0:
        return "hit" if ret > band else "miss"
    if sign < 0:
        return "hit" if ret < -band else "miss"
    return "hit" if abs(ret) <= band else "miss"


def review(records, band=3.0, min_days=0.0, code_filter=None, current_price=None):
    filt = _code_core(code_filter) if code_filter else None

    def _match(c):
        return filt is None or _code_core(c) == filt

    # 按代码聚合抓价, 避免同一标的重复抓
    codes = sorted({r.get("code") for r in records if r.get("code") and _match(r.get("code"))})
    price_cache = {c: _cur_price(c) for c in codes}
    # 现价兜底: 收盘后/限流时实时接口抓不到, 允许 --current-price 手填。
    # 仅在'单标的过滤 + 恰好命中一个代码'时生效, 避免给多标的张冠李戴。
    if current_price is not None and filt is not None and len(codes) == 1:
        price_cache[codes[0]] = (current_price, "手填(--current-price)")
    # 基准指数当前点位也聚合抓一次(超额收益用); 同一基准多条记录复用
    bench_codes = sorted({r.get("bench_code") for r in records if r.get("bench_code")
                          and _match(r.get("code"))})
    bench_cache = {c: _cur_price(c) for c in bench_codes}

    rows, skipped = [], []
    for r in records:
        code = r.get("code")
        if not code or not _match(code):
            continue
        age = _days_since(r.get("recorded_at"))
        if age is not None and age < min_days:
            skipped.append((r, "太新(未满 min-days)"))
            continue
        ref = r.get("ref_price")
        cur, fresh = price_cache.get(code, (None, None))
        if not isinstance(ref, (int, float)) or not isinstance(cur, (int, float)) or ref == 0:
            skipped.append((r, "无基准价或现价未抓到"))
            continue
        ret = round((cur / ref - 1) * 100, 2)
        sign = r.get("direction_sign", 0)
        res = judge(sign, ret, band)
        # 超额收益: 个股收益 − 同期基准指数收益(基准点位齐备时才算)
        bench_code, bench_ref = r.get("bench_code"), r.get("bench_ref_price")
        bench_cur, _ = bench_cache.get(bench_code, (None, None)) if bench_code else (None, None)
        excess = excess_res = bench_ret = None
        if (isinstance(bench_ref, (int, float)) and bench_ref
                and isinstance(bench_cur, (int, float))):
            bench_ret = round((bench_cur / bench_ref - 1) * 100, 2)
            excess = round(ret - bench_ret, 2)
            excess_res = judge(sign, excess, band)
        rows.append({"code": code, "name": r.get("name"), "direction": r.get("direction"),
                     "sign": sign, "ref_price": ref, "cur_price": cur, "freshness": fresh,
                     "ret_pct": ret, "result": res,
                     "bench_code": bench_code, "bench_ret_pct": bench_ret,
                     "excess_pct": excess, "excess_result": excess_res,
                     "age_days": round(age, 1) if age else None,
                     "as_of": r.get("as_of"), "score": r.get("score")})
    return rows, skipped


def summarize(rows):
    n = len(rows)
    hits = sum(1 for x in rows if x["result"] == "hit")
    out = {"judged": n, "hits": hits, "hit_rate": round(hits / n * 100, 1) if n else None,
           "by_direction": {}}
    for s, label in DIR_BUCKET.items():
        sub = [x for x in rows if x["sign"] == s]
        if not sub:
            continue
        h = sum(1 for x in sub if x["result"] == "hit")
        out["by_direction"][label] = {
            "n": len(sub), "hits": h, "hit_rate": round(h / len(sub) * 100, 1),
            "avg_ret": round(sum(x["ret_pct"] for x in sub) / len(sub), 2)}
    # 超额收益口径(仅对存有基准点位、能算超额的条目): 与原始口径并列, 单独统计
    ex = [x for x in rows if x.get("excess_result") is not None]
    if ex:
        eh = sum(1 for x in ex if x["excess_result"] == "hit")
        out["excess"] = {"judged": len(ex), "hits": eh,
                         "hit_rate": round(eh / len(ex) * 100, 1),
                         "avg_excess": round(sum(x["excess_pct"] for x in ex) / len(ex), 2)}
    else:
        out["excess"] = None
    return out


def main():
    ap = argparse.ArgumentParser(description="复盘预测命中率(读 record.py 的 JSONL)")
    ap.add_argument("--code", default=None, help="只复盘某标的")
    ap.add_argument("--band", type=float, default=3.0, help="中性带宽%%(默认3): |收益|<=band 视为持平")
    ap.add_argument("--min-days", type=float, default=0.0, help="只复盘记录满 N 天的预测(默认0)")
    ap.add_argument("--current-price", type=float, default=None,
                    help="手填现价(收盘后/限流抓不到时兜底); 仅在 --code 单标的时生效")
    ap.add_argument("--file", default=LOG_PATH, help="预测日志文件")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()

    import os
    if not os.path.exists(args.file):
        print(f"✗ 预测日志不存在: {args.file}(先用 record.py 留痕)", file=sys.stderr)
        sys.exit(2)

    records = load_records(args.file)
    rows, skipped = review(records, band=args.band, min_days=args.min_days,
                           code_filter=args.code, current_price=args.current_price)
    summary = summarize(rows)

    if args.json:
        print(json.dumps({"summary": summary, "rows": rows,
                          "skipped": len(skipped)}, ensure_ascii=False, indent=2))
        return

    print(f"复盘时间(CST): {datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}  "
          f"中性带宽±{args.band:g}%  min-days={args.min_days:g}")
    print("=" * 64)
    if not rows:
        print(f"无可判定记录(共 {len(records)} 条, 跳过 {len(skipped)} 条: 太新/无基准价/现价未抓到)。")
        return
    for x in rows:
        mark = "✅命中" if x["result"] == "hit" else "❌未中"
        fr = f"  ({x['freshness']})" if x.get("freshness") not in (None, "today") else ""
        # 超额收益(有基准点位时): 标出对基准的超额及其命中
        ex = ""
        if x.get("excess_pct") is not None:
            em = "✅" if x["excess_result"] == "hit" else "❌"
            ex = f"  超额{x['excess_pct']:+.2f}%{em}(基准{x.get('bench_ret_pct'):+.2f}%)"
        print(f"  {mark}  {x.get('name') or ''}({x['code']})  判[{x['direction']}]  "
              f"基准{x['ref_price']:g}→现价{x['cur_price']:g}  收益{x['ret_pct']:+.2f}%{ex}"
              f"  ({x.get('age_days')}天前){fr}")
    print("-" * 64)
    s = summary
    print(f"总体(原始收益): {s['hits']}/{s['judged']} 命中, 命中率 {s['hit_rate']}%")
    if s.get("excess"):
        e = s["excess"]
        print(f"总体(超额收益vs基准): {e['hits']}/{e['judged']} 命中, 命中率 {e['hit_rate']}%  "
              f"平均超额{e['avg_excess']:+.2f}%")
    for label, d in s["by_direction"].items():
        print(f"  {label}: {d['hits']}/{d['n']} 命中率{d['hit_rate']}%  平均收益{d['avg_ret']:+.2f}%")
    if skipped:
        print(f"(跳过 {len(skipped)} 条: 太新/无基准价/现价未抓到)")
    print("\n提示: 超额收益口径已剔除大盘β, 更能反映研判本身的α; 原始口径含大盘涨跌。")
    print("      命中率仅供校准强度rubric/发现系统性偏差; 样本少时不具统计意义, 现价滞后口径见标注。")


if __name__ == "__main__":
    main()
