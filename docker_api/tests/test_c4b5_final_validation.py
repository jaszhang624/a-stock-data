"""R5-C4B-5 Final End-to-End Identity Validation.

NO NEW FEATURE. NO SCHEMA CHANGE. NO MIGRATION.
Validates the complete Instrument Identity model across all layers.

Tests:
A. Hard collision case (SSE:000001 INDEX vs SZSE:000001 EQUITY)
B. End-to-end identity trace for both instruments
C. Provider routing correctness
D. Legacy compatibility (bare code chunks)
E. Existing equity workflow (600519, 000001)
F. Generic ambiguity preserved
"""

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from astock_api.instrument import Instrument, parse_instrument, AmbiguousExchangeError


class TestHardCollisionCase:
    """A. SSE:000001 INDEX vs SZSE:000001 EQUITY — must be fully independent."""

    def test_canonical_id_different(self):
        sse = Instrument(exchange="SSE", code="000001", asset_type="INDEX")
        szse = Instrument(exchange="SZSE", code="000001", asset_type="EQUITY")

        assert sse.canonical_id == "SSE:000001"
        assert szse.canonical_id == "SZSE:000001"
        assert sse.canonical_id != szse.canonical_id

    def test_chunk_key_different(self):
        sse = Instrument(exchange="SSE", code="000001", asset_type="INDEX")
        szse = Instrument(exchange="SZSE", code="000001", asset_type="EQUITY")

        sse_key = f"market_bars|{sse.canonical_id}|daily|200"
        szse_key = f"market_bars|{szse.canonical_id}|daily|200"

        assert sse_key == "market_bars|SSE:000001|daily|200"
        assert szse_key == "market_bars|SZSE:000001|daily|200"
        assert sse_key != szse_key

    def test_result_path_different(self):
        from astock_api.job_engine import JobEngine

        with tempfile.TemporaryDirectory() as tmpdir:
            engine = JobEngine(db_path=os.path.join(tmpdir, "test.db"), data_dir=tmpdir)
            engine.initialize()

            job_id = "collision-test-job"
            sse_path = engine._result_path(job_id, "market_bars|SSE:000001|daily|200")
            szse_path = engine._result_path(job_id, "market_bars|SZSE:000001|daily|200")

            assert sse_path != szse_path

    def test_payload_identity_different(self):
        """Payload for SSE:000001 INDEX must differ from SZSE:000001 EQUITY."""
        sse = Instrument(exchange="SSE", code="000001", asset_type="INDEX")
        szse = Instrument(exchange="SZSE", code="000001", asset_type="EQUITY")

        sse_payload = {
            "symbol": "000001",
            "canonical_id": sse.canonical_id,
            "exchange": sse.exchange,
            "asset_type": sse.asset_type,
        }

        szse_payload = {
            "symbol": "000001",
            "canonical_id": szse.canonical_id,
            "exchange": szse.exchange,
            "asset_type": szse.asset_type,
        }

        # Same symbol (backward compat), different identity
        assert sse_payload["symbol"] == szse_payload["symbol"]  # "000001"
        assert sse_payload["canonical_id"] != szse_payload["canonical_id"]
        assert sse_payload["exchange"] != szse_payload["exchange"]
        assert sse_payload["asset_type"] != szse_payload["asset_type"]

    def test_sqlite_unique_no_conflict(self):
        """Both SSE:000001 and SZSE:000001 chunks can coexist in same job."""
        import uuid

        from astock_api.job_engine import JobEngine

        with tempfile.TemporaryDirectory() as tmpdir:
            engine = JobEngine(db_path=os.path.join(tmpdir, "test.db"), data_dir=tmpdir)
            engine.initialize()

            conn = engine._get_conn()
            try:
                job_id = str(uuid.uuid4())
                now = engine._now_iso()
                conn.execute(
                    "INSERT INTO jobs (job_id, job_type, status, params_json, total_chunks, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (job_id, "market_bars_snapshot", "PENDING", json.dumps({}), 2, now, now)
                )

                # Insert SSE:000001 INDEX chunk
                conn.execute(
                    "INSERT INTO job_chunks (chunk_id, job_id, chunk_key, payload_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), job_id, "market_bars|SSE:000001|daily|200",
                     json.dumps({"symbol": "000001", "canonical_id": "SSE:000001", "exchange": "SSE", "asset_type": "INDEX"}),
                     "PENDING", now, now)
                )

                # Insert SZSE:000001 EQUITY chunk — must NOT conflict
                conn.execute(
                    "INSERT INTO job_chunks (chunk_id, job_id, chunk_key, payload_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), job_id, "market_bars|SZSE:000001|daily|200",
                     json.dumps({"symbol": "000001", "canonical_id": "SZSE:000001", "exchange": "SZSE", "asset_type": "EQUITY"}),
                     "PENDING", now, now)
                )

                conn.commit()
            finally:
                conn.close()

            # Both chunks readable
            chunks = engine.get_chunks(job_id)
            assert len(chunks) == 2

            keys = [c["chunk_key"] for c in chunks]
            assert "market_bars|SSE:000001|daily|200" in keys
            assert "market_bars|SZSE:000001|daily|200" in keys


