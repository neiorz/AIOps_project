# Runbook: Cart Service CPU Saturation & Resource Starvation

## Overview
This runbook provides emergency diagnosis and remediation procedures for CPU saturation, thread pool exhaustion, and resource starvation affecting the `cart-service`.

## Affected Services
- `cart-service` (Port 8082, C# / .NET)
- `redis-cart` (Port 6380, Redis Cache)
- `frontend` (Downstream dependent)

## Diagnostic Symptoms
- PromQL: `process_cpu_percent{service="cart-service"} > 85.0`
- LogQL: `HTTP 504 Gateway Timeout` or `context deadline exceeded` while calling `/cart`
- TraceQL: Distributed trace waterfall spans on `cartservice/GetCart` stalling > 800ms
- CFS quota throttling surging on `cart-service` pod/container

## Root Cause Analysis
The cart service is suffering from CPU starvation and thread saturation. This is typically caused by:
1. Precision StressChaos fault injection simulating heavy burner threads.
2. Unbounded JSON cart item serialization in memory.
3. Thread pool exhaustion blocking async I/O to `redis-cart`.

## Remediation Playbooks
1. **Automated Ansible Playbook:**
   ```bash
   ansible-playbook ansible/restart_service.yml -e service=cart-service
   ```
2. **Kubernetes Rollout Restart:**
   ```bash
   kubectl rollout restart deployment/cart-service -n default
   ```
3. **Reset Process Mesh State:**
   ```bash
   curl -X POST http://127.0.0.1:8082/chaos/reset
   ```

## Verification
- Confirm `GET /health` on port 8082 returns `status: HEALTHY`.
- Confirm Prometheus `process_cpu_percent{service="cart-service"}` returns to normal baseline (< 5%).
