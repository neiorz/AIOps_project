import pytest
import sys
from pathlib import Path

# Add backend directory to sys.path so app imports resolve
backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from fastapi.testclient import TestClient
from app.main import app

@pytest.fixture(scope="session")
def test_client():
    with TestClient(app) as client:
        yield client


@pytest.fixture(scope="session", autouse=True)
def _isolate_database(tmp_path_factory):
    """Point every test at a throwaway SQLite file.

    Chaos-injection tests call the new persistence seams, which would
    otherwise write fixture rows into the developer's real ledger and make
    the scorecard and /ground-truth report fake experiments afterwards.
    """
    from app.db import session as db_session

    original_path = db_session.SQLITE_PATH
    original_try = db_session._try_postgres

    db_session.SQLITE_PATH = tmp_path_factory.mktemp("db") / "test.db"
    # Skip the Postgres probe entirely: it costs seconds per run and tests
    # should exercise the SQLite path deterministically.
    def _no_postgres():
        raise RuntimeError("Postgres disabled during tests")
    db_session._try_postgres = _no_postgres
    db_session.reset_engine_for_tests()

    yield

    db_session.reset_engine_for_tests()
    db_session.SQLITE_PATH = original_path
    db_session._try_postgres = original_try


@pytest.fixture
def isolated_model(tmp_path, monkeypatch):
    """Point ML_MODEL_PATH + the anomaly log at throwaway files.

    Without this, tests write real detections into the repo and the contract
    test stops being deterministic once a model exists.
    """
    from app.config import settings
    from app.ml import collector, scoring

    monkeypatch.setattr(settings, "ML_MODEL_PATH", tmp_path / "model.joblib")
    monkeypatch.setattr(collector, "ANOMALIES_JSON", tmp_path / "anomalies.json")
    monkeypatch.setattr(scoring, "ANOMALIES_JSON", tmp_path / "anomalies.json")
    scoring.invalidate_cache()
    yield tmp_path
    scoring.invalidate_cache()


@pytest.fixture
def isolated_features(tmp_path, monkeypatch):
    """Point the feature store at a throwaway CSV."""
    from app.ml import collector

    path = tmp_path / "features.csv"
    monkeypatch.setattr(collector, "FEATURES_CSV", path)
    yield path

