#!/bin/bash
# Phase 9.3 Security Master Canary Audit Script
# Run on QNAP via SSH: docker exec a-stock-data-api bash /tmp/security_master_canary.sh
# Or copy this script to the container first

docker exec a-stock-data-api python3 << 'EOF'
import json
import sys

print("=" * 60)
print("SECURITY MASTER CANARY AUDIT")
print("=" * 60)

# Step 1: Check mootdx version and tdxpy dependency
try:
    import mootdx
    print(f"\nmootdx version: {mootdx.__version__}")
except Exception as e:
    print(f"mootdx import failed: {e}")

try:
    import tdxpy
    print(f"tdxpy version: {getattr(tdxpy, '__version__', 'unknown')}")
except ImportError as e:
    print(f"tdxpy not found: {e}")

# Step 2: Get TDX client
try:
    from astock_api.upstream.common import tdx_client
    client = tdx_client()
    print(f"\nClient type: {type(client)}")
    print(f"Client methods: {[m for m in dir(client) if not m.startswith('_')]}")
except Exception as e:
    print(f"tdx_client failed: {e}")
    sys.exit(1)

# Step 3: Check stock_count for SH/SZ/BSE
print("\n" + "=" * 60)
print("SECURITY COUNT")
print("=" * 60)

for market in [0, 1, 2]:
    try:
        count = client.stock_count(market)
        market_name = {0: 'SH', 1: 'SZ', 2: 'BSE'}.get(market, f'market-{market}')
        print(f"{market_name} count: {count}")
    except Exception as e:
        market_name = {0: 'SH', 1: 'SZ', 2: 'BSE'}.get(market, f'market-{market}')
        print(f"{market_name} count failed: {e}")

# Step 4: Get stocks for SH/SZ
print("\n" + "=" * 60)
print("SECURITY LIST SAMPLE")
print("=" * 60)

for market in [0, 1]:
    try:
        stocks = client.stocks(market)
        market_name = {0: 'SH', 1: 'SZ'}.get(market, f'market-{market}')
        print(f"\n{market_name} stocks returned: {len(stocks)}")
        
        if hasattr(stocks, 'columns'):
            print(f"Columns: {list(stocks.columns)}")
        elif isinstance(stocks, list) and len(stocks) > 0:
            print(f"First item keys: {list(stocks[0].keys()) if isinstance(stocks[0], dict) else type(stocks[0])}")
        
        # Show first 3 rows
        if hasattr(stocks, 'head'):
            print(f"First 3 rows:\n{stocks.head(3)}")
        elif isinstance(stocks, list) and len(stocks) > 0:
            print(f"First 3 items:")
            for item in stocks[:3]:
                print(f"  {item}")
        
        # Show last row
        if hasattr(stocks, 'tail'):
            print(f"Last 1 row:\n{stocks.tail(1)}")
        elif isinstance(stocks, list) and len(stocks) > 0:
            print(f"Last item: {stocks[-1]}")
            
    except Exception as e:
        market_name = {0: 'SH', 1: 'SZ'}.get(market, f'market-{market}')
        print(f"{market_name} stocks failed: {e}")

# Step 5: Check BSE enumeration
print("\n" + "=" * 60)
print("BSE ENUMERATION CHECK")
print("=" * 60)

try:
    # Try stock_count(2) first
    bse_count = client.stock_count(2)
    print(f"BSE count: {bse_count}")
    
    if bse_count > 0:
        # Try to get BSE stocks via underlying tdxpy client
        try:
            bse_stocks = client.client.get_security_list(market=2, start=0)
            print(f"BSE stocks returned: {len(bse_stocks)}")
            if bse_stocks:
                print(f"First BSE stock: {bse_stocks[0]}")
        except Exception as e:
            print(f"BSE stocks failed: {e}")
    else:
        print("BSE count is 0 - no BSE securities available")
except Exception as e:
    print(f"BSE enumeration failed: {e}")

# Step 6: Check for duplicate codes
print("\n" + "=" * 60)
print("DUPLICATE CHECK")
print("=" * 60)

try:
    sh_stocks = client.stocks(0)
    sz_stocks = client.stocks(1)
    
    if hasattr(sh_stocks, 'columns'):
        # DataFrame - check for duplicate codes
        sh_codes = set(sh_stocks['code'].tolist() if 'code' in sh_stocks.columns else [])
        sz_codes = set(sz_stocks['code'].tolist() if 'code' in sz_stocks.columns else [])
        print(f"SH unique codes: {len(sh_codes)}")
        print(f"SZ unique codes: {len(sz_codes)}")
        print(f"Overlap (SH ∩ SZ): {len(sh_codes & sz_codes)}")
    else:
        print("Cannot check duplicates - stocks not in DataFrame format")
except Exception as e:
    print(f"Duplicate check failed: {e}")

print("\n" + "=" * 60)
print("CANARY AUDIT COMPLETE")
print("=" * 60)
EOF
