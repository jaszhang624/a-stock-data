"""R7-3 Failure Classification and Recovery Policy targeted tests.

Covers:
1. Upstream no data classification → WARN, not retryable, REPORT
2. Network error classification → WARN, retryable, RETRY
3. Identity error classification → CRITICAL, not retryable, STOP
4. Data corruption classification → CRITICAL, not retryable, STOP
5. Unknown error fallback → WARN, not retryable, REPORT
6. Cross-market isolation (classification doesn't mix markets)
7. Deterministic output (same error → same decision)
"""

import pytest


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """Create isolated JobEngine for tests."""
    from astock_api.job_engine import JobEngine

    db_path = str(tmp_path / "jobs.db")
    data_dir = str(tmp_path)
    monkeypatch.setattr(JobEngine, "__init__", lambda self, *a, **kw: None)
    engine = JobEngine()
    engine.db_path = db_path
    engine.data_dir = data_dir
    engine.initialize()
    return engine


# 1. Upstream no data → WARN, not retryable, REPORT
def test_upstream_no_data_classification():
    from astock_api.failure_policy import classify_and_decide, FailureCategory

    decision = classify_and_decide("No data found for symbol 600519")
    assert decision.category == "UPSTREAM_NO_DATA"
    assert decision.severity == "WARN"
    assert decision.retryable is False
    assert decision.action == "REPORT"

    # Also matches other upstream patterns
    for msg in ["Empty response from source", "Symbol not found", "Data unavailable"]:
        d = classify_and_decide(msg)
        assert d.category == "UPSTREAM_NO_DATA", f"Expected UPSTREAM_NO_DATA for: {msg}"


# 2. Network error → WARN, retryable, RETRY
def test_network_error_classification():
    from astock_api.failure_policy import classify_and_decide

    decision = classify_and_decide("Connection timeout after 30s")
    assert decision.category == "NETWORK_ERROR"
    assert decision.severity == "WARN"
    assert decision.retryable is True
    assert decision.action == "RETRY"

    # Also matches other network patterns
    for msg in ["Connection refused", "DNS resolution failed", "Read timed out"]:
        d = classify_and_decide(msg)
        assert d.category == "NETWORK_ERROR", f"Expected NETWORK_ERROR for: {msg}"


# 3. Identity error → CRITICAL, not retryable, STOP
def test_identity_error_classification():
    from astock_api.failure_policy import classify_and_decide

    decision = classify_and_decide("Canonical ID mismatch: expected SSE:000001")
    assert decision.category == "IDENTITY_ERROR"
    assert decision.severity == "CRITICAL"
    assert decision.retryable is False
    assert decision.action == "STOP"

    # Also matches other identity patterns
    for msg in ["Ambiguous exchange for code", "Identity collision detected"]:
        d = classify_and_decide(msg)
        assert d.category == "IDENTITY_ERROR", f"Expected IDENTITY_ERROR for: {msg}"


# 4. Data corruption → CRITICAL, not retryable, STOP
def test_data_corruption_classification():
    from astock_api.failure_policy import classify_and_decide

    decision = classify_and_decide("Checksum mismatch on chunk data")
    assert decision.category == "DATA_CORRUPTION"
    assert decision.severity == "CRITICAL"
    assert decision.retryable is False
    assert decision.action == "STOP"

    # Also matches other corruption patterns
    for msg in ["Invalid JSON response", "Malformed response body"]:
        d = classify_and_decide(msg)
        assert d.category == "DATA_CORRUPTION", f"Expected DATA_CORRUPTION for: {msg}"


# 5. Unknown error fallback → WARN, not retryable, REPORT
def test_unknown_error_fallback():
    from astock_api.failure_policy import classify_and_decide, FailureCategory

    # Empty message
    decision = classify_and_decide("")
    assert decision.category == "UNKNOWN"
    assert decision.severity == "WARN"
    assert decision.retryable is False
    assert decision.action == "REPORT"

    # None message
    decision = classify_and_decide(None)
    assert decision.category == "UNKNOWN"

    # Unrecognized message
    decision = classify_and_decide("Something weird happened that we don't know about")
    assert decision.category == "UNKNOWN"


# 6. Cross-market isolation: classification doesn't mix markets
def test_cross_market_isolation(engine):
    from astock_api.failure_policy import classify_failures_from_jobs, should_stop

    # Add error_message column
    conn = engine._get_conn()
    try:
        cur = conn.execute("PRAGMA table_info(jobs)")
        columns = [row[1] for row in cur.fetchall()]
        if "error_message" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN error_message TEXT")
        # Create failed jobs for SSE and SZSE independently
        conn.execute(
            "INSERT INTO jobs (job_id, job_type, status, params_json, total_chunks, created_at, updated_at, error_message) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("job-1", "market_bars_update", "FAILED", "{}", 1, "2026-08-20T00:00:00Z", "2026-08-20T00:01:00Z", "No data found for SSE symbol")
        )
        conn.execute(
            "INSERT INTO jobs (job_id, job_type, status, params_json, total_chunks, created_at, updated_at, error_message) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("job-2", "market_bars_update", "FAILED", "{}", 1, "2026-08-20T00:00:00Z", "2026-08-20T00:01:00Z", "Connection timeout for SZSE")
        )
        conn.commit()
    finally:
        conn.close()

    decisions = classify_failures_from_jobs(engine)
    assert len(decisions) == 2

    # SSE job: UPSTREAM_NO_DATA (not retryable)
    assert decisions[0].category == "UPSTREAM_NO_DATA"
    assert decisions[0].retryable is False

    # SZSE job: NETWORK_ERROR (retryable)
    assert decisions[1].category == "NETWORK_ERROR"
    assert decisions[1].retryable is True

    # should_stop: neither requires STOP
    assert should_stop(decisions) is False


# 7. Deterministic output: same error → same decision
def test_deterministic_output():
    from astock_api.failure_policy import classify_and_decide

    msg = "Connection timeout after 30s"
    decisions = [classify_and_decide(msg) for _ in range(10)]

    # All decisions must be identical
    first = decisions[0]
    for d in decisions[1:]:
        assert d.category == first.category
        assert d.severity == first.severity
        assert d.retryable == first.retryable
        assert d.action == first.action


# Additional: should_stop and should_retry helpers
def test_should_stop_with_critical():
    from astock_api.failure_policy import classify_and_decide, should_stop

    decisions = [classify_and_decide("Canonical ID mismatch")]
    assert should_stop(decisions) is True


def test_should_retry_with_network():
    from astock_api.failure_policy import classify_and_decide, should_retry

    decisions = [classify_and_decide("Connection timeout")]
    assert should_retry(decisions) is True


# Additional: summarize_failures
def test_summarize_failures():
    from astock_api.failure_policy import classify_and_decide, summarize_failures

    decisions = [
        classify_and_decide("No data found"),
        classify_and_decide("Connection timeout"),
        classify_and_decide("No data found again"),
    ]

    summary = summarize_failures(decisions)
    assert summary["total"] == 3
    assert summary["categories"]["UPSTREAM_NO_DATA"] == 2
    assert summary["categories"]["NETWORK_ERROR"] == 1
    assert "WARN" in summary["worst_severity"]


# Additional: AUTH_ERROR classification
def test_auth_error_classification():
    from astock_api.failure_policy import classify_and_decide

    for msg in ["401 Unauthorized", "Token expired", "Access denied"]:
        d = classify_and_decide(msg)
        assert d.category == "AUTH_ERROR", f"Expected AUTH_ERROR for: {msg}"
        assert d.severity == "ERROR"
        assert d.retryable is False
        assert d.action == "STOP"
