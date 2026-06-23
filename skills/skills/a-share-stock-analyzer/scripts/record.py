#!/usr/bin/env python3
"""把一次研判结论落地为本地 JSONL —— 给 skill 装上"可追溯/可复盘"的闭环。

为什么要有它:
  此前 skill 每次只产出一份预测报告, 说完就忘 —— 既无法回看"当时怎么判的",
  也无法统计方向命中率来校准强度 rubric。本脚本把每次研判的关键结论(标的/时间/
  综合分/方向/关键价位)追加成一行 JSON, review.py 据此拉之后实际涨跌做复盘。

口径:
  - 一次研判 = 一行 JSON, 追加写入 predictions.jsonl(默认在 skill 根目录, 见 LOG_PATH)。
  - ref_price(研判时现价)是复盘算前向收益的基准: 不传则用 quote 即时抓一次(best-effort,
    抓不到存 null, 复盘时该条按"无基准"跳过)。
  - bench_ref_price(研判时基准指数点位): 同时抓一次(默认沪深300), 供 review.py 算
    **超额收益**(个股收益 − 大盘收益) —— skill 全程强调相对强弱, 复盘也应按超额口径判命中,
    否则大盘普涨时个股小涨会被误判"看多命中"。不传则自动抓; --no-fetch 时一并跳过。
  - direction 用 score.py / SKILL 的 7 档之一(强烈看多/偏多/中性偏多/中性/中性偏空/偏空/强烈看空)。

用法:
    python3 record.py --code 600519 --name 贵州茅台 --direction 偏多 --score 1.8 \
        --template 消费医药 --entry "1250-1280" --stop 1180 --target 1380 --note "主力回流+估值低位"
    python3 record.py --code 600519 --direction 偏空 --score -1.6 --no-fetch   # 不抓现价
    python3 record.py --list            # 查看已记录的预测
    python3 record.py --file 自定义.jsonl ...

退出码: 0 成功; 2 参数错误。
"""

import os
import sys
import json
import argparse
from datetime import datetime

from quote import CST, normalize_code

