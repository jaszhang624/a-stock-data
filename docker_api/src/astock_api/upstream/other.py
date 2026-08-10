"""Upstream other module.

Extracted from a-stock-data SKILL.md - upstream version 3.5.1
Commit: 281fc69a0b733ffc6458fe2cf9f1ea56804aa886
"""

"""Upstream other module.

Extracted from a-stock-data SKILL.md - upstream version 3.5.1
Commit: 281fc69a0b733ffc6458fe2cf9f1ea56804aa886
"""
import os
import json
import secrets
import requests

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
IWENCAI_BASE = os.environ.get('IWENCAI_BASE_URL', 'https://openapi.iwencai.com')
IWENCAI_KEY = os.environ.get('IWENCAI_API_KEY', '')

def _claw_headers(call_type: str='normal') -> dict:
    """SkillHub 2.0 必须的 X-Claw 鉴权头"""
    return {'X-Claw-Call-Type': call_type, 'X-Claw-Skill-Id': 'report-search', 'X-Claw-Skill-Version': '2.0.0', 'X-Claw-Plugin-Id': 'none', 'X-Claw-Plugin-Version': 'none', 'X-Claw-Trace-Id': secrets.token_hex(32)}

def iwencai_search(query: str, channel: str='report', size: int=50) -> list[dict]:
    """
    iwencai 语义搜索。
    channel: "report"(研报) / "announcement"(公告) / "news"(新闻)
    size: 默认10, 实测可调到50（隐藏参数）
    """
    headers = {'Authorization': f'Bearer {IWENCAI_KEY}', 'Content-Type': 'application/json', **_claw_headers()}
    payload = {'channels': [channel], 'app_id': 'AIME_SKILL', 'query': query, 'size': size}
    r = requests.post(f'{IWENCAI_BASE}/v1/comprehensive/search', json=payload, headers=headers, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f'iwencai HTTP {r.status_code}: {r.text[:200]}')
    data = r.json()
    if data.get('status_code', 0) != 0:
        raise RuntimeError(f"iwencai error: {data.get('status_msg', '')}")
    return data.get('data') or []

def iwencai_query(query: str, page: int=1, limit: int=50) -> list[dict]:
    """
    iwencai NL数据查询（结构化字段）。
    例: "贵州茅台 ROE" → DataFrame-like rows
    """
    headers = {'Authorization': f'Bearer {IWENCAI_KEY}', 'Content-Type': 'application/json', **_claw_headers()}
    payload = {'query': query, 'page': str(page), 'limit': str(limit), 'is_cache': '1', 'expand_index': 'true'}
    r = requests.post(f'{IWENCAI_BASE}/v1/query2data', json=payload, headers=headers, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f'iwencai HTTP {r.status_code}: {r.text[:200]}')
    data = r.json()
    if data.get('status_code', 0) != 0:
        raise RuntimeError(f"iwencai error: {data.get('status_msg', '')}")
    return data.get('datas') or []

def dedup_articles(articles: list[dict]) -> list[dict]:
    """同一uid仅保留score最高的段落"""
    best = {}
    for a in articles:
        uid = a.get('uid', '') or f"{a.get('title', '')}|{a.get('publish_date', '')}"
        score = float(a.get('score', 0))
        if uid not in best or score > float(best[uid].get('score', 0)):
            best[uid] = a
    return sorted(best.values(), key=lambda x: x.get('publish_date', ''), reverse=True)
import requests
import pandas as pd
from pathlib import Path
HSGT_HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0 Safari/537.36', 'Host': 'data.hexin.cn', 'Referer': 'https://data.hexin.cn/'}

def hsgt_realtime() -> pd.DataFrame:
    """
    沪深股通当日实时分钟流向（含集合竞价 09:10–15:00，262 个时间点）。
    返回字段: time, hgt(沪股通累计净买入), sgt(深股通累计净买入)
    单位: 亿元
    """
    url = 'https://data.hexin.cn/market/hsgtApi/method/dayChart/'
    r = requests.get(url, headers=HSGT_HEADERS, timeout=10)
    d = r.json()
    times = d.get('time', [])
    hgt = d.get('hgt', [])
    sgt = d.get('sgt', [])
    n = len(times)
    return pd.DataFrame({'time': times, 'hgt_yi': hgt[:n] + [None] * (n - len(hgt)), 'sgt_yi': sgt[:n] + [None] * (n - len(sgt))})

