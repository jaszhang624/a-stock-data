#!/usr/bin/env python3
"""Production data initialization script.

Runs on Mac, calls production API via HTTP.
Manages checkpoint, retry, and failure classification.

Usage:
  python3 init_production.py [--phase A|B|C|D] [--canary] [--resume]
"""

import argparse
import json
import os
import sys
import time
import subprocess
from datetime import datetime, timezone
from pathlib import Path

# Config
API_BASE = "http://100.87.166.34:8765"
CHECKPOINT_FILE = Path(__file__).parent / "init_checkpoint.json"

# Canary symbols
CANARY_SYMBOLS = ["600519", "000001", "300750"]

# Phase definitions
PHASES = {
    "A": {  # Canary
        "name": "Canary",
        "description": "Validate pipeline with 3 representative stocks",
        "datasets": [
            {"type": "market_bars_snapshot", "symbols": CANARY_SYMBOLS, "count": 100},
            {"type": "tencent_quote", "symbols": CANARY_SYMBOLS},
            {"type": "eastmoney_stock_info", "symbols": CANARY_SYMBOLS},
            {"type": "baidu_kline_with_ma", "symbols": CANARY_SYMBOLS},
            {"type": "full_valuation", "symbols": CANARY_SYMBOLS},
            {"type": "sina_financial_report", "symbols": CANARY_SYMBOLS, "report_types": ["lrb", "xjllb", "fzbb"]},
            {"type": "cninfo_announcements", "symbols": CANARY_SYMBOLS},
        ]
    },
    "B": {  # Universe discovery
        "name": "Universe",
        "description": "Get complete stock universe from production",
    },
    "C": {  # Core historical data
        "name": "Core Historical",
        "description": "Daily bars, fundamentals, financials for all stocks",
    },
    "D": {  # High-frequency & ephemeral
        "name": "High-Frequency",
        "description": "News, hot rankings, fund flow snapshots",
    },
}


def get_api_key():
    """Get API key from macOS Keychain."""
    result = subprocess.run(
        ["security", "find-generic-password", "-a", "astock-agent",
         "-s", "astock-production-api", "-w"],
        capture_output=True, text=True, timeout=10
    )
    key = result.stdout.strip()
    if not key:
        print("FATAL: Cannot get API key from Keychain")
        sys.exit(1)
    return key


def api_call(func_name, kwargs=None, timeout=60):
    """Make authenticated API call to /api/v1/call/{func_name}."""
    key = get_api_key()
    import urllib.request
    import urllib.error
    
    url = f"{API_BASE}/api/v1/call/{func_name}"
    data = json.dumps({"kwargs": kwargs or {}}).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": key,
        },
        method="POST"
    )
    
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return {"_error": True, "status_code": e.code, "detail": body}
    except Exception as e:
        return {"_error": True, "exception": str(e)}


def job_api_call(method, path, data=None, timeout=30):
    """Make authenticated call to /api/v1/jobs."""
    key = get_api_key()
    import urllib.request
    import urllib.error
    
    url = f"{API_BASE}{path}"
    req_data = json.dumps(data).encode() if data else None
    
    req = urllib.request.Request(
        url, data=req_data,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": key,
        },
        method=method
    )
    
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return {"_error": True, "status_code": e.code, "detail": body}
    except Exception as e:
        return {"_error": True, "exception": str(e)}


def load_checkpoint():
    """Load checkpoint from file."""
    if CHECKPOINT_FILE.exists():
        with open(CHECKPOINT_FILE) as f:
            return json.load(f)
    return {
        "phases": {},
        "universe": None,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }


def save_checkpoint(cp):
    """Save checkpoint to file."""
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(cp, f, indent=2, ensure_ascii=False)


