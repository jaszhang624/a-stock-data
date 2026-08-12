"""Upstream tencent module.

Extracted from a-stock-data SKILL.md - upstream version 3.5.1
Commit: 281fc69a0b733ffc6458fe2cf9f1ea56804aa886
"""
import urllib.request
import requests
import math
import pandas as pd

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'


def tencent_quote(codes: list[str]) -> dict[str, dict]:
    """
    批量拉取腾讯财经实时行情。
    codes: ["688017", "300476", "002463"]
    也支持指数: ["000001", "000300", "399006"]
    也支持ETF: ["510050", "510300"]
    返回: {code: {name, price, pe_ttm, pb, mcap, ...}}
    """
    SH_INDEX = {'000300', '000905', '000016', '000688', '000852', '000010'}
    prefixed = []
    key_of = {}
    for c in codes:
        low = c.lower()
        if low.startswith(('sh', 'sz', 'bj')):
            p = low
        elif c.startswith('92'):
            p = f'bj{c}'
        elif c in SH_INDEX or c.startswith(('5', '6', '9')):
            p = f'sh{c}'
        elif c.startswith(('4', '8')):
            p = f'bj{c}'
        else:
            p = f'sz{c}'
        prefixed.append(p)
        key_of[p] = c
    url = 'https://qt.gtimg.cn/q=' + ','.join(prefixed)
    req = urllib.request.Request(url)
    req.add_header('User-Agent', UA)
    resp = urllib.request.urlopen(req, timeout=10)
    data = resp.read().decode('gbk')
    result = {}
    for line in data.strip().split(';'):
        if not line.strip() or '=' not in line or '"' not in line:
            continue
        key = line.split('=')[0].split('_')[-1]
        vals = line.split('"')[1].split('~')
        if len(vals) < 53:
            continue
        code = key_of.get(key, key[2:])
        result[code] = {'name': vals[1], 'price': float(vals[3]) if vals[3] else 0, 'last_close': float(vals[4]) if vals[4] else 0, 'open': float(vals[5]) if vals[5] else 0, 'change_amt': float(vals[31]) if vals[31] else 0, 'change_pct': float(vals[32]) if vals[32] else 0, 'high': float(vals[33]) if vals[33] else 0, 'low': float(vals[34]) if vals[34] else 0, 'amount_wan': float(vals[37]) if vals[37] else 0, 'turnover_pct': float(vals[38]) if vals[38] else 0, 'pe_ttm': float(vals[39]) if vals[39] else 0, 'amplitude_pct': float(vals[43]) if vals[43] else 0, 'float_mcap_yi': float(vals[44]) if vals[44] else 0, 'mcap_yi': float(vals[45]) if vals[45] else 0, 'pb': float(vals[46]) if vals[46] else 0, 'limit_up': float(vals[47]) if vals[47] else 0, 'limit_down': float(vals[48]) if vals[48] else 0, 'vol_ratio': float(vals[49]) if vals[49] else 0, 'pe_static': float(vals[52]) if vals[52] else 0}
    return result


def baidu_kline_with_ma(code: str, start_time: str='') -> dict:
    """百度股市通K线 — 独有能力: 返回时自带 ma5/ma10/ma20 均价"""
    url = 'https://finance.pae.baidu.com/selfselect/getstockquotation'
    params = {'all': '1', 'isIndex': 'false', 'isBk': 'false', 'isBlock': 'false', 'isFutures': 'false', 'newFormat': '1', 'group': 'quotation_kline_ab', 'finClientType': 'pc', 'code': code, 'start_time': start_time, 'ktype': '1'}
    headers = {'User-Agent': UA, 'Accept': 'application/vnd.finance-web.v1+json', 'Origin': 'https://gushitong.baidu.com', 'Referer': 'https://gushitong.baidu.com/'}
    r = requests.get(url, params=params, headers=headers, timeout=10)
    d = r.json()

    # Result may be a list when Baidu returns an application-level error
    result = d.get('Result')
    if not isinstance(result, dict):
        # Pass ResultCode through so caller can classify the error
        return {'keys': [], 'rows': [], 'ResultCode': d.get('ResultCode', -1)}

    md = result.get('newMarketData', {})
    keys = md.get('keys', [])
    rows = md.get('marketData', '').split(';')
    return {'keys': keys, 'rows': rows}


def full_valuation(code: str) -> dict:
    """单票完整估值分析"""
    from astock_api.upstream.ths import ths_eps_forecast
    prefix = 'bj' if code.startswith(('92', '8')) else 'sh' if code.startswith(('6', '9')) else 'sz'
    url = f'https://qt.gtimg.cn/q={prefix}{code}'
    req = urllib.request.Request(url)
    req.add_header('User-Agent', UA)
    resp = urllib.request.urlopen(req, timeout=10)
    data = resp.read().decode('gbk')
    vals = data.split('"')[1].split('~')
    price = float(vals[3])
    mcap = float(vals[45])
    pe_ttm = float(vals[39]) if vals[39] else 0
    pb = float(vals[46]) if vals[46] else 0
    df = ths_eps_forecast(code)
    eps_cur = eps_next = None
    analyst_count = 0
    if not df.empty and len(df.columns) >= 3:

        def _pick(row, name):
            for c in df.columns:
                if name in str(c):
                    return row.get(c)
            return None
        try:
            r0 = df.iloc[0]
            v = _pick(r0, '均值')
            eps_cur = float(v) if pd.notna(v) else None
            cnt = _pick(r0, '预测机构数')
            analyst_count = int(cnt) if pd.notna(cnt) else 0
            if len(df) >= 2:
                vn = _pick(df.iloc[1], '均值')
                eps_next = float(vn) if pd.notna(vn) else None
        except (ValueError, TypeError):
            pass
    pe_fwd = price / eps_cur if eps_cur else float('inf')
    cagr = eps_next / eps_cur - 1 if eps_cur and eps_next else 0
    peg = pe_fwd / (cagr * 100) if cagr > 0 else float('inf')
    digest = math.log(pe_fwd / 30) / math.log(1 + cagr) if pe_fwd > 30 and cagr > 0 else 0
    return {'name': vals[1], 'price': price, 'mcap_yi': mcap, 'pe_ttm': pe_ttm, 'pb': pb, 'eps_cur': eps_cur, 'eps_next': eps_next, 'pe_fwd': round(pe_fwd, 1) if eps_cur else None, 'cagr_pct': round(cagr * 100, 0) if cagr else None, 'peg': round(peg, 2) if peg != float('inf') else None, 'digest_years': round(digest, 1), 'analyst_count': analyst_count}
