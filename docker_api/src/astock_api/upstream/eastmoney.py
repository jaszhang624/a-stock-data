"""Upstream eastmoney module.

Extracted from a-stock-data SKILL.md - upstream version 3.5.1
Commit: 281fc69a0b733ffc6458fe2cf9f1ea56804aa886
"""

"""Upstream eastmoney module.

Extracted from a-stock-data SKILL.md - upstream version 3.5.1
Commit: 281fc69a0b733ffc6458fe2cf9f1ea56804aa886
"""
import time
import random
import requests
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
DATACENTER_URL = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
EM_SESSION = requests.Session()
EM_MIN_INTERVAL = 1.0
_em_last_call = [0.0]

def em_get(url: str, params: dict | None=None, headers: dict | None=None, timeout: int=15, **kwargs):
    """东财统一请求入口：自动节流 + 复用 session + 默认 UA。
    所有 eastmoney.com 接口都应通过它请求，避免高频被封 IP。"""
    wait = EM_MIN_INTERVAL - (time.time() - _em_last_call[0])
    if wait > 0:
        time.sleep(wait + random.uniform(0.1, 0.5))
    try:
        return EM_SESSION.get(url, params=params, headers=headers, timeout=timeout, **kwargs)
    finally:
        _em_last_call[0] = time.time()

def eastmoney_datacenter(report_name: str, columns: str='ALL', filter_str: str='', page_size: int=50, sort_columns: str='', sort_types: str='-1') -> list[dict]:
    """东财数据中心统一查询 — 龙虎榜/解禁/融资融券/大宗交易/股东户数/分红 共用（已内置限流）"""
    params = {'reportName': report_name, 'columns': columns, 'filter': filter_str, 'pageNumber': '1', 'pageSize': str(page_size), 'sortColumns': sort_columns, 'sortTypes': sort_types, 'source': 'WEB', 'client': 'WEB'}
    r = em_get(DATACENTER_URL, params=params, timeout=15)
    d = r.json()
    if d.get('result') and d['result'].get('data'):
        return d['result']['data']
    return []
import requests
import re
import time
from pathlib import Path
REPORT_API = 'https://reportapi.eastmoney.com/report/list'
PDF_TPL = 'https://pdf.dfcfw.com/pdf/H3_{info_code}_1.pdf'
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'

def eastmoney_reports(code: str, max_pages: int=5) -> list[dict]:
    """拉取指定股票的研报列表"""
    all_records = []
    for page in range(1, max_pages + 1):
        params = {'industryCode': '*', 'pageSize': '100', 'industry': '*', 'rating': '*', 'ratingChange': '*', 'beginTime': '2000-01-01', 'endTime': '2030-01-01', 'pageNo': str(page), 'fields': '', 'qType': '0', 'orgCode': '', 'code': code, 'rcode': '', 'p': str(page), 'pageNum': str(page), 'pageNumber': str(page)}
        r = em_get(REPORT_API, params=params, headers={'Referer': 'https://data.eastmoney.com/'}, timeout=30)
        d = r.json()
        rows = d.get('data') or []
        if not rows:
            break
        all_records.extend(rows)
        if page >= (d.get('TotalPage', 1) or 1):
            break
    return all_records

def download_pdf(record: dict, target_dir: str='./reports') -> str | None:
    """下载单份研报PDF，返回保存路径或None"""
    info_code = record.get('infoCode', '')
    if not info_code:
        return None
    date = (record.get('publishDate') or '')[:10]
    org = re.sub('[\\\\/:*?"<>|]', '_', record.get('orgSName') or '未知')[:40]
    title = re.sub('[\\\\/:*?"<>|]', '_', record.get('title', ''))[:80]
    fname = f'{date}_{org}_{title}.pdf'
    target = Path(target_dir) / fname
    if target.exists():
        return str(target)
    url = PDF_TPL.format(info_code=info_code)
    r = em_get(url, headers={'Referer': 'https://data.eastmoney.com/'}, timeout=60)
    if r.status_code == 200 and len(r.content) >= 1024:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(r.content)
        return str(target)
    return None