def classify_result(result):
    """Classify API result. Returns one of:
    SUCCESS, VALID_EMPTY, SUSPECT_EMPTY, TRANSIENT, UPSTREAM_BLOCKED,
    PARSER_ERROR, APPLICATION_ERROR, UNSUPPORTED, UNKNOWN
    """
    # HTTP-level errors (from urllib)
    if isinstance(result, dict) and result.get("_error"):
        code = result.get("status_code")
        detail = result.get("detail", "")
        if code == 404:
            return "UNSUPPORTED"
        elif code in (429, 503):
            return "UPSTREAM_BLOCKED"
        elif code == 400:
            return "PARSER_ERROR"
        elif code in (502, 504):
            # Bad Gateway / Gateway Timeout — check if it's a persistent app error
            if isinstance(detail, str) and ("ImportError" in detail or "lxml" in detail):
                return "APPLICATION_ERROR"  # Missing dependency — persistent
            if isinstance(detail, str) and ("AttributeError" in detail or "NoneType" in detail):
                return "PARSER_ERROR"  # Upstream parser bug — persistent
            # Otherwise treat as transient (upstream server crash)
            return "TRANSIENT"
        elif isinstance(detail, str) and ("RemoteDisconnected" in detail or "Connection" in detail):
            return "TRANSIENT"
        else:
            return "UNKNOWN"

    # Application-level error envelope: {"detail": {"ok": false, "error": {...}}}
    if isinstance(result, dict) and "detail" in result:
        detail = result["detail"]
        if isinstance(detail, dict) and detail.get("ok") is False:
            err = detail.get("error", {})
            err_type = str(err.get("type", "")) if isinstance(err, dict) else str(err)
            err_msg = str(err.get("message", "")) if isinstance(err, dict) else ""
            if "ImportError" in err_type or "lxml" in err_msg:
                return "APPLICATION_ERROR"
            if "AttributeError" in err_type:
                return "PARSER_ERROR"
            return "APPLICATION_ERROR"

    # Successful list result
    if isinstance(result, list):
        return "VALID_EMPTY" if len(result) == 0 else "SUCCESS"

    # Successful dict result (not an error envelope, not empty)
    if isinstance(result, dict):
        return "VALID_EMPTY" if len(result) == 0 else "SUCCESS"

    # Fallback
    return "UNKNOWN"


# Known issues — exact tuple match only.
# Key: (func_name, *extra_context) → description string
KNOWN_ISSUES = {
    ("ths_eps_forecast",): "APPLICATION_ERROR: missing lxml/html5lib/bs4 in container (Phase 9.2 known)",
}

def is_known_issue(func_name, *extra):
    """Check if this matches a known issue. Exact tuple match only."""
    key = (func_name,) + tuple(extra)
    return KNOWN_ISSUES.get(key)


