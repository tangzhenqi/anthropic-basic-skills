#!/usr/bin/env python3
"""A股估值快照抓取(PE-TTM / PB / 总市值 / 流通市值)——东财权威口径。

为什么单独成脚本(而非塞进 quote.py):
  - quote.py 专注"当日行情+资金流+技术位"的双源交叉验证, 职责已满;
  - 估值是另一类(基本面)数据, 取数接口/口径不同, 拆开各自演进更清晰。

口径与边界:
  - 估值"当前值"一律用接口抓取, 杜绝 WebSearch 摘要的滞后/错位(同 quote.py 铁律);
  - 估值"历史分位"亦由接口直算、不再走 WebSearch: 东财 RPT_VALUEANALYSIS_DET 提供
    约5年日序列, 本脚本本地算 PE-TTM/PB/PS-TTM 及股息率(TTM现价口径)的近3/5年分位,
    并据 PE/PB 分位给出估值护栏建议(见 fetch_val_pctl / _guardrail);
  - 亏损股 PE-TTM 为负(loss_making=true), 此时 PE 无意义, 看 PB / 市销率。

复用 quote.py 的 http_get / normalize_code / 时间戳 / 格式化基建, 避免重复维护。

用法:
    python3 valuation.py 600519                 # 单只
    python3 valuation.py 600519 000858 300750   # 多只
    python3 valuation.py --json 600519          # JSON 输出(供程序消费)

退出码: 0 至少一只成功; 2 全部失败。需联网, 东财接口须带 User-Agent(已内置)。
"""

import sys
import json
import argparse
import urllib.parse
from datetime import datetime, timedelta

# 复用 quote.py 的网络/解析/时间戳/格式化/分位/新鲜度基建 (同目录)
from quote import (http_get, normalize_code, is_today, freshness, fmt_num,
                   UA, CST, _pctl, em_snapshot, FRESHNESS_LABEL)

# 估值用到的 push2 字段(均含在 quote.EM_SNAPSHOT_FIELDS 并集里, 故与 quote 共用一次请求):
# f43 现价(/100做cross-check), f86 时间戳, f116 总市值, f117 流通市值,
# f162 动态PE, f163 PE-TTM, f164 静态PE, f167 市净率PB。PE/PB 均 /100。

DC_BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"
DIV_LOOKBACK_DAYS = 365  # TTM 股息率回看窗口


def fetch_valuation(secid):
    # 与 quote.fetch_eastmoney 共用同一次 push2 快照(em_snapshot 进程内按 secid 去重)
    data = em_snapshot(secid)
    if not data:
        return {"ok": False, "error": "data 为空(代码/市场前缀错? 或指数无估值)"}

    def num(key, div=100.0):
        v = data.get(key)
        if v in (None, "-", "", 0):  # 0 多为"无估值"(指数/未披露), 视作缺失
            return None
        try:
            return float(v) / div
        except (TypeError, ValueError):
            return None

    ts = None
    if isinstance(data.get("f86"), int) and data["f86"] > 0:
        ts = datetime.fromtimestamp(data["f86"], tz=CST)

    pe_ttm = num("f163")
    return {
        "ok": True,
        "name": data.get("f58"),
        "code": data.get("f57"),
        "price": num("f43"),               # 仅作 sanity cross-check
        "pe_ttm": pe_ttm,                  # 主用估值口径
        "pe_dynamic": num("f162"),
        "pe_static": num("f164"),
        "pb": num("f167"),
        "total_mcap": num("f116", div=1.0),  # 元
        "float_mcap": num("f117", div=1.0),  # 元
        "loss_making": pe_ttm is not None and pe_ttm < 0,
        "timestamp": ts.isoformat() if ts else None,
        "is_today": is_today(ts),
        # 与 quote 统一的三档新鲜度(today/last_close/stale): 周末/节假日为 last_close,
        # 不再二元误报"非今日"。报告据此标注口径。
        "freshness": freshness(ts) if ts else "missing",
    }


