# Chaos-Grounded Autonomous AIOps Platform

Autonomous, end-to-end AIOps platform for multi-tenant incident detection, correlation, and AI-grounded root-cause investigation (Graduation Project for NTI).

## Project Overview

- **Target Application**: Google Cloud Online Boutique (10 microservices: frontend, cartservice, productcatalogservice, currencyservice, paymentservice, shippingservice, emailservice, checkoutservice, recommendationservice, adservice).
- **Fault Injection**: Chaos Mesh (NetworkChaos, PodChaos, StressChaos).
- **Telemetry**: OpenTelemetry Collector exporting to Prometheus (metrics), Grafana Loki (logs), and Grafana Tempo (traces).
- **Correlation & Deduplication**: FastAPI + Redis sliding-window clustering engine.
- **RAG Knowledge Base**: ChromaDB vector store indexing DevOps runbooks, architecture topologies, and historical post-mortems.
- **Autonomous AI Agent**: ReAct-pattern SRE Agent with tool-calling to PromQL, LogQL, TraceQL, and Kubernetes API.
- **Multi-Tenant SLA Risk Engine**: Dynamic SLA prioritization (Client A: 5 min, Client B: 15 min, Client C: 30 min).
- **Ground-Truth Ledger**: PostgreSQL ledger tracking injected chaos vs AI diagnosis (RCA Accuracy %, Detection Latency, Evidence Score).
- **Remediation**: Automated K8s API patches and Ansible self-healing playbooks.
- **CI/CD & Benchmarks**: Jenkins automated chaos benchmark pipeline.
- **Dashboard**: Modern React Web Dashboard.

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
├── dashboard/                # React Web Dashboard (live incidents, SLA countdown, RCA explorer)
└── Makefile                  # 1-click commands (make setup, make test, make chaos)
```

## Quick Start (Phase 1)

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