# 默认日志: skill 根目录(scripts 的上一级)/predictions.jsonl
LOG_PATH = os.environ.get(
    "ASHARE_PRED_LOG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "predictions.jsonl"))

# 7 档方向 -> 预期符号(+1 看多 / -1 看空 / 0 中性), 供复盘判命中
DIRECTION_SIGN = {
    "强烈看多": 1, "偏多": 1, "中性偏多": 1,
    "中性": 0,
    "中性偏空": -1, "偏空": -1, "强烈看空": -1,
}


# 复盘超额收益的默认基准: 沪深300(缺则 review 自动退到记录里存的代码)
DEFAULT_BENCH = "sh000300"


def _fetch_ref_price(code):
    """某标的/指数当前价(复盘基准)。best-effort: 抓不到返回 (None, None)。"""
    try:
        import quote
        r = quote.analyze_one(code)
        tx, em = r.get("tencent") or {}, r.get("eastmoney") or {}
        price = (tx.get("price") if tx.get("ok") else None) or (em.get("price") if em.get("ok") else None)
        ts = (tx.get("timestamp") if tx.get("ok") else None) or (em.get("timestamp") if em.get("ok") else None)
        return price, ts
    except Exception:  # noqa: BLE001 - 留痕不应因抓价失败而失败
        return None, None


def append_record(rec, path=LOG_PATH):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_records(path=LOG_PATH):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def main():
    ap = argparse.ArgumentParser(description="研判结论留痕(JSONL), 供 review.py 复盘")
    ap.add_argument("--code", help="股票代码, 如 600519")
    ap.add_argument("--name", default=None, help="股票名称")
    ap.add_argument("--direction", help="7档方向之一: 强烈看多/偏多/中性偏多/中性/中性偏空/偏空/强烈看空")
    ap.add_argument("--score", type=float, default=None, help="综合分(-5..5)")
    ap.add_argument("--template", default=None, help="行业权重模板")
    ap.add_argument("--entry", default=None, help="参考区间, 如 1250-1280")
    ap.add_argument("--stop", default=None, help="止损参考价")
    ap.add_argument("--target", default=None, help="目标参考价")
    ap.add_argument("--note", default=None, help="核心逻辑一句话")
    ap.add_argument("--ref-price", type=float, default=None, help="研判时现价(复盘基准); 不传则自动抓")
    ap.add_argument("--bench", default=DEFAULT_BENCH,
                    help=f"复盘超额收益的基准指数(默认{DEFAULT_BENCH}=沪深300); 设空串关闭")
    ap.add_argument("--bench-ref-price", type=float, default=None,
                    help="研判时基准指数点位; 不传则自动抓(供 review.py 算超额收益)")
    ap.add_argument("--no-fetch", action="store_true", help="不自动抓现价(个股与基准均不抓)")
    ap.add_argument("--as-of", default=None, help="研判口径时间(默认现在), 如 '2026-06-12 14:30 盘中'")
    ap.add_argument("--file", default=LOG_PATH, help=f"日志文件(默认 {LOG_PATH})")
    ap.add_argument("--list", action="store_true", help="列出已记录的预测后退出")
    args = ap.parse_args()

    if args.list:
        recs = load_records(args.file)
        if not recs:
            print(f"(无记录: {args.file})")
            return
        print(f"共 {len(recs)} 条预测 ({args.file}):")
        for r in recs:
            print(f"  {r.get('recorded_at', '?')[:16]}  {r.get('name') or ''}({r.get('code')})  "
                  f"{r.get('direction')}  综合分{r.get('score')}  基准价{r.get('ref_price')}")
        return

    if not args.code or not args.direction:
        print("✗ 需要 --code 和 --direction(或用 --list 查看)", file=sys.stderr)
        sys.exit(2)
    if args.direction not in DIRECTION_SIGN:
        print(f"✗ direction 须为 7 档之一: {list(DIRECTION_SIGN)}", file=sys.stderr)
        sys.exit(2)
    try:
        _, _, disp = normalize_code(args.code)
    except ValueError as e:
        print(f"✗ {e}", file=sys.stderr)
        sys.exit(2)

    ref_price, ref_ts = args.ref_price, None
    if ref_price is None and not args.no_fetch:
        ref_price, ref_ts = _fetch_ref_price(args.code)

    # 基准指数点位(超额收益复盘用): 同样 best-effort, 缺失不影响留痕(原始收益仍可复盘)
    bench_code = (args.bench or "").strip() or None
    bench_ref, bench_ts = args.bench_ref_price, None
    if bench_code and bench_ref is None and not args.no_fetch:
        bench_ref, bench_ts = _fetch_ref_price(bench_code)

    now = datetime.now(CST)
    rec = {
        "recorded_at": now.isoformat(),
        "as_of": args.as_of or now.strftime("%Y-%m-%d %H:%M"),
        "code": disp,
        "name": args.name,
        "direction": args.direction,
        "direction_sign": DIRECTION_SIGN[args.direction],
        "score": args.score,
        "template": args.template,
        "entry": args.entry,
        "stop": args.stop,
        "target": args.target,
        "note": args.note,
        "ref_price": ref_price,
        "ref_price_ts": ref_ts,
        "bench_code": bench_code,
        "bench_ref_price": bench_ref,
        "bench_ref_ts": bench_ts,
    }
    append_record(rec, args.file)
    print(f"✓ 已记录: {disp} {args.direction} 综合分{args.score} 基准价{ref_price}"
          + (f" | 基准{bench_code}={bench_ref}" if bench_code else "") + f" -> {args.file}")
    if ref_price is None and not args.no_fetch:
        print("  ⚠️ 现价未抓到(限流?); 复盘需基准价, 可稍后用 --ref-price 补记一条。")
    elif bench_code and bench_ref is None and not args.no_fetch:
        print("  ⚠️ 基准指数点位未抓到(限流?); 仍可复盘原始收益, 超额收益该条将跳过。")


if __name__ == "__main__":
    main()
