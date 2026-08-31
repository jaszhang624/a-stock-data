"""Instrument Universe Builder — Reproducible, documented universe generation.

R6-1: First durable instrument universe system.

Separates concerns:
A. Source acquisition (TDX security master via mootdx)
B. Classification (market-aware equity rules + validated index seeds)
C. Canonicalization (Instrument model, deterministic sorting)
D. Validation (invariants: no duplicates, valid exchanges, identity consistency)
E. Artifact persistence (canonical JSON + manifest/provenance)

Design principles:
- Raw source snapshot preserved separately from canonical artifact
- Classification policy is explicit and versioned, not inferred
- Canonical artifact is deterministic (no generated_at inside)
- Manifest carries provenance metadata separately
- INDEX seeds are explicitly marked as VALIDATED_SEED_NOT_FULL_UNIVERSE
"""

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ── Classification Policy V1 ───────────────────────────────────────

CLASSIFICATION_POLICY_VERSION = "v1"

# Equity: market-aware prefix rules (frozen, matches R5 baseline)
EQUITY_PREFIXES = {
    "SSE": {"60", "68"},   # Main board + STAR market
    "SZSE": {"00", "30"},  # Main board + ChiNext
    "BSE": {"4", "8"},     # BSE (prefixes as single digit)
}

# Index: validated production seeds from R5-C4C-4
INDEX_SEEDS = [
    {"exchange": "SSE", "code": "000001"},  # 上证指数
    {"exchange": "SSE", "code": "000300"},  # 沪深300
    {"exchange": "SSE", "code": "000688"},  # 科创50
    {"exchange": "SSE", "code": "589000"},  # 科创综指
    {"exchange": "SZSE", "code": "399001"}, # 深证成指
    {"exchange": "SZSE", "code": "399006"}, # 创业板指
    {"exchange": "SZSE", "code": "399300"}, # 中证500
]


# ── Data Models ────────────────────────────────────────────────────

@dataclass
class RawRecord:
    """Normalized raw record from a source provider."""
    provider: str
    provider_market: str
    code: str
    name: str


@dataclass
class CanonicalRecord:
    """Canonical instrument record."""
    exchange: str
    code: str
    canonical_id: str
    asset_type: str
    name: str = ""
    source: str = ""
    classification_source: str = ""

    def to_dict(self) -> dict:
        d = {
            "exchange": self.exchange,
            "code": self.code,
            "canonical_id": self.canonical_id,
            "asset_type": self.asset_type,
        }
        if self.name:
            d["name"] = self.name
        if self.source:
            d["source"] = self.source
        if self.classification_source:
            d["classification_source"] = self.classification_source
        return d


# ── A. Source Acquisition ─────────────────────────────────────────

def fetch_tdx_security_master() -> list[RawRecord]:
    """Fetch raw security master from TDX via mootdx.

    Returns combined SSE + SZSE records.
    Each record retains provider metadata for provenance.
    """
    from mootdx.quotes import StdQuotes

    client = StdQuotes()
    records = []

    # SSE (market=1)
    try:
        sh_raw = client.stocks(market=1)
        for _, row in sh_raw.iterrows():
            code = str(row["code"])
            name = str(row.get("name", "")) if row.get("name") is not None else ""
            # Strip null bytes from name (TDX quirk)
            name = name.replace("\x00", "").strip()
            records.append(RawRecord(
                provider="mootdx",
                provider_market="SSE",
                code=code,
                name=name,
            ))
    except Exception as e:
        logger.error(f"TDX SSE fetch failed: {e}")

    # SZSE (market=0)
    try:
        sz_raw = client.stocks(market=0)
        for _, row in sz_raw.iterrows():
            code = str(row["code"])
            name = str(row.get("name", "")) if row.get("name") is not None else ""
            name = name.replace("\x00", "").strip()
            records.append(RawRecord(
                provider="mootdx",
                provider_market="SZSE",
                code=code,
                name=name,
            ))
    except Exception as e:
        logger.error(f"TDX SZSE fetch failed: {e}")

    return records


# ── B. Classification ─────────────────────────────────────────────

