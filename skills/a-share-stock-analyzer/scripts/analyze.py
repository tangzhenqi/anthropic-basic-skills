#!/usr/bin/env python3
"""A股个股分析统一编排器 —— 一条命令跑齐 行情+资金+估值, 并产出报告骨架。

为什么要有它:
  SKILL 此前要模型分别记得跑 quote.py / funds.py / valuation.py, 容易漏跑(尤其
  funds、valuation), 也重复做代码归一化。本脚本一次并发跑齐三路数据, 合并输出,
  并按 quote 返回的行业自动建议 Step 3.1 权重模板、预填 score.py 命令, 把"该填的
  当日硬数据"全部备齐, 模型只需补搜索类维度(国际/经济政策/情绪舆情)与各维方向强度。

它不替代子脚本: quote/funds/valuation/score 仍可单独调用; 本脚本只是编排+脚手架。
评分不自动算 —— 四维方向/强度是模型的判断(国际/经济需搜索), 故只预填 score.py 命令。

用法:
    python3 analyze.py 600519                 # 跑齐三路数据 + 大盘(上证/沪深300)
    python3 analyze.py 600519 --no-index      # 不附带大盘指数
    python3 analyze.py 600519 --lhb-days 30    # 自定义龙虎榜回看
    python3 analyze.py --json 600519           # JSON(供自动化消费)

退出码: 0 至少一路数据可用; 2 全部失败(网络/限流)。
"""

import sys
import json
import argparse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

# 复用三个子脚本的 analyze_one + 打印函数(同目录)
import quote
import funds
import valuation
import news
import events
import breadth
from quote import CST

# 大盘背景默认附带: 上证指数 + 沪深300
DEFAULT_INDICES = ["sh000001", "sh000300"]

# 东财细分行业关键词 -> Step 3.1 模板大类(顺序优先: 先匹配到先用)
INDUSTRY_KEYWORDS = [
    ("消费医药", ["白酒", "食品", "饮料", "调味", "乳", "肉", "养殖", "啤酒", "医药",
                "医疗", "生物", "中药", "疫苗", "器械", "消费", "家电", "零售", "商业",
                "纺织", "服装", "旅游", "酒店", "餐饮", "家居"]),
    ("科技成长", ["半导体", "芯片", "集成电路", "光伏", "电池", "锂", "新能源", "风电",
                "储能", "军工", "国防", "航空", "航天", "计算机", "软件", "电子", "通信",
                "互联网", "传媒", "游戏", "人工智能", "AI", "云", "数据", "电力设备"]),
    ("周期", ["钢铁", "煤炭", "有色", "金属", "石油", "石化", "化工", "化纤", "采掘",
            "建材", "水泥", "工程", "机械", "船舶", "航运", "港口", "玻璃", "橡胶"]),
    ("金融", ["银行", "券商", "证券", "保险", "信托", "期货", "多元金融", "金融"]),
]


def suggest_template(industry):
    if not industry:
        return "默认", "行业未知, 用默认/均衡"
    for tmpl, kws in INDUSTRY_KEYWORDS:
        for kw in kws:
            if kw in industry:
                return tmpl, f"按行业〔{industry}〕含「{kw}」"
    return "默认", f"行业〔{industry}〕未匹配模板, 用默认/均衡(可人工覆盖)"


def is_st(name):
    return bool(name) and ("ST" in name.upper() or "退" in name)


