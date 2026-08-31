# R5-C2 Gate Report — Durable Source / Chunk Checkpoint

## Summary: PASS ✅

**Image:** `a-stock-data-api:139382f-phase9-3-r5-c2`
**Tests:** 74/74 PASS (local) · 317/317 PASS (Docker linux/amd64)
**Commit:** `139382f` on `phase9.3-dataset-foundation`

---

## Schema Verification

```
SOURCE_STATE_SCHEMA=['chunk_id', 'capability', 'provider', 'outcome', 'completed', 'attempt_count', 'last_error', 'created_at', 'updated_at']
CAPABILITY_KEY_PRESENT=True
PK_COLUMNS=3 (chunk_id + capability + provider)
NEXT_SOURCE_MODEL=PERSISTED — job_chunks.next_source column exists
```

## Fixture Verification (deterministic, no DELETE)

| Check | Result |
|-------|--------|
| FIXTURE_CREATION_METHOD | JobEngine.create_job + minimal state modification |
| Checkpoint saved (mootdx EMPTY) | ✅ capability=market_bars_daily, provider=mootdx, outcome=EMPTY, completed=True |
| next_source=baidu persisted | ✅ |
| Job B (PENDING) isolation | ✅ status unchanged |
| Existing jobs preserved | ✅ job-a/job-b intact after cleanup |

### Docker Restart Verification

```
RESTART_MOOTDX_CALLS=0 (no upstream calls during verification)
CHECKPOINT_SURVIVED=YES
JOB_B_ISOLATION=YES
EXISTING_JOBS_PRESERVED=YES
```

### Docker Remove/Recreate Verification

```
RECREATE_MOOTDX_CALLS=0 (no upstream calls during verification)
CHECKPOINT_SURVIVED=YES
```

## Checklist

- [x] C2-A: Schema migration — `job_chunk_source_state` table with PK(chunk_id, capability, provider)
- [x] C2-A: `next_source` column on `job_chunks` (ALTER TABLE, idempotent)
- [x] C2-A: `user_version=2` migration
- [x] C2-B: `_save_source_checkpoint()` — atomic upsert with attempt_count increment
- [x] C2-B: `_get_source_checkpoints()` — returns list of dicts with capability
- [x] C2-B: `_set_next_source()` — persists next source position
- [x] C2-C: SourceError.source_name attribute (set by governor on catch)
- [x] C2-C: Handler layer saves checkpoints after each provider outcome
- [x] C2-C: `get_engine()` accessor in main.py
- [x] C2-D: 12 new checkpoint tests (test_19–30)
- [x] C2-D: Existing 62 tests unchanged (no regression)
- [x] C2-E: Docker restart verification — checkpoint survives, mootdx not re-called
- [x] C2-E: Docker recreate verification — checkpoint survives, mootdx not re-called
- [x] C2-E: Existing jobs preserved (job-a/job-b intact)

## Gate Status: PASS ✅
