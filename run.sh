#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$DIR/backend/.venv"
PYTHON="$VENV_DIR/bin/python"
PIP="$VENV_DIR/bin/pip"
PYTEST="$VENV_DIR/bin/pytest"
UVICORN="$VENV_DIR/bin/uvicorn"

case "$1" in
  setup)
    echo "==> Initializing Python virtual environment..."
    python3 -m venv "$VENV_DIR"
    "$PIP" install --upgrade pip
    "$PIP" install -r "$DIR/backend/requirements.txt"
    echo "==> Setup complete."
    ;;
  test)
    echo "==> Running backend automated test suite..."
    "$PYTEST" -v "$DIR/backend/tests"
    ;;
  index-runbooks)
    echo "==> Indexing DevOps runbooks into ChromaDB..."
    PYTHONPATH="$DIR/backend" "$PYTHON" -m app.rag.indexer
    ;;
  dev-backend)
    echo "==> Launching FastAPI AIOps Engine..."
    cd "$DIR/backend" && PYTHONPATH="$DIR/backend" "$UVICORN" app.main:app --host 0.0.0.0 --port 8000 --reload
    ;;
  *)
    echo "Usage: ./run.sh [setup|test|index-runbooks|dev-backend]"
    exit 1
    ;;
esac