class TestEndToEndIdentityTrace:
    """B. Trace identity through all layers for both instruments."""

    def test_sse_000001_index_trace(self):
        """SSE:000001 INDEX must never be re-inferred as SZSE."""
        from astock_api.instrument import instrument_to_mootdx_market, instrument_to_baidu_symbol

        inst = Instrument(exchange="SSE", code="000001", asset_type="INDEX")

        # Layer 1: Instrument
        assert inst.exchange == "SSE"
        assert inst.code == "000001"
        assert inst.asset_type == "INDEX"

        # Layer 2: Canonical ID
        assert inst.canonical_id == "SSE:000001"

        # Layer 3: Chunk key
        chunk_key = f"market_bars|{inst.canonical_id}|daily|200"
        assert chunk_key == "market_bars|SSE:000001|daily|200"

        # Layer 4: Mootdx routing (SSE → market=1)
        tdx_market = instrument_to_mootdx_market(inst)
        assert tdx_market == 1

        # Layer 5: Baidu routing (SSE → sh prefix)
        baidu_sym = instrument_to_baidu_symbol(inst)
        assert baidu_sym == "sh000001"

    def test_szse_000001_equity_trace(self):
        """SZSE:000001 EQUITY must route correctly."""
        from astock_api.instrument import instrument_to_mootdx_market, instrument_to_baidu_symbol

        inst = Instrument(exchange="SZSE", code="000001", asset_type="EQUITY")

        assert inst.canonical_id == "SZSE:000001"
        assert instrument_to_mootdx_market(inst) == 0
        assert instrument_to_baidu_symbol(inst) == "sz000001"

    def test_sse_000001_not_reinferred_as_szse(self):
        """Critical: SSE:000001 INDEX must NOT be re-inferred as SZSE at any layer."""
        # parse_instrument with explicit exchange + INDEX asset_type must NOT change it
        parsed = parse_instrument("000001", exchange="SSE", asset_type="INDEX")
        assert parsed.exchange == "SSE"
        assert parsed.canonical_id == "SSE:000001"

        # parse_instrument with INDEX alone requires explicit exchange
        with pytest.raises(AmbiguousExchangeError):
            parse_instrument("000001", asset_type="INDEX")


