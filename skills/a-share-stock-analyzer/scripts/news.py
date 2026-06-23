#!/usr/bin/env python3
"""A股个股公告抓取 —— 东财公告接口权威口径(标题 + 发布日期 + 类型)。

为什么要有这个脚本(而非继续用 WebSearch):
  SKILL【维度二/四】此前把"最新公告/事件"交给 WebSearch 摘要, 但全文铁律是
  "硬事实不采信 AI 摘要"(摘要常把旧公告、传闻当成今日, 或臆造拼接)。公告的
  "标题 + 发布日期 + 类型"是结构化硬事实 —— 接口直取最稳。本脚本把它固化成接口,
  让事件类信号(业绩预告/分红/股权激励/监管问询/停复牌等)有据可查、日期可核验。

口径与边界:
  - 数据源: 东财公告中心 np-anotice-stock.eastmoney.com/api/security/ann。
    返回近期公告列表(标题/发布日期/所属栏目)。只读、幂等。
  - 只给"标题 + 日期 + 栏目分类", 不抓正文、不做解读 —— 解读是模型的活, 脚本只供事实。
  - 默认回看 14 天; 多数日子无公告很正常, 空结果如实写"近 N 天无公告"。
  - 公告是"已发生事件", 不计入当日资金/行情打分; 作为事件背景与情绪/基本面佐证。

复用 quote.py 的 http_get / normalize_code / CST / UA 基建(与 funds/valuation 同约定)。

用法:
    python3 news.py 600519                 # 近14天公告
    python3 news.py 600519 000858          # 多只
    python3 news.py --days 30 600519       # 自定义回看天数
    python3 news.py --json 600519          # JSON 输出(供 analyze.py / 自动化消费)

退出码: 0 至少一只成功抓到(含"无公告"也算成功); 2 全部失败(网络/限流)。
注意: 需联网, 东财接口须带 User-Agent(已内置); 同域名走 quote 的并发限流闸。
"""

import sys
import json
import argparse
import urllib.parse
from datetime import datetime, timedelta

# 复用 quote.py 的网络/解析/时间戳基建 (同目录)
from quote import http_get, normalize_code, UA, CST

ANN_BASE = "https://np-anotice-stock.eastmoney.com/api/security/ann"


def bare_code(secid):
    return secid.split(".", 1)[1]


def _ann_date(s):
    """'2026-06-10 00:00:00' / '2026-06-10' -> date; 失败返回 None。"""
    if not s:
        return None
    try:
        return datetime.strptime(s.split(" ")[0], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _columns(item):
    """公告栏目分类(如'业绩预告'/'分红送配'/'风险提示'), 多个用 / 连接。"""
    cols = item.get("columns") or []
    names = [c.get("column_name") for c in cols if c.get("column_name")]
    return " / ".join(names) if names else None


def fetch_announcements(code, days=14, page_size=30):
    """近 days 天公告列表。ann_type=A 为沪深A股公告; 按发布日期倒序。"""
    params = {
        "sr": "-1", "page_size": str(page_size), "page_index": "1",
        "ann_type": "A", "client_source": "web", "stock_list": code,
        "f_node": "0", "s_node": "0",
    }
    url = ANN_BASE + "?" + urllib.parse.urlencode(params)
    d = json.loads(http_get(url, headers={"User-Agent": UA})) or {}
    lst = ((d.get("data") or {}).get("list")) or []
    cutoff = datetime.now(CST).date() - timedelta(days=days)
    recent = []
    for it in lst:
        dt = _ann_date(it.get("notice_date") or it.get("eiTime"))
        if dt is None or dt < cutoff:
            continue
        recent.append({
            "date": dt.isoformat(),
            "title": (it.get("title") or "").strip(),
            "column": _columns(it),
            "art_code": it.get("art_code"),
        })
    return {"ok": True, "days": days, "count": len(recent), "items": recent}


def analyze_one(user_code, days=14):
    try:
        _, secid, disp = normalize_code(user_code)
    except ValueError as e:
        return {"input": user_code, "ok": False, "error": str(e)}
    out = {"input": user_code, "display_code": disp, "secid": secid}
    try:
        ann = fetch_announcements(bare_code(secid), days=days)
        out.update(ann)
        out["ok"] = True
    except Exception as e:  # noqa: BLE001 - 网络/限流不拖垮整批
        out["ok"] = False
        out["error"] = f"公告抓取异常: {e}"
    return out


def print_one(item):
    if not item.get("ok"):
        print(f"  ✗ {item.get('input')}: {item.get('error') or '抓取失败'}")
        return
    if not item.get("count"):
        print(f"  {item.get('display_code')}: 近{item.get('days')}天无公告")
        return
    print(f"  {item.get('display_code')}  近{item.get('days')}天 {item['count']} 条公告:")
    for a in item["items"]:
        col = f"  [{a['column']}]" if a.get("column") else ""
        print(f"    {a['date']}{col}  {a['title']}")


def main():
    ap = argparse.ArgumentParser(description="A股个股公告(东财公告中心, 标题+日期+栏目)")
    ap.add_argument("codes", nargs="+", help="股票代码, 如 600519 000858")
    ap.add_argument("--days", type=int, default=14, help="回看天数(默认14)")
    ap.add_argument("--json", action="store_true", help="仅输出 JSON")
    args = ap.parse_args()

    items = [analyze_one(c, days=args.days) for c in args.codes]

    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
    else:
        print(f"抓取时间(本地 CST): {datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 60)
        for it in items:
            print_one(it)
            print()
        print("提示: 公告为'已发生事件'的硬事实(标题/日期可核验), 不计入当日资金/行情打分;")
        print("      作事件背景与情绪/基本面佐证。解读交给模型, 脚本只供事实。")

    sys.exit(0 if any(it.get("ok") for it in items) else 2)


if __name__ == "__main__":
    main()
