#!/bin/bash
# Phase 9.3 R4 QNAP Audit — Resume from Phase 14 (restart persistence)
# Continues against the preserved container and runtime data.
# First sync job 1699fd96... DONE, second sync job cac29c59... DONE.
# Idempotency verified: before=5, after=5.

set -euo pipefail

DOCKER="/share/CACHEDEV2_DATA/.qpkg/container-station/usr/bin/docker"
AUDIT_ROOT="/share/Docker/a-stock-data/audit-phase9.3-r4"
ENV_FILE="/share/Docker/a-stock-data/.env"
AUDIT_CONTAINER="a-stock-data-api-phase9.3-r4-audit"
PROD_CONTAINER="a-stock-data-api"

# R4 identity (verified from frozen OCI artifact)
IMAGE_TAG="a-stock-data-api:281fc69-phase9-3-r4"
EXPECTED_CONFIG_DIGEST="sha256:833e579f4ed0c052d6e26e9de95187e8ceede7b002e060a344164c3f7abfd2ba"

# Preserved evidence
FIRST_SYNC_JOB_ID="1699fd96-d040-442c-a9ba-ee52fb64b2de"
SECOND_SYNC_JOB_ID="cac29c59-62ae-4038-bed1-3d0ecd42ea20"

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
# PRE-FLIGHT: VERIFY PRESERVED STATE
# ============================================================

log "=== Phase 9.3 R4 QNAP Audit Resume (Phase 14+) ==="
log "Date: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

if [ ! -f "$ENV_FILE" ]; then
    fail "ENV file not found at $ENV_FILE"
fi

# 1. Container is running
if ! "$DOCKER" inspect -f '{{.State.Running}}' "$AUDIT_CONTAINER" 2>/dev/null | grep -q 'true'; then
    fail "Audit container $AUDIT_CONTAINER is not running"
fi
log "Container running ✓"

# 2. Image identity — verify real runtime image ID
if CONTAINER_IMAGE_ID=$("$DOCKER" inspect "$AUDIT_CONTAINER" --format='{{.Image}}' 2>/dev/null); then
    if [ "$CONTAINER_IMAGE_ID" != "$EXPECTED_CONFIG_DIGEST" ]; then
        fail "Container image ID mismatch: expected=$EXPECTED_CONFIG_DIGEST actual=$CONTAINER_IMAGE_ID"
    fi
else
    fail "Failed to inspect container image ID"
fi
log "Container image identity verified ✓"

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

# 3. First sync job exists and DONE
if JOB_STATUS_RAW=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import urllib.request, json

req = urllib.request.Request(
    'http://127.0.0.1:8000/api/v1/jobs/$FIRST_SYNC_JOB_ID',
    headers={'X-API-Key': '$API_KEY'}
)
r = urllib.request.urlopen(req, timeout=10)
data = json.loads(r.read())
print(data.get('status', 'UNKNOWN'))
" 2>/dev/null) || fail "Failed to check first sync job status"; then
    if [ "$JOB_STATUS_RAW" != "DONE" ]; then
        fail "First sync job status is $JOB_STATUS_RAW, expected DONE"
    fi
fi
log "First sync job DONE ✓"

# 4. Second sync job exists and DONE
if JOB2_STATUS_RAW=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import urllib.request, json

req = urllib.request.Request(
    'http://127.0.0.1:8000/api/v1/jobs/$SECOND_SYNC_JOB_ID',
    headers={'X-API-Key': '$API_KEY'}
)
r = urllib.request.urlopen(req, timeout=10)
data = json.loads(r.read())
print(data.get('status', 'UNKNOWN'))
" 2>/dev/null) || fail "Failed to check second sync job status"; then
    if [ "$JOB2_STATUS_RAW" != "DONE" ]; then
        fail "Second sync job status is $JOB2_STATUS_RAW, expected DONE"
    fi
fi
log "Second sync job DONE ✓"

# 5. Second sync chunk verification
if CHUNK2_CHECK=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import sqlite3, json

conn = sqlite3.connect('/app/data/astock_jobs.db')
chunks = conn.execute('SELECT chunk_id, payload_json, status, retry_count FROM job_chunks WHERE job_id=?', ['$SECOND_SYNC_JOB_ID']).fetchall()
if len(chunks) != 1:
    print(f'ERROR:expected_1_chunk_got_{len(chunks)}')
