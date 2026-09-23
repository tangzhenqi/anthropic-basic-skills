#!/usr/bin/env python3
"""做T预案: 已有底仓时, 把'怎么做T'从每次手搓变成确定性算术 —— 日内正T/倒T 的挂单价、
差价目标、手续费后净收益、失败处理线, 以及跨日波段T, 全部挂靠真实价位(不臆造)。

为什么要有它:
  用户持仓后最常问'怎么做T'。挂多少、差价要多大才覆盖费用、做失败了怎么收场, 都是算术+规则,
  每次手搓容易口径漂移, 更容易在盘中被瞬时数据带着讲故事。本脚本:
    - 价位锚点: 今日高/低/均价(VWAP)、昨日高/低/均价、MA20/MA60 (quote.py 接口权威, 不搜摘要);
    - 差价目标 = 近20日均振幅 × 捕获率(默认0.6, 一天的高低点吃不全), 且不低于 3 倍单股往返费;
    - 可及性: 阻力/支撑须落在'一日正常振幅'之内才算日内可及, 否则不给日内挂单(不臆造);
    - 方向/防守闸门: --direction 偏空系, 或现价距预设防守线(--stop)不足一日均振幅 → 禁正T(不加敞口);
      已跌破防守线 → 不做T, 执行止损。未给 --stop 时只展示 position.py 同口径的参考止损, 不设闸
      (自动止损随现价重算、永远在一个振幅外, 当不了固定触发线);
    - 失败处理: 正T 收盘前必须卖回等量老仓; 倒T 被轧空不追高, 按方向决定买回或视为减仓;
    - 费用: 印花税(仅卖) + 佣金(双边, 有最低 5 元) + 过户费, 算净收益与每股成本下降。
  ⚠️ 只做价位与算术, 不替代方向研判(方向看 analyze/score)。不看盘中主力净流入瞬时值。

用法:
    python3 t_plan.py 000066 --shares 4000
    python3 t_plan.py 000066 --shares 4000 --cost 19.8 --direction 中性偏多 --stop 14.44
    python3 t_plan.py 000066 --shares 4000 --t-shares 1000 --no-cash      # 无闲钱 → 不给正T
    python3 t_plan.py 000066 --shares 4000 --json

退出码: 0 正常; 2 现价抓取失败; 3 数据未取全(日K/日内行情缺, 多为限流, 重跑即可)。
"""

import sys
import json
import math
import argparse
from datetime import datetime

from quote import CST, analyze_one, kline_retry, kline_failure_hint
from position import suggest_stop, stop_buffer_pct
import trading_calendar as tc

BULLISH = {"强烈看多", "偏多", "中性偏多"}
BEARISH = {"中性偏空", "偏空", "强烈看空"}
CAPTURE = 0.6          # 日内差价 ≈ 均振幅 × 0.6(高低点吃不全)
MIN_FEE_MULT = 3       # 差价至少覆盖 3 倍单股往返费, 否则不值得做
PLACE_PCT = 0.2        # 挂单离锚点 0.2%: 卖挂阻力略下、买挂支撑略上, 防'差一分没成交'
LAST_CALL = (14, 50)   # 14:50 后不开新T, 只做收尾


def fees(t_shares, sell_px, buy_px, comm_pct=0.025, min_comm=5.0, stamp_pct=0.05, transfer_pct=0.001):
    """一买一卖往返费用(元): 佣金双边(各有最低额) + 印花税(仅卖) + 过户费双边。"""
    sell_amt, buy_amt = t_shares * sell_px, t_shares * buy_px
    comm = max(min_comm, sell_amt * comm_pct / 100) + max(min_comm, buy_amt * comm_pct / 100)
    return comm + sell_amt * stamp_pct / 100 + (sell_amt + buy_amt) * transfer_pct / 100