def eastmoney_industry_reports(industry_code: str='*', max_pages: int=5, begin: str='2024-01-01') -> list[dict]:
    """拉取行业研报列表（qType=1）。
    industry_code="*" = 全行业；传东财行业码（如 "1238"=IT服务Ⅱ）= 单行业。
    行业名 / 行业码在每条 record 的 industryName / industryCode 字段。"""
    all_records = []
    for page in range(1, max_pages + 1):
        params = {'industryCode': industry_code, 'pageSize': '100', 'industry': '*', 'rating': '*', 'ratingChange': '*', 'beginTime': begin, 'endTime': '2030-01-01', 'pageNo': str(page), 'fields': '', 'qType': '1'}
        r = em_get(REPORT_API, params=params, headers={'Referer': 'https://data.eastmoney.com/'}, timeout=30)
        d = r.json()
        rows = d.get('data') or []
        if not rows:
            break
        all_records.extend(rows)
        if page >= (d.get('TotalPage', 1) or 1):
            break
    return all_records

def eastmoney_concept_blocks(code: str) -> dict:
    """
    个股所属板块/概念归属（东财 slist，一次请求拿全，已内置限流）。
    返回: {total, boards: [{name, code(BK码), change_pct, lead_stock}], concept_tags: [板块名...]}
    boards 混合 行业/概念/地域，板块名自解释；concept_tags 是所有板块名的便捷列表。
    """
    market_code = 1 if code.startswith('6') else 0
    params = {'fltt': '2', 'invt': '2', 'secid': f'{market_code}.{code}', 'spt': '3', 'pi': '0', 'pz': '200', 'po': '1', 'fields': 'f12,f14,f3,f128'}
    headers = {'User-Agent': UA, 'Referer': 'https://quote.eastmoney.com/'}
    try:
        r = em_get('https://push2.eastmoney.com/api/qt/slist/get', params=params, headers=headers, timeout=15)
        d = r.json()
    except Exception as e:
        print(f'[WARN] 东财板块归属请求失败: {e}')
        return {'total': 0, 'boards': [], 'concept_tags': []}
    diff = (d.get('data') or {}).get('diff') or {}
    items = diff.values() if isinstance(diff, dict) else diff
    boards = []
    for it in items:
        boards.append({'name': it.get('f14', ''), 'code': it.get('f12', ''), 'change_pct': it.get('f3', ''), 'lead_stock': it.get('f128', '')})
    return {'total': len(boards), 'boards': boards, 'concept_tags': [b['name'] for b in boards]}
import requests

def eastmoney_fund_flow_minute(code: str) -> list[dict]:
    """
    个股资金流向（分钟级，当日盘中）。
    code: 6位股票代码
    返回: [{time, main_net, small_net, mid_net, large_net, super_net}, ...]
    单位: 元
    """
    secid = f'1.{code}' if code.startswith('6') else f'0.{code}'
    url = 'https://push2.eastmoney.com/api/qt/stock/fflow/kline/get'
    params = {'secid': secid, 'klt': 1, 'fields1': 'f1,f2,f3,f7', 'fields2': 'f51,f52,f53,f54,f55,f56,f57'}
    headers = {'User-Agent': UA, 'Referer': 'https://quote.eastmoney.com/', 'Origin': 'https://quote.eastmoney.com'}
    try:
        r = em_get(url, params=params, headers=headers, timeout=10)
        d = r.json()
    except Exception as e:
        print(f'[WARN] push2 资金流请求失败: {e}')
        return []
    rows = []
    for line in d.get('data', {}).get('klines', []):
        parts = line.split(',')
        if len(parts) >= 6:
            rows.append({'time': parts[0], 'main_net': float(parts[1]), 'small_net': float(parts[2]), 'mid_net': float(parts[3]), 'large_net': float(parts[4]), 'super_net': float(parts[5])})
    return rows
import requests

def industry_comparison(top_n: int=20) -> dict:
    """
    全行业涨跌幅排名（东财行业板块，~100 个行业）。
    返回: {top: [...], bottom: [...], total: int}
    """
    url = 'https://push2.eastmoney.com/api/qt/clist/get'
    params = {'pn': '1', 'pz': '100', 'po': '1', 'np': '1', 'fltt': '2', 'invt': '2', 'fid': 'f3', 'fs': 'm:90+t:2', 'fields': 'f2,f3,f4,f12,f13,f14,f104,f105,f128,f136,f140,f141,f207'}
    headers = {'User-Agent': UA}
    r = em_get(url, params=params, headers=headers, timeout=15)
    d = r.json()
    items = d.get('data', {}).get('diff', [])
    if not items:
        return {'top': [], 'bottom': [], 'total': 0}
    rows = []
    for i, item in enumerate(items):
        rows.append({'rank': i + 1, 'name': item.get('f14', ''), 'change_pct': item.get('f3', 0), 'code': item.get('f12', ''), 'up_count': item.get('f104', 0), 'down_count': item.get('f105', 0), 'leader': item.get('f140', ''), 'leader_change': item.get('f136', 0)})
    return {'top': rows[:top_n], 'bottom': rows[-top_n:], 'total': len(rows)}