def run_all(code, lhb_days, indices):
    """并发跑齐: 个股 quote/funds/valuation + 各大盘指数 quote。"""
    out = {"input": code}
    with ThreadPoolExecutor(max_workers=7 + len(indices)) as ex:
        f_q = ex.submit(quote.analyze_one, code)
        f_f = ex.submit(funds.analyze_one, code, lhb_days)
        f_v = ex.submit(valuation.analyze_one, code)
        f_n = ex.submit(news.analyze_one, code)
        f_e = ex.submit(events.analyze_one, code)
        # 市场宽度是大盘级背景, 仅在附带大盘指数时抓(与 --no-index 同进退)
        f_b = ex.submit(breadth.snapshot) if indices else None
        f_idx = {ix: ex.submit(quote.analyze_one, ix) for ix in indices}
        out["quote"] = _safe(f_q, {"ok": False, "error": "行情抓取异常"})
        out["funds"] = _safe(f_f, {"ok": False, "error": "资金面抓取异常"})
        out["valuation"] = _safe(f_v, {"ok": False, "error": "估值抓取异常"})
        out["news"] = _safe(f_n, {"ok": False, "error": "公告抓取异常"})
        out["events"] = _safe(f_e, {"ok": False, "error": "未来事件抓取异常"})
        if f_b is not None:
            out["breadth"] = _safe(f_b, {"ok": False, "error": "市场宽度抓取异常"})
        out["indices"] = {ix: _safe(fut, {"ok": False}) for ix, fut in f_idx.items()}
    return out


def _safe(fut, fallback):
    try:
        return fut.result()
    except Exception as e:  # noqa: BLE001 - 单路失败不拖垮编排
        fb = dict(fallback)
        fb["error"] = f"{fb.get('error', '异常')}: {e}"
        return fb


def _val_pctl(valres):
    pc = (valres or {}).get("pctl") or {}
    return pc.get("guardrail_pctl") if pc.get("ok") else None


def _industry(res):
    """行业: 优先 quote 的东财行业(f127); 快照限流缺失时退到 valuation 的 datacenter BOARD_NAME。"""
    q_ind = (res.get("quote") or {}).get("industry")
    if q_ind:
        return q_ind
    return ((res.get("valuation") or {}).get("ps") or {}).get("board_name")


def _price_of(qres):
    """从 quote.analyze_one 结果取已解析现价(腾讯优先, 退东财)。"""
    tx, em = (qres or {}).get("tencent") or {}, (qres or {}).get("eastmoney") or {}
    if tx.get("ok") and tx.get("price"):
        return tx.get("price")
    return em.get("price") if em.get("ok") else None


# 大盘择时基准: 个股相对强弱优先对沪深300, 缺则上证
BENCH_PREF = ["sh000300", "sh000001"]
WIN_LABEL = {"ret_1": "1日", "ret_5": "5日", "ret_20": "20日"}


def market_timing(indices):
    """各大盘指数的均线多空背景(确定性, 替掉'A股市场情绪'那条 WebSearch)。"""
    out = {}
    for ix, qres in (indices or {}).items():
        kl = (qres or {}).get("kline") or {}
        price = _price_of(qres)
        direction, why = quote.ma_trend(price, kl.get("ma20"), kl.get("ma60"))
        out[ix] = {"name": (qres or {}).get("name") or ix, "direction": direction,
                   "why": why, "ret_5": kl.get("ret_5"), "ret_20": kl.get("ret_20")}
    return out


def relative_strength(stock_q, indices):
    """个股 vs 基准大盘 近 1/5/20 日相对强弱(RS = 个股收益 − 基准收益, 单位 %)。
    RS>0 = 跑赢大盘(强于大盘/可能逆势走强); RS<0 = 跑输(随大盘或弱于大盘)。"""
    skl = (stock_q or {}).get("kline") or {}
    bench_code = next((b for b in BENCH_PREF if (indices or {}).get(b, {}).get("ok")), None)
    if not bench_code:
        return None
    bkl = (indices[bench_code] or {}).get("kline") or {}
    rs = {}
    for w in ("ret_1", "ret_5", "ret_20"):
        s, b = skl.get(w), bkl.get(w)
        rs[w] = round(s - b, 2) if isinstance(s, (int, float)) and isinstance(b, (int, float)) else None
    return {"benchmark": bench_code,
            "benchmark_name": (indices[bench_code] or {}).get("name") or bench_code,
            "stock": {w: skl.get(w) for w in ("ret_1", "ret_5", "ret_20")},
            "rs": rs}


