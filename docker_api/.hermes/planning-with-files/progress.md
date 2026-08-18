# Progress Log — R5-C1 Retry Exhaustion Terminalization (2026-08-18)

## Summary of All Completed Work
- ✅ Root cause identified: `_try_transition_to_running` 跳过 RUNNING job + `retry_count < MAX_RETRIES` off-by-one
- ✅ `_mark_chunk_exhausted`: 原子单事务终态化 (status=FAILED, retry_count=MAX_RETRIES, next_retry_at=NULL)
- ✅ `_recover_state`: 启动时自动终态化 stranded RETRY chunks (retry_count>=MAX_RETRIES) → FAILED with TRANSIENT_EXHAUSTED
- ✅ `_try_transition_to_running`: 允许 RUNNING job 继续 claim chunk (修复 R4 regression)
- ✅ Boundary fix: `retry_count >= MAX_RETRIES - 1` → exhausted immediately
- ✅ Data mount: named volume `a-stock-data-r5-db` for `/app/data` persistence
- ✅ Stranded recovery integration test: PASS (Job A RUNNING→FAILED, Job B PENDING→DONE)
- ✅ Restart recovery: PASS (状态一致，exhausted chunk 不重新变 RETRY)
- ✅ Recreate recovery: PASS (stop/rm/recreate 后状态一致，_recover_state idempotent)
- ✅ Upstream calls during recovery: 0 (无 mootdx/baidu 请求)
- ✅ Concurrent isolation: PASS (Job B 不受 Job A stranded recovery 影响)
- ✅ Full test collection: 305 tests (62 job_engine + 243 regression)
- ✅ Full test result: 305/305 PASS

## Status: R5-C1_GATE=PASS — 停止，等待用户指令进入 R5-C2