def _northbound_cache_path() -> Path:
    """北向资金本地 CSV 缓存路径"""
    p = Path.home() / '.tradingagents' / 'cache' / 'northbound_daily.csv'
    p.parent.mkdir(parents=True, exist_ok=True)
    return p

def _save_northbound_snapshot(date: str, hgt: float, sgt: float):
    """写入/更新当天北向收盘数据到 CSV"""
    path = _northbound_cache_path()
    rows = {}
    if path.exists():
        for line in path.read_text().strip().split('\n')[1:]:
            parts = line.split(',')
            if len(parts) == 3:
                rows[parts[0]] = line
    rows[date] = f'{date},{hgt},{sgt}'
    with open(path, 'w') as f:
        f.write('date,hgt,sgt\n')
        for d in sorted(rows.keys()):
            f.write(rows[d] + '\n')

def _load_northbound_history(n: int=20) -> pd.DataFrame:
    """读取最近 N 天北向历史"""
    path = _northbound_cache_path()
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    return df.tail(n)
import requests
from datetime import datetime, timedelta

def dragon_tiger_board(code: str, trade_date: str, look_back: int=30) -> dict:
    """
    龙虎榜数据聚合。
    trade_date: YYYY-MM-DD
    look_back: 回看天数
    返回: {records: [...], seats: {buy: [...], sell: [...]}, institution: {...}}
    """
    start = datetime.strptime(trade_date, '%Y-%m-%d') - timedelta(days=look_back)
    start_str = start.strftime('%Y-%m-%d')
    records = []
    data = eastmoney_datacenter('RPT_DAILYBILLBOARD_DETAILSNEW', filter_str=f'''(TRADE_DATE>='{start_str}')(TRADE_DATE<='{trade_date}')(SECURITY_CODE="{code}")''', page_size=50, sort_columns='TRADE_DATE', sort_types='-1')
    for row in data:
        records.append({'date': str(row.get('TRADE_DATE', ''))[:10], 'reason': row.get('EXPLANATION', ''), 'net_buy': round((row.get('BILLBOARD_NET_AMT') or 0) / 10000, 1), 'turnover': round(float(row.get('TURNOVERRATE') or 0), 2)})
    seats = {'buy': [], 'sell': []}
    if records:
        latest_date = records[0]['date']
        buy_data = eastmoney_datacenter('RPT_BILLBOARD_DAILYDETAILSBUY', filter_str=f'''(TRADE_DATE='{latest_date}')(SECURITY_CODE="{code}")''', page_size=10, sort_columns='BUY', sort_types='-1')
        for row in buy_data[:5]:
            seats['buy'].append({'name': row.get('OPERATEDEPT_NAME', ''), 'buy_amt': round((row.get('BUY') or 0) / 10000, 1), 'sell_amt': round((row.get('SELL') or 0) / 10000, 1), 'net': round((row.get('NET') or 0) / 10000, 1)})
        sell_data = eastmoney_datacenter('RPT_BILLBOARD_DAILYDETAILSSELL', filter_str=f'''(TRADE_DATE='{latest_date}')(SECURITY_CODE="{code}")''', page_size=10, sort_columns='SELL', sort_types='-1')
        for row in sell_data[:5]:
            seats['sell'].append({'name': row.get('OPERATEDEPT_NAME', ''), 'buy_amt': round((row.get('BUY') or 0) / 10000, 1), 'sell_amt': round((row.get('SELL') or 0) / 10000, 1), 'net': round((row.get('NET') or 0) / 10000, 1)})
    institution = {'buy_amt': 0, 'sell_amt': 0, 'net_amt': 0}
    for detail_data, side in [(buy_data, 'buy'), (sell_data, 'sell')]:
        for row in detail_data:
            if str(row.get('OPERATEDEPT_CODE', '')) == '0':
                amt = row.get('BUY') or 0 if side == 'buy' else row.get('SELL') or 0
                if side == 'buy':
                    institution['buy_amt'] += amt
                else:
                    institution['sell_amt'] += amt
    institution['buy_amt'] = round(institution['buy_amt'] / 10000, 1)
    institution['sell_amt'] = round(institution['sell_amt'] / 10000, 1)
    institution['net_amt'] = round(institution['buy_amt'] - institution['sell_amt'], 1)
    return {'records': records, 'seats': seats, 'institution': institution}
