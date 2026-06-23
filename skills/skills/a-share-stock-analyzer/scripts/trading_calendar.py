#!/usr/bin/env python3
"""A股交易日历(沪深)——把"今日是不是交易日 / 最近一个交易日是哪天"做成确定性查表。

为什么要有它:
  quote.freshness 此前用"距今 ≤4 个日历日"启发式兜底节假日, 长假(春节/国庆 7+ 天)
  会误判 —— 而 freshness 直接决定一条数据"能否写入报告"(滞后即弃用)。误判 =
  错误地采用滞后数据, 或错误地弃用合法的"最近收盘"数据。这里用真实节假日表替掉启发式。

数据来源与口径:
  - HOLIDAYS 是"工作日但休市"的日期(即法定节假日落在周一至周五的那些天)。
    周六/周日本就休市, 不列入(由 weekday 判断)。调休补班的周末(如某些周六上班)
    对 A股无意义 —— A股只在周一至周五交易, 从不在任何周末开市, 故补班日不算交易日。
  - 2024-01-01 ~ 2026-06-11 的休市日由上证指数(sh000001)实际日K反推, 为地面真值;
    2026-06-12 之后(端午/中秋/国庆)取自国务院办公厅 2026 节假日安排通知(2025-11-04 发布)。
  - CALENDAR_VALID_THROUGH 之后无权威数据 -> is_trading_day/recent_trading_day 返回 None,
    调用方(quote.freshness)据此回退到"周末 + ≤4日"旧启发式, 保证永不比改前更差。

每年维护(约 11 月底国务院发布次年安排后):
  1) 把新一年的休市工作日追加进 HOLIDAYS;
  2) 把 CALENDAR_VALID_THROUGH 推到新一年 12-31。
  也可用本文件末尾 `regenerate_from_index()` 的思路, 从指数日K反推已发生的真值校对。
"""

from datetime import date, datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))  # Asia/Shanghai, 与 quote.py 一致

# 沪深休市的"工作日"(周末不列, 由 weekday 处理)。ISO 字符串便于核对/diff。
HOLIDAYS = frozenset({
    # ---- 2024(上证指数实际交易日反推) ----
    "2024-01-01",
    "2024-02-09", "2024-02-12", "2024-02-13", "2024-02-14", "2024-02-15", "2024-02-16",
    "2024-04-04", "2024-04-05",
    "2024-05-01", "2024-05-02", "2024-05-03",
    "2024-06-10",
    "2024-09-16", "2024-09-17",
    "2024-10-01", "2024-10-02", "2024-10-03", "2024-10-04", "2024-10-07",
    # ---- 2025(上证指数实际交易日反推) ----
    "2025-01-01",
    "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31", "2025-02-03", "2025-02-04",
    "2025-04-04",
    "2025-05-01", "2025-05-02", "2025-05-05",
    "2025-06-02",
    "2025-10-01", "2025-10-02", "2025-10-03", "2025-10-06", "2025-10-07", "2025-10-08",
    # ---- 2026 上半年(上证指数实际交易日反推, 截至 2026-06-11) ----
    "2026-01-01", "2026-01-02",
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-23",
    "2026-04-06",
    "2026-05-01", "2026-05-04", "2026-05-05",
    # ---- 2026 下半年(国务院办公厅 2026 节假日安排, 2025-11-04 发布; 周末已被剔除) ----
    "2026-06-19",                                                   # 端午
    "2026-09-25",                                                   # 中秋
    "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",  # 国庆
})

# 节假日表权威覆盖到此日期(含); 之后日期视为未知, 由调用方回退旧启发式。
CALENDAR_VALID_THROUGH = date(2026, 12, 31)


def _as_date(d):
    if isinstance(d, datetime):
        return d.astimezone(CST).date()
    return d


def in_range(d):
    """该日期是否落在节假日表的权威覆盖区间内。超出则不可信, 调用方应回退。"""
    return _as_date(d) <= CALENDAR_VALID_THROUGH


def days_until_expiry(now=None):
    """节假日表权威覆盖区间还剩多少天到期(含当天)。
    <=0 表示已过期(freshness 已回退'周末+≤4日'启发式, 长假可能误判, 须更新本文件)。
    selftest.py 据此提前告警, 避免每年维护被遗忘。"""
    now = now or datetime.now(CST)
    return (CALENDAR_VALID_THROUGH - _as_date(now)).days


def is_trading_day(d):
    """d 是否为沪深交易日。超出权威区间返回 None(交由调用方回退判断)。"""
    d = _as_date(d)
    if not in_range(d):
        return None
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS


def recent_trading_day(now=None):
    """<= now 的最近一个交易日。now 当天若是交易日即返回当天。
    若往回找超过 14 天仍未命中(理论不会, 最长春节假期 < 14 天)或越出权威区间, 返回 None。"""
    now = now or datetime.now(CST)
    d = _as_date(now)
    for _ in range(14):
        if not in_range(d):
            return None
        if d.weekday() < 5 and d.isoformat() not in HOLIDAYS:
            return d
        d -= timedelta(days=1)
    return None


def prev_trading_day(ref):
    """严格早于 ref 的最近一个交易日(ref 本身不计)。越界/找不到返回 None。"""
    return recent_trading_day(_as_date(ref) - timedelta(days=1))