import requests
_BOARD_FS = {'industry': 'm:90+t:2', 'concept': 'm:90+t:3', 'region': 'm:90+t:1'}
_BOARD_PERIOD = {'today': ('f62', 'f62', 'f184', 'f3', 'f204'), '5d': ('f164', 'f164', 'f165', 'f109', 'f257'), '10d': ('f174', 'f174', 'f175', 'f160', None)}

def board_fund_flow(board_type: str='industry', period: str='today', top_n: int=20) -> dict:
    """
    板块资金流向排名（按主力净流入降序）。
    board_type: industry(行业) / concept(概念) / region(地域)
    period:     today(今日) / 5d(5日) / 10d(10日)
    返回: {board_type, period, total, rows:[{rank, name, code, change_pct,
           main_net(主力净额,元), main_pct(主力净占比,%), leader(领涨股),
           # 仅 today：super_large_net/large_net/medium_net/small_net(超大/大/中/小单净额,元)}]}
    注：板块级只有 今日/5日/10日（无 3日，个股级才有）。主力净额 = 超大单 + 大单。
    """
    if board_type not in _BOARD_FS:
        raise ValueError(f'board_type 须为 {list(_BOARD_FS)}')
    if period not in _BOARD_PERIOD:
        raise ValueError(f'period 须为 {list(_BOARD_PERIOD)}')
    fid, f_main, f_pct, f_chg, f_leader = _BOARD_PERIOD[period]
    fields = ['f12', 'f14', f_chg, f_main, f_pct]
    if f_leader:
        fields.append(f_leader)
    if period == 'today':
        fields += ['f66', 'f72', 'f78', 'f84']
    url = 'https://push2.eastmoney.com/api/qt/clist/get'
    base = {'pz': '200', 'po': '1', 'np': '1', 'fltt': '2', 'invt': '2', 'fid': fid, 'fs': _BOARD_FS[board_type], 'fields': ','.join(dict.fromkeys(fields))}

    def _page(pn: int):
        r = em_get(url, params={**base, 'pn': str(pn)}, headers={'User-Agent': UA}, timeout=15)
        d = r.json().get('data') or {}
        return (d.get('diff') or [], int(d.get('total') or 0))
    _PAGE = 200
    items, total = _page(1)
    pn = 2
    while len(items) < top_n:
        if total and len(items) >= total:
            break
        more, _ = _page(pn)
        if not more:
            break
        items += more
        pn += 1
        if len(more) < _PAGE:
            break
    total = max(total, len(items))
    rows = []
    for i, it in enumerate(items):
        row = {'rank': i + 1, 'name': it.get('f14', ''), 'code': it.get('f12', ''), 'change_pct': it.get(f_chg, 0), 'main_net': it.get(f_main, 0), 'main_pct': it.get(f_pct, 0), 'leader': it.get(f_leader, '') if f_leader else ''}
        if period == 'today':
            row.update({'super_large_net': it.get('f66', 0), 'large_net': it.get('f72', 0), 'medium_net': it.get('f78', 0), 'small_net': it.get('f84', 0)})
        rows.append(row)
    return {'board_type': board_type, 'period': period, 'total': total, 'rows': rows[:top_n]}
import requests

def stock_fund_flow_120d(code: str) -> list[dict]:
    """
    个股资金流（日级，最近120个交易日）。
    返回: [{date, main_net(主力净流入), small_net, mid_net, large_net, super_net}]
    单位: 元
    """
    market_code = 1 if code.startswith('6') else 0
    url = 'https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get'
    params = {'secid': f'{market_code}.{code}', 'fields1': 'f1,f2,f3,f7', 'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65', 'lmt': '120'}
    headers = {'User-Agent': UA, 'Referer': 'https://quote.eastmoney.com/', 'Origin': 'https://quote.eastmoney.com'}
    try:
        r = em_get(url, params=params, headers=headers, timeout=15)
        d = r.json()
    except Exception as e:
        print(f'[WARN] push2 资金流请求失败: {e}')
        return []
    klines = d.get('data', {}).get('klines', [])
    rows = []
    for line in klines:
        parts = line.split(',')
        if len(parts) >= 7:
            rows.append({'date': parts[0], 'main_net': float(parts[1]) if parts[1] != '-' else 0, 'small_net': float(parts[2]) if parts[2] != '-' else 0, 'mid_net': float(parts[3]) if parts[3] != '-' else 0, 'large_net': float(parts[4]) if parts[4] != '-' else 0, 'super_net': float(parts[5]) if parts[5] != '-' else 0})
    return rows