class TestProviderRoutingCorrectness:
    """C. Provider routing for both instruments."""

    def test_sse_000001_mootdx_market(self):
        from astock_api.instrument import instrument_to_mootdx_market

        inst = Instrument(exchange="SSE", code="000001", asset_type="INDEX")
        assert instrument_to_mootdx_market(inst) == 1

    def test_szse_000001_mootdx_market(self):
        from astock_api.instrument import instrument_to_mootdx_market

        inst = Instrument(exchange="SZSE", code="000001", asset_type="EQUITY")
        assert instrument_to_mootdx_market(inst) == 0

    def test_sse_000001_baidu_symbol(self):
        from astock_api.instrument import instrument_to_baidu_symbol

        inst = Instrument(exchange="SSE", code="000001", asset_type="INDEX")
        assert instrument_to_baidu_symbol(inst) == "sh000001"

    def test_szse_000001_baidu_symbol(self):
        from astock_api.instrument import instrument_to_baidu_symbol

        inst = Instrument(exchange="SZSE", code="000001", asset_type="EQUITY")
        assert instrument_to_baidu_symbol(inst) == "sz000001"


class TestLegacyCompatibility:
    """D. Legacy chunks (bare code) still work."""

    def test_legacy_chunk_readable(self):
        import uuid

        from astock_api.job_engine import JobEngine

        with tempfile.TemporaryDirectory() as tmpdir:
            engine = JobEngine(db_path=os.path.join(tmpdir, "test.db"), data_dir=tmpdir)
            engine.initialize()

            conn = engine._get_conn()
            try:
                job_id = str(uuid.uuid4())
                now = engine._now_iso()
                conn.execute(
                    "INSERT INTO jobs (job_id, job_type, status, params_json, total_chunks, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (job_id, "market_bars_snapshot", "PENDING", json.dumps({"symbols": ["600519"]}), 1, now, now)
                )
                conn.execute(
                    "INSERT INTO job_chunks (chunk_id, job_id, chunk_key, payload_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), job_id, "market_bars|600519|daily|200", json.dumps({"symbol": "600519"}), "PENDING", now, now)
                )
                conn.commit()
            finally:
                conn.close()

            chunks = engine.get_chunks(job_id)
            assert len(chunks) == 1
            assert chunks[0]["chunk_key"] == "market_bars|600519|daily|200"

    def test_legacy_result_path_stable(self):
        from astock_api.job_engine import JobEngine

        with tempfile.TemporaryDirectory() as tmpdir:
            engine = JobEngine(db_path=os.path.join(tmpdir, "test.db"), data_dir=tmpdir)
            engine.initialize()

            legacy_path = engine._result_path("job-1", "market_bars|600519|daily|200")
            new_path = engine._result_path("job-1", "market_bars|SSE:600519|daily|200")

            assert legacy_path != new_path
            # Legacy path is deterministic (SHA256 of chunk_key)

    def test_legacy_recovery_uses_stored_key(self):
        import uuid

        from astock_api.job_engine import JobEngine

        with tempfile.TemporaryDirectory() as tmpdir:
            engine = JobEngine(db_path=os.path.join(tmpdir, "test.db"), data_dir=tmpdir)
            engine.initialize()

            conn = engine._get_conn()
            try:
                job_id = str(uuid.uuid4())
                now = engine._now_iso()
                conn.execute(
                    "INSERT INTO jobs (job_id, job_type, status, params_json, total_chunks, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (job_id, "market_bars_snapshot", "PENDING", json.dumps({"symbols": ["600519"]}), 1, now, now)
                )
                conn.execute(
                    "INSERT INTO job_chunks (chunk_id, job_id, chunk_key, payload_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), job_id, "market_bars|600519|daily|200", json.dumps({"symbol": "600519"}), "RUNNING", now, now)
                )
                conn.commit()
            finally:
                conn.close()

            # Write result at legacy path
            legacy_path = engine._result_path(job_id, "market_bars|600519|daily|200")
            os.makedirs(os.path.dirname(legacy_path), exist_ok=True)
            with open(legacy_path, 'w') as f:
                json.dump({"job_id": job_id, "chunk_key": "market_bars|600519|daily|200", "data": {}}, f)

            # _recover_state resets RUNNING → PENDING (orphan recovery)
            engine._recover_state()

            # _execute_chunk then finds the result file and marks DONE
            chunk = engine._claim_chunk(job_id)
            if chunk:
                def handler(payload):
                    raise AssertionError("Should not execute — result exists")
                engine._execute_chunk(chunk, handler)

            chunks = engine.get_chunks(job_id)
            assert len(chunks) == 1
            assert chunks[0]["status"] == "DONE"


