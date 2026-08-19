"""C4B-4 Backward Compatibility / Identity Exposure tests.

Verifies:
A. Old request (bare symbol) still creates job with canonical identity
B. SZSE compatibility via EQUITY context
C. New chunk response includes canonical_id, exchange, asset_type
D. Legacy result (only symbol) still readable without crash
E. Existing fields unchanged for old clients
F. C4B-3 chunk identity not changed by response enrichment
G. Generic parse_instrument("000001") still raises AmbiguousExchangeError
"""

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest


class TestOldRequestCompatibility:
    """A. Old request (bare symbol) still works."""

    def test_bare_symbol_600519_creates_canonical_chunk(self):
        """symbol='600519' creates chunk with canonical_id=SSE:600519."""
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
            assert len(chunks) == 1

            payload = json.loads(chunks[0]["payload_json"])
            assert payload["symbol"] == "600519"  # backward compat
            assert payload["canonical_id"] == "SSE:600519"
            assert payload["exchange"] == "SSE"
            assert payload["asset_type"] == "EQUITY"

    def test_bare_symbol_000001_creates_szse_chunk(self):
        """symbol='000001' with EQUITY context creates SZSE:000001."""
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

            assert payload["symbol"] == "000001"
            assert payload["canonical_id"] == "SZSE:000001"
            assert payload["exchange"] == "SZSE"


class TestNewResponseIdentity:
    """C. New chunk response includes identity fields."""

    def test_chunk_response_has_canonical_id(self):
        """New chunks expose canonical_id in response."""
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

            assert "canonical_id" in payload
            assert payload["canonical_id"] == "SSE:600519"

    def test_chunk_response_has_exchange(self):
        """New chunks expose exchange in response."""
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

            assert "exchange" in payload
            assert payload["exchange"] == "SSE"

    def test_chunk_response_has_asset_type(self):
        """New chunks expose asset_type in response."""
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

            assert "asset_type" in payload
            assert payload["asset_type"] == "EQUITY"


class TestLegacyResultCompatibility:
    """D. Legacy result (only symbol) still readable."""

    def test_legacy_payload_without_canonical_id_does_not_crash(self):
        """A legacy payload with only 'symbol' field does not crash."""
        import uuid

        from astock_api.job_engine import JobEngine

        with tempfile.TemporaryDirectory() as tmpdir:
            engine = JobEngine(db_path=os.path.join(tmpdir, "test.db"), data_dir=tmpdir)
            engine.initialize()

            # Manually create a legacy job/chunk with bare payload
            conn = engine._get_conn()
            try:
                job_id = str(uuid.uuid4())
                now = engine._now_iso()
                conn.execute(
                    "INSERT INTO jobs (job_id, job_type, status, params_json, total_chunks, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (job_id, "market_bars_snapshot", "PENDING", json.dumps({"symbols": ["600519"]}), 1, now, now)
                )
                chunk_id = str(uuid.uuid4())
                # Legacy payload: only symbol, no canonical_id/exchange/asset_type
                conn.execute(
                    "INSERT INTO job_chunks (chunk_id, job_id, chunk_key, payload_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (chunk_id, job_id, "market_bars|600519|daily|200", json.dumps({"symbol": "600519"}), "PENDING", now, now)
                )
                conn.commit()
            finally:
                conn.close()

            # Must not crash when reading chunks
            chunks = engine.get_chunks(job_id)
            assert len(chunks) == 1

            # Payload still has symbol (backward compat)
            payload = json.loads(chunks[0]["payload_json"])
            assert payload["symbol"] == "600519"

    def test_legacy_result_file_unmodified(self):
        """Reading a legacy result file does not modify it."""
        import uuid

        from astock_api.job_engine import JobEngine

        with tempfile.TemporaryDirectory() as tmpdir:
            engine = JobEngine(db_path=os.path.join(tmpdir, "test.db"), data_dir=tmpdir)
            engine.initialize()

            # Create a legacy result file
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
                    (chunk_id, job_id, "market_bars|600519|daily|200", json.dumps({"symbol": "600519"}), "DONE", now, now)
                )
                conn.commit()
            finally:
                conn.close()

            # Read the chunk — must not modify payload_json
            chunks = engine.get_chunks(job_id)
            payload_after = json.loads(chunks[0]["payload_json"])

            # Legacy payload should remain unchanged (no canonical_id added to DB)
            assert "canonical_id" not in payload_after  # Not in stored payload
            assert payload_after["symbol"] == "600519"