def _stock_change_pct(qres):
    """个股当日涨跌幅(腾讯优先退东财), 供'个股 vs 板块'当日相对强弱。"""
    tx, em = (qres or {}).get("tencent") or {}, (qres or {}).get("eastmoney") or {}
    if tx.get("ok") and isinstance(tx.get("change_pct"), (int, float)):
        return tx["change_pct"]
    return em.get("change_pct") if (em.get("ok") and isinstance(em.get("change_pct"), (int, float))) else None


# 指数成分/风格/通道类"元板块": 聚合上千只票, 净流入天然巨大却无题材信号意义, 排除。
META_BOARD_KEYWORDS = (
    "融资融券", "转融券", "沪股通", "深股通", "陆股通", "标普", "道琼斯",
    "MSCI", "富时", "罗素", "成份股", "成分股", "重仓", "预盈", "预增", "预亏",
    "举牌", "AH股", "B股", "QFII", "社保", "证金", "基金重仓", "破净",
    "大盘股", "中盘股", "小盘股", "微盘股",   # 市值风格, 非题材
)


def _is_meta_board(name):
    return bool(name) and any(k in name for k in META_BOARD_KEYWORDS)


def hot_boards(boards, exclude_bk, stock_chg, top=2):
    """从个股所属全部板块里挑'主力净流入最强'的若干个(剔除已单列的行业板块 + 指数/风格元板块)。

    为什么: slist 一次已把个股所属行业/概念/题材板块全抓回(boards), 此前只用了行业板块。
    A股题材驱动, 个股当日最相关的同涨板块往往是某概念/题材(如机器人/AI/低空), 其主力净
    流入与'个股 vs 该板块'当日相对强弱, 比行业板块更能解释当日异动。纯展示层, 不新增请求。
    注: slist 里混有'融资融券/沪股通/MSCI'等指数成分元板块(聚合上千票、净流入天然最大),
    它们无题材意义却会霸榜, 故按关键词剔除; 剩余按主力净流入排序, 由模型判概念/地域性质。
    """
    pool = [b for b in (boards or [])
            if b.get("bk") != exclude_bk and isinstance(b.get("net_inflow"), (int, float))
            and not _is_meta_board(b.get("name"))]
    pool.sort(key=lambda b: b["net_inflow"], reverse=True)
    out = []
    for b in pool[:top]:
        rs = (round(stock_chg - b["change_pct"], 2)
              if isinstance(stock_chg, (int, float)) and isinstance(b.get("change_pct"), (int, float))
              else None)
        out.append({**b, "rs_1": rs})
    return out


