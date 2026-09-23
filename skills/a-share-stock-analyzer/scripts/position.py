#!/usr/bin/env python3
"""持仓管理: 吃进'持仓股数 + 平均成本', 算盈亏平衡、各价位 PnL 阶梯、止损/目标与盈亏比(R:R),
并评估加仓/减仓情景 —— 把 SKILL Step 6'操作参考'从静态文字升级为确定性算术。

为什么要有它:
  研判(analyze/score)解决'看多看空', 但用户一旦有仓位, 问的全是'能减吗/能加吗/现在止损亏多少/
  到目标赚多少'。这些是确定性算术, 不该每次手搓。本脚本:
    - 现价/技术锚点(MA20/MA60/前高前低)走 quote.py(接口权威, 不搜摘要), 止损/目标'挂靠真实技术位';
    - 浮盈亏、盈亏平衡、各关键价位 PnL、止损亏损额、目标收益额、盈亏比 R:R 一次算齐;
    - 加仓→新均价/新平衡价; 减仓→已实现盈亏 + 剩余敞口。
  ⚠️ 只做算术与价位挂靠, 不替代研判方向(方向看 analyze/score)。价位不臆造: 无K线(新股/停牌)时
     止损/目标留空, 要求手填。

用法:
    python3 position.py 000066 --shares 3500 --cost 18.2
    python3 position.py 000066 --shares 3500 --cost 18.2 --stop 17.5 --target 20.78
    python3 position.py 000066 --shares 3500 --cost 18.2 --add-shares 1000 --add-price 17.7
    python3 position.py 000066 --shares 3500 --cost 18.2 --trim-shares 1500 --trim-price 18.4
    python3 position.py 000066 --shares 3500 --cost 18.2 --price 17.8 --no-fetch   # 手填现价不抓
    python3 position.py 000066 --shares 3500 --cost 18.2 --stop-amp-mult 1.5  # 止损留更宽波动空间
    python3 position.py 000066 --shares 3500 --cost 18.2 --json

退出码: 0 正常; 2 现价抓取失败且未 --price 手填(无法算 PnL); 3 日K缺失(多为限流), 止损/目标留空, 重跑即可。
"""

import sys
import json
import math
import argparse
from datetime import datetime

from quote import CST, analyze_one, fmt_num, kline_retry, kline_failure_hint

FRESH_LABEL = {
    "today": "",
    "last_close": "📅 最近交易日收盘口径(非今日)",
    "stale": "⛔ 数据滞后, 勿用",
}


def _live(code):
    """现价 + 涨跌幅 + 技术锚点 + 新鲜度(腾讯优先退东财)。抓不到 price=None。"""
    r = kline_retry(analyze_one(code))
    tx, em = r.get("tencent") or {}, r.get("eastmoney") or {}
    src = tx if (tx.get("ok") and tx.get("price")) else em
    kl = r.get("kline") or {}
    return {
        "ok": bool(src.get("ok") and src.get("price")),
        "name": r.get("name"), "code": r.get("display_code") or code,
        "industry": r.get("industry"),
        "price": src.get("price"), "change_pct": src.get("change_pct"),
        "freshness": r.get("freshness"), "usable": r.get("usable"),
        "cross": r.get("cross_validation"),
        "ma20": kl.get("ma20"), "ma60": kl.get("ma60"),
        "high_20": kl.get("high_20"), "low_20": kl.get("low_20"),
        "high_60": kl.get("high_60"), "low_60": kl.get("low_60"),
        "amp_avg20": kl.get("amp_avg20"),
        "kline_ok": kl.get("ok", False),
        "kline_hint": kline_failure_hint(kl),
    }


# 止损/目标自动挂靠时的最小缓冲: 现价正贴某锚点时, 紧贴它做止损会被噪声秒扫、
# 且令 R:R 失真(风险分母≈0)。目标用固定 BUFFER%; 止损另按该票波动放大(见 stop_buffer_pct)。
ANCHOR_BUFFER_PCT = 1.5
# 止损缓冲 = max(固定下限, 倍数 × 近20日平均日振幅): 止损离买价须盖得住该票一日正常波动,
# 高振幅票固定 1.5% 远不够(实战: MA60 距现价 1.76%, 而日均振幅 2.78%, 挂 MA60 即扎进噪声)。
# 插针另由'收盘破位且站不回'的执行纪律过滤(SKILL Step 6), 故 1 倍即可, 不再层层加码。
STOP_AMP_MULT = 1.0