def _dc_get(report_name, filter_expr, sort_col, page_size):
    """datacenter 通用 GET -> data 列表(空列表=无记录, 非错误)。括号/引号须 URL 编码否则 400。"""
    params = {"reportName": report_name, "columns": "ALL", "source": "WEB",
              "client": "WEB", "pageSize": str(page_size), "filter": filter_expr}
    if sort_col:
        params["sortColumns"] = sort_col
        params["sortTypes"] = "-1"
    url = DC_BASE + "?" + urllib.parse.urlencode(params)
    d = json.loads(http_get(url, headers={"User-Agent": UA})) or {}
    if not d.get("success"):
        msg = d.get("message") or ""
        if "为空" in msg or "没有" in msg:
            return []
        raise RuntimeError(msg or "datacenter 返回 success=false")
    return ((d.get("result") or {}).get("data")) or []


def _board_bk(orig_board_code):
    """RPT_VALUEANALYSIS_DET 的 ORIG_BOARD_CODE -> 东财行业板块交易码 BKxxxx。
    实测: 白酒Ⅱ ORIG=1277->BK1277, 电池 ORIG=1033->BK1033(零填充4位)。
    供 analyze.py 取该股'所属行业板块'的板块行情/资金流(secid=90.BKxxxx)。"""
    s = str(orig_board_code).strip()
    if not s.isdigit():
        return None
    return f"BK{int(s):04d}"


def fetch_ps(code):
    """市销率 PS-TTM + PEG: 东财估值分析 RPT_VALUEANALYSIS_DET(今日口径)。
    SKILL 对亏损股要求'看 PB/市销率', 此前 valuation.py 缺市销率, 这里补上。"""
    rows = _dc_get("RPT_VALUEANALYSIS_DET", f'(SECURITY_CODE="{code}")', "TRADE_DATE", 1)
    if not rows:
        return {"ok": False}
    r = rows[0]
    return {
        "ok": True,
        "ps_ttm": r.get("PS_TTM"),
        "peg": r.get("PEG_CAR"),
        "board_name": r.get("BOARD_NAME"),  # 东财行业(datacenter 口径), 作 quote 行业的备援
        "board_bk": _board_bk(r.get("ORIG_BOARD_CODE")),  # 行业板块交易码, 供板块行情/资金流
        "trade_date": (r.get("TRADE_DATE") or "").split(" ")[0],
    }


def _guardrail(pctl):
    """SKILL Step 3.3 估值护栏: 近3-5年极高分位(>90%)->高估,下调一档; 极低(<10%)->低估,可上调一档。"""
    if pctl is None:
        return None
    if pctl >= 90:
        return "极高分位(>90%), 高估 -> 综合分下调一档"
    if pctl <= 10:
        return "极低分位(<10%), 低估 -> 综合分可上调一档(基本面无恶化时)"
    return "处于中间分位, 不触发护栏调档"


def _div_per_share_events(code):
    """近~6年'已除权'每股税前派现事件 [(除权日, 每股派现元), ...], 供历史股息率序列重建。
    PRETAX_BONUS_RMB=每10股税前派现; 只取 EX_DIVIDEND_DATE 非空(已实施)的派现。"""
    rows = _dc_get("RPT_SHAREBONUS_DET", f'(SECURITY_CODE="{code}")', "EX_DIVIDEND_DATE", 40)
    evs = []
    for r in rows:
        ed, amt = r.get("EX_DIVIDEND_DATE"), r.get("PRETAX_BONUS_RMB")
        if not ed or amt in (None, "", 0):
            continue
        try:
            edt = datetime.strptime(ed.split(" ")[0], "%Y-%m-%d")
        except (ValueError, TypeError):
            continue
        evs.append((edt, float(amt) / 10.0))
    return evs


