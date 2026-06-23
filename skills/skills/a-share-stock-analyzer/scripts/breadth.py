#!/usr/bin/env python3
"""A股市场宽度 / 情绪温度 —— 两市成交额 + 涨停/跌停家数 + 主要指数涨跌。纯数据接口。

为什么要有它:
  SKILL【维度二·市场情绪】此前只有"个股换手率 + 上证/沪深300 点位"。但A股情绪是
  全市场层面的: 两市成交额(流动性/风险偏好)、涨停/跌停家数(打板情绪/退潮恐慌)是
  比"大盘涨跌幅"更直接的情绪温度计。这些是市场级硬数据(非个股), 一次分析抓一份即可,
  给情绪维一个确定性的"大盘背景温度", 而不是靠 WebSearch 摘要拍脑袋"市场情绪偏暖/冷"。

  注意边界(与 SKILL 铁律一致):
  - 个股北向/陆股通实时数据 2024-08-18 已停披; 市场聚合北向同样不再实时, 故本脚本
    **不碰北向**, 只用仍在实时披露的 成交额 / 涨跌停家数。
  - 涨跌"家数"(advance/decline)东财无可靠轻量接口(指数 f104/f105/f106 恒 0), 故改用
    信号更强的 涨停/跌停家数(打板情绪), 不臆造 A/D。

口径:
  - 两市成交额 = 上证综指(1.000001, 全沪) f6 + 深证综指(0.399106, 全深) f6。两综指各自
    覆盖全市场, 之和≈沪深两市成交额(优于"上证+深成", 后者深成仅500只不全)。ulist.np 实时。
  - 涨停/跌停家数: push2ex 涨停池(getTopicZTPool)/跌停池(getTopicDTPool)的 data.tc。
    交易日盘中实时滚动, 非交易日/盘前取最近交易日。日期用真实交易日历推。
  - 主要指数(上证/深成/创业板)当日涨跌幅(f3), 作风险偏好背景(创业板=小盘成长情绪)。

复用 quote.py 的 http_get/fmt_num/CST/UA 与 trading_calendar 基建。

用法:
    python3 breadth.py                 # 市场宽度快照(成交额/涨跌停/指数)
    python3 breadth.py --json          # JSON(供 analyze.py / 自动化消费)
    python3 breadth.py --date 20260611 # 指定交易日(涨跌停池; 默认最近交易日)

退出码: 0 至少一类抓到; 2 全部失败(网络/限流)。
"""

import sys
import json
import argparse
from datetime import datetime

from quote import http_get, fmt_num, UA, CST
import trading_calendar as _cal

# 全市场成交额: 上证综指(全沪) + 深证综指(全深); 另带创业板指作小盘情绪
TURNOVER_SECIDS = ["1.000001", "0.399106"]   # 求和得两市成交额
INDEX_SECIDS = ["1.000001", "0.399001", "0.399006"]  # 上证/深成/创业板 涨跌幅背景
ZT_URL = ("https://push2ex.eastmoney.com/getTopicZTPool?ut=7eea3edcaed734bea9cbfc24409ed989"
          "&dpt=wz.ztzt&Pageindex=0&pagesize=1&sort=fbt%3Aasc&date={date}")
DT_URL = ("https://push2ex.eastmoney.com/getTopicDTPool?ut=7eea3edcaed734bea9cbfc24409ed989"
          "&dpt=wz.ztzt&Pageindex=0&pagesize=1&sort=fund%3Aasc&date={date}")

# 打板情绪分档阈值(净涨停 = 涨停 − 跌停; 配合绝对量, era-robust 不依赖成交额绝对值)
ZT_WARM = 30      # 涨停 >= 30 且远多于跌停 -> 做多/打板情绪偏暖
DT_PANIC = 15     # 跌停 >= 15 且 >= 涨停 -> 退潮/恐慌


def _ulist(secids, fields):
    url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?fields=" + fields
           + "&secids=" + ",".join(secids))
    d = json.loads(http_get(url, headers={"User-Agent": UA})) or {}
    return ((d.get("data") or {}).get("diff")) or []


def fetch_turnover():
    """两市成交额(沪综指+深综指 f6 之和) + 各指数点位/涨跌幅。f2点位/100, f3涨跌幅/100, f6元。"""
    rows = _ulist(TURNOVER_SECIDS, "f2,f3,f6,f12,f14")
    by = {r.get("f12"): r for r in rows}
    parts, total = [], 0.0
    for sid in TURNOVER_SECIDS:
        code = sid.split(".", 1)[1]
        r = by.get(code)
        amt = r.get("f6") if r else None
        if isinstance(amt, (int, float)):
            total += amt
        parts.append({"code": code, "name": (r or {}).get("f14"),
                      "amount": amt,
                      "change_pct": (r.get("f3") / 100) if r and isinstance(r.get("f3"), (int, float)) else None})
    return {"ok": bool(rows), "total_amount": total if rows else None, "parts": parts}


