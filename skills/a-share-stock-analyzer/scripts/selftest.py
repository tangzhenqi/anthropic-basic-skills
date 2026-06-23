#!/usr/bin/env python3
"""接口契约自检 —— 一条命令验证东财/腾讯各接口字段口径是否仍如脚本预期。

为什么要有它:
  这套 skill 重度依赖东财未公开字段码(f43/f170/RZYE/ORIG_BOARD_CODE...)。一旦东财
  悄改字段或返回格式漂移, fetch_* 会"静默取错数"却不报错 —— 这比限流更危险(限流会
  抛错, 漂移不会)。本脚本对固定活跃标的逐接口断言"关键字段存在/类型/量级合理", 把
  「限流(THROTTLED, 稍后自愈)」与「接口真变了(FAIL, 要改代码)」明确分开, 一眼定位。

  典型用法: 报告"数字看着不对/某维总是空"时先跑一遍; 或定期(/loop)健康巡检。

用法:
    python3 selftest.py                  # 默认标的 600519(活跃、有两融/分红, 覆盖最全)
    python3 selftest.py 000001           # 换标的(建议非ST活跃股)
    python3 selftest.py --retries 3      # 每项遇限流多重试几次(默认2)
    python3 selftest.py --json

退出码: 0 全部通过(或仅限流); 1 检测到契约不符(字段/格式变更, 需改码); 2 全部限流(无法判定)。
"""

import sys
import json
import time
import argparse

import quote
import valuation
import funds
import news
import events
import breadth
import trading_calendar as _cal

# 网络/限流类异常的特征词: 命中即判 THROTTLED(可自愈), 而非 FAIL(契约变更)
NETWORK_MARKERS = ("remote end closed", "remotedisconnected", "timed out", "timeout",
                   "connection", "urlerror", "ssl", "reset", "refused", "errno",
                   "限流", "too many", "503", "502")


def _is_network(text):
    t = (text or "").lower()
    return any(m in t for m in NETWORK_MARKERS)


