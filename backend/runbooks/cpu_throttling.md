# Runbook: CPU Saturation & High Throttling

## Overview
When microservices exceed their allocated CPU limits or CFS quotas, the Linux CFS scheduler throttles thread execution. This induces extreme processing latency without necessarily terminating the pod.

## Symptoms
- Extreme latency degradation while error rate may initially remain low.
- Prometheus metric `container_cpu_cfs_throttled_periods_total` surging.
- Pod CPU usage pinned at 100% of defined `resources.limits.cpu`.
- Request queues piling up in service mesh or reverse proxy.

## Diagnostic Steps (Telemetry Investigation)
1. **PromQL Inspection**:
   - Throttle percentage:
     `sum(increase(container_cpu_cfs_throttled_periods_total[5m])) by (pod) / sum(increase(container_cpu_cfs_periods_total[5m])) by (pod) * 100`
   - CPU utilization vs limit:
     `sum(rate(container_cpu_usage_seconds_total[5m])) by (pod) / sum(kube_pod_container_resource_limits{resource="cpu"}) by (pod) * 100`
2. **Kubernetes API**:
   - Check top pods: `kubectl top pods -n default`
   - Inspect HPA status: `kubectl get hpa -A`
3. **TraceQL Analysis**:
   - Spans exhibit elongated execution time inside compute routines rather than downstream RPC calls.

## Remediation & Self-Healing
1. Automated Horizontal Pod Autoscaling (HPA):
   - Trigger scale out: `kubectl scale deployment <service> --replicas=<current_replicas + 2>`
2. Bump CPU limits:
   - `kubectl set resources deployment/<service> --limits=cpu=1000m --requests=cpu=500m`
3. If caused by StressChaos:
   - Clean up CPU burner: `kubectl delete stresschaos -l target=<service>`