def sector_block(stock_q, valuation):
    """个股所属行业板块背景: 当日涨跌/主力净流入(slist 单请求) + 个股相对板块 RS + 板块择时。

    定位行业板块: 优先用 valuation 的 board_bk(ORIG_BOARD_CODE 派生), 退到 quote 行业名/
    datacenter BOARD_NAME 名称匹配。当日 RS(个股涨跌−板块涨跌)slist 即给; 5/20 日 RS 与
    板块择时用板块 K线(90.BKxxxx)补, best-effort —— 板块 K线限流失败不影响当日 RS。
    """
    secid = (stock_q or {}).get("secid")
    if not secid:
        return None
    ps = (valuation or {}).get("ps") or {}
    target_bk = ps.get("board_bk")
    target_name = (stock_q or {}).get("industry") or ps.get("board_name")
    try:
        sect = quote.fetch_sector(secid, target_bk, target_name)
    except Exception as e:  # noqa: BLE001 - 板块是增量背景, 失败不拖垮主流程
        return {"ok": False, "reason": f"板块抓取异常: {e}"}
    if not sect.get("ok"):
        return sect

    board = sect.get("industry")
    bk = (board or {}).get("bk") or target_bk
    skl = (stock_q or {}).get("kline") or {}
    rs = {"ret_1": None, "ret_5": None, "ret_20": None}
    # 当日 RS: 个股涨跌幅 − 板块涨跌幅(slist 即给, 不依赖板块 K线)
    stock_chg = _stock_change_pct(stock_q)
    if board and isinstance(stock_chg, (int, float)) and isinstance(board.get("change_pct"), (int, float)):
        rs["ret_1"] = round(stock_chg - board["change_pct"], 2)

    # 板块 K线(best-effort): 5/20 日 RS + 板块均线择时
    board_kl, timing = None, None
    if bk:
        try:
            board_kl = quote.fetch_kline(f"90.{bk}")
        except Exception:  # noqa: BLE001
            board_kl = {"ok": False}
        if board_kl.get("ok"):
            for w in ("ret_5", "ret_20"):
                s, bv = skl.get(w), board_kl.get(w)
                rs[w] = round(s - bv, 2) if isinstance(s, (int, float)) and isinstance(bv, (int, float)) else None
            # 当日 RS 改用同口径 K线(个股K线 ret_1 − 板块K线 ret_1), 与 5/20 日一致
            if isinstance(skl.get("ret_1"), (int, float)) and isinstance(board_kl.get("ret_1"), (int, float)):
                rs["ret_1"] = round(skl["ret_1"] - board_kl["ret_1"], 2)
            d, why = quote.ma_trend(board_kl.get("last_close"), board_kl.get("ma20"), board_kl.get("ma60"))
            timing = {"direction": d, "why": why}

    return {"ok": True, "boards": sect.get("boards"), "industry": board,
            "bk": bk, "rs": rs, "board_kline": board_kl, "timing": timing,
            "hot_boards": hot_boards(sect.get("boards"), bk, stock_chg)}


def print_sector(sb):
    """打印板块背景(所属行业板块当日涨跌/主力净流入 + 个股相对板块 RS + 板块择时)。"""
    if not sb:
        return
    print("\n【板块背景 + 个股相对板块】")
    if not sb.get("ok"):
        print(f"  板块数据未取到({sb.get('reason') or '限流/新股'})")
        return
    board = sb.get("industry")
    if not board:
        # 没匹配到行业板块时, 退而列出净流入最强/最弱的概念板块作背景
        boards = sb.get("boards") or []
        if boards:
            top = max(boards, key=lambda b: b.get("net_inflow") or -9e18)
            print(f"  未定位到行业板块; 所属板块中主力净流入最强: {top['name']}({top['bk']}) "
                  f"{quote.fmt_num(top.get('net_inflow'), '元')}")
        return
    chg = board.get("change_pct")
    chg_s = f"{chg:+.2f}%" if isinstance(chg, (int, float)) else "—"
    print(f"  所属行业板块: {board['name']}({board['bk']})  今日 {chg_s}   "
          f"板块主力净流入: {quote.fmt_num(board.get('net_inflow'), '元')}")
    t = sb.get("timing")
    if t and t.get("direction"):
        print(f"    板块择时: {t['direction']}({t['why']})")
    rs = sb.get("rs") or {}
    parts = []
    for w, lbl in (("ret_1", "1日"), ("ret_5", "5日"), ("ret_20", "20日")):
        v = rs.get(w)
        if isinstance(v, (int, float)):
            parts.append(f"{lbl} {v:+.2f}pct")
    if parts:
        anchor = rs.get("ret_5") if isinstance(rs.get("ret_5"), (int, float)) else rs.get("ret_1")
        tag = ("跑赢板块(板块内相对强势)" if isinstance(anchor, (int, float)) and anchor > 0
               else "跑输板块(板块内相对弱势)" if isinstance(anchor, (int, float)) else "")
        print(f"    相对强弱 RS (个股−板块): " + "  ".join(parts) + (f"  → {tag}" if tag else ""))
        if not (sb.get("board_kline") or {}).get("ok"):
            print(f"    (板块K线限流, 仅当日 RS; 5/20日 RS 稍后重试 analyze.py 可补)")
    # 同属热门板块(概念/题材, 按主力净流入排序; 行业板块已在上面单列, 此处剔除)
    hb = sb.get("hot_boards") or []
    if hb:
        print("  同属强势板块(按主力净流入, 多为概念/题材, 或含地域——性质由模型判):")
        for b in hb:
            chg = b.get("change_pct")
            chg_s = f"{chg:+.2f}%" if isinstance(chg, (int, float)) else "—"
            rs1 = b.get("rs_1")
            rs_s = f"  个股−板块 {rs1:+.2f}pct" if isinstance(rs1, (int, float)) else ""
            print(f"    · {b.get('name')}({b.get('bk')})  今日 {chg_s}   "
                  f"主力净流入 {quote.fmt_num(b.get('net_inflow'), '元')}{rs_s}")