def min_spread(t_shares, price, **fee_kw):
    """值得做的最小每股差价 = MIN_FEE_MULT × 单股往返费。"""
    return MIN_FEE_MULT * fees(t_shares, price, price, **fee_kw) / t_shares


def target_spread(price, amp_avg20, t_shares, capture=CAPTURE, **fee_kw):
    """日内目标差价(元/股): 均振幅×捕获率, 不低于最小差价; 无振幅数据 → None(不臆造)。"""
    if not isinstance(amp_avg20, (int, float)) or amp_avg20 <= 0:
        return None
    return max(price * amp_avg20 / 100 * capture, min_spread(t_shares, price, **fee_kw))


def _floor2(x):
    return math.floor(x * 100 + 1e-6) / 100


def _ceil2(x):
    return math.ceil(x * 100 - 1e-6) / 100


def nearest(levels, ref, side, lo=None, hi=None):
    """ref 上方(side='above', 含等于)或下方(side='below', 含等于)最近的锚点, 限定在 [lo, hi] 内。"""
    pool = [(n, v) for n, v in levels if isinstance(v, (int, float))
            and (lo is None or v >= lo - 1e-6) and (hi is None or v <= hi + 1e-6)]  # 容浮点误差
    if side == "above":
        pool = [(n, v) for n, v in pool if v >= ref]
        return min(pool, key=lambda x: x[1]) if pool else (None, None)
    pool = [(n, v) for n, v in pool if v <= ref]
    return max(pool, key=lambda x: x[1]) if pool else (None, None)


def _leg(kind, t_shares, first_px, second_px, first_basis, second_basis, mspread, fee_kw):
    """组装一条T腿: 倒T/波段 first=卖/second=买回; 正T first=买/second=卖。差价不足标 viable=False。"""
    sell_px, buy_px = (first_px, second_px) if kind in ("reverse", "swing") else (second_px, first_px)
    spread = sell_px - buy_px
    fee = fees(t_shares, sell_px, buy_px, **fee_kw)
    return {"kind": kind, "first_px": first_px, "second_px": second_px,
            "first_basis": first_basis, "second_basis": second_basis,
            "spread": spread, "fee": fee, "net": t_shares * spread - fee,
            "viable": spread >= mspread - 1e-9}


def intraday(price, levels, reach_pct, spread, t_shares, mspread, fee_kw, lo_ref, hi_ref):
    """日内正T/倒T 挂单。可及区间: 阻力 ≤ lo_ref×(1+reach), 支撑 ≥ hi_ref×(1−reach)。"""
    r_cap = lo_ref * (1 + reach_pct / 100)
    s_floor = hi_ref * (1 - reach_pct / 100)
    out = {"reach_pct": reach_pct, "r_cap": r_cap, "s_floor": s_floor}
    # 正在创出的今日低点不算'经过检验'的支撑: 现价离它不足 0.3×差价时不拿它当正T买点(接飞刀)。
    # 只管买侧: 倒T 卖在新高附近失败了也只是'在更高价减了仓', 亏损有界; 正T 接飞刀是加敞口。
    fresh = 0.3 * spread
    buy_levels = [(n, v) for n, v in levels
                  if not (n == "今日低点" and isinstance(v, (int, float)) and price - v < fresh)]

    # 倒T: 卖在上方最近阻力略下方, 买回 = 卖价 − 差价, 但不低于卖价下方最近支撑(否则挂了也成交不了)
    rn, rv = nearest(levels, price, "above", hi=r_cap)
    if rv is not None:
        sell = _floor2(max(price, rv * (1 - PLACE_PCT / 100)))
        buy, basis = _ceil2(sell - spread), f"卖价−差价{spread:.2f}"
        # 途中有支撑(可能先止跌): 挂它略上方更易成交, 但剩余差价须仍覆盖费用, 否则保留目标价并提示
        sn, sv = nearest(levels, sell - spread, "above", hi=sell - 0.01)
        if sn is not None:
            alt = _ceil2(sv * (1 + PLACE_PCT / 100))
            if sell - alt >= mspread - 1e-9:
                buy, basis = min(alt, sell - 0.01), f"{sn}略上方(途中支撑, 先止跌概率大)"
            else:
                basis += f"; 途经{sn}{sv:.2f}可能先止跌, 到不了就不买回"
        out["reverse"] = _leg("reverse", t_shares, sell, buy, f"{rn}略下方", basis, mspread, fee_kw)
    else:
        out["reverse"] = None

    # 正T: 买在下方最近支撑略上方, 卖出 = 买价 + 差价, 但不高于买价上方最近阻力
    sn, sv = nearest(buy_levels, price, "below", lo=s_floor)
    if sv is not None:
        buy = _ceil2(min(price, sv * (1 + PLACE_PCT / 100)))
        sell, basis = _floor2(buy + spread), f"买价+差价{spread:.2f}"
        rn2, rv2 = nearest(levels, buy + spread, "below", lo=buy + 0.01)
        if rn2 is not None:
            alt = _floor2(rv2 * (1 - PLACE_PCT / 100))
            if alt - buy >= mspread - 1e-9:
                sell, basis = max(alt, buy + 0.01), f"{rn2}略下方(途中阻力, 先受压概率大)"
            else:
                basis += f"; 途经{rn2}{rv2:.2f}可能先受压, 到不了就按失败规则收尾"
        out["forward"] = _leg("forward", t_shares, buy, sell, f"{sn}略上方", basis, mspread, fee_kw)
        out["forward"]["fail_line"] = sv
        out["forward"]["fail_basis"] = sn
    else:
        out["forward"] = None
    return out