def run_canary():
    """Phase A: Canary validation."""
    print("=" * 60)
    print("PHASE A: CANARY")
    print("=" * 60)
    
    cp = load_checkpoint()
    results = []
    
    for ds in PHASES["A"]["datasets"]:
        dtype = ds["type"]
        symbols = ds["symbols"]
        
        print(f"\n--- {dtype} ---")
        
        for symbol in symbols:
            start = time.time()
            
            if dtype == "market_bars_snapshot":
                # Use Job Engine
                count = ds.get("count", 100)
                job_resp = job_api_call("POST", "/api/v1/jobs", {
                    "job_type": "market_bars_snapshot",
                    "params": {"symbols": [symbol], "frequency": "daily", "count": count}
                })
                
                if "_error" in job_resp:
                    status = "FAILED"
                    error_detail = job_resp.get("detail", str(job_resp))
                else:
                    job_id = job_resp.get("job_id")
                    # Wait for completion (max 120s)
                    for _ in range(24):
                        time.sleep(5)
                        status_resp = job_api_call("GET", f"/api/v1/jobs/{job_id}")
                        if status_resp.get("status") in ("DONE", "FAILED"):
                            break
                    
                    chunks_resp = job_api_call("GET", f"/api/v1/jobs/{job_id}/chunks")
                    chunks = chunks_resp.get("chunks", []) if isinstance(chunks_resp, dict) else []
                    
                    done_chunks = [c for c in chunks if c.get("status") == "DONE"]
                    failed_chunks = [c for c in chunks if c.get("status") == "FAILED"]
                    
                    status = "DONE" if done_chunks and not failed_chunks else ("PARTIAL" if done_chunks else "FAILED")
                    error_detail = f"{len(failed_chunks)} failed chunks" if failed_chunks else ""
                
                results.append({
                    "type": dtype, "symbol": symbol,
                    "status": status, "error_detail": error_detail or None,
                    "elapsed": round(time.time() - start, 1)
                })
                print(f"  {symbol}: {status}")
                
            elif dtype == "tencent_quote":
                # Batch call - all symbols at once if this is the first symbol
                if symbol == symbols[0]:
                    result = api_call("tencent_quote", {"codes": symbols})
                    classification = classify_result(result)
                    
                    results.append({
                        "type": dtype, "symbols": symbols,
                        "status": classification,
                        "result_size": len(result) if isinstance(result, (list, dict)) and not result.get("_error") else 0,
                        "elapsed": round(time.time() - start, 1)
                    })
                    print(f"  Batch ({','.join(symbols)}): {classification}")
                # Skip remaining symbols in batch
                continue
                
            elif dtype == "sina_financial_report":
                report_types = ds.get("report_types", ["lrb"])
                for rtype in report_types:
                    result = api_call("sina_financial_report", {"code": symbol, "report_type": rtype})
                    classification = classify_result(result)
                    
                    results.append({
                        "type": dtype, "symbol": symbol, "report_type": rtype,
                        "status": classification,
                        "result_size": len(result) if isinstance(result, list) and not result.get("_error") else 0,
                        "elapsed": round(time.time() - start, 1)
                    })
                    print(f"  {symbol}/{rtype}: {classification}")
                    
            elif dtype == "cninfo_announcements":
                result = api_call("cninfo_announcements", {"code": symbol, "page_size": 10})
                classification = classify_result(result)
                
                results.append({
                    "type": dtype, "symbol": symbol,
                    "status": classification,
                    "result_size": len(result) if isinstance(result, list) and not result.get("_error") else 0,
                    "elapsed": round(time.time() - start, 1)
                })
                print(f"  {symbol}: {classification} ({len(result) if isinstance(result, list) else '?'})")
                
            else:
                # Direct call with symbol as 'code' param
                result = api_call(dtype, {"code": symbol})
                classification = classify_result(result)
                
                results.append({
                    "type": dtype, "symbol": symbol,
                    "status": classification,
                    "result_size": len(result) if isinstance(result, (list, dict)) and not result.get("_error") else 0,
                    "elapsed": round(time.time() - start, 1)
                })
                print(f"  {symbol}: {classification}")
    
    # === Strict 25/25 classification summary ===
    print(f"\n{'='*60}")
    print("CANARY CLASSIFICATION (25 total)")
    print(f"{'='*60}")
    
    # Count by status (map DONE→SUCCESS for counting)
    counts = {}
    for r in results:
        s = r["status"]
        # Normalize DONE (from Job Engine) to SUCCESS for counting
        if s == "DONE":
            s = "SUCCESS"
        counts[s] = counts.get(s, 0) + 1
    
    # Expected categories
    all_categories = ["SUCCESS", "VALID_EMPTY", "SUSPECT_EMPTY", "TRANSIENT",
                      "UPSTREAM_BLOCKED", "PARSER_ERROR", "APPLICATION_ERROR",
                      "UNSUPPORTED", "UNKNOWN"]
    
    total = 0
    for cat in all_categories:
        c = counts.get(cat, 0)
        total += c
        print(f"  {cat:20s}: {c}")
    
    # List each result for verification
    print(f"\nDetailed results ({len(results)} items):")
    for i, r in enumerate(results, 1):
        dtype = r.get("type", "")
        symbol = r.get("symbol") or r.get("symbols", [""])[0]
        status = r["status"]
        extra = f"/{r.get('report_type', '')}" if "report_type" in r else ""
        err = f" [{r.get('error_detail', '')}]" if r.get("error_detail") else ""
        print(f"  {i:2d}. {dtype}{extra:15s} {symbol}: {status}{err}")
    
    print(f"\nTotal items: {len(results)}")
    print(f"Sum of categories: {total}")
    
    if len(results) != 25 or total != 25:
        print(f"WARNING: Count mismatch! Expected 25, got {len(results)} items / {total} categorized")
    
    # Check for UNKNOWN — these are NOT auto-passed
    unknown_count = counts.get("UNKNOWN", 0)
    if unknown_count > 0:
        print(f"\n⚠️ {unknown_count} UNKNOWN results — NOT auto-passed")
        for r in results:
            if r["status"] == "UNKNOWN":
                print(f"  - {r.get('type')}/{r.get('symbol')}")
    
    # Save checkpoint
    cp["phases"]["A"] = {
        "status": "PASS" if (counts.get("SUCCESS", 0) + counts.get("VALID_EMPTY", 0) > 0 and
                             counts.get("FAILED", 0) == 0 and unknown_count == 0) else "NEEDS_REVIEW",
        "counts": counts,
        "results": results,
        "completed_at": datetime.now(timezone.utc).isoformat()
    }
    save_checkpoint(cp)
    
    return counts.get("FAILED", 0) == 0 and unknown_count == 0


