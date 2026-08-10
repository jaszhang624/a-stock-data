"""Upstream sina module.

Extracted from a-stock-data SKILL.md - upstream version 3.5.1
Commit: 281fc69a0b733ffc6458fe2cf9f1ea56804aa886
"""
import requests

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

def sina_financial_report(code: str, report_type: str='lrb', num: int=8) -> list[dict]:
    """
    新浪财报三表。
    code: 6位代码
    report_type: "fzb"(资产负债表) / "lrb"(利润表) / "llb"(现金流量表)
    num: 取最近 N 期（默认 8 期）
    返回: 按报告期倒序的记录列表，每期一条 dict：
          {"报告期": "2026-03-31", "<科目>": "<值>", "<科目>_同比": <同比>, ...}
          （item_value 为新浪原始字符串数值，仅在有同比时附 "_同比" 键）
    """
    prefix = 'sh' if code.startswith('6') else 'sz'
    paper_code = f'{prefix}{code}'
    url = 'https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022'
    params = {'paperCode': paper_code, 'source': report_type, 'type': '0', 'page': '1', 'num': str(num)}
    headers = {'User-Agent': UA}
    r = requests.get(url, params=params, headers=headers, timeout=15)
    report_list = r.json().get('result', {}).get('data', {}).get('report_list', {}) or {}
    rows = []
    for period in sorted(report_list.keys(), reverse=True)[:num]:
        obj = report_list[period]
        rec = {'报告期': f'{period[:4]}-{period[4:6]}-{period[6:8]}'}
        for it in obj.get('data', []) or []:
            title = it.get('item_title', '')
            if not title or it.get('item_value') is None:
                continue
            rec[title] = it.get('item_value')
            tongbi = it.get('item_tongbi')
            if tongbi not in (None, ''):
                rec[title + '_同比'] = tongbi
        rows.append(rec)
    return rows
import requests
SINA_OPT_HDR = {'Referer': 'https://stock.finance.sina.com.cn/', 'User-Agent': UA}

def _opt_f(x):
    try:
        return float(x)
    except Exception:
        return x

def _sina_opt_list(param: str) -> list:
    """新浪 hq.sinajs.cn 取值（GBK，逗号分隔，去 var hq_str_XXX="..." 壳）。"""
    r = requests.get(f'https://hq.sinajs.cn/list={param}', headers=SINA_OPT_HDR, timeout=10)
    r.encoding = 'gbk'
    t = r.text
    return t.split('"')[1].split(',') if '"' in t else []

def sina_option_codes(underlying: str='510050', call: bool=True) -> dict:
    """ETF期权合约清单。underlying: 510050/510300/588000/510500。call=True认购/False认沽。
    返回 {月份YYMM: [合约代码,...]}，第一个 key 即近月。"""
    cate = {'510050': '50ETF', '510300': '300ETF', '588000': '科创50ETF', '510500': '500ETF'}.get(underlying, '50ETF')
    url = f'https://stock.finance.sina.com.cn/futures/api/openapi.php/StockOptionService.getStockName?exchange=null&cate={cate}'
    try:
        months = requests.get(url, headers=SINA_OPT_HDR, timeout=10).json()['result']['data']['contractMonth']
    except Exception as e:
        print(f'[WARN] 期权月份获取失败: {e}')
        return {}
    months = [m.replace('-', '')[2:] for m in months[1:]]
    flag = 'OP_UP_' if call else 'OP_DOWN_'
    out = {}
    for m in months:
        codes = [c.replace('CON_OP_', '') for c in _sina_opt_list(f'{flag}{underlying}{m}') if c.startswith('CON_OP_')]
        if codes:
            out[m] = codes
    return out

def sina_option_tquote(code: str) -> dict:
    """期权T型报价。返回 bid_vol/bid/last/ask/ask_vol/open_interest(持仓量)/pct/
    strike(行权价)/prev_close/open/limit_up/limit_down/name/amplitude/high/low/volume/amount。"""
    v = _sina_opt_list(f'CON_OP_{code}')
    if len(v) < 43:
        return {}
    return {'bid_vol': _opt_f(v[0]), 'bid': _opt_f(v[1]), 'last': _opt_f(v[2]), 'ask': _opt_f(v[3]), 'ask_vol': _opt_f(v[4]), 'open_interest': _opt_f(v[5]), 'pct': _opt_f(v[6]), 'strike': _opt_f(v[7]), 'prev_close': _opt_f(v[8]), 'open': _opt_f(v[9]), 'limit_up': _opt_f(v[10]), 'limit_down': _opt_f(v[11]), 'name': v[37], 'amplitude': _opt_f(v[38]), 'high': _opt_f(v[39]), 'low': _opt_f(v[40]), 'volume': _opt_f(v[41]), 'amount': _opt_f(v[42])}

def sina_option_greeks(code: str) -> dict:
    """期权希腊字母 + 隐含波动率。返回 name/volume/delta/gamma/theta/vega/
    iv(隐含波动率,小数)/high/low/trade_code/strike/last/theory(理论价值)。"""
    raw = _sina_opt_list(f'CON_SO_{code}')
    if len(raw) < 16:
        return {}
    v = [raw[0]] + raw[4:]
    return {'name': v[0], 'volume': _opt_f(v[1]), 'delta': _opt_f(v[2]), 'gamma': _opt_f(v[3]), 'theta': _opt_f(v[4]), 'vega': _opt_f(v[5]), 'iv': _opt_f(v[6]), 'high': _opt_f(v[7]), 'low': _opt_f(v[8]), 'trade_code': v[9], 'strike': _opt_f(v[10]), 'last': _opt_f(v[11]), 'theory': _opt_f(v[12])}