def swing(price, anchors, amp_avg20, t_shares, mspread, fee_kw):
    """跨日波段T: 卖在上方至少 1 倍均振幅外的最近阻力, 买回在现价下方至少 1 倍均振幅外的最近支撑。"""
    buf = stop_buffer_pct(amp_avg20)
    rn, rv = nearest(anchors, price * (1 + buf / 100), "above")
    sn, sv = nearest(anchors, price * (1 - buf / 100), "below")
    if rv is None or sv is None:
        return None
    sell = _floor2(rv * (1 - PLACE_PCT / 100))
    buy = _ceil2(sv * (1 + PLACE_PCT / 100))
    return _leg("swing", t_shares, sell, buy, f"{rn}略下方", f"{sn}略上方", mspread, fee_kw)


def session_state(now, quote_ts):
    """盘中(可日内T) / 尾盘(不开新T) / 盘外(给下一交易日预案)。quote_ts 须是今日才算盘中。"""
    trading = tc.is_trading_day(now.date())
    if trading is None:  # 日历越界 → 退回工作日判断
        trading = now.weekday() < 5
    same_day = bool(quote_ts) and quote_ts.date() == now.date()
    hm = (now.hour, now.minute)
    if not (trading and same_day) or hm < (9, 30) or hm >= (15, 0):
        return "off"
    return "late" if hm >= LAST_CALL else "live"