def classify_equity(raw_records: list[RawRecord]) -> list[CanonicalRecord]:
    """Classify equity instruments using market-aware prefix rules.

    Rules (frozen, matches R5 baseline):
    - SSE: 60xxxx (main), 68xxxx (STAR)
    - SZSE: 00xxxx (main), 30xxxx (ChiNext)
    - BSE: 4xxxxx, 8xxxxx (if source available)

    Classification depends on exchange + code, not code alone.
    """
    classified = []

    for rec in raw_records:
        exchange = rec.provider_market
        code = rec.code

        # Only SSE and SZSE have reliable TDX sources; BSE is unsupported
        if exchange not in ("SSE", "SZSE"):
            continue

        prefixes = EQUITY_PREFIXES.get(exchange, set())
        # Check if code matches any equity prefix for this exchange
        is_equity = False
        for prefix in prefixes:
            if code.startswith(prefix):
                is_equity = True
                break

        if not is_equity:
            continue  # Non-equity (indices, ETFs, bonds, etc.)

        canonical_id = f"{exchange}:{code}"
        classified.append(CanonicalRecord(
            exchange=exchange,
            code=code,
            canonical_id=canonical_id,
            asset_type="EQUITY",
            name=rec.name,
            source=f"{rec.provider}/{exchange}",
            classification_source="market_aware_prefix",
        ))

    return classified


def get_index_seeds() -> list[CanonicalRecord]:
    """Return validated index seeds from R5-C4C-4 production validation.

    These are NOT a claim of completeness — they represent indices
    that have been explicitly validated through the production pipeline.

    Classification source: validated_seed
    """
    seeds = []
    for seed in INDEX_SEEDS:
        exchange = seed["exchange"]
        code = seed["code"]
        canonical_id = f"{exchange}:{code}"
        seeds.append(CanonicalRecord(
            exchange=exchange,
            code=code,
            canonical_id=canonical_id,
            asset_type="INDEX",
            source="validated_seed",
            classification_source="validated_seed",
        ))
    return seeds


# ── C. Canonicalization ───────────────────────────────────────────

def _sort_key(record: CanonicalRecord) -> tuple:
    """Deterministic sort key: asset_type, exchange, code."""
    return (record.asset_type, record.exchange, record.code)


def canonicalize(records: list[CanonicalRecord]) -> list[dict]:
    """Sort and deduplicate canonical records.

    Returns sorted list of dicts for JSON serialization.
    """
    # Deduplicate by canonical_id (first wins)
    seen = set()
    unique = []
    for rec in records:
        if rec.canonical_id not in seen:
            seen.add(rec.canonical_id)
            unique.append(rec)

    # Sort deterministically
    unique.sort(key=_sort_key)

    return [rec.to_dict() for rec in unique]


# ── D. Validation ─────────────────────────────────────────────────

VALID_EXCHANGES = {"SSE", "SZSE", "BSE"}
VALID_ASSET_TYPES = {"EQUITY", "INDEX"}


def validate_canonical(records: list[dict]) -> list[str]:
    """Validate canonical records against invariants.

    Returns list of error messages (empty = valid).
    """
    errors = []
    seen_ids = set()

    for i, rec in enumerate(records):
        cid = rec.get("canonical_id", "")

        # Check for duplicates
        if cid in seen_ids:
            errors.append(f"Duplicate canonical_id at index {i}: {cid}")
        seen_ids.add(cid)

        # Validate exchange
        exchange = rec.get("exchange", "")
        if exchange not in VALID_EXCHANGES:
            errors.append(f"Invalid exchange at index {i}: {exchange}")

        # Validate asset_type
        asset_type = rec.get("asset_type", "")
        if asset_type not in VALID_ASSET_TYPES:
            errors.append(f"Invalid asset_type at index {i}: {asset_type}")

        # Validate code format
        code = rec.get("code", "")
        if not code.isdigit() or len(code) != 6:
            errors.append(f"Invalid code at index {i}: {code}")

        # Validate canonical_id consistency
        expected_cid = f"{exchange}:{code}"
        if cid != expected_cid:
            errors.append(
                f"canonical_id mismatch at index {i}: "
                f"got '{cid}', expected '{expected_cid}'"
            )

    return errors


# ── E. Artifact Persistence ───────────────────────────────────────

def compute_sha256(data: bytes) -> str:
    """Compute SHA256 hex digest."""
    return hashlib.sha256(data).hexdigest()


def persist_canonical_artifact(
    records: list[dict], output_path: str
) -> tuple[str, str]:
    """Persist canonical artifact as deterministic JSON.

    Returns (file_path, sha256).
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # Deterministic JSON: sorted keys, no trailing newline issues
    content = json.dumps(records, indent=2, ensure_ascii=False) + "\n"
    sha = compute_sha256(content.encode("utf-8"))

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)

    return output_path, sha


def persist_manifest(
    manifest: dict, output_path: str
) -> str:
    """Persist manifest/provenance file.

    Returns file path.
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    content = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)

    return output_path