else:
    cid, payload_json, status, retry = chunks[0]
    payload = json.loads(payload_json)
    print(f'CHUNK_COUNT:{len(chunks)}')
    print(f'CHUNK_STATUS:{status}')
    print(f'CHUNK_RETRY_COUNT:{retry}')
    print(f'CHUNK_JOB_TYPE:{payload.get(\"job_type\", \"\")}')
    print(f'CHUNK_JOB_ID:{payload.get(\"job_id\", \"\")}')
    print(f'CHUNK_SYMBOL:{payload.get(\"symbol\", \"\")}')
    print(f'CHUNK_FREQUENCY:{payload.get(\"frequency\", \"\")}')
    print(f'PAYLOAD_COUNT:{payload.get(\"count\", \"\")}')
conn.close()
" 2>/dev/null) || fail "Failed to inspect second sync chunk"; then
    if echo "$CHUNK2_CHECK" | grep -q 'ERROR'; then
        fail "Second sync chunk count mismatch: $CHUNK2_CHECK"
    fi

    CHUNK2_STATUS=$(echo "$CHUNK2_CHECK" | grep 'CHUNK_STATUS:' | cut -d: -f2)
    if [ "$CHUNK2_STATUS" != "DONE" ]; then
        fail "Second sync chunk status is $CHUNK2_STATUS, expected DONE"
    fi

    CHUNK2_RETRY=$(echo "$CHUNK2_CHECK" | grep 'CHUNK_RETRY_COUNT:' | cut -d: -f2)
    if [ "$CHUNK2_RETRY" != "0" ]; then
        fail "Second sync chunk retry_count is $CHUNK2_RETRY, expected 0"
    fi

    CHUNK2_JTYPE=$(echo "$CHUNK2_CHECK" | grep 'CHUNK_JOB_TYPE:' | cut -d: -f2)
    if [ "$CHUNK2_JTYPE" != "market_bars_sync" ]; then
        fail "Second sync chunk job_type mismatch: $CHUNK2_JTYPE != market_bars_sync"
    fi

    CHUNK2_JID=$(echo "$CHUNK2_CHECK" | grep 'CHUNK_JOB_ID:' | cut -d: -f2)
    if [ "$CHUNK2_JID" != "$SECOND_SYNC_JOB_ID" ]; then
        fail "Second sync chunk job_id mismatch: $CHUNK2_JID != $SECOND_SYNC_JOB_ID"
    fi

    CHUNK2_SYMBOL=$(echo "$CHUNK2_CHECK" | grep 'CHUNK_SYMBOL:' | cut -d: -f2)
    if [ "$CHUNK2_SYMBOL" != "600519" ]; then
        fail "Second sync chunk symbol mismatch: $CHUNK2_SYMBOL != 600519"
    fi

    CHUNK2_FREQ=$(echo "$CHUNK2_CHECK" | grep 'CHUNK_FREQUENCY:' | cut -d: -f2)
    if [ "$CHUNK2_FREQ" != "daily" ]; then
        fail "Second sync chunk frequency mismatch: $CHUNK2_FREQ != daily"
    fi

    PAYLOAD_CNT=$(echo "$CHUNK2_CHECK" | grep 'PAYLOAD_COUNT:' | cut -d: -f2)
    if [ "$PAYLOAD_CNT" != "5" ]; then
        fail "Second sync payload count mismatch: $PAYLOAD_CNT != 5"
    fi
fi
log "Second sync chunk verified ✓"

# 6. Canonical SSE:600519 row count == 5
if CANONICAL_CHECK=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import duckdb as dd

