#!/usr/bin/env python3
"""R5-C3 Docker verification: restart/recreate crash recovery with real named volume.

Usage:
    # Setup fixture state in DB
    docker run --rm -v a-stock-data-r5-db:/app/data IMAGE python scripts/c3_fixture_verify.py setup

    # Restart verification (container restart)
    docker run --rm -v a-stock-data-r5-db:/app/data IMAGE python scripts/c3_fixture_verify.py restart

    # Recreate verification (stop → rm → recreate)
    docker run --rm -v a-stock-data-r5-db:/app/data IMAGE python scripts/c3_fixture_verify.py recreate

    # Cleanup
    docker run --rm -v a-stock-data-r5-db:/app/data IMAGE python scripts/c3_fixture_verify.py cleanup
"""

import json
import os
import sys
import uuid

# Ensure src is importable
sys.path.insert(0, '/app/src')

from astock_api.job_engine import JobEngine


def setup():
    """Create deterministic fixture jobs with crash states."""
    db_path = os.environ.get('DB_PATH', '/app/data/jobs.db')
    data_dir = os.environ.get('DATA_DIR', '/app/data')

    engine = JobEngine(db_path=db_path, data_dir=data_dir)
    engine.initialize()

    # Job A: partial completion (2 DONE + 2 PENDING)
    job_a = engine.create_job("market_bars_snapshot", {
        "symbols": ["600519", "000001", "600036", "000858"],
        "frequency": "daily",
        "count": 100,
    })

    chunks_a = engine.get_chunks(job_a["job_id"])
    # Mark first 2 as DONE
    for c in chunks_a[:2]:
        engine._mark_chunk_done(c["chunk_id"], job_a["job_id"], "/path/to/result.json")

    # Job B: orphan RUNNING with mootdx EMPTY + next_source=baidu
    job_b = engine.create_job("market_bars_snapshot", {
        "symbols": ["600519"],
        "frequency": "daily",
        "count": 100,
    })

    chunk_b = engine._claim_chunk(job_b["job_id"])
    cid_b = chunk_b["chunk_id"]

    # Save mootdx EMPTY checkpoint + set next_source=baidu
    engine._save_source_checkpoint(cid_b, "mootdx", "EMPTY", completed=True)
    engine._set_next_source(cid_b, "baidu")

    # Set to RUNNING (simulates crash mid-execution)
    conn = engine._get_conn()
    try:
        now = engine._now_iso()
        conn.execute(
            "UPDATE job_chunks SET status='RUNNING', started_at=? WHERE chunk_id=?",
            (now, cid_b)
        )
        conn.commit()
    finally:
        conn.close()

    # Job C: orphan RUNNING with no checkpoint (crash before any source)
    job_c = engine.create_job("market_bars_snapshot", {
        "symbols": ["000001"],
        "frequency": "daily",
        "count": 100,
    })

    chunk_c = engine._claim_chunk(job_c["job_id"])
    conn = engine._get_conn()
    try:
        now = engine._now_iso()
        conn.execute(
            "UPDATE job_chunks SET status='RUNNING', started_at=? WHERE chunk_id=?",
            (now, chunk_c["chunk_id"])
        )
        conn.commit()
    finally:
        conn.close()

    # Job D: terminal DONE job (should never re-execute)
    job_d = engine.create_job("market_bars_snapshot", {
        "symbols": ["600519"],
        "frequency": "daily",
        "count": 100,
    })

    chunk_d = engine._claim_chunk(job_d["job_id"])
    engine._mark_chunk_done(chunk_d["chunk_id"], job_d["job_id"], "/path/to/result.json")

    # Print fixture state
    print(f"SETUP_COMPLETE=YES")
    print(f"JOB_A_ID={job_a['job_id']}")
    print(f"JOB_B_ID={job_b['job_id']}")
    print(f"JOB_C_ID={job_c['job_id']}")
    print(f"JOB_D_ID={job_d['job_id']}")

    # Verify Job A state
    chunks_a_after = engine.get_chunks(job_a["job_id"])
    done_a = sum(1 for c in chunks_a_after if c["status"] == "DONE")
    pending_a = sum(1 for c in chunks_a_after if c["status"] == "PENDING")
    print(f"JOB_A_DONE={done_a}")
    print(f"JOB_A_PENDING={pending_a}")

    # Verify Job B state
    chunks_b_after = engine.get_chunks(job_b["job_id"])
    print(f"JOB_B_STATUS={chunks_b_after[0]['status']}")

    # Verify Job D state
    chunks_d_after = engine.get_chunks(job_d["job_id"])
    print(f"JOB_D_STATUS={chunks_d_after[0]['status']}")


