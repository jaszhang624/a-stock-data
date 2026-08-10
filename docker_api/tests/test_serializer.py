"""Test serializer module."""
import numpy as np
import pandas as pd
from astock_api.serializer import normalize_result


def test_none():
    assert normalize_result(None) is None


def test_dataframe():
    df = pd.DataFrame({"a": [1, 2], "b": [3.0, np.nan]})
    result = normalize_result(df)
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["a"] == 1
    assert result[1]["b"] is None


def test_series():
    s = pd.Series([1, 2, 3])
    result = normalize_result(s)
    assert isinstance(result, list)
    assert result == [1, 2, 3]


def test_numpy_scalar():
    assert normalize_result(np.int64(5)) == 5
    assert normalize_result(np.float64(3.14)) == 3.14


def test_inf():
    assert normalize_result(np.inf) is None
    assert normalize_result(-np.inf) is None


def test_dict():
    d = {"a": 1, "b": np.nan}
    result = normalize_result(d)
    assert result["a"] == 1
    assert result["b"] is None


def test_list():
    assert normalize_result([1, np.int64(2)]) == [1, 2]