class TestExistingFieldsUnchanged:
    """E. Existing fields unchanged for old clients."""

    def test_symbol_field_still_present(self):
        """symbol field remains in payload for backward compatibility."""
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

            assert "symbol" in payload
            assert payload["symbol"] == "600519"

    def test_frequency_field_still_present(self):
        """frequency field remains in payload."""
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

            assert "frequency" in payload
            assert payload["frequency"] == "daily"


class TestC4B3ChunkIdentityUnchanged:
    """F. C4B-3 canonical chunk_key not changed by response enrichment."""

    def test_canonical_chunk_key_not_modified_by_response_layer(self):
        """Response enrichment does not modify the stored chunk_key."""
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
            chunk_key_before = chunks[0]["chunk_key"]

            # Read again — chunk_key should be stable
            chunks2 = engine.get_chunks(result["job_id"])
            chunk_key_after = chunks2[0]["chunk_key"]

            assert chunk_key_before == chunk_key_after
            assert chunk_key_before == "market_bars|SSE:600519|daily|200"


class TestGenericAmbiguityPreserved:
    """G. Generic parse_instrument("000001") still raises AmbiguousExchangeError."""

    def test_generic_000001_still_ambiguous(self):
        """parse_instrument("000001") without asset_type must raise AmbiguousExchangeError."""
        from astock_api.instrument import parse_instrument, AmbiguousExchangeError

        with pytest.raises(AmbiguousExchangeError):
            parse_instrument("000001")

    def test_equity_context_not_polluting_generic_parser(self):
        """API default EQUITY context does not change generic parser behavior."""
        from astock_api.instrument import parse_instrument, AmbiguousExchangeError

        # Generic parser still raises for ambiguous codes
        with pytest.raises(AmbiguousExchangeError):
            parse_instrument("000001")

        # But explicit EQUITY context works
        inst = parse_instrument("000001", asset_type="EQUITY")
        assert inst.canonical_id == "SZSE:000001"


class TestJobRoutesIdentityExposure:
    """API route returns identity fields in chunk response."""

    def test_get_chunks_response_includes_identity(self):
        """GET /jobs/{id}/chunks returns canonical_id, exchange, asset_type."""
        from fastapi.testclient import TestClient
        from astock_api.main import app

        client = TestClient(app)
        engine = getattr(app.state, "job_engine", None)

        if engine is None:
            pytest.skip("Job engine not initialized in test client")

        # Create a job via engine
        result = engine.create_job("market_bars_snapshot", {
            "symbols": ["600519"],
            "frequency": "daily",
            "count": 3,
        })

        # Get chunks via API (mock auth)
        with patch("astock_api.job_routes.verify_api_key", lambda: None):
            resp = client.get(f"/api/v1/jobs/{result['job_id']}/chunks")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["chunks"]) >= 1

        chunk_resp = data["chunks"][0]
        assert "canonical_id" in chunk_resp
        assert chunk_resp["canonical_id"] == "SSE:600519"
        assert chunk_resp["exchange"] == "SSE"
        assert chunk_resp["asset_type"] == "EQUITY"

    def test_get_chunks_legacy_enriched(self):
        """Legacy chunk without canonical_id is enriched in response."""
        import uuid

        from fastapi.testclient import TestClient
        from astock_api.main import app

        client = TestClient(app)
        engine = getattr(app.state, "job_engine", None)

        if engine is None:
            pytest.skip("Job engine not initialized in test client")

        # Manually create a legacy job/chunk
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

        # Get chunks via API — should be enriched with identity
        with patch("astock_api.job_routes.verify_api_key", lambda: None):
            resp = client.get(f"/api/v1/jobs/{job_id}/chunks")

        assert resp.status_code == 200
        data = resp.json()
        chunk_resp = data["chunks"][0]

        # Enriched with canonical_id (derived from symbol)
        assert chunk_resp["canonical_id"] == "SSE:600519"
        assert chunk_resp["exchange"] == "SSE"
        assert chunk_resp["asset_type"] == "EQUITY"
