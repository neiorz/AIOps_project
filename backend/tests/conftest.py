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
