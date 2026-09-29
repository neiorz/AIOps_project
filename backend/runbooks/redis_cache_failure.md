# Runbook: Redis Cache & Session Store Failure

## Overview
The cartservice and session store depend on Redis. When Redis suffers connection exhaustion, OOM, or network disruption, users cannot view or add items to their shopping cart.

## Symptoms
- Cart service reports `StackExchange.Redis.RedisConnectionException` or `Connection refused`.
- Frontend displays "Failed to load cart" or HTTP 500 on `/cart`.
- Redis pod status may show `CrashLoopBackOff` or high memory pressure.

## Diagnostic Steps (Telemetry Investigation)
1. **PromQL Inspection**:
   - Redis connected clients: `redis_connected_clients`
   - Cart service error rate: `sum(rate(http_requests_total{service="cartservice", status=~"5.."}[5m]))`
2. **LogQL Verification**:
   - `app="cartservice" |= "Redis" |= "ERR" or "timeout"`
   - `app="redis-cart" |= "OOM command not allowed"`
3. **Kubernetes API**:
   - `kubectl logs -l app=redis-cart --tail=100`
   - `kubectl get endpoints redis-cart`

## Remediation & Self-Healing
1. Check Redis availability and restart if unresponsive:
   - `kubectl rollout restart deployment redis-cart`
2. Run Ansible playbook to purge unneeded cache keys if memory full:
   - Execute self-healing action: `ansible-playbook ansible/restart_service.yml -e "service=redis"`
3. Verify Cart Service recovery:
   - `kubectl rollout restart deployment cartservice`

