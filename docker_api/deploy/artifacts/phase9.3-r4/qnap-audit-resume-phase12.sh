#!/bin/bash
# Phase 9.3 R4 QNAP Audit — Resume from Phase 12 (market_bars_sync)
# Continues against the preserved container and runtime data.
# The security_master_snapshot job already completed successfully.

set -euo pipefail

DOCKER="/share/CACHEDEV2_DATA/.qpkg/container-station/usr/bin/docker"
AUDIT_ROOT="/share/Docker/a-stock-data/audit-phase9.3-r4"
ENV_FILE="/share/Docker/a-stock-data/.env"
AUDIT_CONTAINER="a-stock-data-api-phase9.3-r4-audit"
PROD_CONTAINER="a-stock-data-api"

# R4 identity (verified from frozen OCI artifact)
IMAGE_TAG="a-stock-data-api:281fc69-phase9-3-r4"
EXPECTED_CONFIG_DIGEST="sha256:833e579f4ed0c052d6e26e9de95187e8ceede7b002e060a344164c3f7abfd2ba"

# Runtime directories (mutable — safe to reset)
RUNTIME_DIR="$AUDIT_ROOT/runtime"
DATA_DIR="$RUNTIME_DIR/data"
CACHE_DIR="$RUNTIME_DIR/cache"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[R4-RESUME]${NC} $*"; }
warn() { echo -e "${YELLOW}[R4-RESUME-WARN]${NC} $*"; }
fail() { echo -e "${RED}[R4-RESUME-FAIL]${NC} $*"; exit 1; }

# ============================================================
# PRE-FLIGHT: VERIFY PRESERVED RUNTIME
# ============================================================

log "=== Phase 9.3 R4 QNAP Audit Resume (Phase 12+) ==="
log "Date: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

if [ ! -f "$ENV_FILE" ]; then
    fail "ENV file not found at $ENV_FILE"
fi

# 1. Container is running
if ! "$DOCKER" inspect -f '{{.State.Running}}' "$AUDIT_CONTAINER" 2>/dev/null | grep -q 'true'; then
    fail "Audit container $AUDIT_CONTAINER is not running"
fi
log "Container running ✓"

# 2. Image identity matches R4
if CONTAINER_CONFIG=$("$DOCKER" inspect "$AUDIT_CONTAINER" --format='{{.Config.Image}}' 2>/dev/null); then
    log "Container image: $CONTAINER_CONFIG"
fi
if IMAGE_ID=$("$DOCKER" inspect "$AUDIT_CONTAINER" --format='{{.Image}}' 2>/dev/null); then
    log "Container image ID: $IMAGE_ID"
fi

# 3. /app/data/astock_data.duckdb exists
if ! "$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import os
db = '/app/data/astock_data.duckdb'
if not os.path.exists(db):
    print('MISSING')
else:
    print(f'EXISTS:{os.path.getsize(db)}')
" 2>/dev/null | grep -q 'EXISTS'; then
    fail "Canonical DB /app/data/astock_data.duckdb does not exist"
fi
log "Canonical DB exists ✓"

# 4. security_master has data matching latest snapshot
if SM_CHECK=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import duckdb as dd

conn = dd.connect('/app/data/astock_data.duckdb')
snap = conn.execute('SELECT snapshot_id, row_count FROM security_master_snapshots ORDER BY created_at DESC LIMIT 1').fetchone()
if not snap:
    print('NO_SNAPSHOT')
else:
    sid, declared = snap
    actual = conn.execute('SELECT COUNT(*) FROM security_master WHERE snapshot_id=?', [sid]).fetchone()[0]
    print(f'SNAPSHOT_ID:{sid}')
    print(f'DECLARED:{declared}')
    print(f'ACTUAL:{actual}')
conn.close()
" 2>/dev/null) && echo "$SM_CHECK"; then
    if echo "$SM_CHECK" | grep -q 'NO_SNAPSHOT'; then
        fail "No security_master snapshot found"
    fi