import requests
import re
import json

def eastmoney_stock_news(code: str, page_size: int=20) -> list[dict]:
    """
    东财个股新闻（JSONP 接口）。
    返回: [{title, content, time, source, url}]
    """
    cb = 'jQuery_news'
    url = 'https://search-api-web.eastmoney.com/search/jsonp'
    inner_params = json.dumps({'uid': '', 'keyword': code, 'type': ['cmsArticleWebOld'], 'client': 'web', 'clientType': 'web', 'clientVersion': 'curr', 'param': {'cmsArticleWebOld': {'searchScope': 'default', 'sort': 'default', 'pageIndex': 1, 'pageSize': page_size, 'preTag': '', 'postTag': ''}}}, separators=(',', ':'))
    params = {'cb': cb, 'param': inner_params}
    headers = {'User-Agent': UA, 'Referer': 'https://so.eastmoney.com/'}
    r = em_get(url, params=params, headers=headers, timeout=15)
    text = r.text
    json_str = text[text.index('(') + 1:text.rindex(')')]
    d = json.loads(json_str)
    rows = []
    articles = d.get('result', {}).get('cmsArticleWebOld', []) or []
    for a in articles:
        rows.append({'title': re.sub('<[^>]+>', '', a.get('title', '')), 'content': re.sub('<[^>]+>', '', a.get('content', ''))[:200], 'time': a.get('date', ''), 'source': a.get('mediaName', ''), 'url': a.get('url', '')})
    return rows
import requests
import uuid

def eastmoney_global_news(page_size: int=50) -> list[dict]:
    """
    东方财富全球财经资讯（7x24 滚动）。
    返回: [{title, summary, time}]
    """
    url = 'https://np-weblist.eastmoney.com/comm/web/getFastNewsList'
    params = {'client': 'web', 'biz': 'web_724', 'fastColumn': '102', 'sortEnd': '', 'pageSize': str(page_size), 'req_trace': str(uuid.uuid4())}
    headers = {'User-Agent': UA, 'Referer': 'https://kuaixun.eastmoney.com/'}
    r = em_get(url, params=params, headers=headers, timeout=10)
    d = r.json()
    rows = []
    for item in d.get('data', {}).get('fastNewsList', []):
        rows.append({'title': item.get('title', ''), 'summary': item.get('summary', '')[:200], 'time': item.get('showTime', '')})
    return rows
import requests

def eastmoney_stock_info(code: str) -> dict:
    """
    东财个股基本面信息。
    返回: {code, name, industry, total_shares, float_shares, mcap, float_mcap, list_date}
    """
    market_code = 1 if code.startswith('6') else 0
    url = 'https://push2.eastmoney.com/api/qt/stock/get'
    params = {'fltt': '2', 'invt': '2', 'fields': 'f57,f58,f84,f85,f127,f116,f117,f189,f43', 'secid': f'{market_code}.{code}'}
    headers = {'User-Agent': UA}
    r = em_get(url, params=params, headers=headers, timeout=10)
    d = r.json().get('data', {})
    return {'code': d.get('f57', ''), 'name': d.get('f58', ''), 'industry': d.get('f127', ''), 'total_shares': d.get('f84', 0), 'float_shares': d.get('f85', 0), 'mcap': d.get('f116', 0), 'float_mcap': d.get('f117', 0), 'list_date': str(d.get('f189', '')), 'price': d.get('f43', 0)}
import requests
ZTB_UT = '7eea3edcaed734bea9cbfc24409ed989'

def _fmt_zt_time(t) -> str:
    """涨停板时间整数 → HH:MM:SS（92500 → 09:25:00）。"""
    s = str(t).zfill(6)
    return f'{s[0:2]}:{s[2:4]}:{s[4:6]}'

