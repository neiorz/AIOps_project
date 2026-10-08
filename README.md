# Chaos-Grounded Autonomous AIOps Platform

Autonomous, end-to-end AIOps platform for multi-tenant incident detection, correlation, and AI-grounded root-cause investigation (Graduation Project for NTI).

## Project Overview

- **Target Application**: Google Cloud Online Boutique (10 microservices: frontend, cartservice, productcatalogservice, currencyservice, paymentservice, shippingservice, emailservice, checkoutservice, recommendationservice, adservice).
- **Fault Injection**: Chaos Mesh (NetworkChaos, PodChaos, StressChaos).
- **Telemetry**: OpenTelemetry Collector exporting to Prometheus (metrics), Grafana Loki (logs), and Grafana Tempo (traces).
- **Correlation & Deduplication**: FastAPI in-memory sliding-window clustering engine (120s window, tenant-isolated).
- **RAG Knowledge Base**: ChromaDB vector store indexing DevOps runbooks, architecture topologies, and historical post-mortems.
- **Autonomous AI Agent**: ReAct-pattern SRE Agent with tool-calling to PromQL, LogQL, TraceQL, and Kubernetes API.
- **Multi-Tenant SLA Risk Engine**: Dynamic SLA prioritization (Client A: 5 min, Client B: 15 min, Client C: 30 min).
- **Ground-Truth Ledger**: SQL ledger (SQLite by default; PostgreSQL via `POSTGRES_URL`) tracking injected chaos vs AI diagnosis (RCA Accuracy %, Detection Latency, Evidence Score).
- **Remediation**: Automated K8s API patches and Ansible self-healing playbooks.
- **CI/CD**: Jenkins pipeline (`jenkins/Jenkinsfile`).
- **Evaluation Scorecard**: `GET /api/v1/benchmarks/scorecard` computes accuracy, MTTD, noise reduction and tool usage from the ground-truth ledger and live counters — nothing is hardcoded.
- **Dashboards**: Streamlit operator UI (`streamlit_app.py`, `make ui`) + React explorer (`dashboard/`).

## Repository Architecture

```text
aiops-platform/
├── terraform/                # Infrastructure as Code (VPC, VMs, K8s cluster)
├── ansible/                  # Node bootstrap & AI self-healing playbooks (clean_disk.yml, restart_service.yml)
├── jenkins/                  # Jenkinsfile for CI/CD and scheduled Chaos benchmarks
├── k8s/                      # Kubernetes manifests & Helm values (Online Boutique, Chaos Mesh, Observability)
├── backend/                  # FastAPI AIOps Engine
│   ├── app/
│   │   ├── api/              # Alert webhooks, SLA priority, and benchmark APIs
│   │   ├── correlation/      # Alert clustering & deduplication engine (Redis)
│   │   ├── rag/              # ChromaDB vector store & DevOps runbook retriever
│   │   ├── agent/            # Autonomous LLM SRE Agent (ReAct tool-calling pattern)
│   │   ├── tools/            # Prometheus, Loki, Tempo, and K8s API tool definitions
│   │   └── sla/              # Multi-tenant SLA risk & priority queue calculator
│   ├── runbooks/             # Markdown DevOps runbooks & troubleshooting SOPs
│   ├── tests/                # Automated pytest suite (endpoints, RAG, agent logic)
│   ├── Dockerfile
│   └── requirements.txt
├── dashboard/                # React web explorer (Vite + React 18)
├── observability/            # Prometheus / Loki / Tempo configs (docker compose)
├── streamlit_app.py          # Streamlit operator dashboard (make ui)
└── Makefile                  # 1-click commands (make setup, make test, make chaos)
```

## Quick Start

1. Setup environment and install dependencies:
   ```bash
   make setup
   ```
2. Run automated backend test suite:
   ```bash
   make test
   ```
3. Start backend development server:
   ```bash
   make dev-backend
   ```
   API Docs available at `http://localhost:8000/docs`.
4. Start the operator dashboard:
   ```bash
   make ui
   ```
   Open `http://localhost:8501`.
5. Train the anomaly model — `model.joblib` is intentionally **not**
   committed, so a clean clone trains it from the tracked
   `backend/app/ml/data/features.csv`:
   ```bash
   curl -X POST localhost:8000/api/v1/anomalies/train
   ```

## Measured Evaluation Results (Phase 3 benchmark run)

Every number below is read from a live API response — reproduce with:

```bash
curl -s localhost:8000/api/v1/benchmarks/scorecard   # scored metrics
curl -s localhost:8000/api/v1/chaos/ledger           # raw ground truth
```

| Metric | Measured value | Source field |
|---|---|---|
| RCA accuracy | **71.4%** (15 of 21 experiments correct) | `rca_accuracy_percentage` |
| Noise reduction | **62.5%** (16 raw alerts → 6 incidents) | `noise_reduction_percentage` |
| Mean time to detect | **18.751 s** | `mean_time_to_detect_seconds` |
| SLA protection rate | **100%** (no SLA breach in the run) | `sla_protection_rate` |
| Tool calls during the run | **622** total, avg **4.0** per RCA (mesh 6 / PromQL 6 / LogQL 6 / k8s 604) | `investigation_efficiency` |
| Ground-truth experiments | **21** rows: 13 real Chaos Mesh CRs, 1 disclosed local-simulation fallback, 7 pre-contract rows with no recorded mode | ledger `injection_mode` |
| Per-RCA duration | **18.3 – 29.6 s** including the llama3.2:3b narrative | ledger `investigation_duration_seconds` |
| Regression suite | **249 tests passing** (offline, deterministic) | `make test` |

The accuracy figure is unclamped — incorrect diagnoses lower it by design
(`backend/app/api/benchmarks.py`).

Scope note: ledger-backed fields (accuracy, MTTD, experiments) survive server
restarts, while counter fields (tool calls, noise reduction) reflect the
**current** server process — after a restart they honestly read `0` / `null`
until new activity arrives.

