# R5-C1 Gate Report — 2026-08-18

ROOT_CAUSE=_try_transition_to_running 跳过 RUNNING job + retry_count < MAX_RETRIES off-by-one
TERMINALIZATION_IMPLEMENTATION=_mark_chunk_exhausted 原子单事务 (status=FAILED, retry_count=MAX_RETRIES, next_retry_at=NULL)
RETRY_INVARIANT=RETRY iff retry_count < MAX_RETRIES; status=FAILED when retry_count >= MAX_RETRIES
DATA_MOUNT=named volume a-stock-data-r5-db for /app/data persistence
STRANDED_RECOVERY=PASS (Job A RUNNING to FAILED, Job B PENDING to DONE)
RESTART_RECOVERY=PASS (状态一致，exhausted chunk 不重新变 RETRY)
RECREATE_RECOVERY=PASS (stop/rm/recreate 后状态一致，_recover_state idempotent)
UPSTREAM_CALLS_DURING_RECOVERY=0 (无 mootdx/baidu 请求)
CONCURRENT_ISOLATION=PASS (Job B 不受 Job A stranded recovery 影响)
FULL_TEST_COLLECTION=305 tests (62 job_engine + 243 regression)
FULL_TEST_RESULT=305/305 PASS
IMAGE=a-stock-data-api:620b507-phase9-3-r5
R5_C1_GATE=PASS