class TestExistingEquityWorkflow:
    """E. Existing equity API (bare symbol) still works."""

    def test_600519_equity_workflow(self):
        from astock_api.job_engine import JobEngine

        with tempfile.TemporaryDirectory() as tmpdir:
            engine = JobEngine(db_path=os.path.join(tmpdir, "test.db"), data_dir=tmpdir)
            engine.initialize()

            result = engine.create_job("market_bars_snapshot", {
                "symbols": ["600519"],
                "frequency": "daily",
                "count": 200,
            })

            chunks = engine.get_chunks(result["job_id"])
            payload = json.loads(chunks[0]["payload_json"])

            assert chunks[0]["chunk_key"] == "market_bars|SSE:600519|daily|200"
            assert payload["symbol"] == "600519"
            assert payload["canonical_id"] == "SSE:600519"

    def test_000001_equity_workflow(self):
        from astock_api.job_engine import JobEngine

        with tempfile.TemporaryDirectory() as tmpdir:
            engine = JobEngine(db_path=os.path.join(tmpdir, "test.db"), data_dir=tmpdir)
            engine.initialize()

            result = engine.create_job("market_bars_snapshot", {
                "symbols": ["000001"],
                "frequency": "daily",
                "count": 200,
            })

            chunks = engine.get_chunks(result["job_id"])
            payload = json.loads(chunks[0]["payload_json"])

            assert chunks[0]["chunk_key"] == "market_bars|SZSE:000001|daily|200"
            assert payload["symbol"] == "000001"
            assert payload["canonical_id"] == "SZSE:000001"


class TestGenericAmbiguityPreserved:
    """F. Generic parse_instrument("000001") still raises AmbiguousExchangeError."""

    def test_000001_still_ambiguous(self):
        with pytest.raises(AmbiguousExchangeError):
            parse_instrument("000001")

    def test_399001_still_ambiguous(self):
        with pytest.raises(AmbiguousExchangeError):
            parse_instrument("399001")


class TestRetrySemanticsUnchanged:
    """Legacy and new chunks have identical retry behavior."""

    def test_legacy_chunk_retry(self):
        import uuid
        from astock_api.job_engine import JobEngine, TransientJobError

        with tempfile.TemporaryDirectory() as tmpdir:
            engine = JobEngine(db_path=os.path.join(tmpdir, "test.db"), data_dir=tmpdir)
            engine.initialize()

            conn = engine._get_conn()
            try:
                job_id = str(uuid.uuid4())
                now = engine._now_iso()
                conn.execute(
                    "INSERT INTO jobs (job_id, job_type, status, params_json, total_chunks, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (job_id, "market_bars_snapshot", "PENDING", json.dumps({}), 1, now, now)
                )
                conn.execute(
                    "INSERT INTO job_chunks (chunk_id, job_id, chunk_key, payload_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), job_id, "market_bars|600519|daily|200", json.dumps({"symbol": "600519"}), "PENDING", now, now)
                )
                conn.commit()
            finally:
                conn.close()

            chunk = engine._claim_chunk(job_id)
            assert chunk is not None

            def handler(payload):
                raise TransientJobError("network timeout")

            engine._execute_chunk(chunk, handler)

            chunks = engine.get_chunks(job_id)
            assert len(chunks) == 1
            assert chunks[0]["status"] == "RETRY"

    def test_canonical_chunk_retry(self):
        from astock_api.job_engine import JobEngine, TransientJobError

        with tempfile.TemporaryDirectory() as tmpdir:
            engine = JobEngine(db_path=os.path.join(tmpdir, "test.db"), data_dir=tmpdir)
            engine.initialize()

            result = engine.create_job("market_bars_snapshot", {
                "symbols": ["600519"],
                "frequency": "daily",
                "count": 200,
            })

            chunk = engine._claim_chunk(result["job_id"])
            assert chunk is not None

            def handler(payload):
                raise TransientJobError("network timeout")

            engine._execute_chunk(chunk, handler)

            chunks = engine.get_chunks(result["job_id"])
            assert len(chunks) == 1
            assert chunks[0]["status"] == "RETRY"


