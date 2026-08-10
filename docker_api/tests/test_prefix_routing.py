"""Test prefix routing."""
from astock_api.upstream.common import get_prefix, norm_ticker


def test_sh_stocks():
    assert get_prefix("600519") == "sh"
    assert get_prefix("688001") == "sh"


def test_sz_stocks():
    assert get_prefix("000001") == "sz"
    assert get_prefix("300001") == "sz"


def test_etf():
    assert get_prefix("510300") == "sh"  # 沪深300ETF
    assert get_prefix("159915") == "sz"  # 创业板ETF


def test_bj():
    assert get_prefix("430090") == "bj"
    assert get_prefix("830799") == "bj"


def test_prefixed():
    assert get_prefix("sh600519") == "sh"
    assert get_prefix("sz000001") == "sz"


def test_norm_ticker():
    assert norm_ticker("600519") == "sh600519"
    assert norm_ticker("sz000001") == "sz000001"
    assert norm_ticker("510300") == "sh510300"


def test_no_confusion():
    # sh000001 and sz000001 should NOT map to the same thing
    assert norm_ticker("sh000001") == "sh000001"
    assert norm_ticker("sz000001") == "sz000001"
    assert norm_ticker("sh000001") != norm_ticker("sz000001")
