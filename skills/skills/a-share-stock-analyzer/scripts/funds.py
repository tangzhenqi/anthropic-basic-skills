#!/usr/bin/env python3
"""A股资金面补充: 融资融券 + 龙虎榜 —— 东财 datacenter 权威口径。

为什么要有这个脚本(而非继续用 WebSearch):
  SKILL【维度一】此前把"融资融券余额 / 龙虎榜机构席位"交给 WebSearch 摘要,
  但同一文档的铁律是"资金数据一律不得采信 AI 摘要"(摘要常滞后/错位/臆造)。
  这与 quote.py(行情+主力资金)的双源接口口径自相矛盾。本脚本把这两类资金信号
  也固化成接口抓取, 让"资金维度"的强度评分(rubric 要求数据已核验)真正站得住。

口径与边界:
  - 融资融券: datacenter RPTA_WEB_RZRQ_GGMX, 按交易日给融资余额/融券余额/融资买入额
    及近 N 日趋势。东财盘后披露, 通常滞后约 1 个交易日 —— 用 quote.freshness 标注口径,
    SKILL 允许窗口为"最近 1 个交易日", 超窗口只作背景、不计入打分。
  - 龙虎榜: datacenter RPT_DAILYBILLBOARD_DETAILSNEW, 给近期上榜记录(上榜原因/
    龙虎榜净买额/机构概述/当日涨跌幅)。多数股票多数日子不上榜 —— 空结果是正常状态,
    不是失败; 只有"最近 1 个交易日内"的上榜才计入当日资金方向, 更早的作背景。
  - 不是两融标的 / 从未上过龙虎榜 -> 各自返回 ok=True 但 records 为空, 报告如实写"无"。

复用 quote.py 的 http_get / normalize_code / freshness / fmt_num / CST / UA 基建,
避免重复维护(与 valuation.py 同样的复用约定)。

用法:
    python3 funds.py 600519                 # 融资融券近5日 + 龙虎榜近90天
    python3 funds.py 600519 000858          # 多只
    python3 funds.py --json 600519          # JSON 输出(供程序消费)
    python3 funds.py --lhb-days 30 600519   # 自定义龙虎榜回看天数(默认90)

退出码: 0 至少一只成功抓到(两融或龙虎榜任一接口通); 2 全部失败(网络/限流)。
注意: 需联网, 东财 datacenter 须带 User-Agent(已内置)。datacenter 偶发限流,
      http_get 已带退避重试; 仍失败按退出码 2 处理, 勿把限流误当代码 bug。
"""

import sys
import json
import argparse
import urllib.parse
from datetime import datetime, timedelta

# 复用 quote.py 的网络/解析/时间戳/格式化基建 (同目录)
from quote import http_get, normalize_code, freshness, fmt_num, UA, CST, FRESHNESS_LABEL

DC_BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def bare_code(secid):
    """从 quote.normalize_code 的 secid('1.600519') 取裸 6 位代码, datacenter 筛选用。"""
    return secid.split(".", 1)[1]


def _dc_get(report_name, filter_expr, sort_col, page_size):
    """datacenter 通用 GET -> data 列表(空列表表示无记录, 而非错误)。"""
    params = {
        "reportName": report_name,
        "columns": "ALL",
        "source": "WEB",
        "client": "WEB",
        "pageSize": str(page_size),
        "sortColumns": sort_col,
        "sortTypes": "-1",
        "filter": filter_expr,
    }
    # filter 里的 ()="必须百分号编码(datacenter 对未编码括号/引号返回 400)
    url = DC_BASE + "?" + urllib.parse.urlencode(params)
    d = json.loads(http_get(url, headers={"User-Agent": UA})) or {}
    if not d.get("success"):
        msg = d.get("message") or ""
        # "返回数据为空" 是合法的无记录状态(非两融标的/从未上榜), 不是错误 -> 空列表
        if "为空" in msg or "没有" in msg:
            return []
        raise RuntimeError(msg or "datacenter 返回 success=false")
    return ((d.get("result") or {}).get("data")) or []


def _parse_date(s):
    """'2026-06-10 00:00:00' -> 收盘口径 tz-aware datetime(挂 15:00 CST)。"""
    if not s:
        return None
    try:
        d = datetime.strptime(s.split(" ")[0], "%Y-%m-%d")
        return d.replace(hour=15, tzinfo=CST)
    except (ValueError, TypeError):
        return None