def recommend(state, price, vwap, day_lo, day_hi, direction, near_defense, has_cash, rev, fwd,
              broken_defense=False):
    """当下先做哪条腿(规则化, 不看主力瞬时值)。返回 (动作, 理由列表)。"""
    if broken_defense:
        return "不做T", ["现价已跌破预设防守线 → 按止损纪律执行(收盘确认且次日站不回), 不靠做T摊低"]
    why = []
    allow_fwd = has_cash and fwd is not None and fwd["viable"]
    allow_rev = rev is not None and rev["viable"]
    if direction in BEARISH:
        allow_fwd = False
        why.append(f"方向{direction} → 只做倒T(先卖), 不做加敞口的正T")
    if near_defense:
        allow_fwd = False
        why.append("现价距预设防守线不足一日均振幅 → 禁正T; 真破位按止损执行, 做T让位")
    if not has_cash:
        why.append("无闲钱 → 不做正T")
    if state == "late":
        return "不开新T", why + ["14:50 后只收尾: 已开的T按失败规则回到原股数"]
    if state == "off":
        why.append("非盘中 → 以下为下一交易日挂单预案, 开盘后以当日均价与高低点复核")
    if not allow_fwd and not allow_rev:
        return "不做T", why + ["上下均无日内可及且差价足够的价位"]

    pos = None
    if None not in (day_lo, day_hi) and day_hi > day_lo:
        pos = (price - day_lo) / (day_hi - day_lo)
    if state == "live" and vwap and pos is not None:
        if price < vwap and pos < 0.35:
            why.append(f"现价在今日均价{vwap:.2f}下方且处今日区间下部({pos:.0%}) → 此时先卖=卖在低位")
            return ("先买后卖(正T), 挂支撑" if allow_fwd else "等冲高再倒T, 现在不卖"), why
        if price > vwap and pos > 0.65:
            why.append(f"现价在今日均价{vwap:.2f}上方且处今日区间上部({pos:.0%}) → 此时先买=买在高位")
            return ("先卖后买(倒T), 挂阻力" if allow_rev else "等回落, 现在不买"), why
        why.append(f"现价在今日区间中部({pos:.0%}) → 两边挂单等价格来, 不追")
    legs = [x for x, ok in (("倒T", allow_rev), ("正T", allow_fwd)) if ok]
    return "挂单等待: " + " / ".join(legs), why


def plan(price, anchors, amp_avg20, shares, t_shares, state, *, vwap=None, day_lo=None, day_hi=None,
         prev=None, cost=None, direction=None, has_cash=True, stop=None, fee_kw=None,
         kline_hint=None):
    """纯函数: 由价位与持仓生成完整做T预案(便于离线测试)。"""
    fee_kw = fee_kw or {}
    t_shares = min(t_shares, shares)
    mspread = min_spread(t_shares, price, **fee_kw)
    spread = target_spread(price, amp_avg20, t_shares, **fee_kw)
    stop_buf = stop_buffer_pct(amp_avg20)
    if stop is not None:  # 用户预设的固定防守线才能当闸门
        defense, defense_basis = stop, "预设"
        broken_defense = price < stop
        near_defense = not broken_defense and (price - stop) / price * 100 < stop_buf
    else:  # 自动止损只作参考展示(随现价重算, 不设闸)
        defense, defense_basis, _ = suggest_stop(price, anchors, stop_buf)
        defense_basis = f"{defense_basis}·参考" if defense_basis else None
        broken_defense = near_defense = False

    levels = [(n, v) for n, v in anchors if n in ("MA20", "MA60")]  # 前高前低离得远, 只给波段用
    if prev:
        levels += [("昨日高点", prev.get("high")), ("昨日低点", prev.get("low")),
                   ("昨日均价", prev.get("vwap"))]
    if state != "off":
        levels += [("今日高点", day_hi), ("今日低点", day_lo), ("今日均价", vwap)]

    out = {"price": price, "shares": shares, "t_shares": t_shares, "state": state,
           "amp_avg20": amp_avg20, "spread": spread, "min_spread": mspread,
           "vwap": vwap, "day_lo": day_lo, "day_hi": day_hi, "prev": prev,
           "defense": defense, "defense_basis": defense_basis, "near_defense": near_defense,
           "broken_defense": broken_defense,
           "direction": direction, "has_cash": has_cash, "cost": cost, "warnings": []}
    if kline_hint:
        out["warnings"].append(kline_hint)
    if state != "off" and vwap is None:
        out["warnings"].append("今日高/低/均价未取到(腾讯行情缺, 多为限流) → 日内锚点只剩均线与昨日K, 建议重跑")
    if spread is None:
        if kline_hint:  # 缺数据 ≠ 不该做T, 不能给出'不做T'这种像行情结论的动作
            out.update(intraday=None, swing=None, action="数据未取全, 请重跑",
                       why=["日K缺失 → 振幅/均线/昨日K 都没有, 算不出挂单价(不臆造)"])
        else:
            out.update(intraday=None, swing=None, action="不做T",
                       why=["K线不足以计算振幅(上市首日/长期停牌) → 不臆造价位"])
        return out

    if state == "off":
        lo_ref = hi_ref = price
        reach = amp_avg20
    else:
        today_amp = (day_hi - day_lo) / day_lo * 100 if day_lo else 0
        reach = max(amp_avg20, today_amp)
        lo_ref, hi_ref = day_lo or price, day_hi or price
    it = intraday(price, levels, reach, spread, t_shares, mspread, fee_kw, lo_ref, hi_ref)
    out["intraday"] = it
    out["swing"] = swing(price, anchors, amp_avg20, t_shares, mspread, fee_kw)
    out["action"], out["why"] = recommend(state, price, vwap, day_lo, day_hi, direction,
                                          near_defense, has_cash, it["reverse"], it["forward"],
                                          broken_defense)
    if direction in BEARISH or near_defense or broken_defense or not has_cash:
        it["forward_blocked"] = True
    for leg in (it["reverse"], it["forward"], out["swing"]):
        if leg and cost:
            leg["new_cost"] = cost - leg["net"] / shares
    return out