def persist_raw_snapshot(
    records: list[RawRecord], output_path: str
) -> str:
    """Persist normalized raw source snapshot.

    Returns file path.
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    data = [
        {
            "provider": r.provider,
            "provider_market": r.provider_market,
            "code": r.code,
            "name": r.name,
        }
        for r in records
    ]

    content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)

    return output_path


# ── Builder Orchestrator ───────────────────────────────────────────

class InstrumentUniverseBuilder:
    """Orchestrates universe generation from source to artifact.

    Usage:
        builder = InstrumentUniverseBuilder(data_dir="/app/data/universe")
        result = builder.build(source="tdx_live")

    Or from saved snapshot:
        result = builder.build(source="snapshot", raw_path="/path/to/raw.json")
    """

    def __init__(self, data_dir: str = "/app/data/universe"):
        self.data_dir = data_dir

    def build(
        self,
        source: str = "tdx_live",
        raw_path: str | None = None,
    ) -> dict:
        """Build universe artifact.

        Args:
            source: "tdx_live" or "snapshot"
            raw_path: Path to saved raw snapshot (required if source="snapshot")

        Returns dict with build results and artifact paths.
        """
        # Step A: Acquire raw records
        if source == "tdx_live":
            logger.info("Fetching TDX security master...")
            raw_records = fetch_tdx_security_master()
        elif source == "snapshot" and raw_path:
            logger.info(f"Loading raw snapshot from {raw_path}...")
            with open(raw_path, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
            raw_records = [
                RawRecord(
                    provider=r["provider"],
                    provider_market=r["provider_market"],
                    code=r["code"],
                    name=r.get("name", ""),
                )
                for r in raw_data
            ]
        else:
            raise ValueError("source must be 'tdx_live' or 'snapshot' with raw_path")

        # Persist raw snapshot
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        raw_output = os.path.join(self.data_dir, "raw", f"tdx_security_master_{date_str}.json")
        persist_raw_snapshot(raw_records, raw_output)

        # Step B: Classify
        logger.info("Classifying equity instruments...")
        equity_records = classify_equity(raw_records)

        logger.info("Adding validated index seeds...")
        index_records = get_index_seeds()

        # Step C: Canonicalize
        all_records = equity_records + index_records
        canonical = canonicalize(all_records)

        # Step D: Validate
        validation_errors = validate_canonical(canonical)
        if validation_errors:
            logger.error(f"Validation failed with {len(validation_errors)} errors:")
            for err in validation_errors[:10]:
                logger.error(f"  - {err}")
            raise ValueError(f"Canonical validation failed: {'; '.join(validation_errors[:5])}")

        # Step E: Persist canonical artifact
        canonical_path = os.path.join(self.data_dir, "instrument_universe_v2.json")
        _, canonical_sha = persist_canonical_artifact(canonical, canonical_path)

        # Build manifest
        equity_by_exchange = {}
        index_count = 0
        for rec in canonical:
            if rec["asset_type"] == "EQUITY":
                ex = rec["exchange"]
                equity_by_exchange[ex] = equity_by_exchange.get(ex, 0) + 1
            elif rec["asset_type"] == "INDEX":
                index_count += 1

        manifest = {
            "schema_version": "v2",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "generator_version": CLASSIFICATION_POLICY_VERSION,
            "source_provider": source,
            "classification_policy_version": CLASSIFICATION_POLICY_VERSION,
            "raw_counts": {
                "total_raw": len(raw_records),
                "by_market": {},
            },
            "canonical_counts": {
                "total": len(canonical),
                "by_asset_type": {"EQUITY": sum(equity_by_exchange.values()), "INDEX": index_count},
                "by_exchange_asset_type": {
                    f"{ex}_EQUITY": cnt for ex, cnt in equity_by_exchange.items()
                } | {"INDEX": index_count},
            },
            "artifact_sha256": canonical_sha,
            "known_limitations": [
                "Full authoritative INDEX universe is not yet implemented. "
                "Current INDEX records are validated supported seeds."
            ],
        }

        # Raw counts by market
        raw_by_market = {}
        for r in raw_records:
            m = r.provider_market
            raw_by_market[m] = raw_by_market.get(m, 0) + 1
        manifest["raw_counts"]["by_market"] = raw_by_market

        # Persist manifest
        manifest_path = os.path.join(self.data_dir, "instrument_universe_v2.manifest.json")
        persist_manifest(manifest, manifest_path)

        # Return build result summary
        return {
            "raw_total": len(raw_records),
            "equity_by_exchange": equity_by_exchange,
            "equity_total": sum(equity_by_exchange.values()),
            "index_seed_count": index_count,
            "canonical_total": len(canonical),
            "canonical_sha256": canonical_sha,
            "canonical_path": canonical_path,
            "manifest_path": manifest_path,
            "raw_snapshot_path": raw_output,
        }