def verify(label):
    """Verify recovery state after restart/recreate."""
    db_path = os.environ.get('DB_PATH', '/app/data/jobs.db')
    data_dir = os.environ.get('DATA_DIR', '/app/data')

    engine = JobEngine(db_path=db_path, data_dir=data_dir)
    engine.initialize()  # This triggers _recover_state()

    jobs = engine.list_jobs()
    print(f"{label}_TOTAL_JOBS={len(jobs)}")

    for job in jobs:
        jid = job["job_id"]
        chunks = engine.get_chunks(jid)

        done_count = sum(1 for c in chunks if c["status"] == "DONE")
        pending_count = sum(1 for c in chunks if c["status"] == "PENDING")
        running_count = sum(1 for c in chunks if c["status"] == "RUNNING")
        failed_count = sum(1 for c in chunks if c["status"] == "FAILED")

        print(f"{label}_JOB_{jid[:8]}_STATUS={job['status']}")
        print(f"{label}_JOB_{jid[:8]}_DONE={done_count}")
        print(f"{label}_JOB_{jid[:8]}_PENDING={pending_count}")
        print(f"{label}_JOB_{jid[:8]}_RUNNING={running_count}")
        print(f"{label}_JOB_{jid[:8]}_FAILED={failed_count}")

    # Check for RUNNING chunks (should be 0 after recovery)
    all_chunks = []
    for job in jobs:
        all_chunks.extend(engine.get_chunks(job["job_id"]))

    running_total = sum(1 for c in all_chunks if c["status"] == "RUNNING")
    print(f"{label}_ORPHAN_RUNNING_RECOVERED={'YES' if running_total == 0 else 'NO'}")

    # Check checkpoint preservation for Job B
    for job in jobs:
        chunks = engine.get_chunks(job["job_id"])
        for c in chunks:
            cps = engine._get_source_checkpoints(c["chunk_id"])
            if len(cps) > 0:
                for cp in cps:
                    print(f"{label}_CHECKPOINT_{c['chunk_id'][:8]}={cp['provider']}:{cp['outcome']}")

    # Verify no RUNNING chunks remain
    if running_total == 0:
        print(f"{label}_RECOVERY=PASS")
    else:
        print(f"{label}_RECOVERY=FAIL (running={running_total})")


def cleanup():
    """Remove c3 fixture jobs."""
    db_path = os.environ.get('DB_PATH', '/app/data/jobs.db')
    data_dir = os.environ.get('DATA_DIR', '/app/data')

    engine = JobEngine(db_path=db_path, data_dir=data_dir)
    engine.initialize()

    jobs = engine.list_jobs()
    for job in jobs:
        jid = job["job_id"]
        chunks = engine.get_chunks(jid)

        conn = engine._get_conn()
        try:
            for c in chunks:
                cid = c["chunk_id"]
                conn.execute("DELETE FROM job_chunk_source_state WHERE chunk_id=?", (cid,))
            conn.execute("DELETE FROM job_chunks WHERE job_id=?", (jid,))
            conn.execute("DELETE FROM jobs WHERE job_id=?", (jid,))
            conn.commit()
        finally:
            conn.close()

    print(f"CLEANUP_COMPLETE=YES")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python c3_fixture_verify.py {setup|restart|recreate|cleanup}")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "setup":
        setup()
    elif cmd == "restart" or cmd == "recreate":
        verify(cmd.upper())
    elif cmd == "cleanup":
        cleanup()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