def print_human(res):
    q = res.get("quote") or {}
    name = q.get("name") or "?"
    code = q.get("display_code") or res["input"]
    industry = _industry(res)
    st = is_st(name)

    print(f"抓取时间(本地 CST): {datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}")
    print("#" * 64)
    print(f"# {name} ({code})" + (f"  〔{industry}〕" if industry else "")
          + ("  ⚠️ST/*ST/退市风险" if st else ""))
    print("#" * 64)

    print("\n【一】行情 / 主力资金 / 技术锚点  —— quote.py")
    quote.print_human(q)

    print("\n【二】融资融券 / 龙虎榜  —— funds.py")
    funds.print_one(res.get("funds") or {})

    print("\n【三】估值 / 历史分位 / 护栏  —— valuation.py")
    valuation.print_one(res.get("valuation") or {})

    print("\n【四】近期公告(事件背景)  —— news.py")
    news.print_one(res.get("news") or {})

    print("\n【五】未来事件日历(解禁/分红除权/业绩预告)  —— events.py")
    events.print_one(res.get("events") or {})

    bd = res.get("breadth")
    if bd:
        print("\n【市场宽度 / 情绪温度】(全市场)")
        breadth.print_one(bd)

    idx = res.get("indices") or {}
    if idx:
        print("\n【大盘背景 + 择时】")
        timing = market_timing(idx)
        for ix, item in idx.items():
            quote.print_human(item)
            t = timing.get(ix) or {}
            if t.get("direction"):
                r5, r20 = t.get("ret_5"), t.get("ret_20")
                rs_s = (f"  近5日{r5:+.2f}% 近20日{r20:+.2f}%"
                        if isinstance(r5, (int, float)) and isinstance(r20, (int, float)) else "")
                print(f"    大盘择时: {t['direction']}({t['why']}){rs_s}")
        # 个股 vs 大盘 相对强弱
        rs = relative_strength(res.get("quote"), idx)
        if rs:
            parts = []
            for w in ("ret_1", "ret_5", "ret_20"):
                v = rs["rs"].get(w)
                if isinstance(v, (int, float)):
                    parts.append(f"{WIN_LABEL[w]} {v:+.2f}pct")
            if parts:
                strong = rs["rs"].get("ret_5")
                tag = ("跑赢大盘(相对强势)" if isinstance(strong, (int, float)) and strong > 0
                       else "跑输大盘(相对弱势)" if isinstance(strong, (int, float)) else "")
                print(f"\n  相对强弱 RS (个股−{rs['benchmark_name']}): " + "  ".join(parts)
                      + (f"  → {tag}" if tag else ""))

    # 板块背景: 所属行业板块当日涨跌/主力净流入 + 个股相对板块 RS + 板块择时
    print_sector(res.get("sector"))

    # 行业模板建议 + 估值护栏 + 预填 score.py 命令
    tmpl, why = suggest_template(industry)
    vp = _val_pctl(res.get("valuation"))
    print("\n" + "=" * 64)
    print("【研判脚手架(Step 3)】")
    # 未来事件临近提示: 先于四维, 供 Step 1.5 体检与维度一/四的方向修正
    ev_hl = (res.get("events") or {}).get("highlights") or []
    if ev_hl:
        print("  ⚠️ 临近/重点事件(先纳入 Step 1.5 体检与资金/经济维方向):")
        for h in ev_hl:
            print(f"      {h}")
    print(f"  建议行业权重模板: {tmpl}  ({why})")
    if st:
        print("  ⚠️ ST/*ST: 看多侧封顶为'中性偏多'(score.py 加 --st)")
    if vp is not None:
        gtag = ("→下调一档" if vp >= 90 else "→上调一档" if vp <= 10 else "中间, 不调档")
        print(f"  估值护栏主分位: {vp:g}%  {gtag}(score.py 加 --val-pctl {vp:g})")
    else:
        print("  估值护栏主分位: 未取到(脚本限流或无分位数据), score.py 暂不加 --val-pctl")

    cmd = [f"python3 scripts/score.py --template {tmpl}"]
    if vp is not None:
        cmd.append(f"--val-pctl {vp:g}")
    if st:
        cmd.append("--st")
    cmd.append("--funds ? --sentiment ? --intl ? --econ ?")
    print("\n  填好各维方向强度后执行(看多+/看空-/中性0/不计分na, 强度即绝对值):")
    print("    " + " ".join(cmd))

    print("\n  仍需 WebSearch 补的(含完整日期, 核验发布日期):")
    print("    · 情绪: 研报评级/股吧热度/个股传闻  (换手率分位+相对强弱RS(大盘&板块)+大盘/板块择时+板块资金流+同属热门板块+两市成交额/涨跌停打板情绪已由脚本给出)")
    print("    · 国际: 行业相关贸易政策/美联储/中美关系/大宗商品/汇率")
    print("    · 经济: 宏观数据(CPI/PPI/PMI)/货币政策/行业政策  (PE/PB/PS分位已由 valuation, 近期公告已由 news, 解禁/分红除权/业绩预告已由 events 给出)")
    print("=" * 64)


