.PHONY: help setup install test lint run dev-backend dev-dashboard ui \
        index-runbooks k8s-deploy chaos-inject observability-up clean

PYTHON_VENV = backend/.venv/bin
PYTHON = $(PYTHON_VENV)/python
PIP = $(PYTHON_VENV)/pip
PYTEST = $(PYTHON_VENV)/pytest
API = http://localhost:8000/api/v1

help:
	@echo "AIOps Platform - Autonomous Incident Detection & Self-Healing"
	@echo "============================================================"
	@echo "Available targets:"
	@echo "  make setup            - Initialize Python venv and install dependencies"
	@echo "  make test             - Run automated backend test suite (unit & RAG)"
	@echo "  make lint             - Syntax-check every Python file (same as CI)"
	@echo "  make dev-backend      - Run FastAPI backend in reload mode (port 8000)"
	@echo "  make ui               - Run the Streamlit dashboard (Track T6)"
	@echo "  make dev-dashboard    - Run the React dashboard dev server (Track T6 alt)"
	@echo "  make index-runbooks   - Index DevOps runbooks into ChromaDB vector store"
	@echo "  make observability-up - Start local Prometheus/Loki/Tempo (Track T1)"
	@echo "  make k8s-deploy       - Deploy target microservices to Kubernetes"
	@echo "  make chaos-inject     - Inject a sample chaos experiment via the API"
	@echo "  make clean            - Remove temporary files and cached data"

setup install:
	python3 -m venv backend/.venv
	$(PIP) install --upgrade pip
	$(PIP) install -r backend/requirements.txt

test:
	$(PYTEST) -v backend/tests/

lint:
	@echo "Syntax-checking all Python files..."
	find . -path ./backend/.venv -prune -o -name "*.py" -print | \
	  xargs -r $(PYTHON) -m py_compile
	@echo "Lint OK."

index-runbooks:
	cd backend && ../$(PYTHON) -m app.rag.indexer

dev-backend:
	cd backend && ../$(PYTHON_VENV)/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# --- Track T6 : Streamlit dashboard -----------------------------------------
ui:
	@if [ ! -f streamlit_app.py ]; then \
	  echo "streamlit_app.py not found yet - this target is owned by Track T6."; \
	  exit 1; \
	fi
	$(PYTHON) -m streamlit run streamlit_app.py --server.port $${STREAMLIT_PORT:-8501}

# --- React dashboard (dashboard/) -------------------------------------------
# NOTE: dashboard/ currently lacks index.html + src/main.jsx, so Vite cannot run.
dev-dashboard:
	@if [ ! -f dashboard/index.html ]; then \
	  echo "dashboard/index.html is missing - the React app is incomplete."; \
	  echo "The working UI is served by the backend at http://localhost:8000/"; \
	  exit 1; \
	fi
	cd dashboard && npm install && npm run dev

# --- Track T1 : local observability stack -----------------------------------
observability-up:
	@if [ ! -f observability/docker-compose.yml ]; then \
	  echo "observability/docker-compose.yml not found - owned by Track T1."; \
	  exit 1; \
	fi
	docker compose -f observability/docker-compose.yml up -d

k8s-deploy:
	kubectl apply -f k8s/manifests/online-boutique.yaml

chaos-inject:
	curl -sS -X POST $(API)/chaos/inject \
	  -H 'Content-Type: application/json' \
	  -d '{"service":"cart-service","experiment_type":"NetworkLatency","tenant_id":"tenant_a"}' | jq .

clean:
	find . -path ./backend/.venv -prune -o -type d -name "__pycache__" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
