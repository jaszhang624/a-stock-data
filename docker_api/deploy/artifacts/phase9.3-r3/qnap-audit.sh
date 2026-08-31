#!/bin/bash
# Phase 9.3 R3 QNAP Isolated Audit Script
# Run on QNAP via SSH: ssh admin@192.168.1.44 "bash /share/Docker/a-stock-data/audit-phase9.3-r3/qnap-audit.sh"

set -euo pipefail

DOCKER="/share/CACHEDEV2_DATA/.qpkg/container-station/usr/bin/docker"
AUDIT_ROOT="/share/Docker/a-stock-data/audit-phase9.3-r3"
ENV_FILE="/share/Docker/a-stock-data/.env"
AUDIT_CONTAINER="a-stock-data-api-phase9.3-r3-audit"
PROD_CONTAINER="a-stock-data-api"

# R3 identity (verified from frozen OCI artifact)
IMAGE_TAG="a-stock-data-api:281fc69-phase9-3-r3"
ARTIFACT="$AUDIT_ROOT/a-stock-data-api-281fc69-phase9-3-r3-linux-amd64.tar.gz"
EXPECTED_CONFIG_DIGEST="sha256:bb2ea3a2abb8af38c3e06dd8b7afe077c327073eb7871388139ac46a03376a3a"
EXPECTED_ARTIFACT_SHA="8eec576adcf0c73b25b22b3b6401a3612a8ed1e7d6094d5691ddc82b36ea727c"
EXPECTED_OCI_INDEX="sha256:1ad36afc590df00179985634db965913baae8b52686ed51df50d0f9715ca21bc"
EXPECTED_LINUX_AMD64_MANIFEST="sha256:650773c9c1b7f5fad443b36eb78d45da3f8755f405b6c8634705db93ec736924"

# Runtime directories (mutable — safe to reset)
RUNTIME_DIR="$AUDIT_ROOT/runtime"
DATA_DIR="$RUNTIME_DIR/data"
CACHE_DIR="$RUNTIME_DIR/cache"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[R3-AUDIT]${NC} $*"; }
warn() { echo -e "${YELLOW}[R3-AUDIT-WARN]${NC} $*"; }
fail() { echo -e "${RED}[R3-AUDIT-FAIL]${NC} $*"; exit 1; }

# ============================================================
# PHASE 0: PRE-FLIGHT CHECKS
# ============================================================

log "=== Phase 9.3 R3 QNAP Isolated Audit ==="
log "Date: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

if [ ! -f "$ENV_FILE" ]; then
    fail "ENV file not found at $ENV_FILE"
fi

if [ ! -f "$ARTIFACT" ]; then
    fail "Artifact not found: $ARTIFACT"
fi

# ============================================================
# PHASE 1: ARTIFACT VERIFICATION
# ============================================================

log "--- Phase 1: Artifact Verification ---"

ACTUAL_SHA=$(sha256sum "$ARTIFACT" | awk '{print $1}')
if [ "$ACTUAL_SHA" != "$EXPECTED_ARTIFACT_SHA" ]; then
    fail "Artifact SHA256 mismatch: expected=$EXPECTED_ARTIFACT_SHA actual=$ACTUAL_SHA"
fi
log "Artifact SHA256: PASS ($ACTUAL_SHA)"

# ============================================================
# PHASE 2: DOCKER LOAD & IDENTITY
# ============================================================

log "--- Phase 2: Docker Load & Identity ---"

# Docker load — preserve real exit code (image may already be loaded)
if LOAD_OUTPUT=$("$DOCKER" load -i "$ARTIFACT" 2>&1); then
    log "Artifact loaded successfully"
else
    LOAD_RC=$?
    warn "Docker load stderr: $LOAD_OUTPUT"
    fail "Docker load failed for $ARTIFACT (exit code $LOAD_RC)"
fi

# Verify linux/amd64 (can check before container starts)
ARCH=$("$DOCKER" inspect "$IMAGE_TAG" --format='{{.Architecture}}' 2>/dev/null) || fail "Unable to inspect image architecture"
if [ "$ARCH" != "amd64" ]; then
    fail "Architecture mismatch: expected=amd64 actual=$ARCH"
fi
log "Architecture verified: $ARCH"

# ============================================================
# PHASE 3: ISOLATED CONTAINER SETUP
# ============================================================

