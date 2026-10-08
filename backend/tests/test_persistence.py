"""
Track T2 — persistence (SQLAlchemy ground truth + incidents).

Every test runs against the throwaway SQLite file installed by the
session-scoped ``_isolate_database`` fixture in conftest.py, so none of this
touches the developer's real ledger.
"""
import logging

import pytest

from app.db import session as db_session
from app.db.init import init_db
from app.db.models import Base, GroundTruth, Incident
from app.db.repo import (
    compute_sla_protection_rate,
    count_ground_truth,
    load_ground_truth,
    load_incidents,
    save_ground_truth,
    save_incident,
)


def _entry(experiment_id="chaos_unit", **over):
    base = {
        "experiment_id": experiment_id,
        "service": "frontend",
        "experiment_type": "NetworkLatency",
        "tenant_id": "tenant_a",
        "ground_truth_cause": "NetworkChaos: 300ms RTT delay",
        "expected_symptom": "HTTP 504 Gateway Timeout",
        "injected_at": 1_700_000_000.0,
        "status": "ACTIVE",
        "real_mesh_action": {"status": "SUCCESS", "latency_ms": 850},
    }
    base.update(over)
    return base


def _incident(incident_id="inc_unit", **over):
    base = {
        "incident_id": incident_id,
        "primary_service": "frontend",
        "root_cause_candidate": "NetworkChaos",
        "severity": "critical",
        "tenant_id": "tenant_a",
        "created_at": 1_700_000_000.0,
        "updated_at": 1_700_000_001.0,      # 1s later -> within SLA by default
        "total_alerts": 3,
        "status": "OPEN",
        "affected_services": ["frontend"],
        "alert_ids": ["a1", "a2"],
        "sample_alerts": [{"id": "a1", "service": "frontend"}],
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Step 1 + 5: import works, schema auto-creates
# ---------------------------------------------------------------------------
def test_schema_initialises_on_a_fresh_database():
    """Step 5: a brand-new store gets both tables without any migration tool."""
    tables = init_db()
    assert {"ground_truth", "incidents"} <= set(tables)

    # and they are genuinely usable, not just declared
    assert Base.metadata.tables["ground_truth"].c.experiment_id.primary_key
    assert Base.metadata.tables["incidents"].c.incident_id.primary_key
    assert issubclass(GroundTruth, Base) and issubclass(Incident, Base)


def test_backend_falls_back_to_sqlite_and_says_so(caplog):
    """Step 2: an unreachable Postgres must fall back — loudly, not silently."""
    db_session.reset_engine_for_tests()
    with caplog.at_level(logging.WARNING, logger="app.db.session"):
        assert db_session.backend_name() == "sqlite"
    assert any("falling back to SQLite" in r.message for r in caplog.records), (
        "the fallback must be logged so a broken Postgres cannot hide"
    )
    # leave a usable engine behind for later tests
    db_session.get_engine()


def test_postgres_url_is_forced_to_the_psycopg3_dialect():
    """Bare postgresql:// would select psycopg2, which is not installed."""
    from app.db.session import _as_psycopg
    assert _as_psycopg("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert _as_psycopg("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert _as_psycopg("postgresql+psycopg://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert _as_psycopg("sqlite:///x.db") == "sqlite:///x.db"


# ---------------------------------------------------------------------------
# Step 2: store + read back, with upsert
# ---------------------------------------------------------------------------
def test_ground_truth_round_trip_and_upsert():
    assert save_ground_truth(_entry()) is True
    assert count_ground_truth() >= 1

    # same key again -> one row, now carrying the diagnosis
    assert save_ground_truth(_entry(ai_diagnosis="Runbook: Network Latency",
                                    confidence_score=0.5127,
                                    investigation_duration_seconds=0.337)) is True

    rows = [r for r in load_ground_truth() if r["experiment_id"] == "chaos_unit"]
    assert len(rows) == 1, "upsert must not create a duplicate row"
    assert rows[0]["ai_diagnosis"] == "Runbook: Network Latency"
    assert rows[0]["confidence_score"] == pytest.approx(0.5127)
    assert rows[0]["real_mesh_action"]["latency_ms"] == 850   # nested JSON survived
    assert rows[0]["ground_truth_cause"].startswith("NetworkChaos")


def test_incident_round_trip():
    # 1s of detection time against a 300s SLA -> not breached
    row = _incident(created_at=1_000_000_000.0, updated_at=1_000_000_001.0)
    assert save_incident(row) is True

    stored = [r for r in load_incidents() if r["incident_id"] == "inc_unit"]
    assert len(stored) == 1
    assert stored[0]["affected_services"] == ["frontend"]
    assert stored[0]["sample_alerts"][0]["id"] == "a1"
    assert stored[0]["total_alerts"] == 3


def test_save_rejects_rows_without_a_primary_key():
    assert save_ground_truth({}) is False
    assert save_ground_truth({"service": "frontend"}) is False
    assert save_incident({"primary_service": "frontend"}) is False


def test_read_functions_return_empty_list_when_the_store_fails(monkeypatch):
    """Persistence must degrade to a warning, never raise into an API call."""
    import app.db.repo as repo

    def _boom(*_a, **_k):
        raise RuntimeError("database is gone")

    # patch the name REPO bound at import, not the one in session
    monkeypatch.setattr(repo, "session_scope", _boom)
    assert load_ground_truth() == []
    assert load_incidents() == []
    assert count_ground_truth() == 0


# ---------------------------------------------------------------------------
# Step 2 "done when": restart server -> experiments still there
# ---------------------------------------------------------------------------
def test_experiments_survive_a_restart(test_client):
    save_ground_truth(_entry(experiment_id="chaos_restart_proof"))

    from app.api import chaos
    saved = list(chaos.ground_truth_ledger)          # remember what memory holds
    try:
        chaos.ground_truth_ledger.clear()            # simulate a fresh process
        res = test_client.get("/api/v1/chaos/ledger")
        assert res.status_code == 200
        ids = [e["experiment_id"] for e in res.json()]
        assert "chaos_restart_proof" in ids, (
            "ledger must be rebuilt from the database after a restart"
        )
    finally:
        chaos.ground_truth_ledger[:] = saved


def test_persisted_ledger_is_preferred_over_memory(test_client):
    save_ground_truth(_entry(experiment_id="chaos_db_only"))
    from app.api import chaos
    saved = list(chaos.ground_truth_ledger)
    try:
        chaos.ground_truth_ledger.clear()
        entries = chaos.get_ground_truth_ledger()
        assert any(e["experiment_id"] == "chaos_db_only" for e in entries)
    finally:
        chaos.ground_truth_ledger[:] = saved


# ---------------------------------------------------------------------------
# Step 3: GET /api/v1/ground-truth
# ---------------------------------------------------------------------------
def test_ground_truth_endpoint_returns_the_ledger(test_client):
    save_ground_truth(_entry(experiment_id="chaos_endpoint_proof"))

    res = test_client.get("/api/v1/ground-truth")
    assert res.status_code == 200
    body = res.json()
    assert body["backend"] == "sqlite"
    assert body["count"] == len(body["entries"])
    assert body["total_persisted"] >= body["count"]
    assert any(e["experiment_id"] == "chaos_endpoint_proof"
               for e in body["entries"])
    # newest first, matching the in-memory insert(0, ...) convention
    injected = [e["injected_at"] for e in body["entries"]]
    assert injected == sorted(injected, reverse=True)


def test_ground_truth_endpoint_honours_limit(test_client):
    res = test_client.get("/api/v1/ground-truth?limit=1")
    assert res.status_code == 200
    assert len(res.json()["entries"]) <= 1


def test_ground_truth_endpoint_falls_back_to_memory_when_store_is_empty(
        test_client, monkeypatch):
    """If nothing is persisted, report the live ledger instead of lying."""
    import app.api.ground_truth as gt_router
    # patch the name the ROUTER bound at import, not the one in repo
    monkeypatch.setattr(gt_router, "load_ground_truth", lambda limit=None: [])

    from app.api import chaos
    saved = list(chaos.ground_truth_ledger)
    try:
        chaos.ground_truth_ledger[:] = [{"experiment_id": "only_in_memory"}]
        res = test_client.get("/api/v1/ground-truth")
        assert res.status_code == 200
        body = res.json()
        assert body["source"] == "memory"
        assert body["entries"][0]["experiment_id"] == "only_in_memory"
    finally:
        chaos.ground_truth_ledger[:] = saved


# ---------------------------------------------------------------------------
# Step 4: sla_protection_rate stops being null
# ---------------------------------------------------------------------------
def test_sla_protection_rate_is_a_number_when_incidents_exist():
    # 1s against tenant_a's 300s budget -> protected
    save_incident(_incident("inc_protected",
                            created_at=1_000_000_000.0,
                            updated_at=1_000_000_001.0))
    rate = compute_sla_protection_rate()
    assert isinstance(rate, float)
    assert 0.0 <= rate <= 100.0


def test_sla_protection_rate_detects_a_real_breach(monkeypatch):
    """400s breaches tenant_a's 300s budget; 1s does not -> 1 of 2 = 50%."""
    from app.db import repo

    breached = _incident("inc_breached", tenant_id="tenant_a",
                         created_at=1_000_000_000.0,
                         updated_at=1_000_000_400.0)
    ok = _incident("inc_ok", tenant_id="tenant_a",
                   created_at=1_000_000_000.0,
                   updated_at=1_000_000_001.0)

    # The store is shared across the session, so pin the inputs explicitly
    # rather than asserting against whatever other tests persisted.
    monkeypatch.setattr(repo, "_live_incidents", lambda: [])
    monkeypatch.setattr(repo, "load_incidents", lambda limit=None: [breached, ok])

    assert repo.compute_sla_protection_rate() == pytest.approx(50.0)


def test_sla_protection_rate_is_none_only_when_there_is_nothing_to_measure(
        monkeypatch):
    from app.db import repo
    monkeypatch.setattr(repo, "_live_incidents", lambda: [])
    monkeypatch.setattr(repo, "load_incidents", lambda limit=None: [])
    assert repo.compute_sla_protection_rate() is None


def test_scorecard_reports_sla_protection_rate(test_client):
    save_incident(_incident("inc_scorecard",
                            created_at=1_000_000_000.0,
                            updated_at=1_000_000_001.0))
    res = test_client.get("/api/v1/benchmarks/scorecard")
    assert res.status_code == 200
    value = res.json().get("sla_protection_rate")
    assert value is not None, "T2 step 4: this field must no longer be null"
    assert isinstance(value, (int, float))
    assert 0.0 <= value <= 100.0