def stop_buffer_pct(amp_avg20, mult=STOP_AMP_MULT):
    """止损最小距离(%): 固定下限与 mult×近20日均振幅 取大; 无振幅数据退固定下限。"""
    if isinstance(amp_avg20, (int, float)) and amp_avg20 > 0:
        return max(ANCHOR_BUFFER_PCT, mult * amp_avg20)
    return ANCHOR_BUFFER_PCT


def suggest_stop(price, anchors, buffer_pct=ANCHOR_BUFFER_PCT):
    """止损 = 结构位 + 波动空间。返回 (止损价, 挂靠锚点名, 是否下移)。

    - 下方最近支撑已在 buffer% 之外 → 直接挂靠它;
    - 最近支撑落在 buffer% 之内(贴太近, 会被正常波动扫掉) → 仍以它为结构依据, 但止损下移到
      现价×(1−buffer%), 即'破该位且再走出一日正常波动'才算看错, 不跳到远处更深的锚点
      (跳远会把'回踩支撑处入场'这种好入场的止损拉得过宽、R:R 虚低);
    - 下方无任何锚点 → 不臆造, 返回 None。
    """
    below = [(name, v) for name, v in anchors if isinstance(v, (int, float)) and v < price]
    if not below:
        return None, None, False
    floor = price * (1 - buffer_pct / 100)
    near = [(n, v) for n, v in below if v > floor]
    if near:
        name, _ = min(near, key=lambda x: x[1])  # 缓冲区内最低的那个结构位, 止损须在它之下
        return math.floor(floor * 100 + 1e-6) / 100, name, True  # 向下取到分: 进位会让止损落回缓冲内
    name, v = max(below, key=lambda x: x[1])
    return v, name, False


def suggest_target(price, anchors, buffer_pct=ANCHOR_BUFFER_PCT):
    """目标 = 现价上方最近、且至少 buffer% 之外的阻力; 都太近则退最近的并标 too_close。"""
    above = [(name, v) for name, v in anchors if isinstance(v, (int, float)) and v > price]
    if not above:
        return None, None, False
    far = [(n, v) for n, v in above if (v - price) / price * 100 >= buffer_pct]
    if far:
        name, v = min(far, key=lambda x: x[1])
        return v, name, False
    name, v = min(above, key=lambda x: x[1])
    return v, name, True


def pnl_at(level, shares, cost, price):
    """某价位的持仓市值/盈亏额/盈亏%/距现价%。"""
    return {
        "price": level,
        "value": shares * level,
        "pnl": shares * (level - cost),
        "pnl_pct": (level / cost - 1) * 100 if cost else None,
        "dist_pct": (level / price - 1) * 100 if price else None,
    }