def fetch_val_pctl(code, loss_making=False):
    """PE-TTM / PB / PS-TTM 的近3年、近5年历史分位 —— 接口直算, 替掉 WebSearch 摘要。

    SKILL Step 3.3 的估值护栏(±1档)依赖'近3-5年分位', 此前甩给 WebSearch, 与全文
    '估值数据勿采信摘要'的铁律自相矛盾。东财 RPT_VALUEANALYSIS_DET 提供约8年日序列,
    本函数拉下来本地算分位(复用 quote._pctl 的'低于今日占比'口径: 分位越高=越贵)。

    口径: 分位 = 当前值在窗口内'高于历史多少比例的交易日'。PE<0(亏损)日不计入 PE 分位
    (此时 PE 分位无意义)。窗口按自然年回看(近3年/近5年), 不足窗口则用全部可得历史并标注。"""
    rows = _dc_get("RPT_VALUEANALYSIS_DET",
                   f'(SECURITY_CODE="{code}")', "TRADE_DATE", 1300)  # ~5年+交易日
    if not rows:
        return {"ok": False}
    # 接口按 TRADE_DATE 降序, rows[0] 最新
    series = []
    for r in rows:
        ds = (r.get("TRADE_DATE") or "").split(" ")[0]
        try:
            d = datetime.strptime(ds, "%Y-%m-%d")
        except (ValueError, TypeError):
            continue
        series.append({"date": d, "pe": r.get("PE_TTM"), "pb": r.get("PB_MRQ"),
                       "ps": r.get("PS_TTM"), "close": r.get("CLOSE_PRICE")})
    if not series:
        return {"ok": False}
    latest_d = series[0]["date"]

    def _num(v):
        return float(v) if isinstance(v, (int, float)) else None

    def _pctl_for(metric, years, positive_only):
        """metric 在近 years 年窗口内的分位; positive_only=True 时剔除<=0(PE 亏损日)。"""
        cutoff = latest_d - timedelta(days=365 * years)
        window = [_num(s[metric]) for s in series if s["date"] >= cutoff]
        window = [v for v in window if v is not None and (v > 0 if positive_only else True)]
        cur = _num(series[0][metric])
        if cur is None or (positive_only and cur <= 0) or len(window) < 20:
            return None, len(window)
        return _pctl(window, cur), len(window)

    # 股息率(TTM现价口径)历史分位: 用日序列里每个交易日的 CLOSE_PRICE + 该日往前12个月
    # 已除权派现合计, 重建逐日股息率序列再算分位。⚠️ 方向与 PE/PB 相反: 分位越高=当前
    # 派息相对历史越慷慨/估值越便宜。近似口径(未做送转复权, 与多数平台展示一致)。
    div_evs = _div_per_share_events(code)

    def _ttm_dps(asof):
        lo = asof - timedelta(days=365)
        return sum(ps for (ed, ps) in div_evs if lo <= ed <= asof)

    def _div_yield_pctl(years):
        if not div_evs:
            return None, 0
        cutoff = latest_d - timedelta(days=365 * years)
        window = []
        for s in series:
            c = _num(s.get("close"))
            if s["date"] < cutoff or not c or c <= 0:
                continue
            window.append(_ttm_dps(s["date"]) / c * 100.0)
        c0 = _num(series[0].get("close"))
        if not c0 or c0 <= 0 or len(window) < 20:
            return None, len(window)
        cur = _ttm_dps(latest_d) / c0 * 100.0
        return _pctl(window, cur), len(window)

    out = {"ok": True, "latest_date": latest_d.strftime("%Y-%m-%d")}
    for metric, pos in (("pe", True), ("pb", True), ("ps", True)):
        for yrs in (3, 5):
            p, n = _pctl_for(metric, yrs, pos)
            out[f"{metric}_pctl_{yrs}y"] = p
            out[f"{metric}_n_{yrs}y"] = n
    for yrs in (3, 5):
        p, n = _div_yield_pctl(yrs)
        out[f"div_yield_pctl_{yrs}y"] = p
        out[f"div_yield_n_{yrs}y"] = n
    # 护栏分位选取。SKILL: 亏损股 PE 无意义, 一律看 PB/市销率分位。
    # ⚠️ 关键: 快照(push2)的 loss_making 是 PE 符号的权威口径; 但东财日序列(datacenter)
    # 里利润接近0时 PE_TTM 可能仍为'勉强为正的小数'→PE→+∞→分位≈95% 的失真高位。
    # 故亏损/近亏股以 loss_making 为准, 强制改用 PB(再退 PS), 不采信失真的 PE 分位。
    pe5, pe3 = out.get("pe_pctl_5y"), out.get("pe_pctl_3y")
    pb5, pb3, ps5 = out.get("pb_pctl_5y"), out.get("pb_pctl_3y"), out.get("ps_pctl_5y")
    if loss_making:
        primary = pb5 if pb5 is not None else (pb3 if pb3 is not None else ps5)
        basis = ("PB近5年(亏损股)" if pb5 is not None else
                 "PB近3年(亏损股)" if pb3 is not None else
                 "PS近5年(亏损股)" if ps5 is not None else "无(亏损股PB/PS分位缺失)")
    else:
        primary = pe5 if pe5 is not None else (pe3 if pe3 is not None else pb5)
        basis = ("PE-TTM近5年" if pe5 is not None else
                 "PE-TTM近3年" if pe3 is not None else
                 "PB近5年(PE分位缺)" if pb5 is not None else "无")
    out["guardrail_pctl"] = primary
    out["guardrail_basis"] = basis
    out["guardrail"] = _guardrail(primary)
    return out