# ---------- 融资融券 (RPTA_WEB_RZRQ_GGMX) ----------

def fetch_margin(code, days=6):
    """融资融券近 days 个披露日。返回最新值 + 近5日融资余额趋势。
    RZYE 融资余额(元), RZYEZB 融资余额占流通市值%, RZMRE 融资买入额(元),
    RQYE 融券余额(元), RZRQYE 两融余额(元)。"""
    filt = f'(SCODE="{code}")'
    rows = _dc_get("RPTA_WEB_RZRQ_GGMX", filt, "DATE", days)
    if not rows:
        return {"ok": True, "is_margin_target": False,
                "note": "非两融标的或无融资融券数据"}
    # 接口按 DATE 降序; rows[0] 最新。趋势需升序比较。
    asc = list(reversed(rows))
    latest = rows[0]
    dt = _parse_date(latest.get("DATE"))
    rzye_first = asc[0].get("RZYE")
    rzye_last = latest.get("RZYE")
    delta = None
    if isinstance(rzye_first, (int, float)) and isinstance(rzye_last, (int, float)):
        delta = rzye_last - rzye_first
    return {
        "ok": True,
        "is_margin_target": True,
        "date": (latest.get("DATE") or "").split(" ")[0],
        # 融资融券为东财盘后披露, 天然滞后约1个交易日 -> lag_trading_days=1
        "freshness": freshness(dt, lag_trading_days=1) if dt else "missing",
        "rzye": rzye_last,                      # 融资余额(元)
        "rzye_ratio": latest.get("RZYEZB"),     # 占流通市值(%)
        "rzmre": latest.get("RZMRE"),           # 当日融资买入额(元)
        "rqye": latest.get("RQYE"),             # 融券余额(元)
        "rzrqye": latest.get("RZRQYE"),         # 两融余额(元)
        "rzye_delta_window": delta,             # 窗口内融资余额净变化(元)
        "window_days": len(rows),
        "trend": ("回升" if delta and delta > 0 else
                  "下降" if delta and delta < 0 else "持平/无足量数据"),
        "daily": [{"date": (r.get("DATE") or "").split(" ")[0],
                   "rzye": r.get("RZYE"), "rzmre": r.get("RZMRE")} for r in rows],
    }


# ---------- 龙虎榜 (RPT_DAILYBILLBOARD_DETAILSNEW) ----------

def fetch_lhb(code, lookback_days=90, limit=10):
    """龙虎榜近 lookback_days 天的上榜记录(每条: 上榜原因/净买额/机构概述/当日涨跌幅)。
    多数股票长期不上榜, 空结果是正常状态。仅最近 1 个交易日内的上榜才计入当日方向。"""
    filt = f'(SECURITY_CODE="{code}")'
    rows = _dc_get("RPT_DAILYBILLBOARD_DETAILSNEW", filt, "TRADE_DATE", limit)
    cutoff = datetime.now(CST) - timedelta(days=lookback_days)
    recent = []
    for r in rows:
        dt = _parse_date(r.get("TRADE_DATE"))
        if dt is None or dt < cutoff:
            continue
        recent.append({
            "date": (r.get("TRADE_DATE") or "").split(" ")[0],
            "reason": r.get("EXPLANATION"),         # 上榜原因
            "org_summary": r.get("EXPLAIN"),        # 机构概述(如"5家机构卖出")
            "net_amt": r.get("BILLBOARD_NET_AMT"),  # 龙虎榜净买额(元)
            "buy_amt": r.get("BILLBOARD_BUY_AMT"),
            "sell_amt": r.get("BILLBOARD_SELL_AMT"),
            "change_pct": r.get("CHANGE_RATE"),     # 当日涨跌幅(%)
            "deal_ratio": r.get("DEAL_AMOUNT_RATIO"),  # 龙虎榜成交占比(%)
        })
    latest_fresh = None
    if recent:
        dt = _parse_date(recent[0]["date"] + " 00:00:00")
        # 龙虎榜同为盘后披露, 天然滞后约1个交易日 -> lag_trading_days=1
        latest_fresh = freshness(dt, lag_trading_days=1) if dt else "missing"
    return {
        "ok": True,
        "on_list": bool(recent),
        "lookback_days": lookback_days,
        "latest_freshness": latest_fresh,   # today/last_close=可计入当日; 否则背景
        "records": recent,
    }