def build(code, shares, cost, price, anchors, kline_ok,
          stop=None, target=None, fee_pct=0.13,
          add_shares=None, add_price=None, trim_shares=None, trim_price=None,
          amp_avg20=None, stop_amp_mult=STOP_AMP_MULT):
    pos_cost = shares * cost
    pos_now = shares * price
    pnl = pos_now - pos_cost
    breakeven_fee = cost * (1 + fee_pct / 100)  # 含双边费的近似平衡价
    stop_buf = stop_buffer_pct(amp_avg20, stop_amp_mult)

    auto_stop, auto_target = stop is None and kline_ok, target is None and kline_ok
    stop_basis = target_basis = None
    stop_widened = target_tooclose = False
    if auto_stop:
        stop, stop_basis, stop_widened = suggest_stop(price, anchors, stop_buf)
    if auto_target:
        target, target_basis, target_tooclose = suggest_target(price, anchors)

    out = {
        "code": code, "shares": shares, "cost": cost, "price": price,
        "pos_cost": pos_cost, "pos_now": pos_now, "pnl": pnl,
        "pnl_pct": (price / cost - 1) * 100 if cost else None,
        "breakeven": cost, "breakeven_fee": breakeven_fee, "fee_pct": fee_pct,
        "amp_avg20": amp_avg20, "stop_amp_mult": stop_amp_mult, "stop_buffer_pct": stop_buf,
        "stop": stop, "stop_basis": stop_basis, "stop_widened": stop_widened,
        "target": target, "target_basis": target_basis, "target_tooclose": target_tooclose,
    }

    # 沉没成本锚识别: 成本远高于一切技术位(尤其60日前高)时, 多半是把往轮已实现亏损摊到剩余股上
    # 算出的'等效成本', 按它得到的盈亏平衡/回本价/触发盈亏全部失真(典型: 价到目标仍显示亏损)。
    # 打标 → 渲染层警示: 决策应基于现价与技术位, 不基于回本价。(R:R 用现价算, 不受此污染。)
    high_60 = next((v for n, v in anchors if n == "60日前高" and isinstance(v, (int, float))), None)
    out["high_60"] = high_60
    out["cost_anchor_suspect"] = bool(high_60 and cost > high_60 * 1.1)

    # 止损/目标的金额与盈亏比
    if stop is not None:
        out["stop_pnl"] = shares * (stop - cost)
        out["stop_dist_pct"] = (stop / price - 1) * 100
    if target is not None:
        out["target_pnl"] = shares * (target - cost)
        out["target_dist_pct"] = (target / price - 1) * 100
    # 盈亏比 R:R(以现价为基准的前瞻: 上方空间 / 下方风险)
    # ⚠️ 止损落在正常波动之内(多为手填过紧)会让 R:R 虚高失真, 标 distorted 供渲染层提示。
    if stop is not None and target is not None and price > stop:
        risk_pct = (price - stop) / price * 100
        out["rr"] = (target - price) / (price - stop)
        out["rr_distorted"] = risk_pct < stop_buf - 1e-9

    # PnL 阶梯: 关键价位排序后逐档
    levels = {}
    for name, v in ([("止损", stop), ("现价", price), ("目标", target)]
                    + anchors):
        if isinstance(v, (int, float)):
            levels.setdefault(round(v, 3), []).append(name)
    ladder = []
    for lv in sorted(levels):
        ladder.append({**pnl_at(lv, shares, cost, price), "tags": levels[lv]})
    out["ladder"] = ladder

    # 加仓情景: 新均价/新平衡价/新增投入
    if add_shares and add_price:
        ns = shares + add_shares
        nc = (pos_cost + add_shares * add_price) / ns
        # 新钱独立视角: 只评估'加仓这批'以 add_price 为成本的盈亏与 R:R。
        # 混合均价被老仓(尤其沉没成本锚)污染时, 加不加该看这批新钱划不划算, 不看被污染的整体。
        # 止损/目标须相对 add_price 重新挂靠: 挂单价常远离现价, 沿用现价口径的止损可能在买价之上。
        nm_stop, nm_stop_basis, nm_target, nm_target_basis = stop, stop_basis, target, target_basis
        nm_stop_widened = False
        if auto_stop:
            nm_stop, nm_stop_basis, nm_stop_widened = suggest_stop(add_price, anchors, stop_buf)
        if auto_target:
            nm_target, nm_target_basis, _ = suggest_target(add_price, anchors)
        # 手填价位与买价方向矛盾(止损≥买价 / 目标≤买价)时不算, 交渲染层提示
        nm_stop_invalid = nm_stop is not None and nm_stop >= add_price
        nm_target_invalid = nm_target is not None and nm_target <= add_price
        nm_rr = nm_rr_distorted = nm_stop_pnl = nm_target_pnl = None
        if nm_stop is not None and not nm_stop_invalid:
            nm_stop_pnl = add_shares * (nm_stop - add_price)
        if nm_target is not None and not nm_target_invalid:
            nm_target_pnl = add_shares * (nm_target - add_price)
        if nm_stop_pnl is not None and nm_target_pnl is not None:
            nm_rr = (nm_target - add_price) / (add_price - nm_stop)
            nm_rr_distorted = (add_price - nm_stop) / add_price * 100 < stop_buf - 1e-9
        out["add"] = {"add_shares": add_shares, "add_price": add_price,
                      "added_capital": add_shares * add_price,
                      "new_shares": ns, "new_cost": nc,
                      "new_breakeven_fee": nc * (1 + fee_pct / 100),
                      "new_pos_now": ns * price, "new_pnl": ns * (price - nc),
                      "nm_stop": nm_stop, "nm_stop_basis": nm_stop_basis,
                      "nm_stop_widened": nm_stop_widened,
                      "nm_target": nm_target, "nm_target_basis": nm_target_basis,
                      "nm_stop_invalid": nm_stop_invalid, "nm_target_invalid": nm_target_invalid,
                      "nm_stop_pnl": nm_stop_pnl, "nm_target_pnl": nm_target_pnl,
                      "nm_rr": nm_rr, "nm_rr_distorted": nm_rr_distorted}
    # 减仓情景: 已实现盈亏 + 剩余
    if trim_shares and trim_price:
        ts = min(trim_shares, shares)
        out["trim"] = {"trim_shares": ts, "trim_price": trim_price,
                       "realized": ts * (trim_price - cost),
                       "remain_shares": shares - ts,
                       "remain_cost": cost,
                       "remain_value_now": (shares - ts) * price}
    return out