def _em_zt_api(endpoint: str, sort: str, date: str) -> list[dict]:
    """东财涨停板行情中心通用请求（push2ex，走 em_get 限流）。
    endpoint: getTopicZTPool / getTopicZBPool / getTopicDTPool / getYesterdayZTPool
    返回 data.pool 原始列表（data 为 null = 非交易日 / 参数错）。"""
    url = f'https://push2ex.eastmoney.com/{endpoint}'
    params = {'ut': ZTB_UT, 'dpt': 'wz.ztzt', 'Pageindex': 0, 'pagesize': 10000, 'sort': sort, 'date': date}
    headers = {'User-Agent': UA, 'Referer': 'https://quote.eastmoney.com/'}
    try:
        r = em_get(url, params=params, headers=headers, timeout=10)
        return (r.json().get('data') or {}).get('pool') or []
    except Exception as e:
        print(f'[WARN] 涨停板池 {endpoint} 请求失败: {e}')
        return []

def em_zt_pool(date: str) -> list[dict]:
    """涨停池。date=YYYYMMDD（交易日）。
    返回每只: code/name/price/pct/amount/float_cap/turnover/limit_days(连板数)/
    first_seal/last_seal(封板时间)/seal_fund(封板资金,元)/break_times(炸板次数)/
    industry/zt_stat(N天M板)"""
    out = []
    for p in _em_zt_api('getTopicZTPool', 'fbt:asc', date):
        out.append({'code': p['c'], 'name': p['n'], 'price': p['p'] / 1000, 'pct': round(p['zdp'], 2), 'amount': p['amount'], 'float_cap': p['ltsz'], 'turnover': round(p['hs'], 2), 'limit_days': p['lbc'], 'first_seal': _fmt_zt_time(p['fbt']), 'last_seal': _fmt_zt_time(p['lbt']), 'seal_fund': p['fund'], 'break_times': p['zbc'], 'industry': p.get('hybk', ''), 'zt_stat': f"{(p.get('zttj') or {}).get('days', '?')}天{(p.get('zttj') or {}).get('ct', '?')}板"})
    return out

def em_zb_pool(date: str) -> list[dict]:
    """炸板池（涨停后开板）。返回 code/name/price/limit_price(涨停价)/pct/turnover/
    first_seal/break_times/amplitude(振幅)/speed(涨速)/industry/zt_stat"""
    out = []
    for p in _em_zt_api('getTopicZBPool', 'fbt:asc', date):
        out.append({'code': p['c'], 'name': p['n'], 'price': p['p'] / 1000, 'limit_price': p['ztp'] / 1000, 'pct': round(p['zdp'], 2), 'turnover': round(p['hs'], 2), 'first_seal': _fmt_zt_time(p['fbt']), 'break_times': p['zbc'], 'amplitude': round(p['zf'], 2), 'speed': round(p['zs'], 2), 'industry': p.get('hybk', ''), 'zt_stat': f"{(p.get('zttj') or {}).get('days', '?')}天{(p.get('zttj') or {}).get('ct', '?')}板"})
    return out

def em_dt_pool(date: str) -> list[dict]:
    """跌停池。返回 code/name/price/pct/turnover/pe/seal_fund(封单资金)/last_seal/
    board_amount(板上成交额)/dt_days(连续跌停)/open_times(开板次数)/industry"""
    out = []
    for p in _em_zt_api('getTopicDTPool', 'fund:asc', date):
        out.append({'code': p['c'], 'name': p['n'], 'price': p['p'] / 1000, 'pct': round(p['zdp'], 2), 'turnover': round(p['hs'], 2), 'pe': p.get('pe'), 'seal_fund': p['fund'], 'last_seal': _fmt_zt_time(p['lbt']), 'board_amount': p.get('fba'), 'dt_days': p.get('days'), 'open_times': p.get('oc'), 'industry': p.get('hybk', '')})
    return out

def em_yzt_pool(date: str) -> list[dict]:
    """昨日涨停池（昨涨停今表现，算晋级率/赚钱效应）。返回 code/name/price/
    pct(今日涨幅)/turnover/amplitude/speed/y_first_seal(昨封板时间)/
    y_limit_days(昨连板)/industry/zt_stat"""
    out = []
    for p in _em_zt_api('getYesterdayZTPool', 'zs:desc', date):
        out.append({'code': p['c'], 'name': p['n'], 'price': p['p'] / 1000, 'pct': round(p['zdp'], 2), 'turnover': round(p['hs'], 2), 'amplitude': round(p['zf'], 2), 'speed': round(p['zs'], 2), 'y_first_seal': _fmt_zt_time(p['yfbt']), 'y_limit_days': p['ylbc'], 'industry': p.get('hybk', ''), 'zt_stat': f"{(p.get('zttj') or {}).get('days', '?')}天{(p.get('zttj') or {}).get('ct', '?')}板"})
    return out