def analyze_one(user_code, lhb_days=90):
    try:
        _, secid, disp = normalize_code(user_code)
    except ValueError as e:
        return {"input": user_code, "ok": False, "error": str(e)}
    code = bare_code(secid)
    out = {"input": user_code, "display_code": disp, "secid": secid, "ok": False}
    try:
        out["margin"] = fetch_margin(code)
    except Exception as e:  # noqa: BLE001 - 单接口失败不拖垮另一个
        out["margin"] = {"ok": False, "error": f"融资融券抓取异常: {e}"}
    try:
        out["lhb"] = fetch_lhb(code, lookback_days=lhb_days)
    except Exception as e:  # noqa: BLE001
        out["lhb"] = {"ok": False, "error": f"龙虎榜抓取异常: {e}"}
    out["ok"] = bool(out["margin"].get("ok") or out["lhb"].get("ok"))
    return out


def print_one(item):
    if not item.get("ok"):
        print(f"  ✗ {item.get('input')}: {item.get('error') or '抓取失败'}")
        return
    print(f"  {item['display_code']}")

    m = item.get("margin") or {}
    if not m.get("ok"):
        print(f"    融资融券: ✗ {m.get('error')}")
    elif not m.get("is_margin_target"):
        print(f"    融资融券: {m.get('note')}")
    else:
        tag = FRESHNESS_LABEL.get(m.get("freshness"), "")
        tag = f"  {tag}" if tag else ""
        ratio = m.get("rzye_ratio")
        ratio_s = f"(占流通 {ratio:.2f}%)" if isinstance(ratio, (int, float)) else ""
        print(f"    融资融券 [{m.get('date')}]{tag}")
        print(f"      融资余额: {fmt_num(m.get('rzye'), '元')} {ratio_s}   "
              f"当日融资买入: {fmt_num(m.get('rzmre'), '元')}")
        print(f"      融券余额: {fmt_num(m.get('rqye'), '元')}   "
              f"近{m.get('window_days')}日融资余额: {m.get('trend')} "
              f"({fmt_num(m.get('rzye_delta_window'), '元')})")

    lhb = item.get("lhb") or {}
    if not lhb.get("ok"):
        print(f"    龙虎榜: ✗ {lhb.get('error')}")
    elif not lhb.get("on_list"):
        print(f"    龙虎榜: 近{lhb.get('lookback_days')}天无上榜记录")
    else:
        fl = lhb.get("latest_freshness")
        scope = "可计入当日" if fl in ("today", "last_close") else "历史背景, 不计入当日方向"
        print(f"    龙虎榜 近{lhb.get('lookback_days')}天 {len(lhb['records'])} 次上榜 "
              f"(最近一次新鲜度: {FRESHNESS_LABEL.get(fl, fl) or '今日'} -> {scope})")
        for r in lhb["records"][:5]:
            cp = r.get("change_pct")
            cp_s = f"{cp:+.2f}%" if isinstance(cp, (int, float)) else "—"
            print(f"      {r['date']} 涨跌{cp_s}  净买额 {fmt_num(r.get('net_amt'), '元')}")
            print(f"        原因: {r.get('reason')}")
            if r.get("org_summary"):
                print(f"        机构: {r.get('org_summary')}")


def main():
    ap = argparse.ArgumentParser(description="A股资金面补充: 融资融券 + 龙虎榜(东财口径)")
    ap.add_argument("codes", nargs="+", help="股票代码, 如 600519 000858")
    ap.add_argument("--json", action="store_true", help="仅输出 JSON")
    ap.add_argument("--lhb-days", type=int, default=90, help="龙虎榜回看天数(默认90)")
    args = ap.parse_args()

    items = [analyze_one(c, lhb_days=args.lhb_days) for c in args.codes]

    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
    else:
        print(f"抓取时间(本地 CST): {datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 60)
        for it in items:
            print_one(it)
            print()
        print("提示: 融资融券通常滞后约1个交易日, 标注口径后可用; 龙虎榜仅'最近1个交易日'")
        print("      内上榜计入当日方向, 更早记录作背景。无数据=如实写'无', 勿臆造。")

    sys.exit(0 if any(it.get("ok") for it in items) else 2)


if __name__ == "__main__":
    main()