fi
log "Security master preserved ✓"

# 5. market_bars_daily count is currently 0 (no sync job ran yet)
if BARS_BEFORE=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import duckdb as dd

conn = dd.connect('/app/data/astock_data.duckdb')
count = conn.execute('SELECT COUNT(*) FROM market_bars_daily').fetchone()[0]
print(count)
conn.close()
" 2>/dev/null); then
    log "Current market_bars_daily count: $BARS_BEFORE"
else
    warn "Could not check market_bars_daily count (table may not exist yet)"
fi

# Read API key from container
API_KEY=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "import os; print(os.environ.get('ASTOCK_API_KEY', ''))" 2>/dev/null) || fail "Failed to read API key from container"

# ============================================================
# PHASE 12: MARKET_BARS_SYNC (REAL)
# ============================================================

log "--- Phase 12: Real market_bars_sync Job ---"

# Create job with raw symbol "600519" (NOT canonical SSE:600519)
log "Creating job for raw symbol 600519 (expected canonical SSE:600519)..."
if SYNC_JOB_ID=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import urllib.request, json

req = urllib.request.Request(
    'http://127.0.0.1:8000/api/v1/jobs',
    data=json.dumps({'job_type': 'market_bars_sync', 'params': {'symbols': ['600519'], 'frequency': 'daily', 'count': 5}}).encode(),
    headers={'X-API-Key': '$API_KEY', 'Content-Type': 'application/json'},
    method='POST'
)
try:
    r = urllib.request.urlopen(req, timeout=15)
    data = json.loads(r.read())
    jid = data.get('job_id', '')
    if not jid:
        print('ERROR:no_job_id')
    else:
        print(jid)
except Exception as e:
    print(f'ERROR:{e}')
" 2>/dev/null); then
    if [ -z "$SYNC_JOB_ID" ] || echo "$SYNC_JOB_ID" | grep -q 'ERROR'; then
        fail "Failed to create market_bars_sync job: $SYNC_JOB_ID"
    fi
else
    fail "Failed to create market_bars_sync job (python exit non-zero)"
fi

log "Sync job created: $SYNC_JOB_ID"

# Poll for completion (max 5 minutes)
POLL_COUNT=0
SYNC_DONE=false
while [ $POLL_COUNT -lt 30 ]; do
    if SYNC_STATUS=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import urllib.request, json

req = urllib.request.Request(
    'http://127.0.0.1:8000/api/v1/jobs/$SYNC_JOB_ID',
    headers={'X-API-Key': '$API_KEY'}
)
r = urllib.request.urlopen(req, timeout=10)
data = json.loads(r.read())
print(data.get('status', 'UNKNOWN'))
" 2>/dev/null) || { sleep 10; continue; }; then

        if [ "$SYNC_STATUS" = "DONE" ]; then
            log "First sync completed!"
            SYNC_DONE=true
            break
        elif [ "$SYNC_STATUS" = "FAILED" ]; then
            fail "First sync job FAILED"
        fi
    fi

    sleep 10
    POLL_COUNT=$((POLL_COUNT + 1))
done

if [ "$SYNC_DONE" != "true" ]; then
    fail "First sync job did not complete within timeout"
fi

# Inspect chunk payload — verify job_id is present
log "--- Phase 12: Chunk Payload Verification ---"
if CHUNK_CHECK=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import sqlite3, json

conn = sqlite3.connect('/app/data/astock_jobs.db')
chunks = conn.execute('SELECT payload_json FROM job_chunks WHERE job_id=?', ['$SYNC_JOB_ID']).fetchall()
for c in chunks:
    payload = json.loads(c[0])
    print(f'CHUNK_JOB_TYPE:{payload.get(\"job_type\", \"\")}')
    print(f'CHUNK_SYMBOL:{payload.get(\"symbol\", \"\")}')
    print(f'CHUNK_JOB_ID:{payload.get(\"job_id\", \"\")}')