from datetime import datetime, timedelta

def lockup_expiry(code: str, trade_date: str, forward_days: int=90) -> dict:
    """
    限售解禁日历。
    返回: {history: [...], upcoming: [...]}
    """
    history_data = eastmoney_datacenter('RPT_LIFT_STAGE', filter_str=f'(SECURITY_CODE="{code}")', page_size=15, sort_columns='FREE_DATE', sort_types='-1')
    history = []
    for row in history_data:
        history.append({'date': str(row.get('FREE_DATE', ''))[:10], 'type': row.get('FREE_SHARES_TYPE', ''), 'shares': row.get('FREE_SHARES', 0), 'able_shares': row.get('ABLE_FREE_SHARES', 0), 'ratio': row.get('FREE_RATIO', 0)})
    end_date = datetime.strptime(trade_date, '%Y-%m-%d') + timedelta(days=forward_days)
    end_str = end_date.strftime('%Y-%m-%d')
    upcoming_data = eastmoney_datacenter('RPT_LIFT_STAGE', filter_str=f"""(SECURITY_CODE="{code}")(FREE_DATE>='{trade_date}')(FREE_DATE<='{end_str}')""", page_size=20, sort_columns='FREE_DATE', sort_types='1')
    upcoming = []
    for row in upcoming_data:
        upcoming.append({'date': str(row.get('FREE_DATE', ''))[:10], 'type': row.get('FREE_SHARES_TYPE', ''), 'shares': row.get('FREE_SHARES', 0), 'able_shares': row.get('ABLE_FREE_SHARES', 0), 'ratio': row.get('FREE_RATIO', 0)})
    return {'history': history, 'upcoming': upcoming}
from datetime import datetime

def daily_dragon_tiger(trade_date: str=None, min_net_buy: float=None) -> dict:
    """
    全市场龙虎榜。
    trade_date: YYYY-MM-DD（默认当日）
    min_net_buy: 净买入下限（万元），None 不过滤
    返回: {date, total_records, stocks: [{code, name, reason, close, change_pct,
           net_buy_wan, buy_wan, sell_wan, turnover_pct}]}
    """
    if trade_date is None:
        trade_date = datetime.now().strftime('%Y-%m-%d')
    data = eastmoney_datacenter('RPT_DAILYBILLBOARD_DETAILSNEW', filter_str=f"(TRADE_DATE>='{trade_date}')(TRADE_DATE<='{trade_date}')", page_size=500, sort_columns='BILLBOARD_NET_AMT', sort_types='-1')
    if not data:
        return {'date': trade_date, 'total_records': 0, 'stocks': [], 'note': '无数据（非交易日或盘后未更新）'}
    actual_date = str(data[0].get('TRADE_DATE', ''))[:10] if data else trade_date
    stocks = []
    for row in data:
        net_buy = (row.get('BILLBOARD_NET_AMT') or 0) / 10000
        if min_net_buy is not None and net_buy < min_net_buy:
            continue
        stocks.append({'code': row.get('SECURITY_CODE', ''), 'name': row.get('SECURITY_NAME_ABBR', ''), 'reason': row.get('EXPLANATION', ''), 'close': row.get('CLOSE_PRICE') or 0, 'change_pct': round(float(row.get('CHANGE_RATE') or 0), 2), 'net_buy_wan': round(net_buy, 1), 'buy_wan': round((row.get('BILLBOARD_BUY_AMT') or 0) / 10000, 1), 'sell_wan': round((row.get('BILLBOARD_SELL_AMT') or 0) / 10000, 1), 'turnover_pct': round(float(row.get('TURNOVERRATE') or 0), 2)})
    return {'date': actual_date, 'total_records': len(stocks), 'stocks': stocks}

def margin_trading(code: str, page_size: int=30) -> list[dict]:
    """
    融资融券明细（日级）。
    返回: [{date, rzye(融资余额), rzmre(融资买入), rqye(融券余额), ...}]
    """
    data = eastmoney_datacenter('RPTA_WEB_RZRQ_GGMX', filter_str=f'(SCODE="{code}")', page_size=page_size, sort_columns='DATE', sort_types='-1')
    rows = []
    for row in data:
        rows.append({'date': str(row.get('DATE', ''))[:10], 'rzye': row.get('RZYE', 0), 'rzmre': row.get('RZMRE', 0), 'rzche': row.get('RZCHE', 0), 'rqye': row.get('RQYE', 0), 'rqmcl': row.get('RQMCL', 0), 'rqchl': row.get('RQCHL', 0), 'rzrqye': row.get('RZRQYE', 0)})
    return rows