def main():
    ap = argparse.ArgumentParser(description="A股个股分析统一编排器(行情+资金+估值+脚手架)")
    ap.add_argument("code", help="股票代码, 如 600519")
    ap.add_argument("--no-index", action="store_true", help="不附带大盘指数")
    ap.add_argument("--lhb-days", type=int, default=90, help="龙虎榜回看天数(默认90)")
    ap.add_argument("--cache", type=float, default=None, metavar="SEC",
                    help="开启短TTL磁盘缓存(秒); /loop 反复跑或连续多标的时减少重复请求、缓解限流。"
                         "默认关; 实时盘中建议很短(如30~60)。新鲜度仍按数据自带时间戳判, 缓存不破坏铁律")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()

    if args.cache is not None:
        quote.set_cache_ttl(args.cache)

    indices = [] if args.no_index else DEFAULT_INDICES
    res = run_all(args.code, args.lhb_days, indices)

    # 板块背景(slist + best-effort 板块K线): 依赖 valuation 的 board_bk, 故在 run_all 后单独跑
    res["sector"] = sector_block(res.get("quote"), res.get("valuation"))

    # 模板/护栏建议也并入 JSON, 供自动化消费
    q = res.get("quote") or {}
    tmpl, why = suggest_template(_industry(res))
    res["template_suggestion"] = {"template": tmpl, "why": why,
                                  "st": is_st(q.get("name")),
                                  "val_pctl": _val_pctl(res.get("valuation"))}
    res["market_timing"] = market_timing(res.get("indices") or {})
    res["relative_strength"] = relative_strength(res.get("quote"), res.get("indices") or {})

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print_human(res)

    ok = any((res.get(k) or {}).get("ok") for k in ("quote", "funds", "valuation"))
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