def _money(v):
    if v is None:
        return "—"
    return f"{v:+,.0f}元" if v < 0 or v > 0 else "0元"


def _amt(v):
    return "—" if v is None else f"{v:,.0f}元"


def _buf_note(o):
    """止损缓冲的来源说明: 波动放大 or 固定下限。"""
    amp = o.get("amp_avg20")
    if isinstance(amp, (int, float)) and amp > 0:
        return f"(≈{o['stop_amp_mult']:g}×近20日均振幅 {amp:.2f}%, 下限 {ANCHOR_BUFFER_PCT:g}%)"
    return f"(无振幅数据, 取固定下限 {ANCHOR_BUFFER_PCT:g}%)"


def _nm_level(a, kind):
    """新钱视角的止损/目标价位文字: 价位 + 挂靠依据, 或无效/缺失原因。"""
    v, basis = a.get(f"nm_{kind}"), a.get(f"nm_{kind}_basis")
    if v is None:
        return "—(下方/上方无锚点, 不臆造)"
    if a.get(f"nm_{kind}_invalid"):
        side = "≥" if kind == "stop" else "≤"
        return f"{fmt_num(v)} ⚠️手填价{side}买价, 对这批新钱无效"
    if not basis:
        return f"{fmt_num(v)}(手填)"
    return f"{fmt_num(v)}[{basis}{', 下移出正常波动' if a.get(f'nm_{kind}_widened') else ''}]"


