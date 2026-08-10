"""Upstream cninfo module.

Extracted from a-stock-data SKILL.md - upstream version 3.5.1
Commit: 281fc69a0b733ffc6458fe2cf9f1ea56804aa886
"""

"""Upstream cninfo module.

Extracted from a-stock-data SKILL.md - upstream version 3.5.1
Commit: 281fc69a0b733ffc6458fe2cf9f1ea56804aa886
"""
import requests
from datetime import datetime

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'

def _cninfo_ts_to_date(ts):
    """巨潮 announcementTime 返回 Unix 毫秒整数，需转换为日期字符串。"""
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts / 1000).strftime('%Y-%m-%d')
    return str(ts)[:10] if ts else ''
_CNINFO_ORGID_MAP = {}

def _cninfo_orgid(code: str) -> str:
    """查股票真实 orgId。巨潮 orgId 并非统一 `gssx0{code}` 格式（如 601318→9900002221、
    601398→jjxt0000019、688017→9900041602），硬编码会导致大量股票（尤其 601xxx 段）
    返回 totalAnnouncement=0、查不到公告（#19）。优先动态查官方映射表，查不到再回退硬编码。"""
    global _CNINFO_ORGID_MAP
    if not _CNINFO_ORGID_MAP:
        try:
            r = requests.get('http://www.cninfo.com.cn/new/data/szse_stock.json', headers={'User-Agent': UA}, timeout=15)
            _CNINFO_ORGID_MAP = {s['code']: s['orgId'] for s in r.json().get('stockList', [])}
        except Exception as e:
            print(f'[WARN] 巨潮 orgId 映射表拉取失败，回退硬编码规则: {e}')
    org = _CNINFO_ORGID_MAP.get(code)
    if org:
        return org
    if code.startswith('6'):
        return f'gssh0{code}'
    elif code.startswith('8') or code.startswith('4'):
        return f'gsbj0{code}'
    return f'gssz0{code}'

def cninfo_announcements(code: str, page_size: int=30) -> list[dict]:
    """
    巨潮公告全文检索。
    返回: [{title, type, date, url}]
    """
    url = 'https://www.cninfo.com.cn/new/hisAnnouncement/query'
    org_id = _cninfo_orgid(code)
    payload = {'stock': f'{code},{org_id}', 'tabName': 'fulltext', 'pageSize': str(page_size), 'pageNum': '1', 'column': '', 'category': '', 'plate': '', 'seDate': '', 'searchkey': '', 'secid': '', 'sortName': '', 'sortType': '', 'isHLtitle': 'true'}
    headers = {'User-Agent': UA, 'Content-Type': 'application/x-www-form-urlencoded', 'Referer': 'https://www.cninfo.com.cn/new/disclosure', 'Origin': 'https://www.cninfo.com.cn'}
    r = requests.post(url, data=payload, headers=headers, timeout=15)
    d = r.json()
    rows = []
    for item in d.get('announcements', []) or []:
        rows.append({'title': item.get('announcementTitle', ''), 'type': item.get('announcementTypeName', ''), 'date': _cninfo_ts_to_date(item.get('announcementTime')), 'url': f"https://www.cninfo.com.cn/new/disclosure/detail?annoId={item.get('announcementId', '')}"})
    return rows
import requests
from datetime import datetime

def cninfo_irm(code: str, page_size: int=30, page_num: int=1) -> list[dict]:
    """互动易问答（深沪统一走巨潮）。code: 6位代码。
    返回每条: code/company/question(投资者提问)/answer(公司回复,None=未回复)/
    answerer(回答方)/ask_time。"""
    try:
        r1 = requests.post('https://irm.cninfo.com.cn/newircs/index/queryKeyboardInfo', data={'keyWord': code}, headers={'User-Agent': UA}, timeout=10)
        d1 = r1.json().get('data') or []
        if not d1:
            return []
        org_id = d1[0].get('secid')
        params = {'_t': 1, 'stockcode': code, 'orgId': org_id, 'pageSize': page_size, 'pageNum': page_num, 'keyWord': '', 'startDay': '', 'endDay': ''}
        r2 = requests.post('https://irm.cninfo.com.cn/newircs/company/question', params=params, headers={'User-Agent': UA}, timeout=10)
        rows = r2.json().get('rows') or []
    except Exception as e:
        print(f'[WARN] 互动易请求失败: {e}')
        return []
    out = []
    for it in rows:
        pd = it.get('pubDate')
        out.append({'code': it.get('stockCode'), 'company': it.get('companyShortName'), 'question': it.get('mainContent'), 'answer': it.get('attachedContent'), 'answerer': it.get('attachedAuthor'), 'ask_time': datetime.fromtimestamp(pd / 1000).strftime('%Y-%m-%d %H:%M') if pd else ''})
    return out