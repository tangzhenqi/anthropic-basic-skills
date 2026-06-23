#!/usr/bin/env python3
"""A股未来事件日历 —— 限售解禁 / 分红除权 / 业绩预告(前瞻指引)。东财 datacenter 权威口径。

为什么要有它:
  news.py 只抓"已发生"的公告标题; 但A股最强的可预期催化剂恰恰是"未来已知日程":
    · 限售解禁: 大比例解禁前后常有抛压, 是确定性的供给冲击, 却完全没进分析维度;
    · 分红除权: 股权登记日/除权除息日临近, 影响短期价格与情绪(填权/贴权预期);
    · 业绩预告: 预增/预减/扭亏/首亏 + 净利润变动幅度, 是财报披露前最硬的基本面前瞻。
  这三类都是结构化"硬事实"(日期+数量+比例/幅度), 同样不得采信 WebSearch 摘要
  (摘要常把旧解禁/旧预案/旧预告当成最新)。本脚本把它们固化成接口抓取, 供:
    · Step 1.5 前置体检: 临近大比例解禁 / 临近除权 → 风险与情绪闸门;
    · 维度一(资金)/维度四(经济): 解禁=供给压力, 业绩预告=基本面方向。

口径与边界:
  - 事件按"日期 vs 今日"分 upcoming(未来含今日) / past(已发生背景)。
  - 解禁 RPT_LIFT_STAGE: FREE_DATE 解禁日, FREE_SHARES 解禁股数, FREE_RATIO 占流通比%,
    LIFT_MARKET_CAP 解禁市值, FREE_SHARES_TYPE 限售类型(首发/定增/股权激励...)。
  - 分红 RPT_SHAREBONUS_DET: PLAN_NOTICE_DATE 预案公告, EQUITY_RECORD_DATE 股权登记日,
    EX_DIVIDEND_DATE 除权除息日, PRETAX_BONUS_RMB 每10股税前派现, ASSIGN_PROGRESS 进度
    (预案/股东大会通过/实施分配/不分配)。"预案"=已宣布未实施 → 仍是未来事件。
  - 业绩预告 RPT_PUBLIC_OP_NEWPREDICT: PREDICT_TYPE 类型, ADD_AMP_LOWER/UPPER 净利同比
    变动下/上限%, PREDICT_AMT_LOWER/UPPER 预测净利元, REPORT_DATE 报告期, IS_LATEST。
    预告是"已发布的前瞻指引"(对未公布财报的预期), 作基本面方向信号, 非当日资金打分。
  - 多数股票多数时候"无临近事件"是正常状态, 空结果如实写"无", 不臆造。

复用 quote.py 的 http_get / normalize_code / fmt_num / CST / UA 基建(与 funds/valuation 一致)。

用法:
    python3 events.py 600519                 # 解禁/分红除权/业绩预告
    python3 events.py 600519 000858          # 多只
    python3 events.py --json 600519          # JSON(供 analyze.py / 自动化消费)
    python3 events.py --lookback-days 365 600519   # 已发生事件回看窗(默认180)

退出码: 0 至少一只成功抓到任一接口(含合法空); 2 全部失败(网络/限流)。
注意: 需联网, datacenter 须带 User-Agent(已内置); 偶发限流属正常, 勿当代码 bug。
"""

import sys
import json
import argparse
import urllib.parse
from datetime import datetime, timedelta

from quote import http_get, normalize_code, fmt_num, UA, CST

DC_BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"

# 临近高亮窗口(自然日): 落在窗口内的未来事件计入"临近高风险事件"提示
LOCKUP_SOON_DAYS = 60      # 解禁: 60 天内
LOCKUP_BIG_RATIO = 3.0     # 解禁占流通比 >=3% 视为大比例(抛压值得警惕)
DIVIDEND_SOON_DAYS = 30    # 除权/登记: 30 天内
# 业绩预告里方向偏空/剧烈波动的类型(纳入提示)
PREDICT_BEAR_TYPES = ("预减", "首亏", "续亏", "增亏", "略减")
PREDICT_BULL_TYPES = ("预增", "扭亏", "续盈", "略增")
# 业绩预告时效: 公告超过此天数视为"较旧/可能已实现", 不再当前瞻信号高亮(仍在明细列出)
FORECAST_FRESH_DAYS = 120


def bare_code(secid):
    return secid.split(".", 1)[1]


