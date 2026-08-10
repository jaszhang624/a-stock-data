"""Upstream cls module.

Extracted from a-stock-data SKILL.md - upstream version 3.5.1
Commit: 281fc69a0b733ffc6458fe2cf9f1ea56804aa886
"""

"""Upstream cls module.

Extracted from a-stock-data SKILL.md - upstream version 3.5.1
Commit: 281fc69a0b733ffc6458fe2cf9f1ea56804aa886
"""
import requests
import hashlib
from datetime import datetime

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'

def cls_telegraph(page_size: int=50) -> list[dict]:
    """
    财联社电报（全市场实时快讯）。v1 API + 本地签名，零 key。
    返回: [{title, content, time}]  time 已转为 'YYYY-MM-DD HH:MM:SS'
    """
    params = {'appName': 'CailianpressWeb', 'os': 'web', 'sv': '7.7.5', 'last_time': '', 'refresh_type': '1', 'rn': str(page_size)}
    qs = '&'.join((f'{k}={params[k]}' for k in sorted(params)))
    sign = hashlib.md5(hashlib.sha1(qs.encode()).hexdigest().encode()).hexdigest()
    url = f'https://www.cls.cn/v1/roll/get_roll_list?{qs}&sign={sign}'
    headers = {'User-Agent': UA, 'Referer': 'https://www.cls.cn/'}
    r = requests.get(url, headers=headers, timeout=10)
    d = r.json()
    rows = []
    for item in d.get('data', {}).get('roll_data', []) or []:
        ts = item.get('ctime')
        t = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S') if ts else ''
        rows.append({'title': item.get('title', '') or item.get('brief', ''), 'content': item.get('content', '') or item.get('brief', ''), 'time': t})
    return rows