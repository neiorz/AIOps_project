# Runbook: Pod OOMKilled (Out Of Memory) & CrashLoopBackOff

## Overview
Pods experiencing memory leaks or insufficient cgroup limits get terminated by the Linux Kernel Out-Of-Memory (OOM) killer with exit code 137, frequently resulting in `CrashLoopBackOff`.

## Symptoms
- Pod status displays `CrashLoopBackOff` or `OOMKilled`.
- Kubernetes Event: `Exit Code 137` with reason `OOMKilled`.
- Spike in container memory utilization reaching 100% of `limits.memory`.
- Sudden drop in service availability and sudden 502/503 errors.

## Diagnostic Steps (Telemetry Investigation)
1. **Kubernetes API**:
   - Check pod termination reason: `kubectl get pods -l app=<service> -o jsonpath='{.items[*].status.containerStatuses[*].lastState.terminated.reason}'`
   - Describe pod events: `kubectl describe pod <pod-name>`
2. **PromQL Inspection**:
   - Query container memory usage vs limit:
     `sum(container_memory_working_set_bytes{container="<service>"}) by (pod) / sum(kube_pod_container_resource_limits{resource="memory", container="<service>"}) by (pod) * 100`
3. **LogQL Verification**:
   - Search logs right before crash: `{container="<service>"} |= "java.lang.OutOfMemoryError" or "fatal error: runtime: out of memory"`
4. **Chaos Mesh Check**:
   - Inspect active stress experiments: `kubectl get stresschaos -A`

## Remediation & Self-Healing
1. Automated patch of pod memory resource limits:
   - Increase memory limit by 50% via Kubernetes API patch:
     `kubectl set resources deployment/<service> --limits=memory=512Mi --requests=memory=256Mi`
2. If due to Chaos Mesh StressChaos:
   - Reclaim memory stress: `kubectl delete stresschaos <stress-name> -n chaos-mesh`
3. Restart failed deployment:
   - `kubectl rollout restart deployment <service>`

