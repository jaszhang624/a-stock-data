"""JSON serialization layer for upstream data types.

Handles: DataFrame, Series, numpy scalars, Timestamp, datetime, NaN, inf.
"""
import math
from datetime import datetime, date
from typing import Any

import numpy as np
import pandas as pd


def normalize_result(data: Any) -> Any:
    """Convert upstream data types to JSON-serializable format.

    Rules:
    - DataFrame → list of dicts (records)
    - Series → dict or list of scalars
    - NaN/inf → null
    - numpy int/float → native Python
    - Timestamp/datetime → ISO8601 string
    """
    if data is None:
        return None

    if isinstance(data, pd.DataFrame):
        # Replace inf/-inf with NaN, then to records and normalize each value
        df = data.replace([np.inf, -np.inf], np.nan)
        raw_records = df.to_dict(orient='records')
        return [normalize_result(r) for r in raw_records]

    if isinstance(data, pd.Series):
        if data.index.equals(pd.RangeIndex(len(data))):
            # Series with integer index → list
            return [normalize_result(v) for v in data]
        # Series with named index → dict
        return {str(k): normalize_result(v) for k, v in data.items()}

    if isinstance(data, (pd.Timestamp, datetime)):
        return data.isoformat()

    if isinstance(data, date):
        return data.isoformat()

    if isinstance(data, (np.integer,)):
        return int(data)

    if isinstance(data, (np.floating, float)):
        if math.isnan(data) or math.isinf(data):
            return None
        return float(data)

    if isinstance(data, np.bool_):
        return bool(data)

    # Native Python types pass through (before dict/list to avoid recursion)
    if isinstance(data, int):
        return data
    if isinstance(data, float):
        # Catch any remaining floats (shouldn't reach here due to np.floating check above)
        if math.isnan(data) or math.isinf(data):
            return None
        return data
    if isinstance(data, (str, bool)):
        return data

    if isinstance(data, dict):
        return {k: normalize_result(v) for k, v in data.items()}

    if isinstance(data, (list, tuple)):
        return [normalize_result(v) for v in data]

    if isinstance(data, np.ndarray):
        return normalize_result(pd.Series(data.tolist()))

    # Strings, booleans pass through
    if isinstance(data, (str, bool)):
        return data

    # Fallback: try str() but log warning
    import logging
    logging.warning(f"Unknown type in normalize_result: {type(data)}")
    return str(data)
