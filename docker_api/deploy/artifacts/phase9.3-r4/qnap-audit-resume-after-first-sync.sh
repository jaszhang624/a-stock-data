#!/bin/bash
# Phase 9.3 R4 QNAP Audit — Resume after first sync completed
# Continues against the preserved container and runtime data.
# First sync job 1699fd96-d040-442c-a9ba-ee52fb64b2de already DONE.

set -euo pipefail

DOCKER="/share/CACHEDEV2_DATA/.qpkg/container-station/usr/bin/docker"
AUDIT_ROOT="/share/Docker/a-stock-data/audit-phase9.3-r4"
ENV_FILE="/share/Docker/a-stock-data/.env"
AUDIT_CONTAINER="a-stock-data-api-phase9.3-r4-audit"
PROD_CONTAINER="a-stock-data-api"

# R4 identity (verified from frozen OCI artifact)
IMAGE_TAG="a-stock-data-api:281fc69-phase9-3-r4"
EXPECTED_CONFIG_DIGEST="sha256:833e579f4ed0c052d6e26e9de95187e8ceede7b002e060a344164c3f7abfd2ba"

# First sync evidence (preserved from QNAP execution)
FIRST_SYNC_JOB_ID="1699fd96-d040-442c-a9ba-ee52fb64b2de"

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
# PRE-FLIGHT: VERIFY PRESERVED FIRST SYNC STATE
# ============================================================

log "=== Phase 9.3 R4 QNAP Audit Resume (after first sync) ==="
log "Date: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

if [ ! -f "$ENV_FILE" ]; then
    fail "ENV file not found at $ENV_FILE"
fi

# 1. Container is running
if ! "$DOCKER" inspect -f '{{.State.Running}}' "$AUDIT_CONTAINER" 2>/dev/null | grep -q 'true'; then
    fail "Audit container $AUDIT_CONTAINER is not running"
fi
log "Container running ✓"

# Initialize API_KEY from container environment (never print/log it)
if API_KEY=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import os
v = os.environ.get('ASTOCK_API_KEY', '')
if not v:
    raise SystemExit(1)
print(v)
" 2>/dev/null); then
    :
else
    fail "Failed to obtain API key from audit container"
fi

if [ -z "$API_KEY" ]; then
    fail "Audit container API key is empty"
fi

# 2. Image identity — verify real runtime image ID (not just tag)
if CONTAINER_CONFIG=$("$DOCKER" inspect "$AUDIT_CONTAINER" --format='{{.Config.Image}}' 2>/dev/null); then
    log "Container image tag: $CONTAINER_CONFIG"
fi

if CONTAINER_IMAGE_ID=$("$DOCKER" inspect "$AUDIT_CONTAINER" --format='{{.Image}}' 2>/dev/null); then
    if [ "$CONTAINER_IMAGE_ID" != "$EXPECTED_CONFIG_DIGEST" ]; then
        fail "Container image ID mismatch: expected=$EXPECTED_CONFIG_DIGEST actual=$CONTAINER_IMAGE_ID"
    fi
else
    fail "Failed to inspect container image ID"
fi
log "Container image identity verified ✓"

# 3. First job exists and status=DONE
if JOB_STATUS_RAW=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import urllib.request, json

req = urllib.request.Request(
    'http://127.0.0.1:8000/api/v1/jobs/$FIRST_SYNC_JOB_ID',
    headers={'X-API-Key': '$API_KEY'}
)
r = urllib.request.urlopen(req, timeout=10)
data = json.loads(r.read())
print(f'{data.get(\"status\", \"UNKNOWN\")}|{data.get(\"last_error\", \"\")}')
" 2>/dev/null) || fail "Failed to check first sync job status"; then
    STATUS="${JOB_STATUS_RAW%%|*}"
    if [ "$STATUS" != "DONE" ]; then
        fail "First sync job status is $STATUS, expected DONE"
    fi
fi
log "First sync job DONE ✓"

# 4. Exactly one chunk for first job, status=DONE
if CHUNK_CHECK=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import sqlite3, json

conn = sqlite3.connect('/app/data/astock_jobs.db')
chunks = conn.execute('SELECT chunk_id, payload_json, status, retry_count FROM job_chunks WHERE job_id=?', ['$FIRST_SYNC_JOB_ID']).fetchall()
if len(chunks) != 1:
    print(f'ERROR:expected_1_chunk_got_{len(chunks)}')
