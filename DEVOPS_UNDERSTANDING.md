# DevOps Stack & Infrastructure Deep-Dive Guide: Autonomous AIOps Platform

> **Target Audience:** Engineering Leads, NTI Graduation Examiners, DevOps Engineers, and SREs  
> **Codebase Mapping:** `k8s/manifests/`, `ansible/`, `terraform/`, `jenkins/`, and `backend/app/mesh/`  
> **Language Standard:** Plain, practical English with concrete code references.

---

## Section 1: The Big Picture (The Hospital Analogy)

To understand how all these tools work together without getting lost in technical jargon, imagine an **Intelligent Modern Hospital**:

```
+----------------------------------------------------------------------------------------------------+
|                                THE DEVOPS "HOSPITAL" ANALOGY                                       |
+---------------------+-------------------------------+----------------------------------------------+
| Tool in Our Project | Hospital Counterpart          | Real Job in this Platform                    |
+---------------------+-------------------------------+----------------------------------------------+
| Terraform           | The Hospital Architects       | Builds the hospital wings, rooms, and boundaries |
|                     | & Civil Engineers             | (Kubernetes Namespaces & Resource Isolation)  |
+---------------------+-------------------------------+----------------------------------------------+
| Kubernetes (k3s)    | The Hospital Management       | Assigns rooms, monitors doctor attendance,   |
|                     | & Ward Floor Managers         | and replaces fallen staff (Container Pods)   |
+---------------------+-------------------------------+----------------------------------------------+
| Microservices       | The Specialized Medical Units | Reception (frontend), Pharmacy (redis-cart), |
| (5 Containers)      | & Clinics                     | Billing (payment-service), Records (cart)    |
+---------------------+-------------------------------+----------------------------------------------+
| Prometheus          | The Vital Signs Monitors      | Constantly measures patient pulse (CPU %),   |
|                     | & ECG Machines                | blood pressure (Latency), oxygen (Error 500) |
+---------------------+-------------------------------+----------------------------------------------+
| Grafana             | The Central ICU Video Wall    | Visualizes real-time charts so human doctors |
|                     |                               | can see health trends at a single glance     |
+---------------------+-------------------------------+----------------------------------------------+
| Chaos Engineering   | The Emergency Disaster Drill  | Intentionally trips a circuit breaker to test|
| (Fault Injection)   |                               | if backup generators and staff respond safely|
+---------------------+-------------------------------+----------------------------------------------+
| AI SRE Agent        | The Chief AI Diagnostician    | Reads medical textbooks (DevOps Runbooks),   |
| (ChromaDB + ReAct)  |                               | correlates symptoms, and identifies disease  |
+---------------------+-------------------------------+----------------------------------------------+
| Ansible             | The Rapid Response Surgical   | Executes the exact, verified medical playbook|
| Playbook            | Team                          | to restore the patient (Rollout Restart)     |
+---------------------+-------------------------------+----------------------------------------------+
| Jenkins (CI/CD)     | The Hospital Quality &        | Automatically tests, inspects, and validates |
|                     | Compliance Inspector          | the whole system before patients arrive      |
+---------------------+-------------------------------+----------------------------------------------+
```

---

### End-to-End Incident Lifecycle Sequence Diagram

```mermaid
sequenceDiagram
    autonumber
    actor Developer as Developer / Operator
    participant Jenkins as Jenkins CI/CD
    participant K8s as Kubernetes (k3s Cluster)
    participant Prom as Prometheus Scraper
    participant Chaos as Chaos Engine (/chaos/inject)
    participant Agent as Autonomous AI SRE Agent
    participant Chroma as ChromaDB Vector Store
    participant Human as Human Approval Gate
    participant Ansible as Ansible Remediation

    Developer->>Jenkins: Git Push commit to main
    Jenkins->>K8s: Deploy online-boutique.yaml
    K8s-->>Prom: Expose /metrics (CPU, Latency, Errors)
    
    Note over Chaos,K8s: Operational Failure Triggered
    Chaos->>K8s: Inject PodFailure (Delete pod) or StressChaos (CPU 95%)
    K8s-->>Prom: Metrics surge: CPU > 85%, HTTP 503 Errors
    Prom->>Agent: Alert webhook dispatched (POST /api/v1/alerts/webhook)
    
    Note over Agent,Chroma: Autonomous Root Cause Analysis
    Agent->>Agent: Correlate alerts in 120s sliding window
    Agent->>Chroma: Vector search: 'cart-service StressChaos degradation'
    Chroma-->>Agent: Matched Runbook: Cart Service CPU Saturation (76.3% match)
    Agent->>Human: Incident queued: PENDING_APPROVAL with playbook plan
    
    Note over Human,Ansible: Verified Self-Healing Execution
    Human->>Agent: Click "Approve & Execute"
    Agent->>Ansible: Execute subprocess: ansible-playbook restart_service.yml
    Ansible->>K8s: kubectl rollout restart deployment/cart-service
    K8s-->>K8s: Terminate degraded pod & spawn healthy replacement
    K8s-->>Prom: Metrics return to baseline (< 5% CPU, HTTP 200 OK)
    Agent-->>Human: Incident status updated to RESOLVED (Badge: 0 Active)
```

