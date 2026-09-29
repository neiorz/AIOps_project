# Runbook: Payment Service Outage & Transaction Drop

## Overview
Failures in the Payment Service halt checkout transactions, directly affecting business revenue and triggering high-severity SLA breaches.

## Symptoms
- Checkout service throws `rpc error: code = Unavailable desc = connection error: desc = "transport: Error while dialing"`.
- Checkout failure rate surges above 10% on Prometheus dashboards.
- Payment gateway response time spikes or drops to zero.

## Diagnostic Steps (Telemetry Investigation)
1. **PromQL Inspection**:
   - Checkout failure rate: `sum(rate(grpc_client_handled_total{grpc_service="hipstershop.PaymentService", grpc_code!="OK"}[5m]))`
   - Paymentservice health: `up{app="paymentservice"}`
2. **LogQL Verification**:
   - `app="paymentservice" |= "ERROR" or "exception" or "uncaught"`
3. **TraceQL Verification**:
   - Inspect checkout trace waterfall: identify if payment span is tagged `error=true`.

## Remediation & Self-Healing
1. Automated restart of failed pods:
   - `kubectl rollout restart deployment paymentservice`
2. Validate mock external payment credentials or upstream mock endpoints.
3. Fallback to resilient circuit breaking to notify users gracefully rather than hard 500 error.