else:
    cid, payload_json, status, retry = chunks[0]
    payload = json.loads(payload_json)
    print(f'CHUNK_COUNT:{len(chunks)}')
    print(f'CHUNK_STATUS:{status}')
    print(f'CHUNK_RETRY_COUNT:{retry}')
    print(f'CHUNK_JOB_TYPE:{payload.get(\"job_type\", \"\")}')
    print(f'CHUNK_SYMBOL:{payload.get(\"symbol\", \"\")}')
    print(f'CHUNK_JOB_ID:{payload.get(\"job_id\", \"\")}')
    print(f'CHUNK_FREQUENCY:{payload.get(\"frequency\", \"\")}')
    print(f'PAYLOAD_COUNT:{payload.get(\"count\", \"\")}')
conn.close()
" 2>/dev/null) || fail "Failed to inspect first sync chunk"; then
    echo "$CHUNK_CHECK" | while read line; do log "  $line"; done

    if echo "$CHUNK_CHECK" | grep -q 'ERROR'; then
        fail "Chunk count mismatch: $CHUNK_CHECK"
    fi

    CHUNK_STATUS=$(echo "$CHUNK_CHECK" | grep 'CHUNK_STATUS:' | cut -d: -f2)
    if [ "$CHUNK_STATUS" != "DONE" ]; then
        fail "Chunk status is $CHUNK_STATUS, expected DONE"
    fi

    CHUNK_RETRY=$(echo "$CHUNK_CHECK" | grep 'CHUNK_RETRY_COUNT:' | cut -d: -f2)
    if [ "$CHUNK_RETRY" != "0" ]; then
        fail "Chunk retry_count is $CHUNK_RETRY, expected 0"
    fi

    CHUNK_JID=$(echo "$CHUNK_CHECK" | grep 'CHUNK_JOB_ID:' | cut -d: -f2)
    if [ "$CHUNK_JID" != "$FIRST_SYNC_JOB_ID" ]; then
        fail "Chunk payload job_id mismatch: $CHUNK_JID != $FIRST_SYNC_JOB_ID"
    fi

    CHUNK_SYMBOL=$(echo "$CHUNK_CHECK" | grep 'CHUNK_SYMBOL:' | cut -d: -f2)
    if [ "$CHUNK_SYMBOL" != "600519" ]; then
        fail "Chunk payload symbol mismatch: $CHUNK_SYMBOL != 600519"
    fi

    CHUNK_FREQ=$(echo "$CHUNK_CHECK" | grep 'CHUNK_FREQUENCY:' | cut -d: -f2)
    if [ "$CHUNK_FREQ" != "daily" ]; then
        fail "Chunk payload frequency mismatch: $CHUNK_FREQ != daily"
    fi

    PAYLOAD_CNT=$(echo "$CHUNK_CHECK" | grep 'PAYLOAD_COUNT:' | cut -d: -f2)
    if [ "$PAYLOAD_CNT" != "5" ]; then
        fail "Payload count mismatch: $PAYLOAD_CNT != 5"
    fi
fi
log "First sync chunk verified ✓"

# 5. Canonical SSE:600519 row count = 5
if CANONICAL_CHECK=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import duckdb as dd

