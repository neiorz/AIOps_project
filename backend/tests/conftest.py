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


@pytest.fixture(scope="session", autouse=True)
def _no_cluster_side_effects(tmp_path_factory):
    """Make the suite incapable of mutating a real cluster.

    Tests inject PodFailure, run remediation and build Chaos Mesh
    experiments — all three reach for the cluster: `kubectl delete pod`,
    `kubectl rollout restart`, and CR creation. Against a developer's
    configured kubeconfig, with the workload deployed, a plain `make test`
    would delete their pods on every run.

    Two doors, both closed: kubectl reads KUBECONFIG, and the python client
    is patched directly, because telemetry falls back to ~/.kube/config when
    the env var points nowhere — env alone is not enough.
    """
    import os

    import kubernetes.config as k8s_config

    fake_kubeconfig = str(tmp_path_factory.mktemp("no-cluster") / "config")
    original_env = os.environ.get("KUBECONFIG")
    os.environ["KUBECONFIG"] = fake_kubeconfig

    original_load = k8s_config.load_kube_config
    original_incluster = k8s_config.load_incluster_config

    def _refuse(*_args, **_kwargs):
        raise RuntimeError("cluster access disabled during tests")

    k8s_config.load_kube_config = _refuse
    k8s_config.load_incluster_config = _refuse

    yield

    k8s_config.load_kube_config = original_load
    k8s_config.load_incluster_config = original_incluster
    if original_env is None:
        os.environ.pop("KUBECONFIG", None)
    else:
        os.environ["KUBECONFIG"] = original_env


@pytest.fixture(scope="session", autouse=True)
def _no_real_chaos_mesh():
    """Never let the test suite create real Chaos Mesh experiments.

    Track T7 made POST /chaos/inject capable of killing a real pod when the
    CRDs are installed and the workload is deployed. Tests call that endpoint
    freely, so without this guard a run on a developer's machine would inject
    actual faults into their cluster and leave experiments running behind it.
    """
    from app.api.chaos import ChaosMeshInjector

    original_detect = ChaosMeshInjector._detect
    ChaosMeshInjector._detect = lambda self: (False, "disabled under test")
    yield
    ChaosMeshInjector._detect = original_detect


@pytest.fixture(scope="session", autouse=True)
def _offline_llm_generation():
    """Keep the agent's LLM stage offline for the whole suite (Phase 2).

    investigate_incident now calls T3's generation contract on every
    investigation. Without this guard, every investigation test would dial a
    real Ollama server when one happens to be running (~11 s each), and T3
    step 5 requires `make test` to pass with no network at all.

    The default answers UNREACHABLE with no diagnosis and no narrative —
    precisely what the pre-pipeline agent produced, so existing assertions
    see bit-identical results. Tests that exercise the generation path
    replace the seam with their own answer.
    """
    from app.agent import sre_agent
    from app.api.llm import RCAResponse

    def _unreachable(_request):
        return RCAResponse(status="UNREACHABLE", citations=[])

    original = sre_agent._generate_rca
    sre_agent._generate_rca = _unreachable
    yield
    sre_agent._generate_rca = original


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