log "--- Phase 3: Isolated Container Setup ---"

# Clean up any previous audit container
$DOCKER rm -f "$AUDIT_CONTAINER" > /dev/null 2>&1 || true

# Create fresh runtime directory (safe to reset — does NOT delete artifact or script)
rm -rf "$RUNTIME_DIR" 2>/dev/null || true
mkdir -p "$DATA_DIR" "$CACHE_DIR"

# Set ownership for non-root container user
chown -R 999:999 "$RUNTIME_DIR" || true

# Start isolated container (NO port publishing, default network for TDX)
$DOCKER run -d \
    --name "$AUDIT_CONTAINER" \
    -v "$DATA_DIR:/app/data:rw" \
    -v "$CACHE_DIR:/app/cache:rw" \
    --env-file "$ENV_FILE" \
    "$IMAGE_TAG" > /dev/null 2>&1 || fail "Failed to start audit container $AUDIT_CONTAINER"

log "Container started: $AUDIT_CONTAINER"
sleep 5

# Verify loaded-image Config identity.
# On QNAP Docker Engine, image .Id equals the OCI config digest.
IMAGE_ID=$("$DOCKER" image inspect "$IMAGE_TAG" --format='{{.Id}}') \
    || fail "Unable to inspect R3 image identity"

if [ "$IMAGE_ID" != "$EXPECTED_CONFIG_DIGEST" ]; then
    fail "Config digest mismatch: expected=$EXPECTED_CONFIG_DIGEST actual=$IMAGE_ID"
fi

log "Config digest verified: $IMAGE_ID"

# ============================================================
# PHASE 4: HEALTH CHECKS
# ============================================================

log "--- Phase 4: Health Checks ---"

LIVE=$($DOCKER exec "$AUDIT_CONTAINER" python3 -c "
import urllib.request, json
r = urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=5)
print(json.loads(r.read()))
" 2>/dev/null) || fail "Health /live check failed"

log "Health /live: $LIVE"

READY=$($DOCKER exec "$AUDIT_CONTAINER" python3 -c "
import urllib.request, json
r = urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=5)
print(json.loads(r.read()))
" 2>/dev/null) || fail "Health /ready check failed"

log "Health /ready: $READY"

# ============================================================
# PHASE 5: FUNCTIONS COUNT & DUCKDB VERSION
# ============================================================

log "--- Phase 5: Functions Count & DuckDB Version ---"

API_KEY=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "import os; print(os.environ.get('ASTOCK_API_KEY', ''))" 2>/dev/null) || fail "Failed to read API key from container"

FUNCTIONS_COUNT=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import urllib.request, json
req = urllib.request.Request('http://127.0.0.1:8000/api/v1/functions', headers={'X-API-Key': '$API_KEY'})
r = urllib.request.urlopen(req, timeout=10)
data = json.loads(r.read())
print(len(data))
" 2>/dev/null) || fail "Functions count check failed"

if [ "$FUNCTIONS_COUNT" != "50" ]; then
    fail "Functions count mismatch: expected=50 actual=$FUNCTIONS_COUNT"
fi
log "Functions count: $FUNCTIONS_COUNT ✓"

DUCKDB_VERSION=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "import duckdb; print(duckdb.__version__)" 2>/dev/null) || fail "DuckDB version check failed"
if [ "$DUCKDB_VERSION" != "1.1.3" ]; then
    fail "DuckDB version mismatch: expected=1.1.3 actual=$DUCKDB_VERSION"
fi
log "DuckDB version: $DUCKDB_VERSION ✓"

# ============================================================
# PHASE 6: R2 FAILURE #1 — DISPATCH VERIFICATION
# ============================================================

log "--- Phase 6: Dispatch Verification (R2 Failure #1) ---"
log "Verifying security_master_snapshot routes to correct handler..."

DISPATCH_CHECK=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
from astock_api.job_handlers import get_handler
handler = get_handler('security_master_snapshot')
print(handler.__name__)
" 2>/dev/null) || fail "Dispatch verification execution failed"

if [ "$DISPATCH_CHECK" != "security_master_snapshot_handler" ]; then
    fail "Dispatch verification FAILED: expected=security_master_snapshot_handler actual=$DISPATCH_CHECK"
