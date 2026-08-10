"""Upstream ths module.

Extracted from a-stock-data SKILL.md - upstream version 3.5.1
Commit: 281fc69a0b733ffc6458fe2cf9f1ea56804aa886
"""

"""Upstream ths module.

Extracted from a-stock-data SKILL.md - upstream version 3.5.1
Commit: 281fc69a0b733ffc6458fe2cf9f1ea56804aa886
"""
import requests
import pandas as pd
from io import StringIO

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'

def ths_eps_forecast(code: str) -> pd.DataFrame:
    """
    同花顺机构一致预期EPS。
    直连 basic.10jqka.com.cn，解析HTML表格。
    返回 DataFrame: 年度, 预测机构数, 最小值, 均值, 最大值
    "均值" = 机构一致预期EPS
    """
    url = f'https://basic.10jqka.com.cn/new/{code}/worth.html'
    headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36', 'Referer': 'https://basic.10jqka.com.cn/'}
    r = requests.get(url, headers=headers, timeout=15)
    r.encoding = 'gbk'
    dfs = pd.read_html(StringIO(r.text))
    for df in dfs:
        cols = [str(c) for c in df.columns]
        if any(('每股收益' in c or '均值' in c for c in cols)):
            return df
    return dfs[0] if dfs else pd.DataFrame()
import requests
import pandas as pd

def ths_hot_reason(date: str=None) -> pd.DataFrame:
    """
    同花顺当日强势股归因。
    date: 'YYYY-MM-DD' 格式，None=今天
    返回 DataFrame，含每只股票的题材标签 (reason)。

    实测: 73ms 拿到 ~125 只 + 完整字段
    """
    from datetime import date as _date
    if date is None:
        date = _date.today().strftime('%Y-%m-%d')
    url = f'http://zx.10jqka.com.cn/event/api/getharden/date/{date}/orderby/date/orderway/desc/charset/GBK/'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0 Safari/537.36'}
    r = requests.get(url, headers=headers, timeout=10)
    data = r.json()
    if data.get('errocode', 0) != 0:
        raise RuntimeError(f"同花顺热点错误: {data.get('errormsg', '')}")
    rows = data.get('data') or []
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    rename_map = {'name': '名称', 'code': '代码', 'reason': '题材归因', 'close': '收盘价', 'zhangdie': '涨跌额', 'zhangfu': '涨幅%', 'huanshou': '换手率%', 'chengjiaoe': '成交额', 'chengjiaoliang': '成交量', 'ddejingliang': '大单净量', 'market': '市场'}
    df = df.rename(columns=rename_map)
    return df
from datetime import datetime

def ths_limit_up_pool(date: str) -> list[dict]:
    """同花顺涨停揭秘（涨停原因 + 封板质量增强源）。date=YYYYMMDD。
    返回每只: code/name/price/pct/reason(涨停原因题材)/board_type(换手板/一字板/T字板)/
    seal_rate(封板成功率,0~1)/break_times(炸板次数)/seal_amount(封单额,元)/
    high_days(几天几板)/first_time(首次涨停时间)/is_again(是否回封 0/1)"""
    url = 'https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool'
    params = {'page': 1, 'limit': 200, 'field': '199112,10,9001,330323,330324,330325,9002,330329,133971,133970,1968584,3475914,9003,9004', 'filter': 'HS,GEM2STAR', 'order_field': '330324', 'order_type': '0', 'date': date}
    try:
        r = requests.get(url, params=params, headers={'User-Agent': UA}, timeout=10)
        info = (r.json().get('data') or {}).get('info', [])
    except Exception as e:
        print(f'[WARN] 同花顺涨停揭秘请求失败: {e}')
        return []
    out = []
    for it in info:
        ft = it.get('first_limit_up_time')
        out.append({'code': it.get('code'), 'name': it.get('name'), 'price': it.get('latest'), 'pct': it.get('change_rate'), 'reason': it.get('reason_type', ''), 'board_type': it.get('limit_up_type', ''), 'seal_rate': it.get('limit_up_suc_rate'), 'break_times': it.get('open_num') or 0, 'seal_amount': it.get('order_amount'), 'high_days': it.get('high_days', ''), 'first_time': datetime.fromtimestamp(int(ft)).strftime('%H:%M:%S') if ft else '', 'is_again': it.get('is_again_limit')})
    return out