def fetch_dividend(code, price):
    """TTM 现价口径股息率: 近12个月'已除权'每股税前派现合计 / 现价。

    RPT_SHAREBONUS_DET: PRETAX_BONUS_RMB=每10股税前派现, EX_DIVIDEND_DATE=除权除息日。
    只计已除权(EX_DIVIDEND_DATE 非空且在近365天内)的派现 —— 未实施的分红预案不计入,
    避免把'拟分配'当成已落地。price 缺失则只给每股派现、不算收益率。"""
    rows = _dc_get("RPT_SHAREBONUS_DET", f'(SECURITY_CODE="{code}")', "EX_DIVIDEND_DATE", 12)
    cutoff = datetime.now() - timedelta(days=DIV_LOOKBACK_DAYS)
    per10_sum = 0.0
    events = []
    for r in rows:
        ed, amt = r.get("EX_DIVIDEND_DATE"), r.get("PRETAX_BONUS_RMB")
        if not ed or amt in (None, "", 0):
            continue
        try:
            edt = datetime.strptime(ed.split(" ")[0], "%Y-%m-%d")
        except (ValueError, TypeError):
            continue
        if edt >= cutoff:
            per10_sum += float(amt)
            events.append({"ex_date": ed.split(" ")[0], "per10_pretax": float(amt)})
    per_share = per10_sum / 10.0
    yld = (per_share / price * 100) if (price and price > 0) else None
    return {
        "ok": True,
        "ttm_per_share_pretax": per_share,   # 近12月每股税前派现(元)
        "div_yield_ttm": yld,                # TTM现价口径股息率(%), None=无价
        "events_12m": events,                # 计入的除权事件
        "no_dividend": not events,           # 近12月无派现
    }


def analyze_one(user_code):
    try:
        _, secid, disp = normalize_code(user_code)
    except ValueError as e:
        return {"input": user_code, "ok": False, "error": str(e)}
    code = secid.split(".", 1)[1]  # 裸6位代码, datacenter 筛选用
    try:
        r = fetch_valuation(secid)
    except Exception as e:  # noqa: BLE001 - 网络抖动不应拖垮整批
        r = {"ok": False, "error": f"抓取异常: {e}"}
    # 市销率/PEG 与 股息率走 datacenter, 单接口失败不拖垮估值主体
    try:
        r["ps"] = fetch_ps(code)
    except Exception as e:  # noqa: BLE001
        r["ps"] = {"ok": False, "error": str(e)}
    try:
        # 亏损口径以快照(push2)为权威; 快照失败时 loss_making 缺省 False, 退回 PE 分位口径
        r["pctl"] = fetch_val_pctl(code, loss_making=bool(r.get("loss_making")))
    except Exception as e:  # noqa: BLE001
        r["pctl"] = {"ok": False, "error": str(e)}
    try:
        r["dividend"] = fetch_dividend(code, r.get("price") if r.get("ok") else None)
    except Exception as e:  # noqa: BLE001
        r["dividend"] = {"ok": False, "error": str(e)}
    r["input"] = user_code
    r["display_code"] = disp
    r["secid"] = secid
    return r