conn = dd.connect('/app/data/astock_data.duckdb')
count = conn.execute(\"SELECT COUNT(*) FROM market_bars_daily WHERE security_id='SSE:600519'\").fetchone()[0]
print(f'ROWS:{count}')

persisted = conn.execute(\"SELECT DISTINCT job_id FROM market_bars_daily WHERE security_id='SSE:600519' AND job_id != ''\").fetchone()
if persisted:
    print(f'PERSISTED_JOB_ID:{persisted[0]}')
else:
    print('PERSISTED_JOB_ID:EMPTY')

conn.close()
" 2>/dev/null) || fail "Canonical check failed"; then
    echo "$CANONICAL_CHECK" | while read line; do log "  $line"; done

    ROWS=$(echo "$CANONICAL_CHECK" | grep 'ROWS:' | cut -d: -f2)
    if [ "$ROWS" != "5" ]; then
        fail "Canonical SSE:600519 row count is $ROWS, expected 5"
    fi

    PERSISTED_JID=$(echo "$CANONICAL_CHECK" | grep 'PERSISTED_JOB_ID:' | cut -d: -f2)
    if [ "$PERSISTED_JID" != "$FIRST_SYNC_JOB_ID" ]; then
        fail "Persisted job_id mismatch: $PERSISTED_JID != $FIRST_SYNC_JOB_ID"
    fi
fi
log "Canonical data verified ✓"

# ============================================================
# PHASE 13: SECOND SYNC / IDEMPOTENCY TEST
# ============================================================

log "--- Phase 13: Second Sync Idempotency Test ---"

# Save baseline count (derive dynamically)
BEFORE_COUNT=$(echo "$CANONICAL_CHECK" | grep 'ROWS:' | cut -d: -f2)
log "Baseline row count: $BEFORE_COUNT"

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

# Poll for completion (max 5 minutes)
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
            fail "Second sync job FAILED"
        fi
    fi

    sleep 10
    POLL_COUNT=$((POLL_COUNT + 1))
done

if [ $POLL_COUNT -ge 30 ]; then
    fail "Second sync job did not complete within timeout"
fi

# Verify second sync chunk payload contains the second job_id
if CHUNK2_CHECK=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import sqlite3, json

conn = sqlite3.connect('/app/data/astock_jobs.db')
chunks = conn.execute('SELECT payload_json, status, retry_count FROM job_chunks WHERE job_id=?', ['$SYNC2_JOB_ID']).fetchall()
for c in chunks:
    payload = json.loads(c[0])
    print(f'CHUNK_JOB_ID:{payload.get(\"job_id\", \"\")}')
    print(f'CHUNK_STATUS:{c[1]}')
    print(f'CHUNK_RETRY_COUNT:{c[2]}')
conn.close()
" 2>/dev/null) || fail "Failed to inspect second sync chunk"; then
    echo "$CHUNK2_CHECK" | while read line; do log "  $line"; done

    CHUNK2_JID=$(echo "$CHUNK2_CHECK" | grep 'CHUNK_JOB_ID:' | cut -d: -f2)
    if [ "$CHUNK2_JID" != "$SYNC2_JOB_ID" ]; then
        fail "Second sync chunk job_id mismatch: $CHUNK2_JID != $SYNC2_JOB_ID"
    fi

    CHUNK2_RETRY=$(echo "$CHUNK2_CHECK" | grep 'CHUNK_RETRY_COUNT:' | cut -d: -f2)
    if [ "$CHUNK2_RETRY" != "0" ]; then
        fail "Second sync chunk retry_count is $CHUNK2_RETRY, expected 0"
    fi
fi
log "Second sync chunk verified ✓"

# Verify idempotency — row count should not increase
if IDEMPOTENCY_CHECK=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import duckdb as dd

conn = dd.connect('/app/data/astock_data.duckdb')
count = conn.execute(\"SELECT COUNT(*) FROM market_bars_daily WHERE security_id='SSE:600519'\").fetchone()[0]
print(f'AFTER_COUNT:{count}')

empty = conn.execute(\"SELECT COUNT(*) FROM market_bars_daily WHERE security_id='SSE:600519' AND (job_id = '' OR job_id IS NULL)\").fetchone()[0]
print(f'EMPTY_JOB_ID:{empty}')

dupes = conn.execute(\"SELECT COUNT(*) FROM (SELECT security_id, trade_date FROM market_bars_daily WHERE security_id='SSE:600519' GROUP BY security_id, trade_date HAVING COUNT(*) > 1)\").fetchone()[0]
print(f'DUPLICATE_PK_COUNT:{dupes}')

conn.close()
" 2>/dev/null) || fail "Idempotency check failed"; then
    AFTER_COUNT=$(echo "$IDEMPOTENCY_CHECK" | grep 'AFTER_COUNT:' | cut -d: -f2)
    EMPTY_JID=$(echo "$IDEMPOTENCY_CHECK" | grep 'EMPTY_JOB_ID:' | cut -d: -f2)
    DUPLICATE_PK=$(echo "$IDEMPOTENCY_CHECK" | grep 'DUPLICATE_PK_COUNT:' | cut -d: -f2)

    if [ "$BEFORE_COUNT" = "$AFTER_COUNT" ]; then
        log "Idempotency verified: before=$BEFORE_COUNT after=$AFTER_COUNT ✓"
    else
        fail "Idempotency FAILED: before=$BEFORE_COUNT after=$AFTER_COUNT"
    fi

    if [ "$EMPTY_JID" != "0" ]; then
        fail "Found $EMPTY_JID rows with empty job_id after second sync"
    fi

    if [ "$DUPLICATE_PK" != "0" ]; then
        fail "Found $DUPLICATE_PK duplicate primary keys after second sync"
    fi
fi
log "All persisted rows have non-empty job_id ✓"

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
log "R4 AUDIT RESUME (AFTER FIRST SYNC) COMPLETE"
log "============================================================"
log ""
log "Pre-flight: First sync job $FIRST_SYNC_JOB_ID verified ✓"
log "  - Job status DONE, chunk DONE, retry_count=0 ✓"
log "  - Chunk payload: job_id=$FIRST_SYNC_JOB_ID, symbol=600519 ✓"
log "  - Canonical SSE:600519 rows=5, persisted job_id matches ✓"
log "Phase 13: Second sync idempotency verified (row count unchanged) ✓"
log "Phase 14: Restart persistence verified ✓"
log "Phase 15: Recreate persistence verified ✓"
log "Phase 16: Production isolation check complete ✓"
log ""
log "Container preserved at: $AUDIT_CONTAINER"
log "============================================================"
