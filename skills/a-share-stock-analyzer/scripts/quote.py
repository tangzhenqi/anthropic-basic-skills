#!/usr/bin/env python3
"""A股实时行情/资金流抓取与交叉验证工具。

把 SKILL 里反复强调的"实时数据铁律"固化成代码：
  - 同时拉取 腾讯(qt.gtimg.cn) + 东方财富(push2.eastmoney.com) 两个独立纯数据通道
  - 自动按代码推导市场前缀 / secid，自动按小数位缩放东财放大价
  - 自动校验每个来源的时间戳是否为"今日"（非今日 -> stale=true）
  - 自动对两源今日价做交叉验证（误差阈值内 -> cross_validated=true）
  - 主力资金流优先取东财 fflow 口径
  - 统一输出归一化 JSON，杜绝手搓 URL / 手动缩放 / 凭记忆比对时间戳的翻车

用法:
    python3 quote.py 600519                 # 个股, 自动判市场
    python3 quote.py sh600519 sz000001      # 多只, 显式前缀
    python3 quote.py 600519 sh000001 sh000300   # 个股+大盘+沪深300, 一次拉全
    python3 quote.py --json 600519          # 仅输出 JSON(供程序消费)

退出码: 0 正常; 2 全部标的均抓取失败。
注意: 需联网。腾讯接口无需特殊头; 东财接口须带 User-Agent(脚本已内置)。
"""

import sys
import os
import json
import re
import ssl
import time
import random
import hashlib
import tempfile
import threading
import argparse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta, time as dt_time
from concurrent.futures import ThreadPoolExecutor

import trading_calendar as _cal  # 真实沪深节假日表(替掉"≤4日"启发式)

CST = timezone(timedelta(hours=8))  # Asia/Shanghai, 无依赖
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
PRICE_TOL = 0.01  # 交叉验证相对误差阈值 1%
TIMEOUT = 10

# 东财(push2/push2his/datacenter)偶发限流; analyze.py 会一次并发打 ~20 个东财请求,
# 用一个进程级信号量给所有东财请求限并发, 把突发拍平, 缓解间歇性失败(腾讯不受限)。
EM_MAX_CONCURRENCY = 6
_EM_SEM = threading.Semaphore(EM_MAX_CONCURRENCY)

# ---------- 可选短 TTL 磁盘缓存(默认关) ----------
# 为什么: /loop 反复跑 analyze, 或连续分析多标的时, 跨进程会反复打同一批东财接口,
# 自己加剧限流。开缓存后 TTL 窗口内同一 URL 直接读盘, 不再发请求(也跳过 EM 信号量)。
# 安全性: 新鲜度由数据自带时间戳(f86/腾讯ts)判定, 与抓取时刻无关 —— 即便命中旧缓存,
#   freshness 仍会如实标 last_close/stale, 不会把过期数据伪装成"今日"。故缓存只省网络,
#   不破坏"铁律"。默认 TTL=0(关闭), 行为与改前完全一致; 实时盘中分析建议保持关闭或用很短 TTL。
# 配置: 环境变量 ASHARE_CACHE_TTL(秒) / ASHARE_CACHE_DIR; 或 analyze.py --cache N 运行期设定。
_CACHE_TTL = float(os.environ.get("ASHARE_CACHE_TTL", "0") or 0)
_CACHE_DIR = os.environ.get("ASHARE_CACHE_DIR") or os.path.join(tempfile.gettempdir(), "ashare_cache")


def set_cache_ttl(seconds):
    """运行期设置缓存 TTL(秒); <=0 关闭。analyze.py --cache 用。"""
    global _CACHE_TTL
    _CACHE_TTL = float(seconds or 0)


def _cache_path(url):
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return os.path.join(_CACHE_DIR, h + ".txt")


def _cache_get(url):
    if _CACHE_TTL <= 0:
        return None
    try:
        p = _cache_path(url)
        if time.time() - os.stat(p).st_mtime <= _CACHE_TTL:
            with open(p, encoding="utf-8") as f:
                return f.read()
    except OSError:
        return None
    return None


def _cache_put(url, body):
    if _CACHE_TTL <= 0:
        return
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        tmp = _cache_path(url) + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp, _cache_path(url))  # 原子替换, 避免并发读到半截
    except OSError:
        pass

# 常见指数别名 -> 标准前缀代码(供人工/报告引用)
INDEX_ALIASES = {
    "上证": "sh000001", "上证指数": "sh000001", "sh": "sh000001",
    "深成指": "sz399001", "创业板": "sz399006", "创业板指": "sz399006",
    "沪深300": "sh000300", "hs300": "sh000300", "科创50": "sh000688",
    "北证50": "bj899050",
}


