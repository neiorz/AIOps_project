# 📘 Real On-Device AIOps Platform — Comprehensive Defense & Demonstration Guide
> **National Telecommunication Institute (NTI) Graduation Defense**  
> **Project:** Chaos-Grounded Autonomous AIOps Platform for Multi-Tenant Incident Detection, Correlation, AI Root-Cause Investigation & Human Approval Gates  
> **Host Environment:** RHEL 10.2 / Linux (`aarch64` / `x86_64`), Real Kubernetes & Local Microservice Mesh

---

## 📑 Table of Contents
1. [The Architecture Story: Complete DevOps & AIOps Lifecycle](#1-the-architecture-story-complete-devops--aiops-lifecycle)
2. [What Each DevOps Tool Does in Plain English](#2-what-each-devops-tool-does-in-plain-english)
3. [1-Line Verification Commands for Every Tool](#3-1-line-verification-commands-for-every-tool)
4. [The 4-Phase Incident Lifecycle](#4-the-4-phase-incident-lifecycle)
5. [Local Kubernetes & Microservice Setup](#5-local-kubernetes--microservice-setup)
6. [The 5 Dedicated Dashboard Views](#6-the-5-dedicated-dashboard-views)
7. [Step-by-Step Live Defense Demonstration Script](#7-step-by-step-live-defense-demonstration-script)
   - [Step 1: Baseline Health Check](#step-1-baseline-health-check)
   - [Step 2: Triggering Real Fault Injection (Cart Service Stress)](#step-2-triggering-real-fault-injection-cart-service-stress)
   - [Step 3: Verifying Alerts in Prometheus and UI](#step-3-verifying-alerts-in-prometheus-and-ui)
   - [Step 4: AI SRE Root-Cause Diagnosis (ChromaDB Vector Match)](#step-4-ai-sre-root-cause-diagnosis-chromadb-vector-match)
   - [Step 5: The Human Approval Gate (Approve vs Reject Self-Healing)](#step-5-the-human-approval-gate-approve-vs-reject-self-healing)
8. [Troubleshooting & Emergency Commands](#8-troubleshooting--emergency-commands)

---

## 1. The Architecture Story: Complete DevOps & AIOps Lifecycle

During your defense, present this single unified narrative that links **Terraform, Jenkins, Kubernetes, Prometheus, Grafana, ChromaDB, and Ansible** together into a production-grade AIOps pipeline:

```
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                   THE COMPLETE AIOps LIFECYCLE                                   │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘

   [ PHASE 1: PROVISION & DEPLOY ]
   Terraform ─────────▶ Provisions Kubernetes Namespaces (`aiops`, `chaos-mesh`)
      │
      ▼
   Jenkins ───────────▶ Runs automated CI/CD pipeline, compiles code, runs Pytest & deploys manifests
      │
      ▼
   Kubernetes (k3s) ──▶ Runs 5 Microservices (Deployments, Pods, Services) + Mesh Sockets

   [ PHASE 2: TELEMETRY & OBSERVABILITY ]
   Prometheus ────────▶ Scrapes `/metrics` every 2s (CPU, memory, latency, service_up)
      │
      ▼
   Grafana ───────────▶ Visualizes live availability matrices and latency distribution bars

   [ PHASE 3: CHAOS & AI DETECTION ]
   Chaos Mesh ────────▶ Injects REAL failure (kills container or burns 95% CPU loop)
      │
      ▼
   Sliding Window ────▶ Deduplicates 500+ cascading raw alerts into 1 Correlated Incident
      │
      ▼
   AI SRE (ChromaDB) ─▶ Queries vector store with local ONNX embeddings to retrieve exact Runbook

   [ PHASE 4: GOVERNANCE & REMEDIATION ]
   Approval Gate ─────▶ Pauses execution! Awaits explicit human operator sign-off ("Approve/Reject")
      │
      ▼ (Operator clicks "Approve")
   Ansible ───────────▶ Executes `ansible-playbook restart_service.yml` via real subprocess
      │
      ▼
   Kubernetes ────────▶ Resurrects container & verifies HTTP 200 healthcheck -> Incident RESOLVED
```

---

## 2. What Each DevOps Tool Does in Plain English

| Tool | Plain English Explanation | Role in This Project |
| :--- | :--- | :--- |
| **Terraform** | *"The Foundation Builder"*<br>Writes code to build infrastructure automatically rather than clicking in a cloud console. | Provisions the required Kubernetes namespaces (`aiops` and `chaos-mesh`) using the infrastructure-as-code pattern. |
| **Jenkins** | *"The Automated Assembly Line"*<br>Whenever code is updated, Jenkins pulls it, tests it, packages it, and deploys it. | Implements the CI/CD pipeline: runs `py_compile`, executes Pytest unit tests, and applies Kubernetes manifests. |
| **Kubernetes (k3s)** | *"The Container Conductor"*<br>Runs application pods, keeps them alive, and handles rolling restarts and healthchecks. | Orchestrates the Online Boutique microservices (`frontend`, `cart-service`, `payment-service`, `productcatalog-service`, `redis-cart`). |
| **Prometheus** | *"The Security Camera & Metric Recorder"*<br>Constantly probes every service to record CPU, memory, errors, and availability. | Scrapes real `/metrics` endpoints and evaluates alerting rules (e.g. `service_up == 0` or `process_cpu_percent > 85`). |
| **Grafana** | *"The Operations Dashboard"*<br>Translates raw numbers from Prometheus into clean visual dials, graphs, and heatmaps. | Renders live availability matrices and latency progress bars in the Command Center. |
| **ChromaDB Vector Store** | *"The AI's Brain / Search Library"*<br>Stores Markdown DevOps runbooks as high-dimensional semantic vectors. | Enables the AI SRE agent to find the exact recovery runbook in milliseconds using cosine similarity matching. |
| **Ansible** | *"The Automated Technician"*<br>Executes runbooks/playbooks over SSH or locally to configure servers and restart services. | Runs `ansible-playbook ansible/restart_service.yml -e service=<name>` upon human operator approval to self-heal the failed service. |

---

## 3. 1-Line Verification Commands for Every Tool

Use these commands live during your NTI defense to prove to the committee that every tool is real and running on the machine:

```bash
# 1. VERIFY TERRAFORM: Check infrastructure files and validate configuration
terraform fmt -check terraform/ && echo "Terraform Config: VALID"

# 2. VERIFY ANSIBLE: Execute playbook syntax check
ansible-playbook ansible/restart_service.yml --syntax-check

# 3. VERIFY PROMETHEUS METRICS: Direct curl showing live Prometheus exposition format
curl -s http://127.0.0.1:8000/api/v1/mesh/metrics | head -n 20

# 4. VERIFY LIVE MICROSERVICES: Inspect actual listening sockets under Python/Uvicorn
ss -tulpn | grep -E '8081|8082|8083|8084|6380'

# 5. VERIFY CHROMADB VECTOR STORE: Check indexed runbooks count
python3 -c "import sys; sys.path.insert(0, 'backend'); from app.rag.indexer import index_all_runbooks; print(f'ChromaDB Runbooks: {index_all_runbooks()}')"

# 6. VERIFY PYTEST SUITE: Run all 21 automated platform tests
./run.sh test
```

---

## 4. The 4-Phase Incident Lifecycle

When demonstrating an incident, walk the committee through the 4 distinct phases:

### Phase 1: Normal Operations (Baseline)
- All 5 microservices are healthy.
- Prometheus reports `service_up == 1` across all endpoints.
- Incident queue is empty (0 Active).

### Phase 2: Fault Injection & Alert Storm
- Operator or Chaos Mesh triggers a failure (e.g., `cart-service` CPU saturation or `payment-service` SIGKILL).
- The service drops offline; port returns HTTP 503 or latency exceeds 850ms.
- Downstream dependents cascade into 540+ raw alerts.
- The sliding-window correlation engine clusters all 540+ alerts into **1 Correlated Incident**.

### Phase 3: AI Investigation & RAG Vector Matching
- The AI SRE agent queries ChromaDB with: `cart-service Cart_Service_StressChaos`.
- ChromaDB performs cosine similarity search across indexed Markdown runbooks.
- Retrieves **`Runbook: Cart Service CPU Saturation & Resource Starvation`** with **~76% similarity match**.
- Formulates the exact recommended remediation command:
  ```bash
  ansible-playbook ansible/restart_service.yml -e service=cart-service
  ```

### Phase 4: Human Approval Gate & Self-Healing
- **Safety Halt:** Because Human Approval mode is enabled, the system **pauses execution** and sets status to `PENDING_APPROVAL`.
- Displays the prominent amber approval banner with **"✅ Approve & Execute"** and **"❌ Reject Remediation"** controls.
- When the operator clicks **"Approve"**:
  1. Backend executes `ansible-playbook ansible/restart_service.yml -e service=cart-service` via real subprocess.
  2. Ansible runs tasks and rolls out the restart.
  3. Mesh supervisor clears CPU stress, restores normal operations, and verifies HTTP 200 OK.
  4. Incident transitions permanently to **`RESOLVED`** and disappears from active queue.

---

## 5. Local Kubernetes & Microservice Setup

### Connecting Kubernetes (`kubectl get pods`)
Your RHEL machine has `k3s` running as a background service. To allow your user account to run `kubectl` commands directly:

Run the following command in your terminal:
```bash
# Set up ~/.kube/config from k3s
mkdir -p ~/.kube
sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
sudo chown $(id -u):$(id -g) ~/.kube/config
chmod 600 ~/.kube/config

# Deploy the Online Boutique microservices
kubectl apply -f k8s/manifests/online-boutique.yaml

# Verify real running pods
kubectl get pods
```

---

## 6. The 5 Dedicated Dashboard Views

Open `http://localhost:8000` in your web browser:

1. **Tab 1: System Architecture (`#architecture`)**
   - High-level traffic ingress flow: `Client -> frontend (8083) -> cart-service (8082) & payment-service (8081) -> redis-cart (6380)`.
   - Real-time status cards showing live PID, port, CPU %, and memory usage.
   - When a service fails, its node in the diagram **turns bright red with an active warning border**.
2. **Tab 2: Metrics & Prometheus (`#metrics`)**
   - Cluster CPU %, resident memory, P95 latency (ms), and HTTP error rate.
   - Direct raw feed from `GET /api/v1/mesh/metrics` scraped from the real processes.
   - Real-time PromQL search filter (e.g. type `service_up` or `process_cpu_percent`).
3. **Tab 3: Visualization & Grafana (`#grafana`)**
   - Visual service availability matrix and request latency distribution bars.
4. **Tab 4: Chaos Engineering (`#chaos`)**
   - Interactive fault console: Select microservice, select real fault type, and click **"💥 Inject Real Fault & Trigger Alert"**.
   - Terminal log displays the exact action and ground-truth record.
5. **Tab 5: Incidents & Approvals (`#incidents`)**
   - Triage queue prioritized by SLA risk score.
   - AI SRE diagnosis card with ChromaDB vector runbook match.
   - **The Human Approval Gate Card:**
     - Displays: `ACTION PAUSED — HUMAN APPROVAL REQUIRED`.
     - Displays proposed command: `ansible-playbook ansible/restart_service.yml -e service=cart-service`.
     - **"✅ Approve & Execute"** -> Executes real Ansible playbook, resurrects the port, verifies HTTP 200, marks incident `RESOLVED`.
     - **"❌ Reject Remediation"** -> Cancels execution, preserves incident for manual inspection.

---

## 7. Step-by-Step Live Defense Demonstration Script

Follow this script during your graduation defense presentation:

### Step 1: Baseline Health Check
1. Open `http://localhost:8000` in the browser.
2. Direct the committee's attention to the header:
   - Point out **"Mesh Status: 5/5 UP"**.
   - Note the **"Human Approval Gate (Active)"** badge in amber.
3. Click on **Tab 1: System Architecture**:
   - Show the architecture diagram: All services are green (`UP`).
   - Show the live resource table: Real CPU (~1-2%) and real memory (~20-50MB per process).
4. Click on **Tab 2: Metrics & Prometheus**:
   - Show that Prometheus is actively scraping real metrics (`service_up == 1`).

### Step 2: Triggering Real Fault Injection (Cart Service Stress)
1. Click on **Tab 4: Chaos Engineering**.
2. Set the controls:
   - **Target Microservice:** `cart-service (Port 8082 - C#)`
   - **Experiment Type:** `🔥 StressChaos (Spike CPU & Memory >90%)`
   - **Tenant SLA Context:** `Tenant B (Standard - 15 Minute SLA)`
3. Click the **"💥 Inject Real Fault & Trigger Alert"** button.
4. Watch the Chaos Terminal:
   - It reports: `[CHAOS] Real CPU & Memory stress burner activated on cart-service`.
   - Ground truth recorded in PostgreSQL ledger.
   - Cascading alert storm triggered (542 alerts compressed into 1 correlated incident).

### Step 3: Verifying Alerts in Prometheus and UI
1. The dashboard will automatically switch to **Tab 5: Incidents & Approvals**.
2. Notice the new incident in the queue: `Cart_Service_StressChaos`.
3. Switch briefly to **Tab 1: System Architecture**:
   - Notice `cart-service` CPU has spiked to **94.6%**!
4. Switch to **Tab 2: Metrics & Prometheus**:
   - Notice `process_cpu_percent{service="cart-service"}` has spiked above 90%.

### Step 4: AI SRE Root-Cause Diagnosis (ChromaDB Vector Match)
1. Return to **Tab 5: Incidents & Approvals**.
2. Click on the active incident in the queue.
3. Show the **AI SRE Diagnosis Block**:
   - **Headline:** `Runbook: Cart Service CPU Saturation & Resource Starvation`
   - **Confidence Score:** `76.3% Match`
   - **Matched Runbook:** `runbooks/cart_service_failure.md` retrieved from ChromaDB vector store via local MiniLM ONNX embeddings.

### Step 5: The Human Approval Gate (Approve vs Reject Self-Healing)
1. Point to the amber banner: **"ACTION PAUSED — HUMAN APPROVAL REQUIRED"**.
2. Explain to the committee:
   > *"Notice that despite the AI identifying the solution with high confidence, our safety guardrails prevent any autonomous action. The system has safely halted and is awaiting our explicit authorization."*
3. Show the proposed remediation command:
   ```bash
   ansible-playbook ansible/restart_service.yml -e service=cart-service
   ```
4. Click **"✅ Approve & Execute Self-Healing"**:
   - Watch the live execution terminal stream the **real Ansible execution output** (`PLAY RECAP: localhost: ok=4 changed=0 ...`).
   - The self-healing engine resets the CPU burn on `cart-service`.
   - The health check succeeds (`HTTP 200 OK`).
   - The incident transitions to **`RESOLVED`** and disappears from the active queue.
5. Switch back to **Tab 1: System Architecture**:
   - Show the committee that `cart-service` CPU utilization has returned to normal baseline (~1.2%)!

---

## 8. Troubleshooting & Emergency Commands

If any local process gets stuck or an unexpected state occurs during testing:

### 1. Restart All Microservices
```bash
curl -X POST http://localhost:8000/api/v1/mesh/start-all
```

### 2. Check Port Availability
```bash
ss -tulpn | grep 8000
```

### 3. Run Automated Pytest Suite
```bash
./run.sh test
```
All 21 unit tests verify RAG indexing, alert correlation, SLA calculators, real mesh status, and the human approval workflow.

### 4. Reset Backend Daemon
```bash
pkill -f "run.sh dev-backend"
./run.sh dev-backend
```
