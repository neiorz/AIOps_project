# Runbook: Network Latency & Inter-Service Packet Loss

## Overview
High inter-service network latency or packet drop occurs when service-to-service communication degrades due to network congestion, noisy neighbor saturation, bad routing, or simulated chaos injection (Chaos Mesh NetworkChaos).

## Symptoms
- Cascading HTTP 504 Gateway Timeout or 503 Service Unavailable errors.
- Spike in P99 and P95 latency metrics (> 1000ms) on OpenTelemetry / Prometheus (`http_req_duration_seconds`).
- Client timeout logs in frontend or upstream gateway proxies.
- Connection resets and high retry rates.

## Diagnostic Steps (Telemetry Investigation)
1. **PromQL Inspection**:
   - Query P99 latency: `histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, service))`
   - Query packet drop: `sum(rate(node_network_receive_drop_total[5m])) by (instance)`
2. **LogQL Verification**:
   - Search upstream proxy/service logs: `{app="frontend"} |= "context deadline exceeded" or "connection timed out"`
3. **TraceQL Deep-Dive**:
   - Inspect slow traces in Tempo: `{ span.duration > 1s && status.code = error }`
   - Identify the specific hop causing the delay (e.g., `frontend -> checkoutservice -> paymentservice`).
4. **Chaos Mesh Check**:
   - Verify if an active NetworkChaos resource is running: `kubectl get networkchaos -A`

## Remediation & Self-Healing
1. If Chaos experiment is active and outside approved test window:
   - Terminate Chaos: `kubectl delete networkchaos <chaos-name> -n chaos-mesh`
2. If network saturation is localized:
   - Adjust traffic routing with Istio / Envoy VirtualService circuit breaker.
   - Scale target deployment replicas to distribute ingress connections: `kubectl scale deployment <service-name> --replicas=3`
3. Fallback:
   - Restart the target service pod to reset connection pool: `kubectl rollout restart deployment <service-name>`