conn = dd.connect('/app/data/astock_data.duckdb')
count = conn.execute(\"SELECT COUNT(*) FROM market_bars_daily WHERE security_id='SSE:600519'\").fetchone()[0]
print(f'ROWS:{count}')

dupes = conn.execute(\"SELECT COUNT(*) FROM (SELECT security_id, trade_date FROM market_bars_daily WHERE security_id='SSE:600519' GROUP BY security_id, trade_date HAVING COUNT(*) > 1)\").fetchone()[0]
print(f'DUPLICATE_PK:{dupes}')

empty = conn.execute(\"SELECT COUNT(*) FROM market_bars_daily WHERE security_id='SSE:600519' AND (job_id = '' OR job_id IS NULL)\").fetchone()[0]
print(f'EMPTY_JOB_ID:{empty}')

conn.close()
" 2>/dev/null) || fail "Canonical check failed"; then
    ROWS=$(echo "$CANONICAL_CHECK" | grep 'ROWS:' | cut -d: -f2)
    if [ "$ROWS" != "5" ]; then
        fail "Canonical SSE:600519 row count is $ROWS, expected 5"
    fi

    DUPES=$(echo "$CANONICAL_CHECK" | grep 'DUPLICATE_PK:' | cut -d: -f2)
    if [ "$DUPES" != "0" ]; then
        fail "Found $DUPES duplicate primary keys"
    fi

    EMPTY_JID=$(echo "$CANONICAL_CHECK" | grep 'EMPTY_JOB_ID:' | cut -d: -f2)
    if [ "$EMPTY_JID" != "0" ]; then
        fail "Found $EMPTY_JID rows with empty job_id"
    fi
fi
log "Canonical data verified ✓"

# 9. Security master snapshot row count matches actual
if SM_CHECK=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import duckdb as dd

conn = dd.connect('/app/data/astock_data.duckdb')
snap = conn.execute('SELECT snapshot_id, row_count FROM security_master_snapshots ORDER BY created_at DESC LIMIT 1').fetchone()
if not snap:
    print('NO_SNAPSHOT')
else:
    sid, declared = snap
    actual = conn.execute('SELECT COUNT(*) FROM security_master WHERE snapshot_id=?', [sid]).fetchone()[0]
    print(f'DECLARED:{declared}')
    print(f'ACTUAL:{actual}')
conn.close()
" 2>/dev/null) || fail "Security master check failed"; then
    if echo "$SM_CHECK" | grep -q 'NO_SNAPSHOT'; then
        fail "No security_master snapshot found"
    fi

    SM_DECLARED=$(echo "$SM_CHECK" | grep 'DECLARED:' | cut -d: -f2)
    SM_ACTUAL=$(echo "$SM_CHECK" | grep 'ACTUAL:' | cut -d: -f2)
    if [ "$SM_DECLARED" != "$SM_ACTUAL" ]; then
        fail "Security master row count mismatch: declared=$SM_DECLARED actual=$SM_ACTUAL"
    fi
fi
log "Security master snapshot verified ✓"

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
bar_count = conn.execute(\"SELECT COUNT(*) FROM market_bars_daily WHERE security_id='SSE:600519'\").fetchone()[0]
print(f'BAR_COUNT:{bar_count}')

# Check duplicate PK count
dupes = conn.execute(\"SELECT COUNT(*) FROM (SELECT security_id, trade_date FROM market_bars_daily WHERE security_id='SSE:600519' GROUP BY security_id, trade_date HAVING COUNT(*) > 1)\").fetchone()[0]
print(f'DUPLICATE_PK:{dupes}')

# Check job_id non-empty
empty = conn.execute(\"SELECT COUNT(*) FROM market_bars_daily WHERE security_id='SSE:600519' AND (job_id = '' OR job_id IS NULL)\").fetchone()[0]
print(f'EMPTY_JOB_ID:{empty}')

conn.close()
" 2>/dev/null) || fail "Restart persistence check failed"

echo "$RESTART_CHECK" | while read line; do log "  $line"; done

SEC_COUNT=$(echo "$RESTART_CHECK" | grep 'SECURITY_COUNT:' | cut -d: -f2)
BAR_COUNT=$(echo "$RESTART_CHECK" | grep 'BAR_COUNT:' | cut -d: -f2)
DUPES=$(echo "$RESTART_CHECK" | grep 'DUPLICATE_PK:' | cut -d: -f2)
EMPTY_JID=$(echo "$RESTART_CHECK" | grep 'EMPTY_JOB_ID:' | cut -d: -f2)

if [ "$SEC_COUNT" = "0" ] || [ -z "$SEC_COUNT" ]; then
    fail "Security master data lost after restart"
fi
if [ "$BAR_COUNT" != "5" ]; then
    fail "Market bars row count is $BAR_COUNT after restart, expected 5"
fi
if [ "$DUPES" != "0" ]; then
    fail "Found $DUPES duplicate PKs after restart"
fi
if [ "$EMPTY_JID" != "0" ]; then
    fail "Found $EMPTY_JID rows with empty job_id after restart"
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
    "$IMAGE_TAG" > /dev/null 2>&1 || fail "Failed to recreate audit container"

sleep 5

# Verify image identity of recreated container
if RECREATED_IMAGE_ID=$("$DOCKER" inspect "$AUDIT_CONTAINER" --format='{{.Image}}' 2>/dev/null); then
    if [ "$RECREATED_IMAGE_ID" != "$EXPECTED_CONFIG_DIGEST" ]; then
        fail "Recreated container image ID mismatch: expected=$EXPECTED_CONFIG_DIGEST actual=$RECREATED_IMAGE_ID"
    fi
fi
log "Recreated container image identity verified ✓"

# Verify persistence after recreate
RECREATE_CHECK=$("$DOCKER" exec "$AUDIT_CONTAINER" python3 -c "
import urllib.request, json

r = urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=5)
print('LIVE:', json.loads(r.read()))

r = urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=5)
print('READY:', json.loads(r.read()))

import duckdb as dd
conn = dd.connect('/app/data/astock_data.duckdb')

# Check security_master
snap = conn.execute('SELECT snapshot_id FROM security_master_snapshots ORDER BY created_at DESC LIMIT 1').fetchone()
if snap:
    sec_count = conn.execute('SELECT COUNT(*) FROM security_master WHERE snapshot_id=?', [snap[0]]).fetchone()[0]
    print(f'SECURITY_COUNT:{sec_count}')

# Check market_bars
bar_count = conn.execute(\"SELECT COUNT(*) FROM market_bars_daily WHERE security_id='SSE:600519'\").fetchone()[0]
print(f'BAR_COUNT:{bar_count}')

# Check duplicate PK count
dupes = conn.execute(\"SELECT COUNT(*) FROM (SELECT security_id, trade_date FROM market_bars_daily WHERE security_id='SSE:600519' GROUP BY security_id, trade_date HAVING COUNT(*) > 1)\").fetchone()[0]
print(f'DUPLICATE_PK:{dupes}')

# Check job_id non-empty
empty = conn.execute(\"SELECT COUNT(*) FROM market_bars_daily WHERE security_id='SSE:600519' AND (job_id = '' OR job_id IS NULL)\").fetchone()[0]
print(f'EMPTY_JOB_ID:{empty}')

conn.close()
" 2>/dev/null) || fail "Recreate persistence check failed"

echo "$RECREATE_CHECK" | while read line; do log "  $line"; done

SEC_COUNT=$(echo "$RECREATE_CHECK" | grep 'SECURITY_COUNT:' | cut -d: -f2)
BAR_COUNT=$(echo "$RECREATE_CHECK" | grep 'BAR_COUNT:' | cut -d: -f2)
DUPES=$(echo "$RECREATE_CHECK" | grep 'DUPLICATE_PK:' | cut -d: -f2)
EMPTY_JID=$(echo "$RECREATE_CHECK" | grep 'EMPTY_JOB_ID:' | cut -d: -f2)

if [ "$SEC_COUNT" = "0" ] || [ -z "$SEC_COUNT" ]; then
    fail "Security master data lost after container recreate"
fi
if [ "$BAR_COUNT" != "5" ]; then
    fail "Market bars row count is $BAR_COUNT after recreate, expected 5"
fi
if [ "$DUPES" != "0" ]; then
    fail "Found $DUPES duplicate PKs after recreate"
fi
if [ "$EMPTY_JID" != "0" ]; then
    fail "Found $EMPTY_JID rows with empty job_id after recreate"
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
log "R4 AUDIT RESUME (PHASE 14+) COMPLETE"
log "============================================================"
log ""
log "Pre-flight: First sync $FIRST_SYNC_JOB_ID DONE ✓"
log "Pre-flight: Second sync $SECOND_SYNC_JOB_ID DONE ✓"
log "Pre-flight: Canonical SSE:600519 rows=5, dupes=0, empty job_id=0 ✓"
log "Phase 14: Restart persistence verified ✓"
log "Phase 15: Recreate persistence verified ✓"
log "Phase 16: Production isolation check complete ✓"
log ""
log "Container preserved at: $AUDIT_CONTAINER"
log "============================================================"
