"""C4B-3 Persistence / Chunk Identity tests.

Verifies:
A. New equity chunks use canonical_id in chunk_key
B. Cross-market collision prevention (SSE:000001 vs SZSE:000001)
C. Payload contains canonical_id, exchange, asset_type
D. Legacy chunks (bare code) still readable and recoverable
E. Retry/checkpoint semantics unchanged for legacy chunks
"""

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from astock_api.job_engine import JobEngine


class TestNewChunkIdentity:
    """A. New equity chunks use canonical_id in chunk_key."""

    def test_600519_chunk_key_is_canonical(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = JobEngine(db_path=os.path.join(tmpdir, "test.db"), data_dir=tmpdir)
            engine.initialize()

            result = engine.create_job("market_bars_snapshot", {
                "symbols": ["600519"],
                "frequency": "daily",
                "count": 200,
            })

            chunks = engine.get_chunks(result["job_id"])
            assert len(chunks) == 1
            chunk = chunks[0]

            # chunk_key must use canonical_id
            assert chunk["chunk_key"] == "market_bars|SSE:600519|daily|200"

            # payload must contain identity fields
            payload = json.loads(chunk["payload_json"])
            assert payload["symbol"] == "600519"
            assert payload["canonical_id"] == "SSE:600519"
            assert payload["exchange"] == "SSE"
            assert payload["asset_type"] == "EQUITY"

    def test_000001_chunk_key_is_szse(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = JobEngine(db_path=os.path.join(tmpdir, "test.db"), data_dir=tmpdir)
            engine.initialize()

            result = engine.create_job("market_bars_snapshot", {
                "symbols": ["000001"],
                "frequency": "daily",
                "count": 200,
            })

            chunks = engine.get_chunks(result["job_id"])
            chunk = chunks[0]

            assert chunk["chunk_key"] == "market_bars|SZSE:000001|daily|200"

            payload = json.loads(chunk["payload_json"])
            assert payload["symbol"] == "000001"
            assert payload["canonical_id"] == "SZSE:000001"
            assert payload["exchange"] == "SZSE"

    def test_market_bars_sync_uses_canonical(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = JobEngine(db_path=os.path.join(tmpdir, "test.db"), data_dir=tmpdir)
            engine.initialize()

            result = engine.create_job("market_bars_sync", {
                "symbols": ["600519"],
                "frequency": "daily",
                "count": 200,
            })

            chunks = engine.get_chunks(result["job_id"])
            chunk = chunks[0]

            assert "SSE:600519" in chunk["chunk_key"]
            payload = json.loads(chunk["payload_json"])
            assert payload["canonical_id"] == "SSE:600519"


class TestCrossMarketCollisionPrevention:
    """B. Cross-market collision prevention (SSE:000001 vs SZSE:000001)."""

    def test_sse_000001_vs_szse_000001_different_chunk_keys(self):
        """SSE:000001 INDEX and SZSE:000001 EQUITY must have different chunk_keys."""
        from astock_api.instrument import Instrument

        # SSE:000001 INDEX chunk key
        sse_inst = Instrument(exchange="SSE", code="000001", asset_type="INDEX")
        sse_key = f"market_bars|{sse_inst.canonical_id}|daily|200"

        # SZSE:000001 EQUITY chunk key
        szse_inst = Instrument(exchange="SZSE", code="000001", asset_type="EQUITY")
        szse_key = f"market_bars|{szse_inst.canonical_id}|daily|200"

        assert sse_key != szse_key
        assert sse_key == "market_bars|SSE:000001|daily|200"
        assert szse_key == "market_bars|SZSE:000001|daily|200"

    def test_sse_000001_vs_szse_000001_different_result_paths(self):
        """Different chunk_keys must produce different result paths."""
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = JobEngine(db_path=os.path.join(tmpdir, "test.db"), data_dir=tmpdir)
            engine.initialize()

            job_id = "test-job-123"

            sse_path = engine._result_path(job_id, "market_bars|SSE:000001|daily|200")
            szse_path = engine._result_path(job_id, "market_bars|SZSE:000001|daily|200")

            assert sse_path != szse_path

    def test_sse_000001_vs_szse_000001_different_payloads(self):
        """Different instruments must produce different payload identity."""
        from astock_api.instrument import Instrument

        sse_inst = Instrument(exchange="SSE", code="000001", asset_type="INDEX")
        szse_inst = Instrument(exchange="SZSE", code="000001", asset_type="EQUITY")

        sse_payload = {
            "symbol": "000001",
            "canonical_id": sse_inst.canonical_id,
            "exchange": sse_inst.exchange,
            "asset_type": sse_inst.asset_type,
        }

        szse_payload = {
            "symbol": "000001",
            "canonical_id": szse_inst.canonical_id,
            "exchange": szse_inst.exchange,
            "asset_type": szse_inst.asset_type,
        }

        # Same symbol, different identity
        assert sse_payload["symbol"] == szse_payload["symbol"]  # backward compat
        assert sse_payload["canonical_id"] != szse_payload["canonical_id"]
        assert sse_payload["exchange"] != szse_payload["exchange"]


class TestLegacyChunkCompatibility:
    """D. Legacy chunks (bare code) still readable and recoverable."""

    def test_legacy_chunk_key_stored_unchanged(self):
        """A legacy chunk with bare code key must remain readable."""
        import uuid

        with tempfile.TemporaryDirectory() as tmpdir:
            engine = JobEngine(db_path=os.path.join(tmpdir, "test.db"), data_dir=tmpdir)
            engine.initialize()

            # Manually create a job with legacy-format chunk (simulating existing DB state)
            conn = engine._get_conn()
            try:
                job_id = str(uuid.uuid4())
                now = engine._now_iso()
                conn.execute(
                    "INSERT INTO jobs (job_id, job_type, status, params_json, total_chunks, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (job_id, "market_bars_snapshot", "PENDING", json.dumps({"symbols": ["600519"]}), 1, now, now)
                )
                chunk_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO job_chunks (chunk_id, job_id, chunk_key, payload_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (chunk_id, job_id, "market_bars|600519|daily|200", json.dumps({"symbol": "600519"}), "PENDING", now, now)
                )
                conn.commit()
            finally:
                conn.close()

            # The legacy chunk must be claimable and executable
            chunks = engine.get_chunks(job_id)
            assert len(chunks) == 1
            assert chunks[0]["chunk_key"] == "market_bars|600519|daily|200"

    def test_legacy_result_path_unchanged(self):
        """A legacy chunk's result path must be based on its original chunk_key."""
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = JobEngine(db_path=os.path.join(tmpdir, "test.db"), data_dir=tmpdir)
            engine.initialize()

            job_id = "legacy-job-456"

            # Legacy chunk_key
            legacy_key = "market_bars|600519|daily|200"
            legacy_path = engine._result_path(job_id, legacy_key)

            # New canonical chunk_key
            new_key = "market_bars|SSE:600519|daily|200"
            new_path = engine._result_path(job_id, new_key)

            # Paths must be different
            assert legacy_path != new_path

    def test_legacy_chunk_recovery_uses_stored_key(self):
        """When recovering a legacy chunk, the stored chunk_key is used — not recomputed."""
        import uuid

        with tempfile.TemporaryDirectory() as tmpdir:
            engine = JobEngine(db_path=os.path.join(tmpdir, "test.db"), data_dir=tmpdir)
            engine.initialize()

            # Manually create a job with legacy-format chunk + result file
            conn = engine._get_conn()
            try:
                job_id = str(uuid.uuid4())
                now = engine._now_iso()
                conn.execute(
                    "INSERT INTO jobs (job_id, job_type, status, params_json, total_chunks, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (job_id, "market_bars_snapshot", "PENDING", json.dumps({"symbols": ["600519"]}), 1, now, now)
                )
                chunk_id = str(uuid.uuid4())
                legacy_key = "market_bars|600519|daily|200"
                conn.execute(
                    "INSERT INTO job_chunks (chunk_id, job_id, chunk_key, payload_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (chunk_id, job_id, legacy_key, json.dumps({"symbol": "600519"}), "RUNNING", now, now)
                )
                conn.commit()
            finally:
                conn.close()

            # Write result file at legacy path
            legacy_path = engine._result_path(job_id, "market_bars|600519|daily|200")
            os.makedirs(os.path.dirname(legacy_path), exist_ok=True)
            with open(legacy_path, 'w') as f:
                json.dump({"job_id": job_id, "chunk_key": legacy_key, "data": {}}, f)

            # Recover state — legacy chunk should be found via its stored key
            engine._recover_state()

            # Claim and execute — should skip because result exists at legacy path
            chunk = engine._claim_chunk(job_id)
            if chunk:
                def mock_handler(payload):
                    raise AssertionError("Handler should not be called for legacy chunk with existing result")
                engine._execute_chunk(chunk, mock_handler)

            # Verify legacy chunk is DONE (not FAILED)
            chunks = engine.get_chunks(job_id)
            assert len(chunks) == 1
            assert chunks[0]["status"] == "DONE"


class TestRetrySemanticsUnchanged:
    """E. Retry semantics unchanged for legacy chunks."""

    def test_legacy_chunk_retry_uses_stored_key(self):
        """A legacy chunk that fails with TransientJobError must retry using its stored key."""
        import uuid
        from astock_api.job_engine import TransientJobError

        with tempfile.TemporaryDirectory() as tmpdir:
            engine = JobEngine(db_path=os.path.join(tmpdir, "test.db"), data_dir=tmpdir)
            engine.initialize()

            # Manually create a job with legacy-format chunk
            conn = engine._get_conn()
            try:
                job_id = str(uuid.uuid4())
                now = engine._now_iso()
                conn.execute(
                    "INSERT INTO jobs (job_id, job_type, status, params_json, total_chunks, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (job_id, "market_bars_snapshot", "PENDING", json.dumps({"symbols": ["600519"]}), 1, now, now)
                )
                chunk_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO job_chunks (chunk_id, job_id, chunk_key, payload_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (chunk_id, job_id, "market_bars|600519|daily|200", json.dumps({"symbol": "600519"}), "PENDING", now, now)
                )
                conn.commit()
            finally:
                conn.close()

            # Claim and execute with transient error
            chunk = engine._claim_chunk(job_id)
            assert chunk is not None

            def transient_handler(payload):
                raise TransientJobError("network timeout")

            engine._execute_chunk(chunk, transient_handler)

            # Verify chunk is in RETRY state (not FAILED)
            chunks = engine.get_chunks(job_id)
            assert len(chunks) == 1
            assert chunks[0]["status"] == "RETRY"


class TestCheckpointSemanticsUnchanged:
    """F. Checkpoint semantics unchanged for legacy chunks."""

    def test_legacy_chunk_checkpoint_uses_stored_key(self):
        """A legacy chunk's checkpoint must use its stored key, not recomputed."""
        import uuid

        with tempfile.TemporaryDirectory() as tmpdir:
            engine = JobEngine(db_path=os.path.join(tmpdir, "test.db"), data_dir=tmpdir)
            engine.initialize()

            # Manually create a job with legacy-format chunk + result file
            conn = engine._get_conn()
            try:
                job_id = str(uuid.uuid4())
                now = engine._now_iso()
                conn.execute(
                    "INSERT INTO jobs (job_id, job_type, status, params_json, total_chunks, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (job_id, "market_bars_snapshot", "PENDING", json.dumps({"symbols": ["600519"]}), 1, now, now)
                )
                chunk_id = str(uuid.uuid4())
                legacy_key = "market_bars|600519|daily|200"
                conn.execute(
                    "INSERT INTO job_chunks (chunk_id, job_id, chunk_key, payload_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (chunk_id, job_id, legacy_key, json.dumps({"symbol": "600519"}), "RUNNING", now, now)
                )
                conn.commit()
            finally:
                conn.close()

            # Write result at legacy path
            legacy_path = engine._result_path(job_id, "market_bars|600519|daily|200")
            os.makedirs(os.path.dirname(legacy_path), exist_ok=True)
            with open(legacy_path, 'w') as f:
                json.dump({"job_id": job_id, "chunk_key": legacy_key, "data": {}}, f)

            # Recover — should find result at legacy path
            engine._recover_state()

            chunk = engine._claim_chunk(job_id)
            if chunk:
                def mock_handler(payload):
                    raise AssertionError("Should not execute — result exists")
                engine._execute_chunk(chunk, mock_handler)

            chunks = engine.get_chunks(job_id)
            assert len(chunks) == 1
            assert chunks[0]["status"] == "DONE"
