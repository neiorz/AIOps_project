# Chaos-Grounded Autonomous AIOps Platform

Autonomous, end-to-end AIOps platform for multi-tenant incident detection, correlation, AI-grounded root-cause investigation, and automated remediation — with every published number measured live by the platform itself.

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

## Measured Evaluation Results

Every number below is read from a live API response — reproduce with:

```bash
curl -s localhost:8000/api/v1/benchmarks/scorecard   # scored metrics
curl -s localhost:8000/api/v1/chaos/ledger           # raw ground truth
```

| Metric | Measured value | Source field |
|---|---|---|
| RCA accuracy | **66.7%** (26 of 39 experiments correct) | `rca_accuracy_percentage` |
| Noise reduction | **62.5%** (16 raw alerts → 6 incidents, measured during the benchmark run) | `noise_reduction_percentage` |
| Mean time to detect | **20.818 s** | `mean_time_to_detect_seconds` |
| SLA protection rate | **100%** (no SLA breach in the run) | `sla_protection_rate` |
| Tool calls during the run | **505** total (current server process) | `investigation_efficiency` |
| Ground-truth experiments | **39** rows: 30 real Chaos Mesh CRs, 2 disclosed local-simulation fallbacks, 7 pre-contract rows with no recorded mode | ledger `injection_mode` |
| Per-RCA duration | **15.6 s median** (20.8 s mean across 39 rows, including the llama3.2:3b narrative) | ledger `investigation_duration_seconds` |
| Regression suite | **262 tests passing** (offline, deterministic) | `make test` |

The accuracy figure is unclamped — incorrect diagnoses lower it by design
(`backend/app/api/benchmarks.py`).

Scope note: ledger-backed fields (accuracy, MTTD, experiments) survive server
restarts, while counter fields (tool calls, noise reduction) reflect the
**current** server process — after a restart they honestly read `0` / `null`
until new activity arrives.

## Machine Learning Models

Every model in the stack, with its measured accuracy and rating — all
visible live in the operator dashboard's **Models** tab. The detector's
numbers come from a held-out evaluation (790 rows it never trained on),
recorded beside the model at promotion time and served by
`GET /api/v1/anomalies/evaluation`; the log model records its evaluation
beside itself the same way. Reproduce:

```bash
cd backend && .venv/bin/python -m app.ml.retrain --force   # metrics detector
.venv/bin/python -m app.ml.train_logs                      # log detector (needs kubectl + cluster)
```

| Model | Role | Measured accuracy & rating |
|---|---|---|
| **Isolation Forest** (`model.joblib`) | Anomaly detection on service metrics (`cpu_percent`, `memory_mb`, `latency_s`, `error_rate`, `service_up`) | **Accuracy 99.49%** · precision 98.77% · recall 100% · F1 99.38% · AUROC 99.91% · FPR 0.85% (TP 320 / FP 4 / TN 466 / FN 0, on 790 held-out rows) — **2/2 promotion gates pass**: live flag rate **3.03%** ≤ 15%, faulted-service AUROC 0.9991 ≥ 0.95, recall 1.00 ≥ 0.90. Calibration: target 5% → measured 4.55% on 66 live rows |
| **Log sequence detector** (`log_model.joblib`) | Anomaly detection on service **log streams** — DeepLog-style: mined log templates + per-service next-template transitions | **Next-event accuracy 92.02%** on 188 held-out healthy events · false-alarm rate **7.98%** · **100% detection** of a real fault window (297 events captured while an 850 ms latency fault was active in cartservice — all 148 fault-signature lines flagged as unseen templates, plus the ordinary request lines that follow them, so the detector surfaces the whole disrupted interval). Window 3 / top-k 2 · 9 templates over 4 services (cart, frontend, payment, productcatalog) |
| **ReAct RCA agent** + ChromaDB retrieval | Root-cause diagnosis from runbooks & post-mortems | **66.7%** diagnosis accuracy (26 of 39 chaos experiments), unclamped — `GET /api/v1/benchmarks/scorecard` |
| **llama3.2:3b** (local Ollama) | Incident narrative & post-mortem drafting | Status-gated via `GET /api/v1/llm/health` — by design it never supplies a score or confidence; gate numbers come only from retrieval agreement |
| **ChromaDB `all-MiniLM-L6-v2`** | Runbook / topology / post-mortem retrieval | No independent metric by design — its quality surfaces in the RCA accuracy above |

The detector is scored on the *faulted-service* view — the view the promotion
decision is judged on. The pooled view, which includes the healthy bystanders
of a chaos run (scoring them at chance is correct behaviour), is recorded
alongside it in the same response, so nothing is hidden.

The log detector is trained on the services' own pod logs (collected live
via `kubectl`), split in time order — it only ever trains on the past. Its
fault window was produced by activating the platform's local latency
mechanism inside cartservice (`echo 850 > /tmp/latency_ms`, the same
command the platform's local-simulation path writes), driving real traffic
during the fault, and removing the file afterwards — the fault-signature
lines it must catch carry the cause in their own text.

Its honest blind spot: a fault with **no log signature** (silent CPU burn,
pure packet delay without the warning line) is invisible to a logs-only
detector — which is why the metrics detector remains the primary channel;
the two cover each other's gaps. Scale-up path: swap the count-based
transitions for an LSTM (DeepLog proper) or LogBERT when log volume grows —
same templates, same evaluation harness.