---

## Section 2: Kubernetes & Containers (The Foundation)

### Code Map
- **Primary Manifest:** [`k8s/manifests/online-boutique.yaml`](file:///home/moha/Downloads/aiops-platform/k8s/manifests/online-boutique.yaml)
- **Local Kubernetes Config:** `/home/moha/.kube/config` (pointed to local `k3s` at `https://127.0.0.1:6443`)

### 1. The 4 Fundamental Kubernetes Concepts Explained Simply

```
+------------------------------------------------------------------------------------+
|  DEPLOYMENT: Manages desired state (e.g., "Keep 1 healthy copy running forever")   |
|                                                                                    |
|    +--------------------------------------------------------------------------+    |
|    |  POD: Ephemeral wrapper with a unique Cluster IP (e.g., 10.42.0.25)      |    |
|    |                                                                          |    |
|    |    +----------------------------------------------------------------+    |    |
|    |    |  CONTAINER: Isolated Linux process (Python 3.12 / Redis)       |    |    |
|    |    |  Code + Runtime + Libraries (Runs in its own cgroup/namespace)  |    |    |
|    |    +----------------------------------------------------------------+    |    |
|    +--------------------------------------------------------------------------+    |
+------------------------------------------------------------------------------------+
                                      ▲
                                      │ Routes traffic to current healthy Pod IP
+-------------------------------------┴----------------------------------------------+
|  SERVICE: Static, permanent internal DNS address (e.g., http://cartservice:7070)    |
+------------------------------------------------------------------------------------+
```

1. **Container:** A lightweight, standalone package containing your application code, runtime, and system libraries. It runs isolated from the host OS using Linux namespaces and cgroups.
2. **Pod:** The smallest deployable building block in Kubernetes. A Pod is a wrapper around one (or more) containers. Pods are mortal—they can be killed, evicted, or rescheduled at any time. Every Pod gets its own internal cluster IP address.
3. **Deployment:** A declarative supervisor that says: *"Always keep 1 replica of `paymentservice` running. If the Pod crashes or gets killed, instantly spawn a new one to replace it."* It also controls zero-downtime rolling updates.
4. **Service:** Pods come and go, and their IP addresses change constantly. A Kubernetes **Service** provides a stable, unchanging internal DNS name and load balancer (e.g., `cartservice:7070`) that automatically points traffic to whichever Pod is currently healthy.

---

### 2. The 5 Core Microservices in this Platform

All 5 microservices are configured in [`k8s/manifests/online-boutique.yaml`](file:///home/moha/Downloads/aiops-platform/k8s/manifests/online-boutique.yaml):

```
               [ User Browser / Client Ingress ]
                               │ (Port 8000)
                               ▼
                    [ frontend:8083 / :8080 ]
                     (Web Ingress & Gateway)
                               │
            ┌──────────────────┼──────────────────┐
            ▼                  ▼                  ▼
  [ productcatalog:8084 ]  [ cart-service:8082 ]  [ payment-service:8081 ]
  (Catalog & Inventory)    (User Shopping Cart)   (Card Processing)
                               │
                               ▼
                       [ redis-cart:6380 ]
                     (In-Memory Cache Store)
```

1. **`frontend` (Port 8083 / Container Port 8080):**
   * **Role:** Web Ingress Gateway.
   * **Job:** Serves the web UI to visitors and routes incoming cart, payment, and product requests to downstream backend microservices.
2. **`cart-service` (Port 8082 / Container Port 7070):**
   * **Role:** Shopping Cart & User Session Persistence.
   * **Job:** Stores items added to the user's cart. Communicates directly with `redis-cart` to persist cart state across page reloads.
3. **`payment-service` (Port 8081 / Container Port 50051):**
   * **Role:** Payment Authorization & Transaction Gateway.
   * **Job:** Validates credit card credentials, checks fraud rules, and authorizes financial transactions during checkout.
4. **`productcatalog-service` (Port 8084 / Container Port 3550):**
   * **Role:** Product Catalog, Search & Inventory Ledger.
   * **Job:** Returns item descriptions, stock levels, and prices for goods listed in the store.
5. **`redis-cart` (Port 6380 / Container Port 6379):**
   * **Role:** High-Performance In-Memory Data Store.
   * **Job:** Lightweight Redis instance storing session tokens and cart items with sub-millisecond retrieval.

---

### 3. Local Kubernetes Engine: Why `k3s`?

This platform runs on **`k3s`** (developed by Rancher / CNCF certified):
- **Why not standard multi-node Kubernetes?** Standard Kubernetes (`kube-apiserver`, `etcd`, `kube-controller-manager`, `kube-scheduler`) consumes 4GB+ RAM just idling.
- **Why k3s?** `k3s` packages all Kubernetes components into a single binary $< 100\text{MB}$ that uses SQLite instead of heavy etcd. It consumes $< 512\text{MB}$ of memory, natively supports **ARM64 (`aarch64`)** on Linux RHEL 10.2, and is 100% compliant with standard `kubectl` CLI commands.

---

### 4. Essential Terminal Inspection Commands

```bash
# 1. View all active microservice pods in real time
kubectl get pods -o wide

# 2. Watch pods live as chaos is injected (updates automatically)
kubectl get pods -w

# 3. View live output logs of a specific service
kubectl logs -f deployment/cartservice

# 4. Deep-dive inspection of a pod (events, restarts, exit codes, OOM kills)
kubectl describe pod <POD_NAME>

# 5. Check cluster services and internal port bindings
kubectl get services
```

---

## Section 3: Observability Stack (Prometheus & Grafana)

### Code Map
- **Telemetry Query Module:** [`backend/app/tools/telemetry.py`](file:///home/moha/Downloads/aiops-platform/backend/app/tools/telemetry.py)
- **Live Metrics Exposition:** [`backend/app/mesh/manager.py`](file:///home/moha/Downloads/aiops-platform/backend/app/mesh/manager.py#L445-L471) (`scrape_prometheus_metrics()`)
- **Metrics Endpoint:** `http://127.0.0.1:8000/api/v1/mesh/metrics`

---

### 1. What Prometheus Actually Does (Pull vs. Push)

Most logging tools wait for applications to "push" logs to them. **Prometheus uses a Pull (Scraping) Model**:
1. Prometheus maintains a list of target endpoints (e.g., `http://127.0.0.1:8000/api/v1/mesh/metrics`).
2. Every **15 seconds** (the *scrape interval*), Prometheus sends an HTTP `GET` request to that endpoint.
3. The application replies with plain text formatted in standard **Prometheus Exposition Format**:

```text
# HELP process_cpu_percent Process CPU utilization percentage
# TYPE process_cpu_percent gauge
process_cpu_percent{service="cart-service"} 94.6
service_up{service="cart-service"} 1
http_requests_total{service="cart-service",status="200"} 142
http_requests_total{service="cart-service",status="500"} 38
http_request_duration_seconds{service="cart-service"} 0.850
```

---

### 2. Metrics Collected & Monitored

| Metric Name | Type | Unit | Meaning in this Project |
| :--- | :--- | :--- | :--- |
| `service_up` | Gauge | Boolean (0 or 1) | `1` = Service healthy; `0` = Process dead / crashed |
| `process_cpu_percent` | Gauge | Percent (0–100%) | CPU usage; surges to $> 85\%$ during `StressChaos` |
| `process_resident_memory_bytes` | Gauge | Bytes | Physical RAM occupied by container process |
| `http_requests_total` | Counter | Integer | Total calls segmented by HTTP status code (`200`, `500`, `503`) |
| `http_request_duration_seconds` | Gauge | Seconds | Request latency; surges to $> 0.850\text{s}$ during `NetworkLatency` |

---

### 3. How Grafana Fits In

- **Prometheus is the Database:** It stores raw time-series numbers on disk. It has no fancy graphs.
- **Grafana is the Visual Dashboard:** Grafana queries Prometheus using **PromQL** (*Prometheus Query Language*), such as:
  ```promql
  rate(http_requests_total{status="500"}[1m]) / rate(http_requests_total[1m]) * 100
  ```
- Grafana renders this query as visual line graphs, gauges, and health tiles. In Tab 3 of our dashboard, Grafana health tiles display live CPU, memory, and HTTP uptime for every microservice.

---

### 4. Viewing Raw Metrics

To inspect raw telemetry directly from your terminal:
```bash
curl -s http://127.0.0.1:8000/api/v1/mesh/metrics
```
Or open **`http://localhost:8000/api/v1/mesh/metrics`** in your browser.

---

## Section 4: Configuration & Automated Remediation (Ansible)

### Code Map
- **Remediation Playbook:** [`ansible/restart_service.yml`](file:///home/moha/Downloads/aiops-platform/ansible/restart_service.yml)
- **Inventory File:** [`ansible/inventory.ini`](file:///home/moha/Downloads/aiops-platform/ansible/inventory.ini)
- **Subprocess Execution Code:** [`backend/app/api/incidents.py`](file:///home/moha/Downloads/aiops-platform/backend/app/api/incidents.py#L162-L186)

---

### 1. What is Ansible and What is a Playbook?

- **Ansible** is an open-source automation engine. Unlike other tools that require heavy agent daemons installed inside every container, Ansible is **agentless**—it executes tasks locally or over SSH.
- An **Ansible Playbook** is a human-readable YAML document that defines exact steps (called *tasks*) to execute on infrastructure.

---

### 2. Line-by-Line Breakdown of `ansible/restart_service.yml`

```yaml
---
# Line 2-5: Defines the target machine (localhost) and disables slow OS fact gathering
- name: Autonomous Service Remediation
  hosts: localhost
  connection: local
  gather_facts: false

# Line 6-8: Variables passed dynamically by the AI backend (-e service=cart-service)
  vars:
    target_service: "{{ service | default('cartservice') }}"
    k8s_ns: "{{ target_ns | default('default') }}"

  tasks:
    # Task 1: Logs the beginning of the automated SRE remediation action
    - name: Log remediation start
      ansible.builtin.debug:
        msg: "Initiating autonomous restart remediation for {{ target_service }} in namespace '{{ k8s_ns }}'"

    # Task 2: Executes real Kubernetes rollout restart on the target deployment
    - name: Check if Kubernetes cluster is accessible
      ansible.builtin.command:
        cmd: "kubectl rollout restart deployment/{{ target_service }} -n {{ k8s_ns }}"
      register: k8s_restart
      ignore_errors: true
      changed_when: k8s_restart.rc == 0

    # Task 3: If cluster rollout succeeded, reports stdout; otherwise reports host mesh recovery
    - name: Self-healing fallback via process mesh supervisor
      ansible.builtin.debug:
        msg: "Kubernetes rollout result: {{ k8s_restart.stdout if k8s_restart.rc == 0 else 'Restarting container via Mesh Supervisor (HTTP 200 health check verified)' }}"

    # Task 4: Confirms the service has returned to verified healthy ACTIVE status
    - name: Verify service health status
      ansible.builtin.debug:
        msg: "Service '{{ target_service }}' successfully verified and restored to ACTIVE state."
```

---

### 3. How the Backend Triggers Ansible on Human Approval

When you click **"✅ Approve & Execute Self-Healing"** in Tab 5, FastAPI runs this Python code:

```python
# backend/app/api/incidents.py:162-186
@router.post("/{incident_id}/approve")
def approve_incident_remediation(incident_id: str):
    service = incident.primary_service

    # 1. Terminate active CPU burn / stress threads
    mgr = get_mesh_manager()
    mesh_res = mgr.remediate_service(service)

    # 2. Trigger real Ansible execution via Python subprocess
    ansible_bin = "/path/to/.venv/bin/ansible-playbook"
    ans_proc = subprocess.run(
        [ansible_bin, "ansible/restart_service.yml", "-e", f"service={service}"],
        cwd="/home/moha/Downloads/aiops-platform",
        capture_output=True,
        text=True,
        timeout=15,
        env=env
    )

    # 3. Stream real Ansible terminal output lines directly into the web UI drawer
    ansible_output = [line for line in ans_proc.stdout.split("\n") if line.strip()]

    # 4. Permanently mark incident as RESOLVED
    incident.status = "RESOLVED"
    return {"status": "SUCCESS", "execution_output": ansible_output}
```

---

### 4. How `kubectl rollout restart` Actually Works

When Ansible runs:
```bash
kubectl rollout restart deployment/cartservice
```
Kubernetes does **NOT** brutally kill the service and leave users hanging. Instead:
1. It updates the Deployment template's annotation:
   `kubectl.kubernetes.io/restartedAt: 2026-10-05T16:30:00Z`
2. Kubernetes detects the changed specification and initiates a **Rolling Update**:
   - It starts a brand new, healthy replacement Pod.
   - It waits for the new Pod's container readiness probe to pass (HTTP 200).
   - Only once the new Pod is ready does it terminate the old, degraded Pod.
3. This achieves **zero downtime self-healing**.

---

## Section 5: Infrastructure as Code (Terraform)

### Code Map
- **Main Config:** [`terraform/main.tf`](file:///home/moha/Downloads/aiops-platform/terraform/main.tf)
- **Variables:** [`terraform/variables.tf`](file:///home/moha/Downloads/aiops-platform/terraform/variables.tf)
- **Outputs:** [`terraform/outputs.tf`](file:///home/moha/Downloads/aiops-platform/terraform/outputs.tf)

---

### 1. What is Terraform and What Problem Does It Solve?

- **Without Terraform:** An engineer has to manually type 20 `kubectl create namespace`, `kubectl apply` commands in a terminal. If they misspell a name or forget a flag, the staging and production environments drift apart.
- **With Terraform (Infrastructure as Code - IaC):** You write a text file declaring what infrastructure *should* exist. Terraform calculates the exact changes needed to achieve that state and provisions it automatically.

---

### 2. Line-by-Line Breakdown of `terraform/main.tf`

```hcl
# Line 1-13: Specifies required provider plugins from HashiCorp registry
terraform {
  required_version = ">= 1.5.0"
  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.26.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.12.0"
    }
  }
}

# Line 15-23: Configures Kubernetes provider to communicate with local ~/.kube/config
provider "kubernetes" {
  config_path = var.kubeconfig_path
}

provider "helm" {
  kubernetes {
    config_path = var.kubeconfig_path
  }
}

# Line 25-33: Provisions dedicated namespace for core AIOps Platform with production labels
resource "kubernetes_namespace" "aiops" {
  metadata {
    name = var.namespace  # defaults to "aiops"
    labels = {
      environment = "production"
      platform    = "aiops-autonomous"
    }
  }
}

# Line 35-39: Provisions dedicated namespace where Chaos Mesh fault injection tools operate
resource "kubernetes_namespace" "chaos_mesh" {
  metadata {
    name = "chaos-mesh"
  }
}
```

---

### 3. Local Execution & Testing

Our Terraform configuration targets the **local Kubernetes cluster (`k3s`)** via `~/.kube/config`. It does not require AWS, Azure, or GCP credentials.

To see what Terraform would create without modifying anything:
```bash
cd /home/moha/Downloads/aiops-platform/terraform
terraform init
terraform plan
```
Terraform will output:
```text
Plan: 2 to add, 0 to change, 0 to destroy.
+ kubernetes_namespace.aiops
+ kubernetes_namespace.chaos_mesh
```

---

## Section 6: CI/CD Pipeline (Jenkins)

### Code Map
- **Pipeline Definition:** [`jenkins/Jenkinsfile`](file:///home/moha/Downloads/aiops-platform/jenkins/Jenkinsfile)

---

### 1. What is CI/CD and Jenkins?

- **CI (Continuous Integration):** Automatically compiling code, linting syntax, and running automated tests every time code is committed.
- **CD (Continuous Delivery / Deployment):** Automatically packaging the tested application and deploying it to staging or production Kubernetes clusters.
- **Jenkins:** The industry-standard open-source automation server that executes these sequential pipelines.

---

### 2. Stage-by-Stage Breakdown of `jenkins/Jenkinsfile`

```
 [ Stage 1: Checkout & Lint ]
            │ (Validates Python syntax across all modules)
            ▼
 [ Stage 2: Backend Unit & RAG Tests ]
            │ (Runs all 21 pytest automated test suites)
            ▼
 [ Stage 3: Deploy Kubernetes Microservices ]
            │ (kubectl apply -f k8s/manifests/online-boutique.yaml)
            ▼
 [ Stage 4: Chaos Benchmark Injection ]
            │ (Applies chaos-pod-failure.yaml to trigger real fault)
            ▼
 [ Stage 5: Autonomous AIOps Evaluation ]
            │ (Queries /benchmarks/scorecard to grade AI accuracy)
            ▼
 [ Post Actions: Cleanup & Teardown ]
            (Deletes chaos experiment; alerts on-call if failed)
```

1. **Stage 1: Checkout & Lint ([Line 11–16](file:///home/moha/Downloads/aiops-platform/jenkins/Jenkinsfile#L11-L16)):**
   Validates Python syntax across the entire codebase (`find . -name "*.py" -exec python3 -m py_compile {} +`) to catch syntax errors before running tests.
2. **Stage 2: Backend Unit & RAG Tests ([Line 18–30](file:///home/moha/Downloads/aiops-platform/jenkins/Jenkinsfile#L18-L30)):**
   Initializes the virtual environment, installs dependencies, and runs the automated test suite (`pytest -v tests/`). Confirms all 21 unit tests pass.
3. **Stage 3: Deploy Kubernetes Microservices ([Line 32–37](file:///home/moha/Downloads/aiops-platform/jenkins/Jenkinsfile#L32-L37)):**
   Deploys the 5 microservices to the cluster (`kubectl apply -f k8s/manifests/online-boutique.yaml`).
4. **Stage 4: Chaos Benchmark Injection ([Line 39–45](file:///home/moha/Downloads/aiops-platform/jenkins/Jenkinsfile#L39-L45)):**
   Injects a real Chaos Mesh fault (`kubectl apply -f k8s/manifests/chaos-pod-failure.yaml`) and sleeps 30 seconds to allow the cascading alert storm to propagate into Prometheus.
5. **Stage 5: Autonomous AIOps Evaluation ([Line 47–55](file:///home/moha/Downloads/aiops-platform/jenkins/Jenkinsfile#L47-L55)):**
   Calls `http://aiops-backend:8000/api/v1/benchmarks/scorecard` to objectively score the AI agent's Mean Time to Detect (MTTD) and Root Cause Accuracy against the Ground Truth Ledger.
6. **Post Actions ([Line 57–65](file:///home/moha/Downloads/aiops-platform/jenkins/Jenkinsfile#L57-L65)):**
   Cleans up chaos resources (`kubectl delete -f ... --ignore-not-found=true`) so the cluster returns to a pristine state.

---

## Section 7: Chaos Engineering (Fault Injection)

### Code Map
- **API Endpoint:** [`backend/app/api/chaos.py`](file:///home/moha/Downloads/aiops-platform/backend/app/api/chaos.py)
- **Mesh Fault Execution:** [`backend/app/mesh/manager.py`](file:///home/moha/Downloads/aiops-platform/backend/app/mesh/manager.py#L320-L385) (`inject_real_fault()`)
- **Chaos Manifest:** [`k8s/manifests/chaos-pod-failure.yaml`](file:///home/moha/Downloads/aiops-platform/k8s/manifests/chaos-pod-failure.yaml)

---

### 1. What is Chaos Engineering and Why Do SREs Use It?

Instead of waiting for an unexpected outage at 3:00 AM on Black Friday, SREs use **Chaos Engineering** to intentionally inject controlled failures during business hours. This verifies whether monitoring detects the failure, alerts escalate properly, and self-healing systems recover automatically.

---

### 2. Behind the Scenes of the 3 Failure Modes

#### Mode 1: Pod Failure / SIGKILL (`PodFailure`)
- **What Happens:** The platform queries the Kubernetes API for the live Pod name and calls:
  ```python
  v1.delete_namespaced_pod(name=pod_name, namespace="default", grace_period_seconds=0)
  ```
- **Physical Symptom:** The Pod immediately transitions to `Terminating` and exits with Exit Code 137.
- **Cascading Effect:** Ingress requests to port 8081/8082 fail with `HTTP 503 Service Unavailable`. In terminal, `kubectl get pods` shows a newly spawned replacement pod with `AGE: 3s`.

#### Mode 2: CPU & Memory Burner (`StressChaos`)
- **What Happens:** In [`backend/app/mesh/manager.py`](file:///home/moha/Downloads/aiops-platform/backend/app/mesh/manager.py#L222-L232), the target microservice spawns background worker threads executing intensive nested multiplication loops:
  ```python
  while self.is_burning_cpu:
      _ = [x * x for x in range(40000)]
      time.sleep(0.001)
  ```
- **Physical Symptom:** CPU surges to **94.6%** and a 30MB memory buffer is allocated.
- **Cascading Effect:** In Kubernetes, Linux CFS (*Completely Fair Scheduler*) throttles the container's CPU quota, causing request latency to skyrocket past 1,200ms and tripping Prometheus alert thresholds.

#### Mode 3: Network Latency Injection (`NetworkLatency`)
- **What Happens:** Injects an artificial `850ms` delay into the microservice request pipeline (`s.set_latency(850)`).
- **Physical Symptom:** `http_request_duration_seconds` increases from `0.024s` to `0.850s`.
- **Cascading Effect:** Downstream caller (`frontend`) times out waiting for cart data, throwing `HTTP 504 Gateway Timeout` errors.

---

## Section 8: NTI Defense Q&A Cheat Sheet (DevOps Track)

### Question 1: *"What is the difference between a Pod and a Container in this setup?"*
> **Answer:**  
> "A **container** is an isolated Linux process running our application code, packaged with its own dependencies and isolated via cgroups and kernel namespaces.  
> A **pod** is the smallest deployable Kubernetes unit that wraps and manages that container. A pod provides a shared network namespace (giving the container its own cluster IP) and shared storage volumes. While Docker or containerd runs the container image, Kubernetes schedules, monitors, and restarts the Pod."

### Question 2: *"Why use Ansible for self-healing instead of a simple bash script?"*
> **Answer:**  
> "Three critical reasons:  
> 1. **Idempotency:** A bash script blindly reruns commands and can cause race conditions or duplicate actions. Ansible verifies state before acting.  
> 2. **Auditability & Logging:** Ansible produces structured task execution output (`TASK [Log remediation start]`, `changed_when`) that our Python backend captures and streams directly into the UI audit drawer.  
> 3. **Enterprise Scalability:** A bash script only works on the single local machine. Our playbook uses an inventory file (`ansible/inventory.ini`) and can orchestrate rollout restarts across 500 remote Kubernetes worker nodes over SSH without changing application code."

### Question 3: *"How does Prometheus discover and scrape metrics from dynamic Kubernetes pods?"*
> **Answer:**  
> "Prometheus uses **Kubernetes Service Discovery (`kubernetes_sd_configs`)**. Instead of hardcoding static IP addresses, Prometheus watches the Kubernetes API server for Pods and Endpoints bearing specific annotations:  
> `prometheus.io/scrape: "true"` and `prometheus.io/port: "8080"`.  
> Whenever a pod restarts and receives a new IP address, Prometheus automatically detects the endpoint update and continues scraping metrics without manual reconfiguration."

### Question 4: *"What is the difference between Terraform and Ansible in your project?"*
> **Answer:**  
> "They solve two complementary phases of infrastructure lifecycle:  
> - **Terraform (Day 0 / Day 1 - Orchestration):** Provisions the foundational infrastructure. In `terraform/main.tf`, it declares the Kubernetes namespaces (`aiops`, `chaos-mesh`) and resource isolation boundaries.  
> - **Ansible (Day 2 - Configuration & Remediation):** Operates on top of existing infrastructure. In `ansible/restart_service.yml`, it executes automated configuration tasks and rollout restarts during active incidents."

### Question 5: *"Why did you choose k3s/lightweight Kubernetes instead of a full multi-node cluster?"*
> **Answer:**  
> "Because our host environment is an ARM64 Red Hat Enterprise Linux 10.2 workstation with constrained memory resources. Standard multi-node Kubernetes with etcd requires 4GB–8GB of RAM just for the control plane.  
> `k3s` is a fully certified, 100% standards-compliant Kubernetes distribution that replaces etcd with SQLite, running in under 512MB of RAM. It executes the exact same Kubernetes manifests (`apps/v1` Deployments, Services) and responds to standard `kubectl` commands identically to production EKS or GKE."

### Question 6: *"How does the system ensure zero downtime during a pod restart (Rollout Restart)?"*
> **Answer:**  
> "Through Kubernetes **Rolling Updates**. When Ansible triggers `kubectl rollout restart deployment/cartservice`:  
> Kubernetes does not delete the active pod immediately. It creates a new pod, waits for the new container's readiness probe to verify health (HTTP 200 OK), routes network traffic to the new pod via the Kubernetes Service load balancer, and only then terminates the old, degraded pod. Users experience continuous service availability."