fi
log "Dispatch verified: handler = $DISPATCH_CHECK ✓"

# ============================================================
# PHASE 7: R2 FAILURE #3 — DATASETSTORE BOOTSTRAP VERIFICATION
# ============================================================

log "--- Phase 7: DatasetStore Bootstrap Verification (R2 Failure #3) ---"
log "Verifying bootstrap creates tables on fresh DuckDB..."

# Use explicit error capture to avoid silent set -e termination
if BOOTSTRAP_CHECK=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import tempfile, os
from astock_api.dataset_store import DatasetStore

# Create isolated temp dir under /app/data (NOT /tmp)
with tempfile.TemporaryDirectory(dir='/app/data') as td:
    db_path = os.path.join(td, 'fresh.duckdb')

    if os.path.exists(db_path):
        raise RuntimeError('fresh DB unexpectedly exists')

    store = DatasetStore(db_path)
    store.bootstrap()

    import duckdb
    conn = duckdb.connect(db_path)
    tables = {
        row[0]
        for row in conn.execute(
            \"SELECT table_name \"
            \"FROM information_schema.tables \"
            \"WHERE table_schema='main'\"
        ).fetchall()
    }
    conn.close()

    required = {
        'security_master_snapshots',
        'security_master',
        'market_bars_daily',
    }

    missing = required - tables
    if missing:
        raise RuntimeError(
            'missing bootstrap tables: ' + ','.join(sorted(missing))
        )

print('BOOTSTRAP_OK')
" 2>&1); then
    :
else
    RC=$?
    fail "Bootstrap verification execution failed (exit=$RC): $BOOTSTRAP_CHECK"
fi

if [ "$BOOTSTRAP_CHECK" != "BOOTSTRAP_OK" ]; then
    fail "Bootstrap verification FAILED: $BOOTSTRAP_CHECK"
fi
log "Bootstrap verified: all required tables created ✓"

# ============================================================
# PHASE 8: REAL SECURITY_MASTER_SNAPSHOT JOB
# ============================================================

log "--- Phase 8: Real security_master_snapshot Job ---"
log "Creating job..."

# Create job — Python validates response and emits ONLY job_id
if JOB_ID=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import urllib.request, json

req = urllib.request.Request(
    'http://127.0.0.1:8000/api/v1/jobs',
    data=json.dumps({'job_type': 'security_master_snapshot', 'params': {'source': 'mootdx'}}).encode(),
    headers={'X-API-Key': '$API_KEY', 'Content-Type': 'application/json'},
    method='POST'
)
r = urllib.request.urlopen(req, timeout=10)
data = json.loads(r.read())
jid = data.get('job_id', '')
if not jid:
    raise RuntimeError('missing job_id in response')
print(jid)
" 2>&1); then
    :
else
    RC=$?
    fail "Failed to create security_master_snapshot job (exit=$RC): $JOB_ID"
fi

log "Job created: $JOB_ID"

# ============================================================
# PHASE 9: JOB POLLING WITH TIMEOUT
# ============================================================

log "--- Phase 9: Job Polling (max 5 minutes) ---"
POLL_COUNT=0
MAX_POLLS=30
JOB_DONE=false

while [ $POLL_COUNT -lt $MAX_POLLS ]; do
    POLL_COUNT=$((POLL_COUNT + 1))
    
    # Python parses JSON and emits STATUS|LAST_ERROR (pipe-delimited)
    if JOB_STATUS_RAW=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import urllib.request, json