def block_trade(code: str, page_size: int=20) -> list[dict]:
    """
    大宗交易记录。
    返回: [{date, price, vol, amount, buyer, seller, premium_pct}]
    """
    data = eastmoney_datacenter('RPT_DATA_BLOCKTRADE', filter_str=f'(SECURITY_CODE="{code}")', page_size=page_size, sort_columns='TRADE_DATE', sort_types='-1')
    rows = []
    for row in data:
        close = row.get('CLOSE_PRICE') or 0
        deal_price = row.get('DEAL_PRICE') or 0
        premium = (deal_price / close - 1) * 100 if close else 0
        rows.append({'date': str(row.get('TRADE_DATE', ''))[:10], 'price': deal_price, 'close': close, 'premium_pct': round(premium, 2), 'vol': row.get('DEAL_VOLUME', 0), 'amount': row.get('DEAL_AMT', 0), 'buyer': row.get('BUYER_NAME', ''), 'seller': row.get('SELLER_NAME', '')})
    return rows

def holder_num_change(code: str, page_size: int=10) -> list[dict]:
    """
    股东户数变化（季度级）。
    返回: [{date, holder_num, change_num, change_ratio, avg_shares}]
    """
    data = eastmoney_datacenter('RPT_HOLDERNUMLATEST', filter_str=f'(SECURITY_CODE="{code}")', page_size=page_size, sort_columns='END_DATE', sort_types='-1')
    rows = []
    for row in data:
        rows.append({'date': str(row.get('END_DATE', ''))[:10], 'holder_num': row.get('HOLDER_NUM', 0), 'change_num': row.get('HOLDER_NUM_CHANGE', 0), 'change_ratio': row.get('HOLDER_NUM_RATIO', 0), 'avg_shares': row.get('AVG_FREE_SHARES', 0)})
    return rows

def dividend_history(code: str, page_size: int=20) -> list[dict]:
    """
    分红送转历史。
    返回: [{date, bonus_rmb(每股派息), transfer_ratio(转增比例), bonus_ratio(送股比例)}]
    """
    data = eastmoney_datacenter('RPT_SHAREBONUS_DET', filter_str=f'(SECURITY_CODE="{code}")', page_size=page_size, sort_columns='EX_DIVIDEND_DATE', sort_types='-1')
    rows = []
    for row in data:
        rows.append({'date': str(row.get('EX_DIVIDEND_DATE', ''))[:10], 'bonus_rmb': row.get('PRETAX_BONUS_RMB', 0), 'transfer_ratio': row.get('TRANSFER_RATIO', 0), 'bonus_ratio': row.get('BONUS_RATIO', 0), 'plan': row.get('ASSIGN_PROGRESS', '')})
    return rows

def limit_up_sentiment(date: str) -> dict:
    """打板情绪温度计：连板梯队 + 炸板率 + 涨跌停对比。"""
    zt, zb, dt = (em_zt_pool(date), em_zb_pool(date), em_dt_pool(date))
    ladder = {}
    for s in zt:
        ladder[s['limit_days']] = ladder.get(s['limit_days'], 0) + 1
    zt_n, zb_n = (len(zt), len(zb))
    return {'date': date, 'zt_count': zt_n, 'zb_count': zb_n, 'dt_count': len(dt), 'break_rate': round(zb_n / (zt_n + zb_n) * 100, 1) if zt_n + zb_n else 0, 'max_height': max((s['limit_days'] for s in zt), default=0), 'ladder': dict(sorted(ladder.items()))}

def forward_pe(price: float, eps_forecast: float) -> float:
    """前向PE = 当前股价 / 未来年度一致预期EPS"""
    if eps_forecast <= 0:
        return float('inf')
    return price / eps_forecast
import math

def pe_digestion(current_pe: float, cagr: float, target_pe: float=30) -> float:
    """
    当前PE消化到目标PE需要多少年。
    target_pe 固定30x（A股成长股合理估值锚点）。
    cagr: 用 下一年EPS / 当年EPS - 1
    """
    if current_pe <= target_pe:
        return 0.0
    if cagr <= 0:
        return float('inf')
    return math.log(current_pe / target_pe) / math.log(1 + cagr)

