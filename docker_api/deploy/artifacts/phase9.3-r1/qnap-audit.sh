#!/bin/bash
# Phase 9.3 R1 QNAP Isolated Audit Script
# Run on QNAP via SSH: ssh admin@192.168.1.44 "bash /share/Docker/a-stock-data/audit-phase9.3-r1/qnap-audit.sh"

set -euo pipefail

DOCKER="/share/CACHEDEV2_DATA/.qpkg/container-station/usr/bin/docker"
AUDIT_DIR="/share/Docker/a-stock-data/audit-phase9.3-r1"
ENV_FILE="/share/Docker/a-stock-data/.env"
AUDIT_CONTAINER="a-stock-data-api-phase9.3-r1-audit"
PRODUCTION_CONTAINER="a-stock-data-api"

echo "=========================================="
echo "PHASE 9.3 R1 QNAP ISOLATED AUDIT"
echo "=========================================="

# 0. SAFETY BOUNDARY - Record production state BEFORE audit
echo ""
echo "=== 0. PRODUCTION STATE (BEFORE) ==="
$DOCKER inspect $PRODUCTION_CONTAINER --format 'Name: {{.Name}} Image: {{.Config.Image}} Health: {{.State.Health.Status}} RestartCount: {{.RestartCount}}' 2>&1
$DOCKER inspect $PRODUCTION_CONTAINER --format '{{.Image}}' 2>&1

# Re-run safety: remove old audit container if exists (NEVER touch production)
echo ""
echo "=== RE-RUN SAFETY ==="
if $DOCKER inspect "$AUDIT_CONTAINER" >/dev/null 2>&1; then
    echo "Old audit container exists, removing..."
    $DOCKER rm -f "$AUDIT_CONTAINER" 2>&1 || true
fi

# 1. TRANSFER + ARTIFACT VERIFICATION
echo ""
echo "=== 1. ARTIFACT TRANSFER ==="
mkdir -p "$AUDIT_DIR/data" "$AUDIT_DIR/cache"

echo "Verify artifact SHA256 (expected: 8775a6a4261d71c80972ac9c1a3d04ac3cfbeb5ddd779db214ae102099fe555e):"
sha256sum "$AUDIT_DIR/a-stock-data-api-281fc69-phase9-3-r1-linux-amd64.tar.gz" 2>&1

echo ""
echo "Load image:"
$DOCKER load -i "$AUDIT_DIR/a-stock-data-api-281fc69-phase9-3-r1-linux-amd64.tar.gz" 2>&1

echo ""
echo "Verify loaded image:"
$DOCKER inspect a-stock-data-api:281fc69-phase9-3-r1 --format 'ID: {{.Id}} Arch: {{.Architecture}} OS: {{.Os}}' 2>&1

# 2. CREATE ISOLATED AUDIT CONTAINER
echo ""
echo "=== 2. START ISOLATED CANDIDATE ==="

# Start isolated container (no host port, isolated data/cache)
$DOCKER run -d \
  --name "$AUDIT_CONTAINER" \
  --env-file "$ENV_FILE" \
  -v "$AUDIT_DIR/data:/app/data:rw" \
  -v "$AUDIT_DIR/cache:/app/cache:rw" \
  a-stock-data-api:281fc69-phase9-3-r1 \
  uvicorn astock_api.main:app --host 0.0.0.0 --port 8000 --workers 1 2>&1 || { echo "CONTAINER_START_FAILED"; exit 1; }

sleep 5

# Verify container exists
if ! $DOCKER inspect "$AUDIT_CONTAINER" >/dev/null 2>&1; then
    echo "CONTAINER_NOT_FOUND"; exit 1
fi

# Verify health (FAIL-FAST)
echo "Health check:"
$DOCKER exec "$AUDIT_CONTAINER" python3 -c "
import urllib.request, json

r = urllib.request.urlopen('http://127.0.0.1:8000/health/live')
print('LIVE:', r.status, r.read().decode())

r = urllib.request.urlopen('http://127.0.0.1:8000/health/ready')
print('READY:', r.status, r.read().decode())
" 2>&1 || { echo "HEALTH_CHECK_FAILED"; exit 1; }