# ---------------------------------------------------------------- 取数与渲染

def _live(code):
    r = kline_retry(analyze_one(code))
    tx, em, kl = r.get("tencent") or {}, r.get("eastmoney") or {}, r.get("kline") or {}
    src = tx if (tx.get("ok") and tx.get("price")) else em
    vwap = None
    if tx.get("ok") and tx.get("volume_lots") and tx.get("amount_wan"):
        vwap = tx["amount_wan"] * 1e4 / (tx["volume_lots"] * 100)
    ts = src.get("timestamp")
    return {
        "name": r.get("name"), "code": r.get("display_code") or code, "cross": r.get("cross_validation"),
        "freshness": r.get("freshness"), "price": src.get("price"),
        "quote_ts": datetime.fromisoformat(ts) if ts else None,
        "day_hi": tx.get("high") if tx.get("ok") else None,
        "day_lo": tx.get("low") if tx.get("ok") else None, "vwap": vwap,
        "anchors": [("MA20", kl.get("ma20")), ("MA60", kl.get("ma60")),
                    ("20日前高", kl.get("high_20")), ("20日前低", kl.get("low_20")),
                    ("60日前高", kl.get("high_60")), ("60日前低", kl.get("low_60"))],
        "amp_avg20": kl.get("amp_avg20"), "bars": kl.get("recent_bars") or [],
        "kline_hint": kline_failure_hint(kl),
    }


def prev_bar(bars, now, state):
    """'昨日'K: 盘中/尾盘取日期早于今天的最后一根(今日K未走完);
    盘外(给下一交易日做预案)取最近一根——收盘后它就是今日K, 对下一交易日而言即'昨日'。"""
    today = now.date().isoformat()
    done = bars if state == "off" else [b for b in bars if b["date"] < today]
    return done[-1] if done else None


def _px(v):
    return "—" if v is None else f"{v:.2f}"