def calc_peg(pe: float, cagr: float) -> float:
    """
    PEG = 前向PE / (CAGR * 100)
    PEG < 1   → 便宜
    PEG 1-1.5 → 合理
    PEG > 1.5 → 贵
    """
    if cagr <= 0:
        return float('inf')
    return pe / (cagr * 100)
import json, urllib.request, ssl
_ctx = ssl.create_default_context()

def dragon_tiger_backup(trade_date: str) -> dict:
    """龙虎榜官方备用源（东财被封时用）：上交所+深交所官方，零鉴权权威一手，含营业部席位。"""
    out = {'date': trade_date, 'sse_raw': '', 'szse': []}
    su = f'https://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON&CATALOGID=1842_xxpl&TABKEY=tab1&txtStart={trade_date}&txtEnd={trade_date}&random=0.9'
    req = urllib.request.Request(su, headers={'User-Agent': UA, 'Referer': 'https://www.szse.cn/disclosure/supervision/dealinfo/index.html'})
    with urllib.request.urlopen(req, timeout=15, context=_ctx) as r:
        d = json.loads(r.read())
    for row in d[0].get('data', []):
        out['szse'].append({'code': row.get('zqdm'), 'name': row.get('zqjc'), 'amount': row.get('cjje'), 'reason': row.get('plyy')})
    eu = f'https://query.sse.com.cn/infodisplay/showTradePublicFile.do?jsonCallBack=cb&isPagination=false&dateTx={trade_date}'
    req = urllib.request.Request(eu, headers={'User-Agent': UA, 'Referer': 'https://www.sse.com.cn/disclosure/diclosure/public/'})
    with urllib.request.urlopen(req, timeout=15) as r:
        t = r.read().decode('utf-8', 'ignore')
    out['sse_raw'] = '\n'.join(json.loads(t[t.index('(') + 1:t.rindex(')')]).get('fileContents', []))
    return out

def fund_flow_backup(code: str, days: int=60) -> list:
    """个股资金流备用源（东财被封时用）：新浪，日度四档单净额。"""
    pre = ('bj' if code.startswith(('92', '8')) else 'sh' if code.startswith(('6', '9')) else 'sz') + code
    u = f'https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_zjlrqs?page=1&num={days}&sort=opendate&asc=0&daima={pre}'
    req = urllib.request.Request(u, headers={'User-Agent': UA, 'Referer': 'https://finance.sina.com.cn/'})
    with urllib.request.urlopen(req, timeout=15) as r:
        t = r.read().decode('utf-8', 'ignore')
    arr = json.loads(t[t.index('['):t.rindex(']') + 1])
    return [{'date': x.get('opendate'), 'close': x.get('trade'), 'net_amount': x.get('netamount'), 'turnover': x.get('turnover')} for x in arr]

def announcements_backup(code: str, page_size: int=20) -> list:
    """公告备用源（巨潮被封时用）：深市走深交所官方，沪市走东财，均带 PDF 直链。"""
    if code.startswith(('0', '3')):
        body = json.dumps({'channelCode': ['listedNotice_disc'], 'pageSize': page_size, 'pageNum': 1, 'stock': [code]}).encode()
        req = urllib.request.Request('https://www.szse.cn/api/disc/announcement/annList', data=body, headers={'User-Agent': UA, 'Content-Type': 'application/json', 'Referer': 'https://www.szse.cn/disclosure/listed/notice/index.html'})
        with urllib.request.urlopen(req, timeout=15, context=_ctx) as r:
            d = json.loads(r.read())
        return [{'title': a.get('title'), 'time': a.get('publishTime', '')[:10], 'pdf': 'https://disc.static.szse.cn/download' + a.get('attachPath', '')} for a in d.get('data', [])]
    u = f'https://np-anotice-stock.eastmoney.com/api/security/ann?sr=-1&page_size={page_size}&page_index=1&ann_type=A&client_source=web&stock_list={code}&f_node=0&s_node=0'
    req = urllib.request.Request(u, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read())
    return [{'title': a.get('title'), 'time': a.get('notice_date', '')[:10], 'pdf': f"https://pdf.dfcfw.com/pdf/H2_{a.get('art_code', '')}_1.pdf"} for a in d.get('data', {}).get('list', [])]