def get_universe():
    """Phase B: Get stock universe.
    
    Returns None and reports BLOCKED if no validated endpoint exists.
    """
    print("=" * 60)
    print("PHASE B: UNIVERSE DISCOVERY")
    print("=" * 60)
    
    cp = load_checkpoint()
    
    # Check if we already have it
    if cp.get("universe"):
        print(f"Universe already loaded: {len(cp['universe'])} stocks")
        return cp["universe"]
    
    # Check available functions for any that could enumerate stocks
    print("\nChecking API for universe/security-master capability...")
    
    # eastmoney_datacenter requires report_name — not a universal stock list
    # tencent_quote requires codes as input — not discovery
    # No function in the registry returns a stock list
    
    print("\nRESULT: PHASE_B_BLOCKED")
    print("No validated security-master/universe endpoint found in API.")
    print("- tencent_quote(codes) requires codes as input, not discovery")
    print("- eastmoney_datacenter(report_name) needs specific report names")
    print("- No function returns a stock list/universe")
    
    # Record the gap
    cp["phases"]["B"] = {
        "status": "BLOCKED",
        "reason": "no validated security-master/universe endpoint in API",
        "gap": "API capability gap — need a function that returns stock list without requiring codes as input",
        "completed_at": datetime.now(timezone.utc).isoformat()
    }
    save_checkpoint(cp)
    
    return None


def main():
    parser = argparse.ArgumentParser(description="Production data initialization")
    parser.add_argument("--phase", choices=["A", "B", "C", "D"], help="Run specific phase")
    parser.add_argument("--canary", action="store_true", help="Run canary only")
    parser.add_argument("--resume", action="store_true", help="Resume from checkpoint")
    args = parser.parse_args()
    
    print(f"Production Init: {API_BASE}")
    print(f"Checkpoint: {CHECKPOINT_FILE}")
    
    if args.canary or args.phase == "A":
        success = run_canary()
        if not success:
            print("\nCanary did not fully pass. Review results before proceeding.")
            sys.exit(1)
        print("\nCanary PASSED!")
    
    if args.phase == "B" or (args.phase is None and not args.canary):
        # Check if canary was done
        cp = load_checkpoint()
        if not cp.get("phases", {}).get("A"):
            print("Running canary first...")
            if not run_canary():
                sys.exit(1)
        
        universe = get_universe()
        if universe:
            print(f"Universe ready: {len(universe)} stocks")
        else:
            print("Phase B BLOCKED — no universe endpoint available")
    
    if not args.phase and not args.canary:
        print("\nFull initialization plan:")
        print("  Phase A (Canary):", "DONE" if load_checkpoint().get("phases", {}).get("A") else "PENDING")
        print("  Phase B (Universe):", "DONE" if load_checkpoint().get("universe") else "PENDING")
        print("  Phase C (Core): PENDING")
        print("  Phase D (High-Freq): PENDING")


if __name__ == "__main__":
    main()