echo ""
echo "Function count:"
$DOCKER exec "$AUDIT_CONTAINER" python3 -c "
import os, urllib.request, json

api_key = os.environ.get('ASTOCK_API_KEY', '')
if not api_key:
    raise SystemExit('ASTOCK_API_KEY_MISSING')

req = urllib.request.Request(
    'http://127.0.0.1:8000/api/v1/functions',
    headers={'X-API-Key': api_key},
)

with urllib.request.urlopen(req, timeout=10) as r:
    data = json.loads(r.read())

print('Functions:', len(data))
" 2>&1 || { echo "FUNCTION_COUNT_FAILED"; exit 1; }

echo ""
echo "DuckDB version:"
$DOCKER exec "$AUDIT_CONTAINER" python3 -c "
import duckdb; print('DuckDB:', duckdb.__version__)
" 2>&1

# 4. SECURITY MASTER REAL-UPSTREAM AUDIT
echo ""
echo "=== 4. SECURITY MASTER JOB ==="

# Create security_master_snapshot job (X-API-Key header, not Bearer)
echo "Creating security master job..."
JOB_RESPONSE=$($DOCKER exec "$AUDIT_CONTAINER" python3 -c "
import os, urllib.request, json

api_key = os.environ.get('ASTOCK_API_KEY', '')
data = json.dumps({
    'job_type': 'security_master_snapshot',
    'params': {
        'source': 'mootdx',
        'as_of': ''
    }
}).encode()

req = urllib.request.Request(
    'http://127.0.0.1:8000/api/v1/jobs',
    data=data,
    headers={'Content-Type': 'application/json', 'X-API-Key': api_key},
    method='POST'
)

r = urllib.request.urlopen(req, timeout=120)
print(r.read().decode())
" 2>&1) || { echo "JOB_CREATE_FAILED"; exit 1; }

echo "Job response: $JOB_RESPONSE"
JOB_ID=$($DOCKER exec "$AUDIT_CONTAINER" python3 -c "
import json, sys
data = json.loads('''$JOB_RESPONSE''')
print(data.get('job_id', ''))
" 2>&1)

echo "JOB_ID: $JOB_ID"

# Wait for job to complete (poll every 10s, max 5 min)
echo "Waiting for job to complete..."
for i in $(seq 1 30); do
    STATUS=$($DOCKER exec "$AUDIT_CONTAINER" python3 -c "
import os, urllib.request, json

api_key = os.environ.get('ASTOCK_API_KEY', '')
req = urllib.request.Request(
    'http://127.0.0.1:8000/api/v1/jobs/$JOB_ID',
    headers={'X-API-Key': api_key}
)
r = urllib.request.urlopen(req, timeout=10)
data = json.loads(r.read())
print(data.get('status', 'UNKNOWN'))
" 2>&1) || { echo "JOB_STATUS_FAILED"; exit 1; }
    echo "Job status: $STATUS (attempt $i/30)"
    case "$STATUS" in
        DONE|FAILED) break ;;
    esac
    sleep 10
done

echo ""
echo "=== RAW ARTIFACT CHECK ==="
$DOCKER exec "$AUDIT_CONTAINER" python3 -c "
import os, json

job_id = '$JOB_ID'
raw_path = f'/app/data/jobs/{job_id}/raw_enumeration.json'

if os.path.exists(raw_path):
    print('RAW_ARTIFACT_EXISTS=YES')
    with open(raw_path) as f:
        data = json.load(f)
    print('RAW_SSE_ROWS:', len(data.get('SSE', [])))
    print('RAW_SZSE_ROWS:', len(data.get('SZSE', [])))
else:
    print('RAW_ARTIFACT_EXISTS=NO')
    jobs_dir = '/app/data/jobs'
    if os.path.exists(jobs_dir):
        print('Jobs dir contents:', os.listdir(jobs_dir))
" 2>&1

echo ""
echo "=== RAW ARTIFACT REPLAY ==="
$DOCKER exec "$AUDIT_CONTAINER" python3 -c "
from astock_api.security_master_handler import load_raw_artifact

job_id = '$JOB_ID'
try:
    data = load_raw_artifact(job_id)
    print('REPLAY=OK')
    print('SSE rows:', len(data.get('SSE', [])))
    print('SZSE rows:', len(data.get('SZSE', [])))
except Exception as e:
    print(f'REPLAY=FAIL {e}')
" 2>&1

# 5. SECURITY MASTER DATA AUDIT
echo ""
echo "=== 5. DUCKDB DATA AUDIT ==="
$DOCKER exec "$AUDIT_CONTAINER" python3 -c "
import duckdb

conn = duckdb.connect('/app/data/astock_data.duckdb')

# List tables
tables = conn.execute(\"SELECT table_name FROM information_schema.tables WHERE table_schema='main'\").fetchall()
print('Tables:', [t[0] for t in tables])

# Security master counts
try:
    result = conn.execute('SELECT COUNT(*) FROM security_master').fetchone()
    print('CANONICAL_SECURITY_ROWS:', result[0])

    sse = conn.execute(\"SELECT COUNT(*) FROM security_master WHERE exchange='SSE'\").fetchone()
    print('CANONICAL_SSE_ROWS:', sse[0])

    szse = conn.execute(\"SELECT COUNT(*) FROM security_master WHERE exchange='SZSE'\").fetchone()
    print('CANONICAL_SZSE_ROWS:', szse[0])

    # Duplicate check
    dupes = conn.execute('SELECT security_id, COUNT(*) as cnt FROM security_master GROUP BY security_id HAVING cnt > 1').fetchall()
    print('DUPLICATE_SECURITY_ID_COUNT:', len(dupes))

    # Known securities
    moutai = conn.execute(\"SELECT * FROM security_master WHERE security_id='SSE:600519'\").fetchone()
    print('KNOWN_SECURITY_600519:', moutai)

    pingan = conn.execute(\"SELECT * FROM security_master WHERE security_id='SZSE:000001'\").fetchone()
    print('KNOWN_SECURITY_000001:', pingan)

   宁德时代 = conn.execute(\"SELECT * FROM security_master WHERE security_id='SZSE:300750'\").fetchone()
    print('KNOWN_SECURITY_300750:', 宁德时代)

except Exception as e:
    print(f'DATA_AUDIT_ERROR: {e}')

# Snapshot status
try:
    result = conn.execute('SELECT snapshot_id, status, exchange_coverage FROM security_master_snapshots ORDER BY created_at DESC LIMIT 1').fetchone()
    print('SNAPSHOT_STATUS:', result)
except Exception as e:
    print(f'SNAPSHOT_CHECK_ERROR: {e}')

conn.close()
" 2>&1

# 6. MARKET BARS SYNC AUDIT
echo ""
echo "=== 6. MARKET BARS SYNC ==="

# Create market_bars_sync job
echo "Creating market bars sync job..."
$DOCKER exec "$AUDIT_CONTAINER" python3 -c "
import os, urllib.request, json

api_key = os.environ.get('ASTOCK_API_KEY', '')
data = json.dumps({
    'job_type': 'market_bars_sync',
    'params': {
        'symbols': ['SSE:600519'],
        'frequency': 'daily',
        'count': 30
    }
}).encode()

req = urllib.request.Request(
    'http://127.0.0.1:8000/api/v1/jobs',
    data=data,
    headers={'Content-Type': 'application/json', 'X-API-Key': api_key},
    method='POST'
)

r = urllib.request.urlopen(req, timeout=120)
print(r.read().decode())
" 2>&1

# Wait for job to complete
sleep 30

echo ""
echo "=== MARKET BARS DATA ==="
$DOCKER exec "$AUDIT_CONTAINER" python3 -c "
import duckdb

conn = duckdb.connect('/app/data/astock_data.duckdb')

try:
    result = conn.execute(\"SELECT COUNT(*) FROM market_bars_daily WHERE security_id='SSE:600519'\").fetchone()
    print('MARKET_BARS_ROWS:', result[0])

    # Sample row
    sample = conn.execute(\"SELECT * FROM market_bars_daily WHERE security_id='SSE:600519' ORDER BY trade_date DESC LIMIT 3\").fetchall()
    print('SAMPLE_ROWS:', sample)

    # Volume unit check (should be in 手/lots, not shares)
    vol_check = conn.execute(\"SELECT volume FROM market_bars_daily WHERE security_id='SSE:600519' ORDER BY trade_date DESC LIMIT 1\").fetchone()
    print('VOLUME_SAMPLE:', vol_check[0])
    if vol_check and vol_check[0] < 10000:
        print('VOLUME_UNIT_CHECK=PASS (likely in lots/手)')
    else:
        print('VOLUME_UNIT_CHECK=WARNING (value seems high for lots)')

except Exception as e:
    print(f'MARKET_BARS_ERROR: {e}')

conn.close()
" 2>&1

# 8. IDEMPOTENCY AUDIT
echo ""
echo "=== 8. IDEMPOTENCY ==="

# Run same job again
$DOCKER exec "$AUDIT_CONTAINER" python3 -c "
import os, urllib.request, json

api_key = os.environ.get('ASTOCK_API_KEY', '')
data = json.dumps({
    'job_type': 'market_bars_sync',
    'params': {
        'symbols': ['SSE:600519'],
        'frequency': 'daily',
        'count': 30
    }
}).encode()

req = urllib.request.Request(
    'http://127.0.0.1:8000/api/v1/jobs',
    data=data,
    headers={'Content-Type': 'application/json', 'X-API-Key': api_key},
    method='POST'
)

r = urllib.request.urlopen(req, timeout=120)
print('SECOND_SYNC:', r.read().decode())
" 2>&1

sleep 30

echo ""
echo "=== IDEMPOTENCY CHECK ==="
$DOCKER exec "$AUDIT_CONTAINER" python3 -c "
import duckdb

conn = duckdb.connect('/app/data/astock_data.duckdb')
result = conn.execute(\"SELECT COUNT(*) FROM market_bars_daily WHERE security_id='SSE:600519'\").fetchone()
print('MARKET_BARS_ROWS_AFTER_SECOND_SYNC:', result[0])
conn.close()
" 2>&1

# 9. PERSISTENCE AUDIT
echo ""
echo "=== 9. RESTART PERSISTENCE ==="

# Restart only the audit container
$DOCKER restart "$AUDIT_CONTAINER" 2>&1 || { echo "RESTART_FAILED"; exit 1; }
sleep 5

echo "After restart:"
$DOCKER exec "$AUDIT_CONTAINER" python3 -c "
import urllib.request, json

r = urllib.request.urlopen('http://127.0.0.1:8000/health/ready')
print('HEALTH_READY_AFTER_RESTART:', r.status, r.read().decode())

import duckdb
conn = duckdb.connect('/app/data/astock_data.duckdb')

try:
    sec_count = conn.execute('SELECT COUNT(*) FROM security_master').fetchone()
    print('SECURITY_ROWS_AFTER_RESTART:', sec_count[0])

    bars_count = conn.execute(\"SELECT COUNT(*) FROM market_bars_daily WHERE security_id='SSE:600519'\").fetchone()
    print('MARKET_BARS_AFTER_RESTART:', bars_count[0])

    # Raw artifact still exists
    import os, json
    jobs_dir = '/app/data/jobs'
    if os.path.exists(jobs_dir):
        for d in os.listdir(jobs_dir):
            raw_path = os.path.join(jobs_dir, d, 'raw_enumeration.json')
            if os.path.exists(raw_path):
                print('RAW_ARTIFACT_AFTER_RESTART=YES')
except Exception as e:
    print(f'PERSISTENCE_CHECK_ERROR: {e}')

conn.close()
" 2>&1

# 10. PRODUCTION NON-INTERFERENCE CHECK
echo ""
echo "=== 10. PRODUCTION STATE (AFTER) ==="
$DOCKER inspect $PRODUCTION_CONTAINER --format 'Name: {{.Name}} Image: {{.Config.Image}} Health: {{.State.Health.Status}} RestartCount: {{.RestartCount}}' 2>&1

echo ""
echo "=========================================="
echo "AUDIT COMPLETE"
echo "=========================================="