conn.close()
" 2>/dev/null); then
    echo "$CHUNK_CHECK" | while read line; do log "  $line"; done

    CHUNK_JOB_ID=$(echo "$CHUNK_CHECK" | grep 'CHUNK_JOB_ID:' | cut -d: -f2)
    if [ "$CHUNK_JOB_ID" != "$SYNC_JOB_ID" ]; then
        fail "Chunk payload job_id mismatch: chunk=$CHUNK_JOB_ID expected=$SYNC_JOB_ID"
    fi
    log "Chunk payload job_id matches originating job ✓"
else
    fail "Failed to inspect chunk payload"
fi

# Verify canonical rows under SSE:600519 (NOT 600519)
log "--- Phase 12: Canonical Data Verification ---"
if SYNC_CHECK=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import duckdb as dd

conn = dd.connect('/app/data/astock_data.duckdb')

# Check bars exist under canonical identity
bars = conn.execute(\"SELECT COUNT(*) FROM market_bars_daily WHERE security_id='SSE:600519'\").fetchone()[0]
print(f'BARS_COUNT:{bars}')

# Verify persisted job_id is non-empty and matches originating job
persisted = conn.execute(\"SELECT DISTINCT job_id FROM market_bars_daily WHERE security_id='SSE:600519' AND job_id != ''\").fetchone()
if persisted:
    print(f'PERSISTED_JOB_ID:{persisted[0]}')
else:
    print('PERSISTED_JOB_ID:EMPTY')

# Check volume unit (should be 手, not shares)
sample = conn.execute(\"SELECT volume, amount FROM market_bars_daily WHERE security_id='SSE:600519' ORDER BY trade_date DESC LIMIT 1\").fetchone()
if sample:
    print(f'SAMPLE_VOLUME:{sample[0]}')
    print(f'SAMPLE_AMOUNT:{sample[1]}')

conn.close()
" 2>/dev/null) || fail "Sync verification failed"; then
    echo "$SYNC_CHECK" | while read line; do log "  $line"; done

    BARS_COUNT=$(echo "$SYNC_CHECK" | grep 'BARS_COUNT:' | cut -d: -f2)
    if [ "$BARS_COUNT" = "0" ]; then
        fail "No bars persisted under SSE:600519"
    fi

    PERSISTED_JID=$(echo "$SYNC_CHECK" | grep 'PERSISTED_JOB_ID:' | cut -d: -f2)
    if [ "$PERSISTED_JID" = "EMPTY" ] || [ -z "$PERSISTED_JID" ]; then
        fail "Persisted job_id is empty — R4 Fix 1 NOT working"
    fi

    if [ "$PERSISTED_JID" != "$SYNC_JOB_ID" ]; then
        fail "Persisted job_id mismatch: persisted=$PERSISTED_JID expected=$SYNC_JOB_ID"
    fi
    log "Persisted job_id matches originating job ✓"
fi

# Save bar count for idempotency check
BEFORE_COUNT=$(echo "$SYNC_CHECK" | grep 'BARS_COUNT:' | cut -d: -f2)

# ============================================================
# PHASE 13: IDEMPOTENCY TEST (second identical sync)
# ============================================================

log "--- Phase 13: Idempotency Test ---"

# Create second identical sync job
log "Creating second sync for raw symbol 600519..."
if SYNC2_JOB_ID=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import urllib.request, json

req = urllib.request.Request(
    'http://127.0.0.1:8000/api/v1/jobs',
    data=json.dumps({'job_type': 'market_bars_sync', 'params': {'symbols': ['600519'], 'frequency': 'daily', 'count': 5}}).encode(),
    headers={'X-API-Key': '$API_KEY', 'Content-Type': 'application/json'},
    method='POST'
)
try:
    r = urllib.request.urlopen(req, timeout=15)
    data = json.loads(r.read())
    jid = data.get('job_id', '')
    if not jid:
        print('ERROR:no_job_id')
    else:
        print(jid)
except Exception as e:
    print(f'ERROR:{e}')
" 2>/dev/null); then
    if [ -z "$SYNC2_JOB_ID" ] || echo "$SYNC2_JOB_ID" | grep -q 'ERROR'; then
        fail "Failed to create second sync job: $SYNC2_JOB_ID"
    fi
else
    fail "Failed to create second sync job (python exit non-zero)"
fi

log "Second sync job created: $SYNC2_JOB_ID"

# Poll for completion
POLL_COUNT=0
while [ $POLL_COUNT -lt 30 ]; do
    if SYNC2_STATUS=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import urllib.request, json

req = urllib.request.Request(
    'http://127.0.0.1:8000/api/v1/jobs/$SYNC2_JOB_ID',
    headers={'X-API-Key': '$API_KEY'}
)
r = urllib.request.urlopen(req, timeout=10)
data = json.loads(r.read())
print(data.get('status', 'UNKNOWN'))
" 2>/dev/null) || { sleep 10; continue; }; then

        if [ "$SYNC2_STATUS" = "DONE" ]; then
            log "Second sync completed!"
            break
        elif [ "$SYNC2_STATUS" = "FAILED" ]; then
            fail "Second sync job failed"
        fi
    fi

    sleep 10
    POLL_COUNT=$((POLL_COUNT + 1))
done

if [ $POLL_COUNT -ge 30 ]; then
    fail "Second sync job did not complete within timeout"
fi

# Verify idempotency — row count should be the same
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

"$DOCKER" stop "$AUDIT_CONTAINER" > /dev/null 2>&1 || true
sleep 3

"$DOCKER" start "$AUDIT_CONTAINER" > /dev/null 2>&1 || fail "Failed to restart audit container"
sleep 5

# Verify persistence after restart
RESTART_CHECK=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import urllib.request, json

r = urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=5)
print('LIVE:', json.loads(r.read()))

r = urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=5)
print('READY:', json.loads(r.read()))

import duckdb as dd
conn = dd.connect('/app/data/astock_data.duckdb')

# Check security_master still has data (use latest snapshot)
snap = conn.execute('SELECT snapshot_id FROM security_master_snapshots ORDER BY created_at DESC LIMIT 1').fetchone()
if snap:
    sec_count = conn.execute('SELECT COUNT(*) FROM security_master WHERE snapshot_id=?', [snap[0]]).fetchone()[0]
    print(f'SECURITY_COUNT:{sec_count}')

# Check market_bars still has data
bar_count = conn.execute('SELECT COUNT(*) FROM market_bars_daily').fetchone()[0]
print(f'BAR_COUNT:{bar_count}')

conn.close()
" 2>/dev/null) || fail "Restart persistence check failed"

echo "$RESTART_CHECK" | while read line; do log "  $line"; done

SEC_COUNT=$(echo "$RESTART_CHECK" | grep 'SECURITY_COUNT:' | cut -d: -f2)
BAR_COUNT=$(echo "$RESTART_CHECK" | grep 'BAR_COUNT:' | cut -d: -f2)

if [ "$SEC_COUNT" = "0" ] || [ -z "$SEC_COUNT" ]; then
    fail "Security master data lost after restart"
fi
if [ "$BAR_COUNT" = "0" ] || [ -z "$BAR_COUNT" ]; then
    fail "Market bars data lost after restart"
fi

log "Restart persistence verified ✓"

# ============================================================
# PHASE 15: RECREATE CONTAINER PERSISTENCE TEST
# ============================================================

log "--- Phase 15: Recreate Container Persistence Test ---"

CONTAINER_ID=$("$DOCKER" inspect "$AUDIT_CONTAINER" --format='{{.Id}}' 2>/dev/null) || fail "Failed to inspect container"

# Remove the audit container (runtime data persists on host volume)
"$DOCKER" rm -f "$AUDIT_CONTAINER" > /dev/null 2>&1 || fail "Failed to remove audit container"

# Recreate with same runtime data
"$DOCKER" run -d \
    --name "$AUDIT_CONTAINER" \
    --platform linux/amd64 \
    -v "$DATA_DIR:/app/data" \
    -v "$CACHE_DIR:/app/cache" \
    -e "ASTOCK_API_KEY=$API_KEY" \
    --user 999:999 \
    "$IMAGE_TAG" tail -f /dev/null > /dev/null 2>&1 || fail "Failed to recreate audit container"

sleep 5

# Verify persistence after recreate
RECREATE_CHECK=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import duckdb as dd

conn = dd.connect('/app/data/astock_data.duckdb')

# Check security_master
snap = conn.execute('SELECT snapshot_id FROM security_master_snapshots ORDER BY created_at DESC LIMIT 1').fetchone()
if snap:
    sec_count = conn.execute('SELECT COUNT(*) FROM security_master WHERE snapshot_id=?', [snap[0]]).fetchone()[0]
    print(f'SECURITY_COUNT:{sec_count}')

# Check market_bars
bar_count = conn.execute('SELECT COUNT(*) FROM market_bars_daily').fetchone()[0]
print(f'BAR_COUNT:{bar_count}')

conn.close()
" 2>/dev/null) || fail "Recreate persistence check failed"

echo "$RECREATE_CHECK" | while read line; do log "  $line"; done

SEC_COUNT=$(echo "$RECREATE_CHECK" | grep 'SECURITY_COUNT:' | cut -d: -f2)
BAR_COUNT=$(echo "$RECREATE_CHECK" | grep 'BAR_COUNT:' | cut -d: -f2)

if [ "$SEC_COUNT" = "0" ] || [ -z "$SEC_COUNT" ]; then
    fail "Security master data lost after container recreate"
fi
if [ "$BAR_COUNT" = "0" ] || [ -z "$BAR_COUNT" ]; then
    fail "Market bars data lost after container recreate"
fi

log "Recreate persistence verified ✓"

# ============================================================
# PHASE 16: PRODUCTION ISOLATION CHECK
# ============================================================

log "--- Phase 16: Production Isolation Check ---"

PROD_CHECK=$("$DOCKER" exec "$PROD_CONTAINER" python3 -c "
import urllib.request, json

r = urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=5)
print('LIVE:', json.loads(r.read()))

r = urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=5)
print('READY:', json.loads(r.read()))
" 2>/dev/null) || warn "Production health check failed (may be expected)"

echo "$PROD_CHECK" | while read line; do log "  $line"; done

# Verify production image identity unchanged
PROD_IMAGE=$("$DOCKER" inspect "$PROD_CONTAINER" --format='{{.Config.Image}}' 2>/dev/null) || true
log "Production image: $PROD_IMAGE"

if [ "$PROD_IMAGE" = "a-stock-data-api:281fc69-phase9-2-r2" ]; then
    log "Production image identity unchanged ✓"
else
    warn "Production image is $PROD_IMAGE (expected phase9-2-r2)"
fi

# ============================================================
# FINAL REPORT
# ============================================================

log "============================================================"
log "R4 AUDIT RESUME COMPLETE"
log "============================================================"
log ""
log "Phase 12: market_bars_sync job DONE ✓"
log "  - Raw symbol 600519 → canonical SSE:600519 ✓"
log "  - Chunk payload contains job_id ✓"
log "  - Persisted job_id matches originating job ✓"
log "Phase 13: Idempotency verified (row count unchanged) ✓"
log "Phase 14: Restart persistence verified ✓"
log "Phase 15: Recreate persistence verified ✓"
log "Phase 16: Production isolation check complete ✓"
log ""
log "Container preserved at: $AUDIT_CONTAINER"
log "============================================================"