EM_HOT_BODY = {'appId': 'appId01', 'globalId': '786e4c21-70dc-435a-93bb-38'}

def ths_hot_list(period: str='hour') -> list[dict]:
    """同花顺热榜（单接口拿名称+人气+概念标签+排名变化）。period: hour/day。
    返回每只: rank/code/name/heat(人气值)/pct/rank_chg(排名变化)/concepts(概念标签)/tag。"""
    try:
        r = requests.get('https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock', params={'stock_type': 'a', 'type': period, 'list_type': 'normal'}, headers={'User-Agent': UA}, timeout=10)
        lst = (r.json().get('data') or {}).get('stock_list') or []
    except Exception as e:
        print(f'[WARN] 同花顺热榜失败: {e}')
        return []
    out = []
    for it in lst:
        tag = it.get('tag') or {}
        out.append({'rank': it.get('order'), 'code': it.get('code'), 'name': it.get('name'), 'heat': it.get('rate'), 'pct': it.get('rise_and_fall'), 'rank_chg': it.get('hot_rank_chg'), 'concepts': tag.get('concept_tag') or [], 'tag': tag.get('popularity_tag', '')})
    return out

def em_hot_rank(top: int=50) -> list[dict]:
    """东财人气榜（排名 + 排名变化 + 名称/价格）。返回 rank/code/name/price/pct/rank_chg。"""
    try:
        r = requests.post('https://emappdata.eastmoney.com/stockrank/getAllCurrentList', json={**EM_HOT_BODY, 'marketType': '', 'pageNo': 1, 'pageSize': top}, headers={'User-Agent': UA}, timeout=10)
        data = r.json().get('data') or []
        if not data:
            return []
        secids = [('0.' if it['sc'].startswith('SZ') else '1.') + it['sc'][2:] for it in data]
        u = requests.get('https://push2.eastmoney.com/api/qt/ulist.np/get', params={'ut': 'f057cbcbce2a86e2866ab8877db1d059', 'fltt': 2, 'invt': 2, 'fields': 'f14,f3,f12,f2', 'secids': ','.join(secids)}, headers={'User-Agent': UA, 'Referer': 'https://quote.eastmoney.com/'}, timeout=10)
        diff = (u.json().get('data') or {}).get('diff') or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        nm = {x['f12']: (x.get('f14'), x.get('f2'), x.get('f3')) for x in diff}
    except Exception as e:
        print(f'[WARN] 东财人气榜失败: {e}')
        return []
    out = []
    for it in data:
        code = it['sc'][2:]
        name, price, pct = nm.get(code, ('', None, None))
        out.append({'rank': it['rk'], 'code': code, 'name': name, 'price': price, 'pct': pct, 'rank_chg': it.get('hisRc')})
    return out

def em_hot_concept(code: str) -> list[dict]:
    """东财个股热门概念命中（这只票当下被市场归到哪些概念在炒）。
    返回 [{concept, bk, hit(命中热度)}, ...]，按热度降序。"""
    try:
        prefix = 'SH' if code.startswith('6') else 'SZ'
        r = requests.post('https://emappdata.eastmoney.com/stockrank/getHotStockRankList', json={**EM_HOT_BODY, 'srcSecurityCode': prefix + code}, headers={'User-Agent': UA}, timeout=10)
        data = r.json().get('data') or []
    except Exception as e:
        print(f'[WARN] 东财个股概念失败: {e}')
        return []
    return [{'concept': x.get('conceptName'), 'bk': x.get('conceptId'), 'hit': x.get('hitCount')} for x in data]