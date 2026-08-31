"""Instrument Identity Primitives.

Frozen identity contract for A-share securities.

Exchange enum: SSE | SZSE | BSE
Asset type enum: EQUITY | INDEX (ETF/BOND/REPO reserved for future)

Canonical ID format: "{EXCHANGE}:{CODE}"
Examples: SSE:600519, SZSE:000001, SSE:000001

Design principles:
- canonical_id is DERIVED from exchange + code; never independently mutable
- bare code without context raises AmbiguousExchangeError (no silent assumptions)
- EQUITY context allows prefix-based exchange inference
- INDEX requires explicit exchange (prefix does not uniquely determine exchange)
- Explicit exchange + asset_type conflicts are rejected

Compatible with DatasetStore.make_security_id() which uses SSE:/SZSE: naming.
"""

import re
from dataclasses import dataclass, field


# ── Enums ───────────────────────────────────────────────────────────

EXCHANGE_ENUM = {"SSE", "SZSE", "BSE"}
ASSET_TYPE_ENUM = {"EQUITY", "INDEX"}

# Equity prefix → exchange mapping (frozen)
EQUITY_PREFIX_TO_EXCHANGE = {
    "6": "SSE",   # 60xxxx (main), 68xxxx (STAR)
    "9": "SSE",   # 90xxxx (B-shares, SSE)
    "0": "SZSE",  # 00xxxx (main)
    "3": "SZSE",  # 30xxxx (ChiNext)
    "4": "BSE",   # 4xxxxx (BSE)
    "8": "BSE",   # 8xxxxx (BSE)
}

# Equity prefix → exchange mapping for INDEX (empty — no inference allowed)
INDEX_PREFIX_TO_EXCHANGE = {}

# Code pattern: exactly 6 digits
CODE_PATTERN = re.compile(r"^\d{6}$")

# TDX market mapping (for mootdx adapter routing)
TDX_MARKET_MAP = {
    "SSE": 1,   # Shanghai
    "SZSE": 0,  # Shenzhen
}


# ── Exceptions ──────────────────────────────────────────────────────

class AmbiguousExchangeError(ValueError):
    """Raised when a bare code could map to multiple exchanges."""


class InstrumentValidationError(ValueError):
    """Raised when instrument fields fail validation or conflict checks."""


# ── Instrument ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class Instrument:
    """Canonical instrument identity.

    Fields:
        exchange: SSE | SZSE | BSE
        code: 6-digit numeric string (e.g., "600519")
        asset_type: EQUITY | INDEX

    canonical_id is derived as "{EXCHANGE}:{CODE}".
    """

    exchange: str
    code: str
    asset_type: str = "EQUITY"

    @property
    def canonical_id(self) -> str:
        """Derived canonical identity. Never independently mutable."""
        return f"{self.exchange}:{self.code}"

    def __str__(self) -> str:
        return self.canonical_id


# ── Validation helpers ─────────────────────────────────────────────

def _validate_code(code: str) -> str:
    """Validate and normalize code to 6-digit string."""
    if code is None or not isinstance(code, str):
        raise InstrumentValidationError(f"code must be a non-empty string, got {type(code).__name__ if code is not None else 'None'}")
    if not CODE_PATTERN.match(code):
        raise InstrumentValidationError(
            f"code must be exactly 6 digits, got '{code}' "
            f"(length={len(code)}, is_numeric={'yes' if code.isdigit() else 'no'})"
        )
    return code


def _validate_exchange(exchange: str) -> str:
    """Validate and normalize exchange to uppercase canonical form."""
    if not isinstance(exchange, str):
        raise InstrumentValidationError(f"exchange must be a string, got {type(exchange).__name__}")
    normalized = exchange.upper()
    if normalized not in EXCHANGE_ENUM:
        raise InstrumentValidationError(
            f"exchange must be one of {sorted(EXCHANGE_ENUM)}, got '{exchange}'"
        )
    return normalized


def _validate_asset_type(asset_type: str) -> str:
    """Validate and normalize asset type to uppercase canonical form."""
    if not isinstance(asset_type, str):
        raise InstrumentValidationError(f"asset_type must be a string, got {type(asset_type).__name__}")
    normalized = asset_type.upper()
    if normalized not in ASSET_TYPE_ENUM:
        raise InstrumentValidationError(
            f"asset_type must be one of {sorted(ASSET_TYPE_ENUM)}, got '{asset_type}'"
        )
    return normalized


def _infer_exchange_for_equity(code: str) -> str:
    """Infer exchange from code prefix for EQUITY asset type.

    Uses frozen equity mapping:
        6/9 → SSE, 0/3 → SZSE, 4/8 → BSE
    """
    prefix = code[0]
    exchange = EQUITY_PREFIX_TO_EXCHANGE.get(prefix)
    if exchange is None:
        raise AmbiguousExchangeError(
            f"cannot infer exchange for code '{code}' in EQUITY context "
            f"(prefix '{prefix}' not mapped)"
        )
    return exchange