def _leg_lines(leg, t_shares, cost):
    lines = []
    if leg["kind"] == "reverse":
        lines.append(f"    ① 卖 {t_shares:,.0f}股 @ {_px(leg['first_px'])}  [{leg['first_basis']}]")
        lines.append(f"    ② 买回 @ {_px(leg['second_px'])}  [{leg['second_basis']}]")
    elif leg["kind"] == "forward":
        lines.append(f"    ① 买 {t_shares:,.0f}股 @ {_px(leg['first_px'])}  [{leg['first_basis']}]")
        lines.append(f"    ② 卖出等量老仓 @ {_px(leg['second_px'])}  [{leg['second_basis']}]")
    else:
        lines.append(f"    ① 卖 {t_shares:,.0f}股 @ {_px(leg['first_px'])}  [{leg['first_basis']}]")
        lines.append(f"    ② 买回 @ {_px(leg['second_px'])}  [{leg['second_basis']}]")
    tail = f"   → 成本 {cost:.2f}→{leg['new_cost']:.2f}" if cost and "new_cost" in leg else ""
    flag = "" if leg["viable"] else "  ⚠️差价不足以覆盖费用, 不做"
    lines.append(f"    差价 {leg['spread']:.2f}  费用≈{leg['fee']:.0f}元  净 {leg['net']:+,.0f}元{tail}{flag}")
    return lines