def _ssl_context():
    """优先用 certifi 的 CA bundle; 没有就用系统默认。

    解决 macOS Python.framework 常见的 CERTIFICATE_VERIFY_FAILED。
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        return ssl.create_default_context()


_CTX = _ssl_context()


def _decode(raw):
    # 腾讯/新浪用 GBK, 东财用 UTF-8; 先试 utf-8 再退 gbk
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _http_get_once(url, headers):
    """单次 GET 文本。https 证书校验失败时自动降级到 http(公开只读行情接口, 风险可控)。"""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_CTX) as r:
            return _decode(r.read())
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), ssl.SSLError) and url.startswith("https://"):
            req2 = urllib.request.Request("http://" + url[len("https://"):], headers=headers or {})
            with urllib.request.urlopen(req2, timeout=TIMEOUT) as r:
                return _decode(r.read())
        raise


# ---------- 进程内 in-flight 请求合并(coalescing) ----------
# 为什么: analyze.py 并发跑 6 个子脚本, 大盘指数 secid / 板块快照等同一 URL 可能被
#   多个子任务"同时"请求。磁盘缓存对'同时'无能为力(第一份还没落盘第二份就发了),
#   信号量也只是排队、照样各打一次。这里给"正在飞行中的同一 URL"做去重: 第一个线程
#   真去抓(leader), 其余持相同 URL 的线程等它的结果直接复用, 同一 URL 并发只打东财一次。
# 安全性: 只对'同时进行中'的请求合并, 不缓存结果(失败也共享 -> 限流时反而少打几枪);
#   抓完即从在飞表移除, 不增长。与磁盘缓存正交、可叠加。
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT = {}


class _InFlight:
    __slots__ = ("event", "ok", "value")

    def __init__(self):
        self.event = threading.Event()
        self.ok = False
        self.value = None  # ok=True 时为响应体; ok=False 时为异常对象


def _fetch_with_retry(url, headers, retries):
    """真正发请求: 指数退避 + 抖动重试。push2(限流最严)基数更长, datacenter/腾讯较短。
    东财域名统一过 _EM_SEM 限并发(腾讯不限)。仅成功响应入磁盘缓存。"""
    is_em = "eastmoney.com" in url
    # push2/push2his/push2ex 限流远比 datacenter 严(见记忆 ashare-eastmoney-quirks), 退避基数加倍
    base = 0.8 if "push2" in url else 0.4
    for attempt in range(retries + 1):
        try:
            if is_em:
                with _EM_SEM:
                    body = _http_get_once(url, headers)
            else:
                body = _http_get_once(url, headers)
            _cache_put(url, body)  # 仅成功响应入缓存
            return body
        except Exception:  # noqa: BLE001 - 瞬时网络抖动, 退避后重试
            if attempt >= retries:
                raise
            # 指数退避 + ±50% 抖动: 多个并发请求被限流后不再同步重试、避免再次撞墙
            delay = base * (2 ** attempt) * (0.5 + random.random())
            time.sleep(delay)


def http_get(url, headers=None, retries=2):
    """带退避重试 + in-flight 合并的 GET。东财 push2/push2his 偶发 'Remote end closed
    connection'/限流，退避抖动重试即可救回；行情接口幂等只读，重试安全。
    东财域名的请求统一过 _EM_SEM 限并发(腾讯不限), 缓解 push2/datacenter 限流。
    并发请求同一 URL 时只有 leader 真抓、其余复用其结果(coalescing)。
    开启缓存(ASHARE_CACHE_TTL>0)时, TTL 窗口内同一 URL 直接读盘, 不发请求也不占信号量。"""
    cached = _cache_get(url)
    if cached is not None:
        return cached
    # in-flight 合并: 同一 URL 并发只打一次
    with _INFLIGHT_LOCK:
        slot = _INFLIGHT.get(url)
        leader = slot is None
        if leader:
            slot = _InFlight()
            _INFLIGHT[url] = slot
    if not leader:
        slot.event.wait()
        if slot.ok:
            return slot.value
        raise slot.value
    try:
        body = _fetch_with_retry(url, headers, retries)
        slot.ok, slot.value = True, body
        return body
    except Exception as e:  # noqa: BLE001 - 把异常传给同 URL 的等待者
        slot.ok, slot.value = False, e
        raise
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.pop(url, None)
        slot.event.set()  # 唤醒等待者(在 pop 后, 迟到线程会成为新 leader 重抓/命中缓存)


def normalize_code(code):
    """把用户输入归一化为 (tencent_code, em_secid, display_code)。

    接受形式: '600519' / 'sh600519' / 'SH600519' / 上证别名。
    指数请用显式前缀(如 sh000001), 纯6位数字默认按个股市场规则判别。
    """
    code = code.strip()
    if code in INDEX_ALIASES:
        code = INDEX_ALIASES[code]
    code = code.lower()
    m = re.match(r"^(sh|sz|bj)?(\d{6})$", code)
    if not m:
        raise ValueError(f"无法识别的代码: {code!r}")
    prefix, num = m.group(1), m.group(2)
    if not prefix:
        # 自动判市场: 6开头->沪; 0/3->深; 8/4/9->北交所
        if num[0] == "6":
            prefix = "sh"
        elif num[0] in "03":
            prefix = "sz"
        else:
            prefix = "bj"
    market = "1" if prefix == "sh" else "0"  # 东财: 沪=1, 深/北=0
    return f"{prefix}{num}", f"{market}.{num}", f"{prefix}{num}"


def is_today(dt):
    return dt is not None and dt.astimezone(CST).date() == datetime.now(CST).date()


def recent_trading_date(now=None):
    """最近一个'应有收盘数据'的交易日。优先查真实节假日表(trading_calendar),
    超出表的权威区间时退回'仅按周末'粗算(周六/周日回退到周五)。"""
    now = now or datetime.now(CST)
    cal_day = _cal.recent_trading_day(now)
    if cal_day is not None:
        return cal_day
    d = now.astimezone(CST).date()
    while d.weekday() >= 5:  # 5=周六, 6=周日
        d -= timedelta(days=1)
    return d


def _legacy_freshness(d, today):
    """节假日表覆盖区间之外的兜底: 沿用旧的'周末 + ≤4日'启发式(口径标注请人工确认)。"""
    weekend = today.weekday() >= 5
    if weekend and d == recent_trading_date(datetime.combine(today, datetime.min.time(), CST)):
        return "last_close"
    if not weekend and 0 < (today - d).days <= 4:
        return "last_close"
    return "stale"


MARKET_OPEN = dt_time(9, 30)  # 沪深开盘(集合竞价后), 盘前唯一可得为昨收


def freshness(dt, now=None, lag_trading_days=0):
    """单条时间戳的新鲜度: 区分'非交易日的最近收盘'与'真正滞后', 避免周末/节假日误判 stale。
      today      -> 时间戳就是今天(盘中或今日收盘)
      last_close -> 数据为最近交易日收盘(周末/节假日, 或盘前用昨收, 或盘后披露的合理滞后), 可用但须标注口径
      stale      -> 该到的当日/最新数据没拿到, 真滞后, 勿用
      missing    -> 无时间戳

    lag_trading_days: 该数据源的'天然滞后'交易日数。
      0 = 实时数据(行情/主力资金): 交易日盘中/盘后应有当日数据, 只在盘前(<09:30)容忍昨收;
      1 = 盘后披露数据(融资融券/龙虎榜): 东财收盘后才披露, 天然滞后约1个交易日, 容忍回溯1日。

    优先用 trading_calendar 真实节假日表精确判定; 超出表权威区间(未来未维护年份)
    才回退旧的"周末 + ≤4日"启发式 —— 保证永不比改前更差。
    """
    if dt is None:
        return "missing"
    now = now or datetime.now(CST)
    d = dt.astimezone(CST).date()
    today = now.astimezone(CST).date()
    if d == today:
        return "today"
    # 节假日表权威区间之外(今日或数据日越界) -> 旧启发式兜底
    if not (_cal.in_range(today) and _cal.in_range(d)):
        return _legacy_freshness(d, today)
    rtd = _cal.recent_trading_day(now)  # 最近交易日(今日是交易日则=今日)
    if rtd is None:
        return _legacy_freshness(d, today)

    if lag_trading_days >= 1:
        # 盘后披露数据: 从最近交易日起回溯 lag 个交易日内都算合理滞后(可用须标注)
        acceptable = {rtd}
        x = rtd
        for _ in range(lag_trading_days):
            x = _cal.prev_trading_day(x)
            if x:
                acceptable.add(x)
        return "last_close" if d in acceptable else "stale"

    # 实时数据(lag=0)
    if _cal.is_trading_day(today):
        # 盘前(<09:30)当日数据尚未产生, 昨收合法; 盘中/盘后只拿到旧数据 = 真滞后
        prev = _cal.prev_trading_day(today)
        if now.astimezone(CST).time() < MARKET_OPEN and d == prev:
            return "last_close"
        return "stale"
    # 今日非交易日(周末/节假日): 数据等于最近交易日收盘才合法
    return "last_close" if d == rtd else "stale"


# ---------- 腾讯 qt.gtimg.cn (纯文本, 自带 yyyymmddHHMMSS 时间戳) ----------

def fetch_tencent(tencent_code):
    url = f"https://qt.gtimg.cn/q={tencent_code}"
    txt = http_get(url)
    m = re.search(r'="([^"]*)"', txt)
    if not m or "~" not in m.group(1):
        return {"source": "tencent", "ok": False, "error": "空响应/格式异常"}
    f = m.group(1).split("~")

    def g(i, cast=float):
        try:
            return cast(f[i])
        except (IndexError, ValueError):
            return None

    ts = None
    if len(f) > 30 and re.match(r"^\d{14}$", f[30].strip()):
        ts = datetime.strptime(f[30].strip(), "%Y%m%d%H%M%S").replace(tzinfo=CST)
    return {
        "source": "tencent",
        "ok": True,
        "name": f[1] if len(f) > 1 else None,
        "code": f[2] if len(f) > 2 else None,
        "price": g(3),
        "prev_close": g(4),
        "open": g(5),
        "change": g(31),
        "change_pct": g(32),
        "high": g(33),
        "low": g(34),
        "turnover_rate": g(38),
        "timestamp": ts.isoformat() if ts else None,
        "is_today": is_today(ts),
    }


# ---------- 东方财富 push2 (纯 JSON, f86=unix 时间戳, 含主力资金口径) ----------

# 东财 push2 stock/get 字段并集: 行情(quote)所需 + 估值(valuation)所需一次取齐,
# 让 quote.fetch_eastmoney 与 valuation.fetch_valuation 共用同一次请求(见 em_snapshot)。
#   行情: f43 现价/f57 代码/f58 名称/f59 小数位/f60 昨收/f169 涨跌额/f170 涨跌幅
#         /f86 时间戳/f168 换手率/f127 所属行业(供 Step 3.1 自动选权重模板)
#   估值: f116 总市值/f117 流通市值/f162 动态PE/f163 PE-TTM/f164 静态PE/f167 PB
EM_FIELDS = "f43,f57,f58,f59,f60,f169,f170,f86,f168,f127"
EM_SNAPSHOT_FIELDS = EM_FIELDS + ",f116,f117,f162,f163,f164,f167"

# 进程内按 secid 记忆快照原始 data。analyze.py 里 quote 与 valuation 并发跑同一标的,
# 此前各打一次 push2(最易被限流的接口); 这里合并为一次, 减半该标的的 push2 命中。
_snapshot_cache = {}
_snapshot_locks = {}
_snapshot_meta = threading.Lock()


def _secid_lock(secid):
    with _snapshot_meta:
        return _snapshot_locks.setdefault(secid, threading.Lock())


def em_snapshot(secid):
    """东财 push2 stock/get 快照(并集字段)的原始 data, 进程内按 secid 去重。
    并发首调会拿 per-secid 锁串行化, 后到者直接命中缓存 -> 同一标的只打一次接口。
    返回 dict(成功) 或 None(data 为空/接口异常); 缓存命中包括 None(避免重复试错)。"""
    with _snapshot_meta:
        if secid in _snapshot_cache:
            return _snapshot_cache[secid]
    with _secid_lock(secid):
        with _snapshot_meta:
            if secid in _snapshot_cache:
                return _snapshot_cache[secid]
        url = (f"https://push2.eastmoney.com/api/qt/stock/get"
               f"?secid={secid}&fields={EM_SNAPSHOT_FIELDS}")
        try:
            txt = http_get(url, headers={"User-Agent": UA})
            data = (json.loads(txt) or {}).get("data")
        except Exception:  # noqa: BLE001 - 不缓存异常(留给上层重试), 仅成功响应入缓存
            raise
        with _snapshot_meta:
            _snapshot_cache[secid] = data
        return data


def fetch_eastmoney(secid):
    """东财快照: 价/涨跌幅/换手/时间戳/行业。资金流另由 fetch_fflow 取(snapshot f62 不可靠)。
    数据来自 em_snapshot(与 valuation 共用一次 push2 请求)。"""
    data = em_snapshot(secid)
    if not data:
        return {"source": "eastmoney", "ok": False, "error": "data 为空(代码或市场前缀错?)"}

    dec = data.get("f59")  # 小数位
    scale = 10 ** dec if isinstance(dec, int) and dec > 0 else 100

    def scaled(key, s=None):
        v = data.get(key)
        if v in (None, "-", ""):
            return None
        try:
            return float(v) / (s if s is not None else scale)
        except (TypeError, ValueError):
            return None

    ts = None
    if isinstance(data.get("f86"), int) and data["f86"] > 0:
        ts = datetime.fromtimestamp(data["f86"], tz=CST)

    return {
        "source": "eastmoney",
        "ok": True,
        "name": data.get("f58"),
        "code": data.get("f57"),
        "industry": data.get("f127") or None,  # 所属东财行业, 供选权重模板
        "price": scaled("f43"),
        "prev_close": scaled("f60"),
        "change": scaled("f169"),
        "change_pct": scaled("f170", s=100),  # 涨跌幅固定 /100
        "turnover_rate": scaled("f168", s=100),
        "timestamp": ts.isoformat() if ts else None,
        "is_today": is_today(ts),
    }


def _parse_fflow_klines(txt):
    """解析 fflow 返回的 klines -> [{date, main(=f52)}] (f52=大单+超大单)。"""
    klines = ((json.loads(txt) or {}).get("data") or {}).get("klines") or []
    rows = []
    for k in klines:
        parts = k.split(",")
        try:
            rows.append({"date": parts[0], "main": float(parts[1])})
        except (IndexError, ValueError):
            continue
    return rows


def fetch_fflow(secid):
    """东财主力资金流(权威口径): 今日(盘中) + 近5日累计主力净流入(元)。

    今日盘中: push2 fflow/kline (仅返回当日, 随盘滚动)。
    历史已收盘日: push2his fflow/daykline (返回至上一交易日)。
    两者拼接成连续5个交易日。指数也有资金流(全市场口径)。
    """
    f2 = "f51,f52,f53,f54,f55,f56"
    today_url = (f"https://push2.eastmoney.com/api/qt/stock/fflow/kline/get?secid={secid}"
                 f"&fields1=f1,f2,f3,f7&fields2={f2}&klt=101&lmt=1")
    hist_url = (f"https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?secid={secid}"
                f"&fields1=f1,f2,f3,f7&fields2={f2}&klt=101&lmt=8")

    # 今日盘中与历史日两个请求并发(原为串行, 主延迟点)
    def _pull(url):
        return _parse_fflow_klines(http_get(url, headers={"User-Agent": UA}))

    with ThreadPoolExecutor(max_workers=2) as ex:
        f_today = ex.submit(_pull, today_url)
        f_hist = ex.submit(_pull, hist_url)
        today_rows = f_today.result()
        hist_rows = f_hist.result()

    # 按日期去重合并(今日盘中行覆盖历史里可能重复的同日行)
    by_date = {r["date"]: r["main"] for r in hist_rows}
    for r in today_rows:
        by_date[r["date"]] = r["main"]
    if not by_date:
        return {"ok": False, "reason": "无资金流数据(新股/接口为空)"}

    ordered = sorted(by_date.items())  # [(date, main), ...] 升序
    last5 = ordered[-5:]
    today_str = datetime.now(CST).strftime("%Y-%m-%d")
    last_date, last_main = ordered[-1]
    try:
        fresh = freshness(datetime.strptime(last_date, "%Y-%m-%d").replace(tzinfo=CST))
    except ValueError:
        fresh = "missing"
    return {
        "ok": True,
        "today_main_net": last_main,
        "today_date": last_date,
        "today_is_today": last_date == today_str,
        "freshness": fresh,  # today / last_close / stale
        "sum5_main_net": sum(m for _, m in last5),
        "days_counted": len(last5),
        "daily": [{"date": d, "main": m} for d, m in last5],
    }


def fetch_sector(secid, target_bk=None, target_name=None):
    """个股所属板块的即时涨跌幅 + 主力净流入(东财 slist 单请求)。

    SKILL 维度二一直承诺'板块涨跌/是否逆势', 但此前编排器只拉了大盘(上证/沪深300),
    没拉个股所属行业板块 —— '逆板块独跌'还是'随板块杀跌'只能靠目测。本函数一次 slist
    拿齐该股所在全部行业/概念板块的当日涨跌(f3)与主力净流入(f62), 并按 target_bk(优先,
    valuation 的 ORIG_BOARD_CODE 派生)或 target_name 定位'所属行业板块', 作确定性背景。

    返回 {ok, boards:[{bk,name,change_pct,net_inflow}], industry: 命中的行业板块|None}。
    单请求即给当日 RS 所需(个股涨跌 − 板块涨跌); 5/20 日 RS 由 analyze 用板块 K线补(best-effort)。
    """
    url = (f"https://push2.eastmoney.com/api/qt/slist/get?spt=3&secid={secid}"
           f"&fields=f12,f13,f14,f3,f62&po=1&pz=80&pn=1&fid=f62")
    txt = http_get(url, headers={"User-Agent": UA})
    diff = ((json.loads(txt) or {}).get("data") or {}).get("diff") or {}
    items = list(diff.values()) if isinstance(diff, dict) else list(diff)
    boards = []
    for b in items:
        if str(b.get("f13")) != "90":  # 90 = 板块(行业/概念); 其余为指数等, 跳过
            continue
        f3 = b.get("f3")
        boards.append({
            "bk": b.get("f12"),
            "name": b.get("f14"),
            "change_pct": (f3 / 100) if isinstance(f3, (int, float)) else None,
            "net_inflow": b.get("f62"),  # 主力净流入(元)
        })
    if not boards:
        return {"ok": False, "reason": "无板块数据(指数/新股?)"}
    industry = None
    if target_bk:
        industry = next((b for b in boards if b["bk"] == target_bk), None)
    if industry is None and target_name:
        industry = next((b for b in boards if b["name"] == target_name), None)
    return {"ok": True, "boards": boards, "industry": industry}


# ---------- 东方财富日K (前复权, 纯 JSON) ----------
# 用于换手率历史分位、均线、前高前低等"价位锚点"——SKILL 操作参考表需要的止损/
# 目标价不再凭空生成。kline 价格已是真实值(无需缩放)。fields2 顺序:
#   f51 日期, f52 开, f53 收, f54 高, f55 低, f56 量, f57 额,
#   f58 振幅, f59 涨跌幅, f60 涨跌额, f61 换手率
KLINE_F2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"


def _avg(vals, n=None):
    if not vals:
        return None
    s = vals[-n:] if n else vals
    return sum(s) / len(s)


def _pctl(window, today):
    """today 在 window(含今日)中的历史分位: 低于今日的天数占比, 0-100。"""
    if not window or today is None:
        return None
    below = sum(1 for v in window if v < today)
    return round(below / len(window) * 100, 1)


def _ret(closes, n):
    """近 n 个交易日收益率(%): 今收 / n 日前收 - 1。数据不足返回 None。"""
    if len(closes) <= n or closes[-1 - n] in (None, 0):
        return None
    return round((closes[-1] / closes[-1 - n] - 1) * 100, 2)


def ma_trend(price, ma20, ma60):
    """价格相对均线的多空排列(供大盘择时/个股技术背景的确定性判断)。
    多头排列(价>MA20>MA60)→偏多; 空头排列(价<MA20<MA60)→偏空; 其余→震荡。"""
    if None in (price, ma20, ma60):
        return None, "均线数据不足"
    if price > ma20 > ma60:
        return "偏多", "价>MA20>MA60 多头排列"
    if price < ma20 < ma60:
        return "偏空", "价<MA20<MA60 空头排列"
    above = price >= ma20
    return "震荡", ("价在MA20上方但均线未多头排列" if above
                  else "价在MA20下方但均线未空头排列")


def fetch_kline(secid, lmt=120):
    """东财日K(前复权): 近 lmt 个交易日, 算均线/前高前低/换手率分位等技术锚点。"""
    url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}"
           f"&fields1=f1,f2,f3,f4,f5,f6&fields2={KLINE_F2}"
           f"&klt=101&fqt=1&end=20500101&lmt={lmt}")
    txt = http_get(url, headers={"User-Agent": UA})
    klines = ((json.loads(txt) or {}).get("data") or {}).get("klines") or []
    rows = []
    for k in klines:
        p = k.split(",")
        try:
            rows.append({
                "date": p[0],
                "close": float(p[2]),
                "high": float(p[3]),
                "low": float(p[4]),
                "turnover": float(p[10]) if len(p) > 10 and p[10] not in ("", "-") else None,
            })
        except (IndexError, ValueError):
            continue
    if len(rows) < 2:
        return {"ok": False, "reason": "K线数据不足(新股/停牌/接口空)"}

    closes = [r["close"] for r in rows]
    highs = [r["high"] for r in rows]
    lows = [r["low"] for r in rows]
    turns = [r["turnover"] for r in rows if r["turnover"] is not None]
    today_turn = turns[-1] if turns else None
    return {
        "ok": True,
        "last_date": rows[-1]["date"],
        "bars": len(rows),
        "last_close": closes[-1],  # 最近收盘(板块择时 ma_trend 需要现价口径; 个股用双源现价更准)
        "ma20": _avg(closes, 20),
        "ma60": _avg(closes, 60),
        "high_20": max(highs[-20:]),
        "low_20": min(lows[-20:]),
        "high_60": max(highs[-60:]),
        "low_60": min(lows[-60:]),
        "turnover_today": today_turn,
        "turnover_avg20": _avg(turns, 20),
        "turnover_pctl": _pctl(turns[-60:], today_turn),
        # 近 1/5/20 交易日收益率(%), 供相对强弱(个股 vs 大盘)与大盘择时使用
        "ret_1": _ret(closes, 1),
        "ret_5": _ret(closes, 5),
        "ret_20": _ret(closes, 20),
    }


def cross_validate(tx, em):
    """**一致性**维度: 两源价格是否互相印证, 返回 (verdict, note)。
    新鲜度(是否今日/最近收盘/滞后)由 assess_freshness 单独判, 二者正交。"""
    pa = tx.get("price") if tx.get("ok") else None
    pb = em.get("price") if em.get("ok") else None
    if pa is None or pb is None:
        return "single_source", "仅单一来源, 未交叉验证"
    if pa == 0:
        return "uncertain", "价格异常(0)"
    rel = abs(pa - pb) / pa
    if rel <= PRICE_TOL:
        return "cross_validated", f"两源一致(相对误差 {rel:.2%})"
    return "mismatch", f"两源不一致(相对误差 {rel:.2%}), 需人工核实"


def assess_freshness(tx, em, now=None):
    """**新鲜度**维度: today / last_close / stale / unknown, 返回 (level, note)。

    至少一源时间戳为今日 -> today(今日是交易日且有当日数据)。
    无今日数据但都指向最近交易日(周末/疑似节假日) -> last_close(可用, 须标注收盘口径)。
    今日是交易日却只拿到更早数据 -> stale(真滞后, 勿写入报告)。
    """
    now = now or datetime.now(CST)
    dated = []  # [(date, level), ...]
    for s in (tx, em):
        if s.get("ok") and s.get("timestamp"):
            try:
                dt = datetime.fromisoformat(s["timestamp"])
            except (ValueError, TypeError):
                continue
            dated.append((dt.astimezone(CST).date(), freshness(dt, now)))
    if not dated:
        return "unknown", "无可用时间戳, 无法判定新鲜度"
    levels = [l for _, l in dated]
    if "today" in levels:
        return "today", "今日数据(盘中或今日收盘)"
    # 两源都非今日: 若两个独立源给出同一历史日期, 那基本就是市场最近真实交易日
    # (周末/任意长度节假日均适用; 双源独立, 同时同日出错概率极低), 无需内置节假日历。
    dates = {d for d, _ in dated}
    if len(dated) >= 2:
        if len(dates) == 1:
            return "last_close", f"最近交易日收盘口径(非交易日或盘前), 两源一致为 {dates.pop()}"
        return "stale", "两源时间戳日期不一致且均非今日, 数据可疑, 需人工核实"
    # 单源: 用逐源判断(非交易日/盘前 -> last_close, 否则 stale); 单源本就带 caveat
    if levels[0] == "last_close":
        return "last_close", "最近交易日收盘口径(非交易日或盘前, 单一来源, 请核实)"
    return "stale", "今日为交易日却非当日数据, 数据滞后, 勿写入报告"


def analyze_one(user_code):
    try:
        tx_code, secid, disp = normalize_code(user_code)
    except ValueError as e:
        return {"input": user_code, "ok": False, "error": str(e)}

    results = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        ft = ex.submit(fetch_tencent, tx_code)
        fe = ex.submit(fetch_eastmoney, secid)
        ff = ex.submit(fetch_fflow, secid)
        fk = ex.submit(fetch_kline, secid)
        for fut, key, fn in ((ft, "tencent", "腾讯"), (fe, "eastmoney", "东财")):
            try:
                results[key] = fut.result()
            except Exception as e:  # noqa: BLE001 - 单源失败不应拖垮整体
                results[key] = {"source": key, "ok": False, "error": f"{fn}抓取异常: {e}"}
        try:
            results["fflow"] = ff.result()
        except Exception as e:  # noqa: BLE001
            results["fflow"] = {"ok": False, "reason": f"资金流抓取异常: {e}"}
        try:
            results["kline"] = fk.result()
        except Exception as e:  # noqa: BLE001
            results["kline"] = {"ok": False, "reason": f"K线抓取异常: {e}"}

    tx, em, ff = results["tencent"], results["eastmoney"], results["fflow"]
    verdict, vnote = cross_validate(tx, em)
    fresh, fnote = assess_freshness(tx, em)
    ok = tx.get("ok") or em.get("ok")
    # "可直接写入报告" = 抓到数据 + 价格一致或单一来源(非两源打架) + 非滞后
    usable = bool(ok) and verdict in ("cross_validated", "single_source") and fresh in ("today", "last_close")
    note = vnote if fresh == "today" else f"{vnote}；{fnote}"
    name = (tx.get("name") if tx.get("ok") else None) or (em.get("name") if em.get("ok") else None)
    industry = em.get("industry") if em.get("ok") else None
    return {
        "input": user_code,
        "display_code": disp,
        "secid": secid,
        "name": name,
        "industry": industry,
        "ok": ok,
        "cross_validation": verdict,
        "freshness": fresh,
        "usable": usable,
        "note": note,
        "tencent": tx,
        "eastmoney": em,
        "fflow": ff,
        "kline": results["kline"],
    }


def fmt_num(v, suffix="", pct=False):
    if v is None:
        return "—"
    if pct:
        return f"{v:+.2f}%"
    if abs(v) >= 1e8:
        return f"{v / 1e8:.2f}亿{suffix}"
    if abs(v) >= 1e4:
        return f"{v / 1e4:.2f}万{suffix}"
    return f"{v:.2f}{suffix}"


VERDICT_LABEL = {
    "cross_validated": "✅ 已交叉验证",
    "single_source": "⚠️ 仅单一来源, 未交叉验证",
    "mismatch": "❌ 两源不一致, 需人工核实",
    "uncertain": "⚠️ 价格异常",
}

FRESHNESS_LABEL = {
    "today": "",                       # 今日数据, 不额外标注
    "last_close": "📅 最近交易日收盘口径",
    "stale": "⛔ 数据滞后, 勿用",
    "unknown": "❔ 新鲜度未知",
}


def print_human(item):
    if not item.get("ok"):
        print(f"  ✗ {item['input']}: {item.get('error') or '抓取失败'}")
        return
    tx, em = item["tencent"], item["eastmoney"]
    src = tx if tx.get("ok") and tx.get("price") else em
    fresh_tag = FRESHNESS_LABEL.get(item.get("freshness"), "")
    ind = f"  〔{item['industry']}〕" if item.get("industry") else ""
    print(f"  {item['name'] or '?'} ({item['display_code']}){ind}  "
          f"{VERDICT_LABEL.get(item['cross_validation'], item['cross_validation'])}"
          f"{('  ' + fresh_tag) if fresh_tag else ''}")
    print(f"    现价: {fmt_num(src.get('price'))}   "
          f"涨跌幅: {fmt_num(src.get('change_pct'), pct=True)}   "
          f"换手: {fmt_num(src.get('turnover_rate'), pct=True) if src.get('turnover_rate') else '—'}")
    ff = item.get("fflow") or {}
    if ff.get("ok"):
        fl = ff.get("freshness")
        tag = "" if fl == "today" else f" {FRESHNESS_LABEL.get(fl, '')}({ff.get('today_date')})"
        print(f"    主力净流入 最新: {fmt_num(ff.get('today_main_net'), '元')}{tag}   "
              f"近{ff.get('days_counted')}日累计: {fmt_num(ff.get('sum5_main_net'), '元')}")
        if fl == "today":
            print(f"      ⚠️ 盘中'最新'为瞬时值, 会回摆(可同日由大幅净流出翻为净流入), 勿据此单独定方向; "
                  f"看日内累计趋势或收盘值, 并注意大单可拆成小单(主力/散户分类有盲点)")
    kl = item.get("kline") or {}
    if kl.get("ok"):
        tt, avg, pctl = kl.get("turnover_today"), kl.get("turnover_avg20"), kl.get("turnover_pctl")
        if tt is not None and avg:
            ratio = tt / avg if avg else None
            extra = f"(≈均值{ratio:.1f}倍" + (f", 分位{pctl:.0f}%)" if pctl is not None else ")") if ratio else ""
            print(f"    换手率 今日{tt:.2f}%  20日均{avg:.2f}% {extra}")
        print(f"    均线 MA20={fmt_num(kl.get('ma20'))}  MA60={fmt_num(kl.get('ma60'))}   "
              f"前高/前低 20日 {fmt_num(kl.get('high_20'))}/{fmt_num(kl.get('low_20'))}  "
              f"60日 {fmt_num(kl.get('high_60'))}/{fmt_num(kl.get('low_60'))}")
    print(f"    时间戳 腾讯={tx.get('timestamp') or '—'}  东财={em.get('timestamp') or '—'}")
    if item.get("cross_validation") != "cross_validated" or item.get("freshness") != "today":
        print(f"    → {item['note']}")


def main():
    ap = argparse.ArgumentParser(description="A股实时行情/资金流抓取与交叉验证")
    ap.add_argument("codes", nargs="+", help="股票/指数代码, 如 600519 sh000001")
    ap.add_argument("--json", action="store_true", help="仅输出 JSON")
    args = ap.parse_args()

    items = [analyze_one(c) for c in args.codes]

    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
    else:
        print(f"抓取时间(本地 CST): {datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 60)
        for it in items:
            print_human(it)
            print()
        print("提示: 报告中每条数据须带上面的时间戳; 非'✅已交叉验证/今日'的价格须按 note 处理。")

    # 退出码分级(便于 /loop 等自动化判断可信度):
    #   0 至少一个标的可直接写入(一致/单源 且 非滞后)
    #   3 抓到了数据, 但无任何可信标的(全部两源打架/滞后) -> 需人工核实, 勿直接采用
    #   2 全部抓取失败(网络/限流)
    if any(it.get("usable") for it in items):
        sys.exit(0)
    sys.exit(2 if not any(it.get("ok") for it in items) else 3)


if __name__ == "__main__":
    main()