def render(o, live):
    L = []
    fresh = live.get("freshness")
    flag = FRESH_LABEL.get(fresh, "")
    cross = {"cross_validated": "✅已交叉验证", "single_source": "⚠️仅单一来源",
             "inconsistent": "❌两源不一致"}.get(live.get("cross"), "")
    head = f"{live.get('name') or ''}({o['code']})"
    if live.get("industry"):
        head += f"  〔{live['industry']}〕"
    L.append("=" * 60)
    L.append(f"  {head}  {cross}{('  ' + flag) if flag else ''}")
    if live.get("kline_hint"):
        L.append(f"  ⚠️ {live['kline_hint']}")
    L.append(f"    现价 {fmt_num(o['price'])}  涨跌 {fmt_num(live.get('change_pct'), pct=True)}")
    L.append("-" * 60)
    # 当前持仓
    L.append(f"  持仓: {o['shares']:,.0f}股 @ 成本 {fmt_num(o['cost'])}   "
             f"成本市值 {_amt(o['pos_cost'])}")
    L.append(f"  现值: {_amt(o['pos_now'])}   "
             f"浮动盈亏: {_money(o['pnl'])} ({o['pnl_pct']:+.2f}%)")
    L.append(f"  盈亏平衡: {fmt_num(o['breakeven'])}(成本价)  "
             f"/ {fmt_num(o['breakeven_fee'])}(含费≈{o['fee_pct']:g}%双边)")
    if o.get("cost_anchor_suspect"):
        L.append(f"  ⚠️ 成本 {fmt_num(o['cost'])} 高于60日前高 {fmt_num(o['high_60'])}, 疑似含已实现亏损"
                 f"摊销的沉没成本锚 → 上方盈亏平衡/回本价、下方各档'触发盈亏'均按此失真")
        L.append(f"     (典型: 价到目标仍显示亏损)。决策请看现价与技术位/盈亏比, 不要盯回本价。")
    # 止损 / 目标 / 盈亏比
    L.append("-" * 60)
    if o.get("stop") is not None:
        if not o.get("stop_basis"):
            basis = "  (手填)"
        elif o.get("stop_widened"):
            basis = f"  挂靠[{o['stop_basis']}, 贴太近→下移出正常波动]"
        else:
            basis = f"  挂靠[{o['stop_basis']}]"
        L.append(f"  🛑 止损 {fmt_num(o['stop'])}{basis}   触发亏损 {_money(o['stop_pnl'])}"
                 f"   距现价 {o['stop_dist_pct']:+.2f}%")
        L.append(f"     止损最小距离 {o['stop_buffer_pct']:.2f}%  {_buf_note(o)}"
                 f"; 执行按'收盘破位且次日站不回', 不碰即砍")
    else:
        L.append("  🛑 止损: " + ("日K未取到(见上方提示) → 重跑, 或 --stop 手填" if live.get("kline_hint")
                                  else "无K线/未手填 → 不臆造, 请 --stop 指定"))
    if o.get("target") is not None:
        basis = f"  挂靠[{o['target_basis']}]" if o.get("target_basis") else "  (手填)"
        warn = "  ⚠️距现价过近" if o.get("target_tooclose") else ""
        L.append(f"  🎯 目标 {fmt_num(o['target'])}{basis}   触发盈利 {_money(o['target_pnl'])}"
                 f"   距现价 {o['target_dist_pct']:+.2f}%{warn}")
    else:
        L.append("  🎯 目标: " + ("日K未取到(见上方提示) → 重跑, 或 --target 手填" if live.get("kline_hint")
                                  else "无K线/未手填 → 不臆造, 请 --target 指定"))
    if o.get("rr") is not None:
        if o.get("rr_distorted"):
            L.append(f"  ⚖️ 盈亏比 R:R = 1:{o['rr']:.1f} ⚠️失真(止损在正常波动之内, 风险分母过小)"
                     f" → 止损放到 {o['stop_buffer_pct']:.2f}% 之外再看 R:R")
        else:
            verdict = "划算(≥2)" if o["rr"] >= 2 else ("一般(1~2)" if o["rr"] >= 1 else "不划算(<1)")
            L.append(f"  ⚖️ 盈亏比 R:R = 1:{o['rr']:.2f}（现价上方空间 ÷ 下方风险）→ {verdict}")
    # PnL 阶梯
    L.append("-" * 60)
    L.append("  各价位 PnL 阶梯(挂靠技术位):")
    L.append(f"    {'价格':>7} {'距现价':>8} {'持仓市值':>12} {'盈亏额':>12} {'盈亏%':>8}  标记")
    for r in o["ladder"]:
        tags = "·".join(r["tags"])
        L.append(f"    {fmt_num(r['price']):>7} {r['dist_pct']:>+7.2f}% {r['value']:>11,.0f}元 "
                 f"{r['pnl']:>+11,.0f}元 {r['pnl_pct']:>+7.2f}%  {tags}")
    # 加仓情景
    if o.get("add"):
        a = o["add"]
        L.append("-" * 60)
        L.append(f"  ➕ 加仓情景: +{a['add_shares']:,.0f}股 @ {fmt_num(a['add_price'])} "
                 f"(投入 {_amt(a['added_capital'])})")
        L.append(f"     新持仓 {a['new_shares']:,.0f}股   新均价 {fmt_num(a['new_cost'])}"
                 f"   新平衡(含费) {fmt_num(a['new_breakeven_fee'])}")
        L.append(f"     加仓后浮动盈亏 {_money(a['new_pnl'])}")
        # 新钱独立视角(判断'该不该加'看这批, 不看被老仓/沉没成本污染的整体)
        # 止损/目标已相对加仓价重新挂靠, 故单列价位
        L.append(f"     ▸ 新钱视角(相对买价 {fmt_num(a['add_price'])}): "
                 f"止损 {_nm_level(a, 'stop')} / 目标 {_nm_level(a, 'target')}")
        if a.get("nm_rr") is not None:
            if a.get("nm_rr_distorted"):
                rr_txt = f"R:R=1:{a['nm_rr']:.1f} ⚠️失真(止损在正常波动之内)"
            else:
                v = "划算(≥2)" if a["nm_rr"] >= 2 else ("一般(1~2)" if a["nm_rr"] >= 1 else "不划算(<1)")
                rr_txt = f"R:R=1:{a['nm_rr']:.2f} → {v}"
            L.append(f"       止损{_money(a['nm_stop_pnl'])} / 目标{_money(a['nm_target_pnl'])}   {rr_txt}")
        elif a.get("nm_stop_pnl") is not None or a.get("nm_target_pnl") is not None:
            L.append(f"       止损{_money(a.get('nm_stop_pnl'))} / 目标{_money(a.get('nm_target_pnl'))}")
    # 减仓情景
    if o.get("trim"):
        t = o["trim"]
        L.append("-" * 60)
        L.append(f"  ➖ 减仓情景: -{t['trim_shares']:,.0f}股 @ {fmt_num(t['trim_price'])}")
        L.append(f"     已实现盈亏 {_money(t['realized'])}   "
                 f"剩余 {t['remain_shares']:,.0f}股 @ {fmt_num(t['remain_cost'])}"
                 f"(均价不变)   剩余现值 {_amt(t['remain_value_now'])}")
    L.append("-" * 60)
    note = "  ⚠️ 仅算术与价位挂靠, 不代表方向(方向看 analyze/score); 不构成投资建议。"
    if fresh and fresh != "today":
        note += f" 现价为{FRESH_LABEL.get(fresh, fresh)}。"
    L.append(note)
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="持仓管理: 盈亏平衡/PnL阶梯/止损目标/加减仓情景")
    ap.add_argument("code", help="股票代码(裸6位或带前缀)")
    ap.add_argument("--shares", type=float, required=True, help="持仓股数")
    ap.add_argument("--cost", type=float, required=True, help="平均成本价")
    ap.add_argument("--stop", type=float, default=None, help="止损价(不填则挂靠技术位建议)")
    ap.add_argument("--target", type=float, default=None, help="目标价(不填则挂靠技术位建议)")
    ap.add_argument("--stop-amp-mult", type=float, default=STOP_AMP_MULT,
                    help=f"自动止损最小距离 = 该倍数×近20日均振幅(下限{ANCHOR_BUFFER_PCT:g}%%, 默认{STOP_AMP_MULT:g})")
    ap.add_argument("--fee-pct", type=float, default=0.13, help="双边交易费近似%%(默认0.13: 佣金+印花)")
    ap.add_argument("--add-shares", type=float, default=None, help="加仓股数(配 --add-price)")
    ap.add_argument("--add-price", type=float, default=None, help="加仓价")
    ap.add_argument("--trim-shares", type=float, default=None, help="减仓股数(配 --trim-price)")
    ap.add_argument("--trim-price", type=float, default=None, help="减仓价")
    ap.add_argument("--price", type=float, default=None, help="手填现价(配 --no-fetch 或覆盖抓取值)")
    ap.add_argument("--no-fetch", action="store_true", help="不抓实时(须配 --price), 技术锚点将缺失")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()

    live = {"ok": False, "name": None, "code": args.code, "industry": None,
            "price": args.price, "change_pct": None, "freshness": None, "usable": None,
            "cross": None, "ma20": None, "ma60": None, "high_20": None, "low_20": None,
            "high_60": None, "low_60": None, "amp_avg20": None, "kline_ok": False}
    if not args.no_fetch:
        try:
            live = _live(args.code)
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ 实时抓取异常: {e}", file=sys.stderr)
    if args.price is not None:  # 手填覆盖
        live["price"] = args.price

    price = live.get("price")
    if not isinstance(price, (int, float)) or price <= 0:
        print("✗ 未取到现价(接口限流?) 且未 --price 手填, 无法算 PnL。", file=sys.stderr)
        sys.exit(2)

    anchors = [("MA20", live.get("ma20")), ("MA60", live.get("ma60")),
               ("20日前高", live.get("high_20")), ("20日前低", live.get("low_20")),
               ("60日前高", live.get("high_60")), ("60日前低", live.get("low_60"))]
    o = build(live.get("code") or args.code, args.shares, args.cost, price, anchors,
              live.get("kline_ok", False), stop=args.stop, target=args.target,
              fee_pct=args.fee_pct, add_shares=args.add_shares, add_price=args.add_price,
              trim_shares=args.trim_shares, trim_price=args.trim_price,
              amp_avg20=live.get("amp_avg20"), stop_amp_mult=args.stop_amp_mult)

    if args.json:
        print(json.dumps({"as_of": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
                          "live": live, "position": o}, ensure_ascii=False, indent=2))
    else:
        print(f"抓取时间(本地 CST): {datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}")
        print(render(o, live))
    # 自动止损/目标依赖日K; 手填了两者则日K缺失不影响结果, 不报 3
    if live.get("kline_hint") and not args.no_fetch and (args.stop is None or args.target is None):
        sys.exit(3)


if __name__ == "__main__":
    main()