def _check_explicit_conflict(code: str, exchange: str, asset_type: str) -> None:
    """Check that explicit exchange + code + asset_type don't conflict.

    For EQUITY: verify the prefix maps to the given exchange.
    For INDEX: no conflict check (any exchange is valid for indices).
    """
    if asset_type == "EQUITY":
        expected = EQUITY_PREFIX_TO_EXCHANGE.get(code[0])
        if expected and expected != exchange:
            raise InstrumentValidationError(
                f"code '{code}' with asset_type=EQUITY belongs to {expected}, "
                f"not {exchange}"
            )


# ── Adapter routing helpers ────────────────────────────────────────

def instrument_to_mootdx_market(instrument: Instrument) -> int | None:
    """Convert Instrument exchange to mootdx TDX market code.

    Returns:
        1 for SSE, 0 for SZSE, None if unsupported (e.g., BSE or INDEX).

    Raises:
        InstrumentValidationError: if exchange is not recognized.
    """
    market = TDX_MARKET_MAP.get(instrument.exchange)
    if market is None:
        return None  # BSE or other unsupported exchanges
    return market


def instrument_to_baidu_symbol(instrument: Instrument) -> str | None:
    """Convert Instrument to Baidu provider-specific symbol format.

    Returns:
        Provider-specific symbol string (e.g., 'sh600519', 'sz000001')
        or None if unsupported.

    Baidu uses lowercase prefix: sh/SZ for SSE/SZSE.
    """
    prefix_map = {
        "SSE": "sh",
        "SZSE": "sz",
    }
    prefix = prefix_map.get(instrument.exchange)
    if prefix is None:
        return None  # BSE or other unsupported exchanges
    return f"{prefix}{instrument.code}"


# ── parse_instrument ───────────────────────────────────────────────

def parse_instrument(
    code: str,
    exchange: str | None = None,
    asset_type: str | None = None,
) -> Instrument:
    """Parse a code into an Instrument with canonical identity.

    Args:
        code: 6-digit numeric string (e.g., "600519")
        exchange: Optional explicit exchange ("SSE", "SZSE", "BSE")
        asset_type: Optional asset type ("EQUITY", "INDEX")

    Returns:
        Instrument with exchange, code, asset_type, and derived canonical_id.

    Raises:
        AmbiguousExchangeError: code could map to multiple exchanges and no context provided.
        InstrumentValidationError: invalid input or explicit conflict.

    Examples:
        >>> parse_instrument("600519", asset_type="EQUITY")
        Instrument(exchange='SSE', code='600519', asset_type='EQUITY')

        >>> parse_instrument("000001", asset_type="EQUITY")
        Instrument(exchange='SZSE', code='000001', asset_type='EQUITY')

        >>> parse_instrument("000001")  # ambiguous
        AmbiguousExchangeError

        >>> parse_instrument("000001", exchange="SSE", asset_type="INDEX")
        Instrument(exchange='SSE', code='000001', asset_type='INDEX')
    """
    # Validate code first (always required)
    validated_code = _validate_code(code)

    # Normalize optional params
    if exchange is not None:
        validated_exchange = _validate_exchange(exchange)
    else:
        validated_exchange = None

    if asset_type is not None:
        validated_asset_type = _validate_asset_type(asset_type)
    else:
        validated_asset_type = None

    # Resolve exchange and asset_type
    if validated_exchange is not None:
        # Explicit exchange provided — check conflicts, default asset_type to EQUITY
        final_asset_type = validated_asset_type if validated_asset_type else "EQUITY"
        _check_explicit_conflict(validated_code, validated_exchange, final_asset_type)
        return Instrument(
            exchange=validated_exchange,
            code=validated_code,
            asset_type=final_asset_type,
        )
    elif validated_asset_type == "EQUITY":
        # Equity context — infer exchange from prefix
        final_exchange = _infer_exchange_for_equity(validated_code)
        return Instrument(
            exchange=final_exchange,
            code=validated_code,
            asset_type="EQUITY",
        )
    elif validated_asset_type == "INDEX":
        # Index context — exchange is required (no inference)
        raise AmbiguousExchangeError(
            f"exchange must be specified for INDEX code '{validated_code}' "
            f"(prefix does not uniquely determine exchange)"
        )
    else:
        # Generic context — no exchange, no asset_type → ambiguous
        raise AmbiguousExchangeError(
            f"code '{validated_code}' is ambiguous: "
            f"exchange and asset_type must be specified to resolve identity"
        )