class TestCheckpointSemanticsUnchanged:
    """Legacy and new chunks have identical checkpoint behavior."""

    def test_legacy_checkpoint(self):
        import uuid

        from astock_api.job_engine import JobEngine

        with tempfile.TemporaryDirectory() as tmpdir:
            engine = JobEngine(db_path=os.path.join(tmpdir, "test.db"), data_dir=tmpdir)
            engine.initialize()

            conn = engine._get_conn()
            try:
                job_id = str(uuid.uuid4())
                now = engine._now_iso()
                conn.execute(
                    "INSERT INTO jobs (job_id, job_type, status, params_json, total_chunks, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (job_id, "market_bars_snapshot", "PENDING", json.dumps({}), 1, now, now)
                )
                conn.execute(
                    "INSERT INTO job_chunks (chunk_id, job_id, chunk_key, payload_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), job_id, "market_bars|600519|daily|200", json.dumps({"symbol": "600519"}), "RUNNING", now, now)
                )
                conn.commit()
            finally:
                conn.close()

            legacy_path = engine._result_path(job_id, "market_bars|600519|daily|200")
            os.makedirs(os.path.dirname(legacy_path), exist_ok=True)
            with open(legacy_path, 'w') as f:
                json.dump({"job_id": job_id, "chunk_key": "market_bars|600519|daily|200", "data": {}}, f)

            engine._recover_state()
            chunk = engine._claim_chunk(job_id)
            if chunk:
                def handler(payload):
                    raise AssertionError("Should not execute — result exists")
                engine._execute_chunk(chunk, handler)

            chunks = engine.get_chunks(job_id)
            assert len(chunks) == 1
            assert chunks[0]["status"] == "DONE"

    def test_canonical_checkpoint(self):
        from astock_api.job_engine import JobEngine

        with tempfile.TemporaryDirectory() as tmpdir:
            engine = JobEngine(db_path=os.path.join(tmpdir, "test.db"), data_dir=tmpdir)
            engine.initialize()

            result = engine.create_job("market_bars_snapshot", {
                "symbols": ["600519"],
                "frequency": "daily",
                "count": 200,
            })

            # Write result at canonical path
            chunks = engine.get_chunks(result["job_id"])
            chunk_key = chunks[0]["chunk_key"]
            result_path = engine._result_path(result["job_id"], chunk_key)

            os.makedirs(os.path.dirname(result_path), exist_ok=True)
            with open(result_path, 'w') as f:
                json.dump({"job_id": result["job_id"], "chunk_key": chunk_key, "data": {}}, f)

            # Mark as RUNNING to simulate crash
            conn = engine._get_conn()
            try:
                conn.execute("UPDATE job_chunks SET status='RUNNING' WHERE job_id=?", (result["job_id"],))
                conn.commit()
            finally:
                conn.close()

            engine._recover_state()
            chunk = engine._claim_chunk(result["job_id"])
            if chunk:
                def handler(payload):
                    raise AssertionError("Should not execute — result exists")
                engine._execute_chunk(chunk, handler)

            chunks = engine.get_chunks(result["job_id"])
            assert len(chunks) == 1
            assert chunks[0]["status"] == "DONE"
