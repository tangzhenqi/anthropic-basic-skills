# 备用路径：脚本不可用时手动抓接口

> 仅当环境**无法运行 `scripts/` 下脚本**时才用本文件手搓接口。正常情况一律用脚本
> （`analyze.py` / `quote.py` / `funds.py` / `valuation.py` / `news.py`），它们已固化
> 双源抓取、时间戳校验、交叉验证、缩放、限流、节假日历，手搓极易翻车。

用 WebFetch 抓以下接口（**绝不抓 `quote.eastmoney.com` 等 JS 网页页，它们只返回空壳"----"**）：

| 用途 | 接口 |
|------|------|
| 个股/指数/板块 价·涨跌幅·时间戳 | `https://qt.gtimg.cn/q=sh600519,sh000001,sh000300`（纯文本，自带 `YYYYMMDDHHMMSS`） |
| 第二交叉验证 价·时间戳 | `https://push2.eastmoney.com/api/qt/stock/get?secid=1.600519&fields=f43,f57,f58,f59,f60,f169,f170,f86,f168` |
| **主力资金 今日（盘中滚动）** | `https://push2.eastmoney.com/api/qt/stock/fflow/kline/get?secid=1.600519&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56&klt=101&lmt=1` |
| **主力资金 历史日（至上一交易日）** | `https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?secid=1.600519&...&lmt=8` |
| **日K（前复权，算均线/前高前低/换手分位/收益率）** | `https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600519&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&klt=101&fqt=1&end=20500101&lmt=120`（kline 价格已是真实值，无需缩放；fields2 顺序：日期/开/收/高/低/量/额/振幅/涨跌幅/涨跌额/**换手率f61**） |
| **融资融券（近 N 日）** | `https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_WEB_RZRQ_GGMX&columns=ALL&source=WEB&client=WEB&pageSize=6&sortColumns=DATE&sortTypes=-1&filter=(SCODE="600519")`（裸6位代码、括号引号须 URL 编码；`RZYE`融资余额、`RZYEZB`占流通%、`RZMRE`融资买入额、`RQYE`融券余额，元，已是真实值。`success=false`+"返回数据为空"=非两融标的，非报错） |
| **龙虎榜（近期上榜）** | `https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_DAILYBILLBOARD_DETAILSNEW&columns=ALL&source=WEB&client=WEB&pageSize=10&sortColumns=TRADE_DATE&sortTypes=-1&filter=(SECURITY_CODE="600519")`（`TRADE_DATE`、`EXPLANATION`上榜原因、`BILLBOARD_NET_AMT`龙虎榜净买额、`EXPLAIN`机构概述、`CHANGE_RATE`当日涨跌幅。空=近期未上榜，正常） |
| **估值（PE/PB/PS/PEG/分位序列）** | `https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_VALUEANALYSIS_DET&columns=ALL&source=WEB&client=WEB&pageSize=1300&sortColumns=TRADE_DATE&sortTypes=-1&filter=(SECURITY_CODE="600519")`（`PE_TTM`/`PB_MRQ`/`PS_TTM`/`PEG_CAR`/`BOARD_NAME`行业；约8年日序列，本地算近3/5年分位=当前值高于历史多少比例的交易日） |
| **股息（除权派现）** | `RPT_SHAREBONUS_DET`，`PRETAX_BONUS_RMB`=每10股税前派现、`EX_DIVIDEND_DATE`=除权除息日；近12个月已除权派现合计 ÷ 现价 = TTM 现价口径股息率 |
| **公告（标题+日期+栏目）** | `https://np-anotice-stock.eastmoney.com/api/security/ann?sr=-1&page_size=30&page_index=1&ann_type=A&client_source=web&stock_list=600519`（`data.list[].title`/`notice_date`/`columns[].column_name`；标题+日期是硬事实，胜过 AI 摘要） |
| **限售解禁（未来日程）** | datacenter `reportName=RPT_LIFT_STAGE`、`sortColumns=FREE_DATE`、`filter=(SECURITY_CODE="600519")`（`FREE_DATE`解禁日、`FREE_SHARES`解禁股数、`FREE_RATIO`占流通比%、`LIFT_MARKET_CAP`解禁市值元、`FREE_SHARES_TYPE`限售类型。按日期 vs 今日分未来/已发生。空=无解禁，正常） |
| **分红除权（未来登记/除权日）** | datacenter `reportName=RPT_SHAREBONUS_DET`、`sortColumns=PLAN_NOTICE_DATE`（`EQUITY_RECORD_DATE`股权登记日、`EX_DIVIDEND_DATE`除权除息日、`PRETAX_BONUS_RMB`每10股税前派现、`ASSIGN_PROGRESS`进度=预案/股东大会通过/实施分配。同接口亦供股息率，见上） |
| **业绩预告（前瞻指引）** | datacenter `reportName=RPT_PUBLIC_OP_NEWPREDICT`、`sortColumns=NOTICE_DATE`（`PREDICT_TYPE`类型预增/预减/扭亏/首亏、`ADD_AMP_LOWER`/`ADD_AMP_UPPER`净利同比变动下/上限%、`REPORT_DATE`报告期、`NOTICE_DATE`公告日、`IS_LATEST`、`PREDICT_CONTENT`内容。公告超120天视为可能已实现，仅作背景） |
| **两市成交额（市场宽度）** | `https://push2.eastmoney.com/api/qt/ulist.np/get?fields=f2,f3,f6,f12,f14&secids=1.000001,0.399106`（`data.diff[]`；上证综指=全沪、深证综指=全深，`f6`成交额元、`f3`涨跌幅÷100、`f2`点位÷100；两综指 f6 之和≈沪深两市成交额。指数 `stock/get` 不返回 f6，须用 ulist） |
| **涨停/跌停家数（打板情绪）** | 涨停 `https://push2ex.eastmoney.com/getTopicZTPool?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt&Pageindex=0&pagesize=1&sort=fbt:asc&date=YYYYMMDD`、跌停同址 `getTopicDTPool`（取 `data.tc`=家数；date 用真实交易日。⚠️ 指数 f104/f105/f106 涨跌平家数恒返回 0，不可用） |
| **个股所属板块（行业/概念 当日涨跌+主力净流入）** | `https://push2.eastmoney.com/api/qt/slist/get?spt=3&secid=1.600519&fields=f12,f13,f14,f3,f62&po=1&pz=80&pn=1&fid=f62`（`data.diff` 是 dict；取 `f13==90` 的板块：`f12`=板块码 BKxxxx、`f14`=名称、`f3`=涨跌幅(÷100)、`f62`=主力净流入(元)。一次拿齐该股所在全部板块） |
| **板块行情/K线/资金流** | 用板块 secid `90.BKxxxx`，套用上面"个股价/K线/主力资金"同一批接口（如 K线 `…kline/get?secid=90.BK1277…`）。行业板块的 BK 码由估值接口 `RPT_VALUEANALYSIS_DET.ORIG_BOARD_CODE` 派生：`BK{ORIG_BOARD_CODE:0>4}`（如 1277→BK1277 白酒Ⅱ、1033→BK1033 电池），也可在 slist 里按 `BOARD_NAME` 名称匹配 |

## 接口约定（搞错就取到错数）

- 腾讯代码前缀：沪 `sh` + 深 `sz` + 北交所 `bj`，如 `sh600519` / `sz000001`。常用指数：上证 `sh000001`、深成指 `sz399001`、创业板 `sz399006`、沪深300 `sh000300`、科创50 `sh000688`、北证50 `bj899050`。
- 东财 `secid = 市场.代码`：沪 = `1`、深/北 = `0`，如 `1.600519` / `0.000001`。东财接口**须带 User-Agent**，否则被拒。
- 东财价格是放大整数：`stock/get` 一般 `/100`（`f43`=18650 → 186.50）；涨跌幅 `f170` 固定 `/100`。`f86` 是 Unix 时间戳。
- fflow 字段：`f52` = 主力净额（= 大单 `f55` + 超大单 `f56`），单位元，已是真实值不缩放。今日盘中数据来自 push2，历史已收盘日来自 push2his，需拼接去重才得连续5日。
- datacenter 的 `filter` 里 `()="` 必须 URL 百分号编码，否则 400。