def fetch_indices():
    """主要指数当日涨跌幅(上证/深成/创业板), 风险偏好背景。"""
    rows = _ulist(INDEX_SECIDS, "f2,f3,f12,f14")
    out = []
    for r in rows:
        out.append({"code": r.get("f12"), "name": r.get("f14"),
                    "point": (r.get("f2") / 100) if isinstance(r.get("f2"), (int, float)) else None,
                    "change_pct": (r.get("f3") / 100) if isinstance(r.get("f3"), (int, float)) else None})
    return {"ok": bool(rows), "indices": out}


def _pool_count(url_tpl, date):
    d = json.loads(http_get(url_tpl.format(date=date), headers={"User-Agent": UA})) or {}
    data = d.get("data")
    return (data or {}).get("tc") if isinstance(data, dict) else None


def fetch_limits(date):
    """涨停/跌停家数(push2ex 池 tc)。date=YYYYMMDD。"""
    zt = _pool_count(ZT_URL, date)
    dt = _pool_count(DT_URL, date)
    return {"ok": zt is not None or dt is not None,
            "date": date, "limit_up": zt, "limit_down": dt}


def read_limit_sentiment(zt, dt):
    """涨停/跌停家数 -> 打板情绪一句话(纯逻辑, era-robust)。数据缺失返回 None。"""
    if not isinstance(zt, int) or not isinstance(dt, int):
        return None
    net = zt - dt
    if zt >= ZT_WARM and zt >= 2 * max(dt, 1):
        return f"🔥 打板/做多情绪偏暖(涨停{zt} 跌停{dt}, 净涨停{net:+})"
    if dt >= DT_PANIC and dt >= zt:
        return f"❄️ 退潮/恐慌(跌停{dt} 涨停{zt}, 净涨停{net:+})"
    return f"🌡️ 情绪中性/分化(涨停{zt} 跌停{dt}, 净涨停{net:+})"


def snapshot(date=None):
    """市场宽度一次性快照(供 analyze.py 调用)。date 缺省取最近交易日。"""
    if date is None:
        rtd = _cal.recent_trading_day()
        date = rtd.strftime("%Y%m%d") if rtd else datetime.now(CST).strftime("%Y%m%d")
    out = {"date": date, "ok": False}
    for key, fn in (("turnover", fetch_turnover), ("indices", fetch_indices),
                    ("limits", lambda: fetch_limits(date))):
        try:
            out[key] = fn()
        except Exception as e:  # noqa: BLE001 - 单路失败不拖垮其余
            out[key] = {"ok": False, "error": f"{key} 抓取异常: {e}"}
    out["ok"] = any((out.get(k) or {}).get("ok") for k in ("turnover", "indices", "limits"))
    lm = out.get("limits") or {}
    out["limit_sentiment"] = read_limit_sentiment(lm.get("limit_up"), lm.get("limit_down"))
    return out


def print_one(res):
    if not res.get("ok"):
        print("  ✗ 市场宽度抓取失败(网络/限流)")
        return
    t = res.get("turnover") or {}
    if t.get("ok") and t.get("total_amount") is not None:
        seg = "  ".join(f"{p['name']} {fmt_num(p.get('amount'), '元')}"
                        for p in t.get("parts") or [] if p.get("amount") is not None)
        print(f"  两市成交额: {fmt_num(t['total_amount'], '元')}   ({seg})")
    idx = res.get("indices") or {}
    if idx.get("ok"):
        parts = []
        for i in idx.get("indices") or []:
            cp = i.get("change_pct")
            parts.append(f"{i.get('name')} {cp:+.2f}%" if isinstance(cp, (int, float)) else f"{i.get('name')} —")
        if parts:
            print(f"  主要指数: " + "   ".join(parts))
    lm = res.get("limits") or {}
    if lm.get("ok"):
        print(f"  涨停 {lm.get('limit_up')} 家 / 跌停 {lm.get('limit_down')} 家  (交易日 {lm.get('date')})")
    sent = res.get("limit_sentiment")
    if sent:
        print(f"  情绪温度: {sent}")


def main():
    ap = argparse.ArgumentParser(description="A股市场宽度/情绪: 成交额+涨跌停家数+主要指数")
    ap.add_argument("--date", default=None, help="涨跌停池交易日 YYYYMMDD(默认最近交易日)")
    ap.add_argument("--json", action="store_true", help="仅输出 JSON")
    args = ap.parse_args()

    res = snapshot(args.date)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"抓取时间(本地 CST): {datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 60)
        print_one(res)
        print("\n提示: 成交额=流动性/风险偏好; 涨停/跌停家数=打板情绪与退潮恐慌(比涨跌家数信号更强);")
        print("      均为市场级实时硬数据。盘中为滚动值, 非最终收盘。不含北向(已停实时披露)。")
    sys.exit(0 if res.get("ok") else 2)


if __name__ == "__main__":
    main()