def print_one(item):
    # 快照(push2)与 PS/分位/股息(datacenter)是相互独立的来源: push2 偶发限流时,
    # 仍把 datacenter 拿到的分位/PS/股息打印出来, 不因快照失败而整条隐藏。
    snapshot_ok = item.get("ok")
    if not snapshot_ok:
        print(f"  {item.get('display_code') or item.get('input')}  "
              f"⚠️ 快照(PE/PB/市值)抓取失败: {item.get('error')}")
    else:
        ftag = FRESHNESS_LABEL.get(item.get("freshness"), "")
        stale = f"  {ftag}" if ftag else ""
        pe_str = "亏损(PE<0,看PB)" if item.get("loss_making") else fmt_num(item.get("pe_ttm"))
        print(f"  {item.get('name') or '?'} ({item.get('display_code')}){stale}")
        print(f"    PE-TTM: {pe_str}   PB: {fmt_num(item.get('pb'))}   "
              f"动态PE: {fmt_num(item.get('pe_dynamic'))}   静态PE: {fmt_num(item.get('pe_static'))}")
    ps = item.get("ps") or {}
    if ps.get("ok"):
        peg = ps.get("peg")
        peg_s = f"   PEG: {peg:.2f}" if isinstance(peg, (int, float)) and peg > 0 else ""
        print(f"    市销率PS-TTM: {fmt_num(ps.get('ps_ttm'))}{peg_s}")
    pc = item.get("pctl") or {}
    if pc.get("ok"):
        def _ps(p):
            return f"{p:.0f}%" if isinstance(p, (int, float)) else "—"
        print(f"    历史分位(接口直算)  PE-TTM 近3年{_ps(pc.get('pe_pctl_3y'))}/近5年{_ps(pc.get('pe_pctl_5y'))}"
              f"   PB 近5年{_ps(pc.get('pb_pctl_5y'))}   PS 近5年{_ps(pc.get('ps_pctl_5y'))}")
        dy3, dy5 = pc.get("div_yield_pctl_3y"), pc.get("div_yield_pctl_5y")
        if dy3 is not None or dy5 is not None:
            print(f"    股息率历史分位(接口直算, 高=派息相对历史更慷慨/更便宜)  "
                  f"近3年{_ps(dy3)}/近5年{_ps(dy5)}")
        g = pc.get("guardrail")
        if g:
            print(f"    估值护栏[{pc.get('guardrail_basis')}分位{_ps(pc.get('guardrail_pctl'))}]: {g}")
    div = item.get("dividend") or {}
    if div.get("ok"):
        if div.get("no_dividend"):
            print(f"    股息率(TTM): 近12月无派现")
        else:
            y = div.get("div_yield_ttm")
            y_s = f"{y:.2f}%" if isinstance(y, (int, float)) else "—(现价缺失)"
            print(f"    股息率(TTM,现价口径): {y_s}   "
                  f"近12月每股税前派现: {fmt_num(div.get('ttm_per_share_pretax'))}元")
    if snapshot_ok:
        print(f"    总市值: {fmt_num(item.get('total_mcap'), '元')}   "
              f"流通市值: {fmt_num(item.get('float_mcap'), '元')}")
        print(f"    时间戳 东财={item.get('timestamp') or '—'}")


def main():
    ap = argparse.ArgumentParser(description="A股估值快照(PE-TTM/PB/市值, 东财口径)")
    ap.add_argument("codes", nargs="+", help="股票代码, 如 600519 000858")
    ap.add_argument("--json", action="store_true", help="仅输出 JSON")
    args = ap.parse_args()

    items = [analyze_one(c) for c in args.codes]

    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
    else:
        print(f"抓取时间(本地 CST): {datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 54)
        for it in items:
            print_one(it)
            print()
        print("提示: PE/PB/PS 及股息率(TTM现价口径)的'当前值'与'近3/5年历史分位'均已由接口直算(勿再搜摘要);")
        print("      股息率分位方向与估值相反(分位越高=派息越慷慨/越便宜); 为近似口径(未做送转复权)。")
        print("      估值仅作护栏(极端高/低估±1档), 不单独决定方向; 亏损股 PE 无意义, 看 PB/市销率分位。")

    # 快照、PS/分位、股息任一来源拿到都算"有可用数据"(快照限流不应判全败)
    def _got_any(it):
        return bool(it.get("ok") or (it.get("ps") or {}).get("ok")
                    or (it.get("pctl") or {}).get("ok") or (it.get("dividend") or {}).get("ok"))
    sys.exit(0 if any(_got_any(it) for it in items) else 2)


if __name__ == "__main__":
    main()