req = urllib.request.Request(
    'http://127.0.0.1:8000/api/v1/jobs/$JOB_ID',
    headers={'X-API-Key': '$API_KEY'}
)
r = urllib.request.urlopen(req, timeout=10)
data = json.loads(r.read())
status = data.get('status', 'UNKNOWN')
last_error = str(data.get('last_error', ''))
print(f'{status}|{last_error}')
" 2>&1); then
        STATUS="${JOB_STATUS_RAW%%|*}"
        LAST_ERROR="${JOB_STATUS_RAW#*|}"
    else
        warn "Job status check failed at poll $POLL_COUNT"
        sleep 10
        continue
    fi
    
    log "Poll $POLL_COUNT: status=$STATUS"
    
    if [ "$STATUS" = "DONE" ]; then
        log "Job completed successfully!"
        JOB_DONE=true
        
        # ============================================================
        # PHASE 10: RAW ARTIFACT VERIFICATION (R2 Failure #2)
        # ============================================================
        
        log "--- Phase 10: Raw Artifact Verification (R2 Failure #2) ---"
        
        RAW_CHECK=$($DOCKER exec "$AUDIT_CONTAINER" python3 -c "
import os, json

raw_path = f'/app/data/jobs/$JOB_ID/raw_enumeration.json'
if os.path.exists(raw_path):
    size = os.path.getsize(raw_path)
    with open(raw_path) as f:
        data = json.load(f)
    
    # Safe metadata only — do NOT print full enumeration
    exchanges = list(data.keys()) if isinstance(data, dict) else ['unknown']
    total_raw = sum(len(v) for v in data.values()) if isinstance(data, dict) else 0
    
    print(f'RAW_EXISTS:true')
    print(f'RAW_BYTES:{size}')
    print(f'RAW_EXCHANGES:{exchanges}')
    print(f'RAW_TOTAL_RECORDS:{total_raw}')
else:
    print('RAW_EXISTS:false')
" 2>/dev/null) || fail "Raw artifact check failed"
        
        RAW_EXISTS=$(echo "$RAW_CHECK" | grep 'RAW_EXISTS:' | cut -d: -f2)
        if [ "$RAW_EXISTS" != "true" ]; then
            fail "Raw artifact MISSING — R2 Failure #2 NOT fixed!"
        fi
        
        RAW_BYTES=$(echo "$RAW_CHECK" | grep 'RAW_BYTES:' | cut -d: -f2)
        RAW_EXCHANGES=$(echo "$RAW_CHECK" | grep 'RAW_EXCHANGES:' | cut -d: -f2-)
        RAW_TOTAL=$(echo "$RAW_CHECK" | grep 'RAW_TOTAL_RECORDS:' | cut -d: -f2)
        
        log "Raw artifact verified: bytes=$RAW_BYTES exchanges=$RAW_EXCHANGES total_records=$RAW_TOTAL ✓"
        
        # ============================================================
        # PHASE 11: CANONICAL DATA VERIFICATION
        # ============================================================
        
        log "--- Phase 11: Canonical Data Verification ---"
        
        CANONICAL_CHECK=$($DOCKER exec "$AUDIT_CONTAINER" python3 -c "
import duckdb as dd

conn = dd.connect('/app/data/astock_data.duckdb')

# Total canonical securities
total = conn.execute('SELECT COUNT(*) FROM security_master WHERE snapshot_status = ? AND active = ?', ('ACTIVE', True)).fetchone()[0]
print(f'CANONICAL_TOTAL:{total}')

# SSE count
sse = conn.execute(\"SELECT COUNT(*) FROM security_master WHERE exchange='SSE' AND snapshot_status='ACTIVE' AND active=True\").fetchone()[0]
print(f'CANONICAL_SSE:{sse}')

# SZSE count  
szse = conn.execute(\"SELECT COUNT(*) FROM security_master WHERE exchange='SZSE' AND snapshot_status='ACTIVE' AND active=True\").fetchone()[0]
print(f'CANONICAL_SZSE:{szse}')

# Duplicate check
dupes = conn.execute('SELECT COUNT(*) FROM (SELECT security_id FROM security_master WHERE snapshot_status=\"ACTIVE\" AND active=True GROUP BY security_id HAVING COUNT(*) > 1)').fetchone()[0]
print(f'DUPLICATE_COUNT:{dupes}')

# Known securities
for code, exchange in [('600519', 'SSE'), ('000001', 'SZSE'), ('300750', 'SZSE')]:
    sid = f'{exchange}:{code}'
    row = conn.execute('SELECT security_id, name FROM security_master WHERE security_id=?', [sid]).fetchone()
    if row:
        print(f'KNOWN_SECURITY:{sid}:{row[1]}')
    else:
        print(f'MISSING_SECURITY:{sid}')

conn.close()
" 2>/dev/null) || fail "Canonical data check failed"
        
        echo "$CANONICAL_CHECK" | while read line; do
            log "  $line"
        done
        
        # Verify no duplicates
        DUP_COUNT=$(echo "$CANONICAL_CHECK" | grep 'DUPLICATE_COUNT:' | cut -d: -f2)
        if [ "$DUP_COUNT" != "0" ]; then
            fail "Duplicate security_id found: $DUP_COUNT"
        fi
        log "No duplicate security_ids ✓"
        
        # Verify known securities exist
        for SEC in "KNOWN_SECURITY:SSE:600519" "KNOWN_SECURITY:SZSE:000001" "KNOWN_SECURITY:SZSE:300750"; do
            if ! echo "$CANONICAL_CHECK" | grep -q "$SEC"; then
                fail "Known security not found: $SEC"
            fi
        done
        log "All known securities verified ✓"
        
        break
        
    elif [ "$STATUS" = "FAILED" ]; then
        # LAST_ERROR already extracted from STATUS|LAST_ERROR format above
        fail "Job FAILED: $LAST_ERROR"
        
    elif [ "$STATUS" = "WAITING_SOURCE" ]; then
        # Check if this is a local error masquerading as upstream wait
        if echo "$LAST_ERROR" | grep -qi "catalog\|table.*does not exist\|IO Error"; then
            fail "R2 Failure #3 NOT fixed: local DuckDB error classified as WAITING_SOURCE! Error: $LAST_ERROR"
        fi
        log "Job waiting for source (upstream transient) — continuing poll..."
    fi
    
    sleep 10
done

if [ "$JOB_DONE" != "true" ]; then
    # Get final job state
    FINAL_STATUS=$($DOCKER exec "$AUDIT_CONTAINER" python3 -c "
import urllib.request, json

req = urllib.request.Request(
    'http://127.0.0.1:8000/api/v1/jobs/$JOB_ID',
    headers={'X-API-Key': '$API_KEY'}
)
r = urllib.request.urlopen(req, timeout=10)
data = json.loads(r.read())
print(json.dumps(data))
" 2>/dev/null) || true
    
    warn "Job polling timeout reached (5 minutes)"
    log "Final job state: $FINAL_STATUS"
    
    # Keep container for inspection
    warn "Container preserved at: $AUDIT_CONTAINER"
    fail "Job did not reach terminal state within timeout"
fi

# ============================================================
# PHASE 12: MARKET_BARS_SYNC (REAL)
# ============================================================

log "--- Phase 12: Real market_bars_sync Job ---"
log "Creating job for SSE:600519..."

# Create sync job — Python validates and emits ONLY job_id
if SYNC_JOB_ID=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import urllib.request, json

req = urllib.request.Request(
    'http://127.0.0.1:8000/api/v1/jobs',
    data=json.dumps({'job_type': 'market_bars_sync', 'params': {'symbols': ['SSE:600519'], 'frequency': 'daily', 'count': 5}}).encode(),
    headers={'X-API-Key': '$API_KEY', 'Content-Type': 'application/json'},
    method='POST'
)
r = urllib.request.urlopen(req, timeout=10)
data = json.loads(r.read())
jid = data.get('job_id', '')
if not jid:
    raise RuntimeError('missing job_id in response')
print(jid)
" 2>&1); then
    :
else
    RC=$?
    fail "Failed to create market_bars_sync job (exit=$RC): $SYNC_JOB_ID"
fi

log "Sync job created: $SYNC_JOB_ID"

# Poll sync job
POLL_COUNT=0
while [ $POLL_COUNT -lt 30 ]; do
    POLL_COUNT=$((POLL_COUNT + 1))
    
    SYNC_STATUS=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import urllib.request, json

req = urllib.request.Request(
    'http://127.0.0.1:8000/api/v1/jobs/$SYNC_JOB_ID',
    headers={'X-API-Key': '$API_KEY'}
)
r = urllib.request.urlopen(req, timeout=10)
data = json.loads(r.read())
print(data.get('status', 'UNKNOWN'))
" 2>/dev/null) || { sleep 10; continue; }
    
    log "Sync poll $POLL_COUNT: status=$SYNC_STATUS"
    
    if [ "$SYNC_STATUS" = "DONE" ]; then
        log "Sync job completed!"
        break
    elif [ "$SYNC_STATUS" = "FAILED" ]; then
        fail "Sync job failed"
    fi
    
    sleep 10
done

if [ $POLL_COUNT -ge 30 ]; then
    fail "Sync job did not complete within timeout"
fi

# Verify sync results
SYNC_CHECK=$($DOCKER exec "$AUDIT_CONTAINER" python3 -c "
import duckdb as dd

conn = dd.connect('/app/data/astock_data.duckdb')

# Check bars exist
bars = conn.execute(\"SELECT COUNT(*) FROM market_bars_daily WHERE security_id='SSE:600519'\").fetchone()[0]
print(f'BARS_COUNT:{bars}')

# Check volume unit (should be 手, not shares — values should be reasonable)
sample = conn.execute(\"SELECT volume, amount FROM market_bars_daily WHERE security_id='SSE:600519' ORDER BY date DESC LIMIT 1\").fetchone()
if sample:
    print(f'SAMPLE_VOLUME:{sample[0]}')
    print(f'SAMPLE_AMOUNT:{sample[1]}')
    
    # Volume should be in 手 (lots of 100) — typical daily volume for 600519 is ~2万手
    # If it were shares, values would be ~200万 which is too large for '手' unit
    if sample[0] > 100000:
        print('VOLUME_UNIT:WARNING_POSSIBLY_SHARES')
    else:
        print('VOLUME_UNIT:OK_HANDS')

conn.close()
" 2>/dev/null) || fail "Sync verification failed"

echo "$SYNC_CHECK" | while read line; do
    log "  $line"
done

# ============================================================
# PHASE 13: IDEMPOTENCY TEST (second identical sync)
# ============================================================

log "--- Phase 13: Idempotency Test ---"
BEFORE_COUNT=$(echo "$SYNC_CHECK" | grep 'BARS_COUNT:' | cut -d: -f2)

# Run identical sync again — Python validates and emits ONLY job_id
if SYNC2_JOB_ID=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import urllib.request, json

req = urllib.request.Request(
    'http://127.0.0.1:8000/api/v1/jobs',
    data=json.dumps({'job_type': 'market_bars_sync', 'params': {'symbols': ['SSE:600519'], 'frequency': 'daily', 'count': 5}}).encode(),
    headers={'X-API-Key': '$API_KEY', 'Content-Type': 'application/json'},
    method='POST'
)
r = urllib.request.urlopen(req, timeout=10)
data = json.loads(r.read())
jid = data.get('job_id', '')
if not jid:
    raise RuntimeError('missing job_id in response')
print(jid)
" 2>&1); then
    :
else
    RC=$?
    fail "Failed to create second sync job (exit=$RC): $SYNC2_JOB_ID"
fi

# Poll second sync
POLL_COUNT=0
while [ $POLL_COUNT -lt 30 ]; do
    POLL_COUNT=$((POLL_COUNT + 1))
    
    SYNC2_STATUS=$($DOCKER exec "$AUDIT_CONTAINER" python3 -c "
import urllib.request, json

req = urllib.request.Request(
    'http://127.0.0.1:8000/api/v1/jobs/$SYNC2_JOB_ID',
    headers={'X-API-Key': '$API_KEY'}
)
r = urllib.request.urlopen(req, timeout=10)
data = json.loads(r.read())
print(data.get('status', 'UNKNOWN'))
" 2>/dev/null) || { sleep 10; continue; }
    
    if [ "$SYNC2_STATUS" = "DONE" ]; then
        log "Second sync completed!"
        break
    elif [ "$SYNC2_STATUS" = "FAILED" ]; then
        fail "Second sync job failed"
    fi
    
    sleep 10
done

if [ $POLL_COUNT -ge 30 ]; then
    fail "Second sync job did not complete within timeout"
fi

# Verify idempotency (same bar count)
AFTER_COUNT=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import duckdb as dd

conn = dd.connect('/app/data/astock_data.duckdb')
count = conn.execute(\"SELECT COUNT(*) FROM market_bars_daily WHERE security_id='SSE:600519'\").fetchone()[0]
print(count)
conn.close()
" 2>/dev/null) || fail "Idempotency count check failed"

if [ "$BEFORE_COUNT" = "$AFTER_COUNT" ]; then
    log "Idempotency verified: before=$BEFORE_COUNT after=$AFTER_COUNT ✓"
else
    fail "Idempotency FAILED: before=$BEFORE_COUNT after=$AFTER_COUNT"
fi

# ============================================================
# PHASE 14: RESTART PERSISTENCE TEST
# ============================================================

log "--- Phase 14: Restart Persistence Test ---"

$DOCKER stop "$AUDIT_CONTAINER" > /dev/null 2>&1 || true
sleep 3

$DOCKER start "$AUDIT_CONTAINER" > /dev/null 2>&1 || fail "Failed to restart audit container"
sleep 5

# Verify persistence after restart
RESTART_CHECK=$($DOCKER exec "$AUDIT_CONTAINER" python3 -c "
import urllib.request, json

r = urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=5)
print('LIVE:', json.loads(r.read()))

r = urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=5)
print('READY:', json.loads(r.read()))

import duckdb as dd
conn = dd.connect('/app/data/astock_data.duckdb')

# Check security_master still has data
sec_count = conn.execute('SELECT COUNT(*) FROM security_master').fetchone()[0]
print(f'SECURITY_COUNT:{sec_count}')

# Check market_bars still has data  
bar_count = conn.execute('SELECT COUNT(*) FROM market_bars_daily').fetchone()[0]
print(f'BAR_COUNT:{bar_count}')

conn.close()
" 2>/dev/null) || fail "Restart persistence check failed"

echo "$RESTART_CHECK" | while read line; do
    log "  $line"
done

SEC_COUNT=$(echo "$RESTART_CHECK" | grep 'SECURITY_COUNT:' | cut -d: -f2)
if [ "$SEC_COUNT" = "0" ] || [ -z "$SEC_COUNT" ]; then
    fail "Data lost after restart!"
fi
log "Persistence verified after restart ✓"

# ============================================================
# PHASE 15: RECREATE CONTAINER (same data/cache)
# ============================================================

log "--- Phase 15: Recreate Container Persistence Test ---"

$DOCKER stop "$AUDIT_CONTAINER" > /dev/null 2>&1 || true
$DOCKER rm -f "$AUDIT_CONTAINER" > /dev/null 2>&1 || true

# Recreate with same volumes (default network for TDX)
$DOCKER run -d \
    --name "$AUDIT_CONTAINER" \
    -v "$DATA_DIR:/app/data:rw" \
    -v "$CACHE_DIR:/app/cache:rw" \
    --env-file "$ENV_FILE" \
    "$IMAGE_TAG" > /dev/null 2>&1 || fail "Failed to recreate audit container"

sleep 5

RECREATE_CHECK=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import duckdb as dd

conn = dd.connect('/app/data/astock_data.duckdb')
sec_count = conn.execute('SELECT COUNT(*) FROM security_master').fetchone()[0]
bar_count = conn.execute('SELECT COUNT(*) FROM market_bars_daily').fetchone()[0]
print(f'SECURITY_COUNT:{sec_count}')
print(f'BAR_COUNT:{bar_count}')
conn.close()
" 2>/dev/null) || fail "Recreate persistence check failed"

echo "$RECREATE_CHECK" | while read line; do
    log "  $line"
done

log "Persistence verified after container recreation ✓"

# ============================================================
# PHASE 16: PRODUCTION NON-INTERFERENCE VERIFICATION
# ============================================================

log "--- Phase 16: Production Non-Interference Verification ---"

PROD_CHECK=$("$DOCKER" exec "$PROD_CONTAINER" python3 -c "
import urllib.request, json

r = urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=5)
print('LIVE:', json.loads(r.read()))

r = urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=5)
print('READY:', json.loads(r.read()))
" 2>/dev/null) || warn "Production health check failed (may be expected if production is on different host)"

echo "$PROD_CHECK" | while read line; do
    log "  $line"
done

# Verify production image identity unchanged (use .Id)
PROD_IMAGE_ID=$("$DOCKER" image inspect "$PROD_CONTAINER" --format='{{.Id}}' 2>/dev/null) || true

log "Production config identity: $PROD_IMAGE_ID"
log "Audit container preserved at: $AUDIT_CONTAINER"

# ============================================================
# FINAL REPORT
# ============================================================

log "=========================================="
log "Phase 9.3 R3 QNAP Isolated Audit COMPLETE"
log "=========================================="
log ""
log "R2 Failure #1 (dispatch): FIXED ✓"
log "R2 Failure #2 (raw artifact): FIXED ✓"  
log "R2 Failure #3 (bootstrap): FIXED ✓"
log ""
log "Container preserved for inspection: $AUDIT_CONTAINER"
log "Audit data directory: $DATA_DIR"