def _isnum(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check(res):
    """断言结果里 ok=False 是不是限流导致(供各 assert 复用)。返回 True=限流。"""
    return _is_network(str((res or {}).get("error") or (res or {}).get("reason") or ""))


# ---- 各接口的契约断言: 入参为 fetch 结果, 返回 (status, detail) ----
# status ∈ {"PASS","FAIL","THROTTLED","SKIP"}; 合法的"空结果"(非两融/无龙虎榜/无分红)判 PASS。

def a_tencent(r):
    if not r.get("ok"):
        return ("THROTTLED" if _check(r) else "FAIL", r.get("error") or "ok=False")
    if not (_isnum(r.get("price")) and r["price"] > 0):
        return "FAIL", f"price 异常: {r.get('price')!r}"
    if not r.get("name"):
        return "FAIL", "name 缺失"
    if not r.get("timestamp"):
        return "FAIL", "timestamp 缺失(腾讯字段30格式变更?)"
    return "PASS", f"现价{r['price']:g} {r.get('name')} ts={r['timestamp'][11:16]}"


def a_em_snapshot(r):
    if not r.get("ok"):
        return ("THROTTLED" if _check(r) else "FAIL", r.get("error") or "ok=False")
    if not (_isnum(r.get("price")) and r["price"] > 0):
        return "FAIL", f"price 异常(f43缩放?): {r.get('price')!r}"
    if not r.get("industry"):
        return "FAIL", "industry(f127) 缺失 -> 权重模板自动建议会失效"
    return "PASS", f"现价{r['price']:g} 行业={r['industry']}"


def a_fflow(r):
    if not r.get("ok"):
        return ("THROTTLED" if _check(r) else "FAIL", r.get("reason") or "ok=False")
    if not _isnum(r.get("today_main_net")):
        return "FAIL", f"today_main_net 非数值(f52口径?): {r.get('today_main_net')!r}"
    if not (r.get("daily") and r.get("days_counted")):
        return "FAIL", "近5日序列为空(fflow klines 解析变更?)"
    return "PASS", f"今日主力{r['today_main_net']/1e8:+.2f}亿 近{r['days_counted']}日有数"


def a_kline(r, label="个股K线", min_bars=60):
    if not r.get("ok"):
        return ("THROTTLED" if _check(r) else "FAIL", r.get("reason") or "ok=False")
    if not (r.get("bars") and r["bars"] >= min_bars):
        return "FAIL", f"bars={r.get('bars')} 不足{min_bars}(K线接口截断/为空?)"
    for k in ("ma20", "ma60", "last_close"):
        if not (_isnum(r.get(k)) and r[k] > 0):
            return "FAIL", f"{k} 异常: {r.get(k)!r}"
    p = r.get("turnover_pctl")
    if p is not None and not (0 <= p <= 100):
        return "FAIL", f"换手分位越界: {p}"
    return "PASS", f"bars={r['bars']} MA20={r['ma20']:.1f} last={r['last_close']:.1f} ret5={r.get('ret_5')}"


def a_sector(r, bk):
    if not r.get("ok"):
        return ("THROTTLED" if _check(r) else "FAIL", r.get("reason") or "ok=False")
    if not r.get("boards"):
        return "FAIL", "boards 为空(slist diff 结构变更?)"
    ind = r.get("industry")
    if not ind:
        return "FAIL", f"未定位到行业板块(target_bk={bk}; f12/f14 匹配失效?)"
    if not _isnum(ind.get("change_pct")):
        return "FAIL", f"板块涨跌(f3)非数值: {ind.get('change_pct')!r}"
    if not _isnum(ind.get("net_inflow")):
        return "FAIL", f"板块主力净流入(f62)非数值: {ind.get('net_inflow')!r}"
    return "PASS", f"{ind['name']}({ind['bk']}) {ind['change_pct']:+.2f}% 净流入{ind['net_inflow']/1e8:+.2f}亿"


def a_val_snapshot(r):
    if not r.get("ok"):
        return ("THROTTLED" if _check(r) else "FAIL", r.get("error") or "ok=False")
    if not (_isnum(r.get("pb")) and r["pb"] > 0):
        return "FAIL", f"PB(f167) 异常: {r.get('pb')!r}"
    if not r.get("loss_making") and not _isnum(r.get("pe_ttm")):
        return "FAIL", f"PE-TTM(f163) 非数值: {r.get('pe_ttm')!r}"
    return "PASS", f"PE-TTM={r.get('pe_ttm')} PB={r['pb']:.2f}"


def a_val_ps(r):
    if not r.get("ok"):
        return ("THROTTLED" if _check(r) else "FAIL", "ok=False(可能限流或代码无估值)")
    if not r.get("board_name"):
        return "FAIL", "BOARD_NAME 缺失"
    bk = r.get("board_bk")
    if bk is None:
        return "FAIL", "board_bk 未派生(ORIG_BOARD_CODE 缺失/非数字 -> 板块定位会退化为名称匹配)"
    import re
    if not re.match(r"^BK\d{4}$", bk):
        return "FAIL", f"board_bk 格式异常: {bk!r}(应为 BKdddd)"
    return "PASS", f"PS={r.get('ps_ttm')} 行业={r['board_name']} {bk}"


def a_val_pctl(r):
    if not r.get("ok"):
        return ("THROTTLED" if _check(r) else "FAIL", "ok=False(限流或无分位序列)")
    gp = r.get("guardrail_pctl")
    if gp is None:
        return "FAIL", "guardrail_pctl 缺失(PE/PB 分位均算不出 -> 估值护栏失效)"
    if not (0 <= gp <= 100):
        return "FAIL", f"分位越界: {gp}"
    return "PASS", f"护栏分位{gp:g}% (基准{r.get('guardrail_basis')})"


def a_dividend(r):
    if not r.get("ok"):
        return ("THROTTLED" if _check(r) else "FAIL", "ok=False")
    if r.get("no_dividend"):
        return "PASS", "近12月无派现(合法空)"
    if not _isnum(r.get("ttm_per_share_pretax")):
        return "FAIL", f"每股派现非数值(PRETAX_BONUS_RMB?): {r.get('ttm_per_share_pretax')!r}"
    return "PASS", f"每股派现{r['ttm_per_share_pretax']:.2f}元(派现字段在; 自检用占位价不验收益率)"


def a_margin(r):
    if not r.get("ok"):
        return ("THROTTLED" if _check(r) else "FAIL", r.get("error") or "ok=False")
    if not r.get("is_margin_target"):
        return "PASS", "非两融标的(合法空)"
    if not _isnum(r.get("rzye")):
        return "FAIL", f"融资余额(RZYE)非数值: {r.get('rzye')!r}"
    return "PASS", f"融资余额{r['rzye']/1e8:.2f}亿 趋势{r.get('trend')}"


def a_lhb(r):
    if not r.get("ok"):
        return ("THROTTLED" if _check(r) else "FAIL", r.get("error") or "ok=False")
    if not isinstance(r.get("records"), list):
        return "FAIL", "records 非列表(龙虎榜接口结构变更?)"
    return "PASS", (f"近期上榜{len(r['records'])}次" if r.get("on_list") else "近期无上榜(合法空)")


def a_news(r):
    if not r.get("ok"):
        return ("THROTTLED" if _check(r) else "FAIL", r.get("error") or "ok=False")
    if not isinstance(r.get("items"), list):
        return "FAIL", "items 非列表(公告接口结构变更?)"
    return "PASS", f"近{r.get('days')}天 {r.get('count')} 条公告"


def a_events(r):
    # events.analyze_one 聚合 解禁/分红/业绩预告 三接口, 各自空结果是合法的(多数票无事件)
    if not r.get("ok"):
        # 全三路失败时, 取任一子错误判限流 vs 契约
        errs = " ".join(str((r.get(k) or {}).get("error") or "") for k in ("lockup", "dividend", "forecast"))
        return ("THROTTLED" if _is_network(errs) else "FAIL", errs or "三路全 ok=False")
    for k, label in (("lockup", "解禁RPT_LIFT_STAGE"), ("dividend", "分红RPT_SHAREBONUS_DET"),
                     ("forecast", "预告RPT_PUBLIC_OP_NEWPREDICT")):
        sub = r.get(k) or {}
        if not isinstance(sub, dict) or ("ok" not in sub):
            return "FAIL", f"{label} 结构缺失(字段/解析变更?)"
    if not isinstance(r.get("highlights"), list):
        return "FAIL", "highlights 非列表"
    nlk = len((r.get("lockup") or {}).get("upcoming") or [])
    fwd = ((r.get("forecast") or {}).get("latest") or {}).get("forward")
    return "PASS", f"未来解禁{nlk}次 预告forward={fwd} 临近事件{len(r['highlights'])}条"


def a_breadth(r):
    # snapshot 聚合 成交额/指数/涨跌停; 不依赖个股代码, 市场级
    if not r.get("ok"):
        errs = " ".join(str((r.get(k) or {}).get("error") or "") for k in ("turnover", "indices", "limits"))
        return ("THROTTLED" if _is_network(errs) else "FAIL", errs or "三路全 ok=False")
    t = r.get("turnover") or {}
    if t.get("ok") and not _isnum(t.get("total_amount")):
        return "FAIL", f"两市成交额(沪综+深综 f6求和)非数值: {t.get('total_amount')!r}"
    lm = r.get("limits") or {}
    if lm.get("ok"):
        for k in ("limit_up", "limit_down"):
            v = lm.get(k)
            if v is not None and not isinstance(v, int):
                return "FAIL", f"{k} 非整数(push2ex 池 tc 字段变更?): {v!r}"
    amt = t.get("total_amount")
    amt_s = f"{amt/1e8:.0f}亿" if _isnum(amt) else "—"
    return "PASS", f"两市成交额{amt_s} 涨停{lm.get('limit_up')}/跌停{lm.get('limit_down')}"


STATUS_ICON = {"PASS": "✅", "FAIL": "❌", "THROTTLED": "⚠️", "SKIP": "·", "WARN": "🗓️"}


def calendar_health():
    """交易日历到期体检(无网络): 节假日表过期/临近过期会让 freshness 退回启发式、长假误判。
    返回 (status, detail)。status=WARN 不影响退出码(非接口契约问题), 但打印醒目提示维护。"""
    d = _cal.days_until_expiry()
    through = _cal.CALENDAR_VALID_THROUGH.isoformat()
    if d <= 0:
        return "WARN", (f"节假日表已过期 {-d} 天(覆盖至 {through}); freshness 已回退'周末+≤4日'"
                        f"启发式, 长假可能误判 -> 请在 trading_calendar.py 追加新一年节假日并推进 "
                        f"CALENDAR_VALID_THROUGH")
    if d <= 60:
        return "WARN", (f"节假日表仅剩 {d} 天到期(至 {through}); 国务院一般 11 月底发布次年安排, "
                        f"请尽快追加并把 CALENDAR_VALID_THROUGH 推到次年 12-31")
    return "PASS", f"节假日表有效, 剩 {d} 天到期(至 {through})"


def _retrying(fetch, assert_fn, retries, *args):
    """跑一项检查; 仅 THROTTLED 时重试(限流可自愈), FAIL 不重试(真问题)。"""
    last = ("THROTTLED", "未执行")
    for attempt in range(retries + 1):
        try:
            res = fetch()
        except Exception as e:  # noqa: BLE001 - 网络异常 -> 限流; 其余也按可重试处理
            status = "THROTTLED" if _is_network(f"{type(e).__name__}: {e}") else "FAIL"
            last = (status, f"{type(e).__name__}: {e}")
        else:
            try:
                status, detail = assert_fn(res, *args) if args else assert_fn(res)
            except Exception as e:  # noqa: BLE001 - 断言内部炸 = 结构意外, 判 FAIL
                status, detail = "FAIL", f"断言异常 {type(e).__name__}: {e}"
            last = (status, detail)
        if last[0] != "THROTTLED":
            return last
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    return last


def main():
    ap = argparse.ArgumentParser(description="东财/腾讯接口契约自检(区分限流 vs 字段变更)")
    ap.add_argument("code", nargs="?", default="600519", help="自检标的(默认600519)")
    ap.add_argument("--retries", type=int, default=2, help="每项遇限流的重试次数(默认2)")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()

    quote.set_cache_ttl(0)  # 自检必须打真接口, 强制关缓存
    tx_code, secid, disp = quote.normalize_code(args.code)
    bare = secid.split(".", 1)[1]
    R = args.retries

    # 先取估值 ps(拿 board_bk/board_name 给板块相关检查复用); 它本身也是一项检查
    ps_res = {}
    try:
        ps_res = valuation.fetch_ps(bare)
    except Exception:  # noqa: BLE001 - 失败则板块检查退化
        ps_res = {}
    bk = ps_res.get("board_bk")
    bname = ps_res.get("board_name")

    results = []
    results.append(("交易日历有效期", *calendar_health()))  # 无网络, 维护性体检
    results.append(("腾讯行情", *_retrying(lambda: quote.fetch_tencent(tx_code), a_tencent, R)))
    results.append(("东财快照", *_retrying(lambda: quote.fetch_eastmoney(secid), a_em_snapshot, R)))
    results.append(("主力资金流", *_retrying(lambda: quote.fetch_fflow(secid), a_fflow, R)))
    results.append(("个股日K", *_retrying(lambda: quote.fetch_kline(secid), a_kline, R)))
    results.append(("个股所属板块", *_retrying(
        lambda: quote.fetch_sector(secid, bk, bname), a_sector, R, bk)))
    if bk:
        results.append(("板块日K", *_retrying(
            lambda: quote.fetch_kline(f"90.{bk}"), lambda r: a_kline(r, "板块K线"), R)))
    else:
        results.append(("板块日K", "SKIP", "无 board_bk, 跳过(见'东财估值PS')"))
    results.append(("东财估值快照", *_retrying(lambda: valuation.fetch_valuation(secid), a_val_snapshot, R)))
    ps_status, ps_detail = a_val_ps(ps_res) if ps_res else ("THROTTLED", "fetch_ps 未取到(限流?)")
    results.append(("东财估值PS/板块码", ps_status, ps_detail))
    results.append(("估值历史分位", *_retrying(lambda: valuation.fetch_val_pctl(bare), a_val_pctl, R)))
    results.append(("股息率", *_retrying(lambda: valuation.fetch_dividend(bare, 100.0), a_dividend, R)))
    results.append(("融资融券", *_retrying(lambda: funds.fetch_margin(bare), a_margin, R)))
    results.append(("龙虎榜", *_retrying(lambda: funds.fetch_lhb(bare), a_lhb, R)))
    results.append(("公告中心", *_retrying(lambda: news.analyze_one(args.code), a_news, R)))
    results.append(("未来事件", *_retrying(lambda: events.analyze_one(args.code), a_events, R)))
    results.append(("市场宽度", *_retrying(lambda: breadth.snapshot(), a_breadth, R)))

    n_pass = sum(1 for _, s, _ in results if s == "PASS")
    n_fail = sum(1 for _, s, _ in results if s == "FAIL")
    n_thr = sum(1 for _, s, _ in results if s == "THROTTLED")
    n_skip = sum(1 for _, s, _ in results if s == "SKIP")
    n_warn = sum(1 for _, s, _ in results if s == "WARN")

    if args.json:
        print(json.dumps({"code": disp, "results": [
            {"name": n, "status": s, "detail": d} for n, s, d in results],
            "pass": n_pass, "fail": n_fail, "throttled": n_thr,
            "skip": n_skip, "warn": n_warn},
            ensure_ascii=False, indent=2))
    else:
        print(f"接口契约自检  标的 {disp}  (缓存强制关闭, 每项限流重试{R}次)")
        print("=" * 64)
        for name, status, detail in results:
            print(f"  {STATUS_ICON.get(status, '?')} {status:<10}{name:<14}{detail}")
        print("-" * 64)
        print(f"结果: {n_pass} PASS / {n_fail} FAIL / {n_thr} THROTTLED"
              + (f" / {n_skip} SKIP" if n_skip else "")
              + (f" / {n_warn} WARN" if n_warn else ""))
        if n_warn:
            print("🗓️ 有维护性告警(见上'交易日历有效期'行); 不影响接口判定, 但请尽快处理。")
        if n_fail:
            print("❌ 检测到契约不符 -> 东财字段/格式可能已变更, 对照 reference 排查对应 fetch_*。")
        elif n_thr and not n_pass:
            print("⚠️ 全部限流, 无法判定接口是否正常; 稍后(或加 --retries)重试。")
        elif n_thr:
            print("⚠️ 部分限流(可自愈, 非接口问题); 其余已通过。")
        else:
            print("✅ 全部接口契约正常。")

    if n_fail:
        sys.exit(1)
    if n_thr and not n_pass:
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