def _dc_get(report_name, filter_expr, sort_col, page_size):
    """datacenter 通用 GET -> data 列表(空列表=无记录, 非错误)。与 funds/valuation 同口径。"""
    params = {
        "reportName": report_name, "columns": "ALL", "source": "WEB", "client": "WEB",
        "pageSize": str(page_size), "sortColumns": sort_col, "sortTypes": "-1",
        "filter": filter_expr,
    }
    url = DC_BASE + "?" + urllib.parse.urlencode(params)
    d = json.loads(http_get(url, headers={"User-Agent": UA})) or {}
    if not d.get("success"):
        msg = d.get("message") or ""
        if "为空" in msg or "没有" in msg:   # 合法的"无记录", 非错误
            return []
        raise RuntimeError(msg or "datacenter 返回 success=false")
    return ((d.get("result") or {}).get("data")) or []


def _date_only(s):
    """'2026-06-19 00:00:00' -> '2026-06-19'(无则 None)。"""
    return s.split(" ")[0] if s else None


def _to_date(s):
    """日期串 -> date 对象(解析失败 None)。"""
    d = _date_only(s)
    if not d:
        return None
    try:
        return datetime.strptime(d, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _today(now=None):
    return (now or datetime.now(CST)).date()


def classify(date_str, today=None):
    """事件相对今日: 'upcoming'(未来含今日) / 'past'(已发生) / None(无效日期)。"""
    d = _to_date(date_str)
    if d is None:
        return None
    return "upcoming" if d >= (today or _today()) else "past"


def days_until(date_str, today=None):
    """距事件日的自然日数(负=已过去); 无效日期 None。"""
    d = _to_date(date_str)
    if d is None:
        return None
    return (d - (today or _today())).days


# ---------- 限售解禁 (RPT_LIFT_STAGE) ----------

def fetch_lockup(code, lookback_days=180, today=None):
    """解禁: 拆未来(upcoming, 最近优先) + 最近一次已发生(背景)。"""
    rows = _dc_get("RPT_LIFT_STAGE", f'(SECURITY_CODE="{code}")', "FREE_DATE", 40)
    today = today or _today()

    def pack(r):
        return {
            "date": _date_only(r.get("FREE_DATE")),
            "days_until": days_until(r.get("FREE_DATE"), today),
            "shares": r.get("FREE_SHARES"),            # 解禁股数
            "free_ratio": r.get("FREE_RATIO"),         # 占流通比(%)
            "market_cap": r.get("LIFT_MARKET_CAP"),    # 解禁市值(元)
            "type": r.get("FREE_SHARES_TYPE"),         # 限售类型
        }
    upcoming = sorted(
        [pack(r) for r in rows if classify(r.get("FREE_DATE"), today) == "upcoming"],
        key=lambda x: x["days_until"] if x["days_until"] is not None else 1e9)
    past = [pack(r) for r in rows if classify(r.get("FREE_DATE"), today) == "past"]
    last_past = past[0] if past else None   # rows 按 FREE_DATE 降序, past[0]=最近一次
    return {"ok": True, "upcoming": upcoming, "last_past": last_past,
            "has_any": bool(rows)}


# ---------- 分红除权 (RPT_SHAREBONUS_DET) ----------

def fetch_dividend(code, today=None):
    """分红除权: 最新一期方案(进度/每10股派现) + 未来的股权登记日/除权日。"""
    rows = _dc_get("RPT_SHAREBONUS_DET", f'(SECURITY_CODE="{code}")', "PLAN_NOTICE_DATE", 8)
    today = today or _today()
    if not rows:
        return {"ok": True, "latest": None, "upcoming": [], "has_any": False}

    def pack(r):
        rec, ex = r.get("EQUITY_RECORD_DATE"), r.get("EX_DIVIDEND_DATE")
        return {
            "plan_notice_date": _date_only(r.get("PLAN_NOTICE_DATE")),
            "record_date": _date_only(rec),               # 股权登记日
            "ex_date": _date_only(ex),                    # 除权除息日
            "record_days_until": days_until(rec, today),
            "ex_days_until": days_until(ex, today),
            "pretax_per_10": r.get("PRETAX_BONUS_RMB"),   # 每10股税前派现(元)
            "progress": r.get("ASSIGN_PROGRESS"),         # 预案/股东大会通过/实施分配/不分配
            "report_date": _date_only(r.get("REPORT_DATE")),
        }
    latest = pack(rows[0])
    # 未来事件: 任一关键日(登记/除权)>= 今日
    upcoming = [p for p in (pack(r) for r in rows)
                if (p["record_days_until"] is not None and p["record_days_until"] >= 0)
                or (p["ex_days_until"] is not None and p["ex_days_until"] >= 0)]
    return {"ok": True, "latest": latest, "upcoming": upcoming, "has_any": True}


# ---------- 业绩预告 (RPT_PUBLIC_OP_NEWPREDICT) ----------

def fetch_forecast(code, today=None):
    """业绩预告: 最新一条(类型 + 净利同比变动幅度 + 报告期)。已发布的前瞻指引。
    带时效: 公告超 FORECAST_FRESH_DAYS 天视为'可能已实现'(forward=False), 不再当前瞻高亮。"""
    rows = _dc_get("RPT_PUBLIC_OP_NEWPREDICT", f'(SECURITY_CODE="{code}")', "NOTICE_DATE", 5)
    if not rows:
        return {"ok": True, "latest": None, "has_any": False}
    today = today or _today()
    latest = next((r for r in rows if r.get("IS_LATEST") in (1, "1", True)), rows[0])
    nd = _to_date(latest.get("NOTICE_DATE"))
    notice_days_ago = (today - nd).days if nd else None
    forward = notice_days_ago is not None and notice_days_ago <= FORECAST_FRESH_DAYS
    return {
        "ok": True, "has_any": True,
        "latest": {
            "notice_date": _date_only(latest.get("NOTICE_DATE")),
            "notice_days_ago": notice_days_ago,
            "forward": forward,                               # 是否仍具前瞻意义(够新)
            "report_date": _date_only(latest.get("REPORT_DATE")),
            "predict_type": latest.get("PREDICT_TYPE"),
            "indicator": latest.get("PREDICT_FINANCE"),       # 预测指标(归母净利润等)
            "amp_lower": latest.get("ADD_AMP_LOWER"),         # 净利同比变动下限(%)
            "amp_upper": latest.get("ADD_AMP_UPPER"),         # 净利同比变动上限(%)
            "amt_lower": latest.get("PREDICT_AMT_LOWER"),     # 预测净利下限(元)
            "amt_upper": latest.get("PREDICT_AMT_UPPER"),     # 预测净利上限(元)
            "content": latest.get("PREDICT_CONTENT"),
            "reason": latest.get("CHANGE_REASON_EXPLAIN"),
        },
    }


# ---------- 临近高风险/高关注事件提炼(供 Step 1.5 闸门 + 脚手架) ----------

def highlights(res, today=None):
    """从三类事件里提炼"临近且值得提示"的条目, 返回提示字符串列表(可空)。"""
    today = today or _today()
    out = []
    lk = res.get("lockup") or {}
    for ev in (lk.get("upcoming") or []):
        du, ratio = ev.get("days_until"), ev.get("free_ratio")
        if du is not None and du <= LOCKUP_SOON_DAYS:
            big = isinstance(ratio, (int, float)) and ratio >= LOCKUP_BIG_RATIO
            ratio_s = f"占流通{ratio:.1f}%" if isinstance(ratio, (int, float)) else "比例未知"
            out.append(f"⚠️ {du}天后解禁({ev.get('date')}, {ratio_s}, "
                       f"{fmt_num(ev.get('market_cap'), '元')}{'·大比例抛压' if big else ''})")
    dv = res.get("dividend") or {}
    for ev in (dv.get("upcoming") or []):
        for key, lbl in (("record_days_until", "股权登记"), ("ex_days_until", "除权除息")):
            du = ev.get(key)
            if du is not None and 0 <= du <= DIVIDEND_SOON_DAYS:
                date = ev.get("record_date") if key == "record_days_until" else ev.get("ex_date")
                out.append(f"📅 {du}天后{lbl}日({date}, 每10股派{fmt_num(ev.get('pretax_per_10'))}元税前)")
    fc = (res.get("forecast") or {}).get("latest")
    if fc and fc.get("predict_type") and fc.get("forward"):  # 仅够新的预告才当前瞻高亮
        pt = fc["predict_type"]
        amp = fc.get("amp_lower")
        amp_s = (f", 净利同比{fc.get('amp_lower'):+.0f}~{fc.get('amp_upper'):+.0f}%"
                 if isinstance(amp, (int, float)) and isinstance(fc.get("amp_upper"), (int, float)) else "")
        if any(t in pt for t in PREDICT_BEAR_TYPES):
            out.append(f"📉 业绩预告[{pt}]({fc.get('report_date')}{amp_s})")
        elif any(t in pt for t in PREDICT_BULL_TYPES):
            out.append(f"📈 业绩预告[{pt}]({fc.get('report_date')}{amp_s})")
        else:
            out.append(f"📊 业绩预告[{pt}]({fc.get('report_date')}{amp_s})")
    return out


def analyze_one(user_code, lookback_days=180):
    try:
        _, secid, disp = normalize_code(user_code)
    except ValueError as e:
        return {"input": user_code, "ok": False, "error": str(e)}
    code = bare_code(secid)
    out = {"input": user_code, "display_code": disp, "secid": secid, "ok": False}
    for key, fn in (("lockup", lambda: fetch_lockup(code, lookback_days)),
                    ("dividend", lambda: fetch_dividend(code)),
                    ("forecast", lambda: fetch_forecast(code))):
        try:
            out[key] = fn()
        except Exception as e:  # noqa: BLE001 - 单接口失败不拖垮其余
            out[key] = {"ok": False, "error": f"{key} 抓取异常: {e}"}
    out["ok"] = any((out.get(k) or {}).get("ok") for k in ("lockup", "dividend", "forecast"))
    out["highlights"] = highlights(out)
    return out


def print_one(item):
    if not item.get("ok"):
        print(f"  ✗ {item.get('input')}: {item.get('error') or '抓取失败'}")
        return
    print(f"  {item['display_code']}")

    hl = item.get("highlights") or []
    if hl:
        print("    临近/重点事件:")
        for h in hl:
            print(f"      {h}")

    lk = item.get("lockup") or {}
    if not lk.get("ok"):
        print(f"    解禁: ✗ {lk.get('error')}")
    elif lk.get("upcoming"):
        print(f"    未来解禁 {len(lk['upcoming'])} 次:")
        for ev in lk["upcoming"][:4]:
            ratio = ev.get("free_ratio")
            ratio_s = f"占流通{ratio:.2f}%" if isinstance(ratio, (int, float)) else "占比—"
            print(f"      {ev['date']}(还有{ev.get('days_until')}天)  {ratio_s}  "
                  f"解禁市值{fmt_num(ev.get('market_cap'), '元')}  {ev.get('type') or ''}")
    else:
        lp = lk.get("last_past")
        tail = f"(最近一次 {lp['date']})" if lp else ""
        print(f"    未来解禁: 无 {tail}")

    dv = item.get("dividend") or {}
    if not dv.get("ok"):
        print(f"    分红除权: ✗ {dv.get('error')}")
    else:
        lt = dv.get("latest")
        if lt:
            print(f"    最新分红方案 [{lt.get('progress') or '—'}] 报告期{lt.get('report_date') or '—'}: "
                  f"每10股派{fmt_num(lt.get('pretax_per_10'))}元税前")
        for ev in (dv.get("upcoming") or [])[:3]:
            parts = []
            if ev.get("record_days_until") is not None and ev["record_days_until"] >= 0:
                parts.append(f"股权登记日 {ev.get('record_date')}(还有{ev['record_days_until']}天)")
            if ev.get("ex_days_until") is not None and ev["ex_days_until"] >= 0:
                parts.append(f"除权除息日 {ev.get('ex_date')}(还有{ev['ex_days_until']}天)")
            if parts:
                print(f"      未来: " + "  ".join(parts))
        if not lt and not dv.get("upcoming"):
            print(f"    分红除权: 无近期方案")

    fc = item.get("forecast") or {}
    if not fc.get("ok"):
        print(f"    业绩预告: ✗ {fc.get('error')}")
    elif fc.get("latest"):
        lt = fc["latest"]
        amp = lt.get("amp_lower")
        amp_s = (f"  净利同比 {lt.get('amp_lower'):+.1f}~{lt.get('amp_upper'):+.1f}%"
                 if isinstance(amp, (int, float)) and isinstance(lt.get("amp_upper"), (int, float)) else "")
        stale = "" if lt.get("forward") else "  (较旧, 可能已实现, 仅作背景)"
        print(f"    业绩预告 [{lt.get('predict_type') or '—'}] 报告期{lt.get('report_date') or '—'}"
              f"(公告{lt.get('notice_date') or '—'}){amp_s}{stale}")
        if lt.get("content"):
            print(f"      {str(lt['content'])[:80]}")
    else:
        print(f"    业绩预告: 无")


def main():
    ap = argparse.ArgumentParser(description="A股未来事件日历: 解禁/分红除权/业绩预告(东财口径)")
    ap.add_argument("codes", nargs="+", help="股票代码, 如 600519 000858")
    ap.add_argument("--lookback-days", type=int, default=180, help="已发生事件回看窗(默认180)")
    ap.add_argument("--json", action="store_true", help="仅输出 JSON")
    args = ap.parse_args()

    items = [analyze_one(c, lookback_days=args.lookback_days) for c in args.codes]

    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
    else:
        print(f"抓取时间(本地 CST): {datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 60)
        for it in items:
            print_one(it)
            print()
        print("提示: 解禁=供给冲击(大比例临近警惕抛压); 除权登记日临近影响短期情绪;")
        print("      业绩预告=财报前最硬的基本面方向。均为'硬日期事实', 勿采信搜索摘要。")
        print("      多数票'无临近事件'是正常状态, 空即如实写'无'。")

    sys.exit(0 if any(it.get("ok") for it in items) else 2)


if __name__ == "__main__":
    main()