def render(o, live):
    L = ["=" * 64]
    cross = {"cross_validated": "✅已交叉验证", "single_source": "⚠️仅单一来源",
             "inconsistent": "❌两源不一致"}.get(live.get("cross"), "")
    state_txt = {"live": "盘中", "late": "尾盘(14:50后)", "off": "盘外 → 下一交易日预案"}[o["state"]]
    L.append(f"  做T预案  {live.get('name') or ''}({live.get('code')})  {cross}  [{state_txt}]")
    for w in o.get("warnings") or []:
        L.append(f"  ⚠️ {w}")
    L.append(f"    现价 {_px(o['price'])}   今日 高{_px(o['day_hi'])} 低{_px(o['day_lo'])} 均价{_px(o['vwap'])}"
             if o["state"] != "off" else f"    最近收盘 {_px(o['price'])}")
    p = o.get("prev") or {}
    if p:
        L.append(f"    昨日({p.get('date')}) 高{_px(p.get('high'))} 低{_px(p.get('low'))} 均价{_px(p.get('vwap'))}"
                 if o["state"] != "off" else
                 f"    最近交易日({p.get('date')}) 高{_px(p.get('high'))} 低{_px(p.get('low'))} 均价{_px(p.get('vwap'))}")
    amp = o.get("amp_avg20")
    L.append(f"    近20日均振幅 {amp:.2f}%  → 日内目标差价 {o['spread']:.2f}元/股"
             f"(最小 {o['min_spread']:.3f}, 即3倍往返费)" if o.get("spread") else "    振幅: —(见上方提示)")
    L.append(f"    底仓 {o['shares']:,.0f}股, 每次T {o['t_shares']:,.0f}股"
             + (f"   防守止损 {_px(o['defense'])}[{o['defense_basis']}]" if o.get("defense") else ""))
    L.append("-" * 64)
    L.append(f"  ▶ 当下动作: {o['action']}")
    for w in o.get("why") or []:
        L.append(f"     · {w}")
    it = o.get("intraday")
    if it:
        L.append("-" * 64)
        L.append(f"  【倒T: 先卖后买】(日内可及阻力上限 {_px(it['r_cap'])})")
        if it["reverse"]:
            L += _leg_lines(it["reverse"], o["t_shares"], o.get("cost"))
            keep = "14:50 按市价买回(保持仓位, 认小亏)" if o.get("direction") in BULLISH else "视为高位减仓, 不买回"
            L.append(f"    ✗ 失败: 卖后放量上破 {_px(it['reverse']['first_px'] * (1 + 0.3 * amp / 100))} 不追;"
                     f" 未买回则{keep}")
        else:
            L.append("    上方一日振幅内无阻力锚点 → 不给倒T挂单(不臆造)")
        L.append(f"  【正T: 先买后卖】(日内可及支撑下限 {_px(it['s_floor'])})")
        if it.get("forward_blocked"):
            L.append("    ⛔ 已被闸门禁止(见上方理由)")
        elif it["forward"]:
            f = it["forward"]
            L += _leg_lines(f, o["t_shares"], o.get("cost"))
            L.append(f"    ✗ 失败: 跌破 {_px(f['fail_line'])}[{f['fail_basis']}] 到 14:50 仍站不回 →"
                     f" 照样卖出 {o['t_shares']:,.0f}股老仓, 收盘回到 {o['shares']:,.0f}股, 不带多余仓位过夜")
        else:
            L.append("    下方一日振幅内无经过检验的支撑(正在创新低不算) → 不给正T挂单, 不接飞刀")
    sw = o.get("swing")
    L.append("-" * 64)
    L.append("  【波段T: 跨日, 差价更大】")
    if o.get("spread") is None:
        L.append("    —(缺振幅/均线数据, 见上方提示)")
    elif sw:
        L += _leg_lines(sw, o["t_shares"], o.get("cost"))
        L.append("    卖出条件: 冲到卖价附近但不放量; 放量站稳卖价上方 → 不卖, 转看更高阻力")
    else:
        L.append("    上下方缺少 1 倍振幅外的锚点 → 不给波段挂单")
    L.append("-" * 64)
    L.append("  纪律: 收盘必回原股数(日内T) · 每次≤半仓 · 不看主力净流入瞬时值 · 防守止损优先于做T")
    L.append("  T+1: 卖出的是昨日及以前的老仓, 今日新买的明日才能卖。⚠️ 仅价位与算术, 不构成投资建议。")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="做T预案: 日内正T/倒T + 波段T 的挂单价、差价、费用与失败处理")
    ap.add_argument("code", help="股票代码(裸6位或带前缀)")
    ap.add_argument("--shares", type=float, required=True, help="底仓股数(昨日及以前买入, 今日可卖)")
    ap.add_argument("--t-shares", type=float, default=None, help="每次T的股数(默认半仓, 取整百)")
    ap.add_argument("--cost", type=float, default=None, help="持仓成本(给出则显示做T后成本变化)")
    ap.add_argument("--direction", default=None,
                    help="研判方向(score.py 七档之一); 偏空系禁正T, 决定倒T失败后买回与否")
    ap.add_argument("--stop", type=float, default=None,
                    help="预设防守线(如 position.py/留痕的止损); 距现价<一日振幅禁正T, 已破则不做T")
    ap.add_argument("--no-cash", action="store_true", help="没有闲置资金 → 不给正T")
    ap.add_argument("--comm-pct", type=float, default=0.025, help="佣金费率%%(默认万2.5)")
    ap.add_argument("--min-comm", type=float, default=5.0, help="单笔最低佣金(默认5元)")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()

    live = _live(args.code)
    price = live.get("price")
    if not isinstance(price, (int, float)) or price <= 0:
        print("✗ 未取到现价(接口限流?), 无法生成做T预案。", file=sys.stderr)
        sys.exit(2)
    now = datetime.now(CST)
    state = session_state(now, live.get("quote_ts"))
    t_shares = args.t_shares or max(100, math.floor(args.shares / 2 / 100) * 100)
    o = plan(price, live["anchors"], live.get("amp_avg20"), args.shares, t_shares, state,
             vwap=live.get("vwap"), day_lo=live.get("day_lo"), day_hi=live.get("day_hi"),
             prev=prev_bar(live["bars"], now, state), cost=args.cost, direction=args.direction,
             has_cash=not args.no_cash, stop=args.stop, fee_kw={"comm_pct": args.comm_pct, "min_comm": args.min_comm},
             kline_hint=live.get("kline_hint"))
    if args.json:
        print(json.dumps({"as_of": now.strftime("%Y-%m-%d %H:%M"), "plan": o},
                         ensure_ascii=False, indent=2, default=str))
    else:
        print(f"抓取时间(本地 CST): {now.strftime('%Y-%m-%d %H:%M:%S')}")
        print(render(o, live))
    if o["warnings"]:
        sys.exit(3)


if __name__ == "__main__":
    main()
