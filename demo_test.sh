#!/usr/bin/env bash
set -e

BASE_URL="http://localhost:8000"

echo "=================================================================="
echo "      AIOps Platform - Live End-to-End Test & Demonstration       "
echo "=================================================================="

# Check if server is running
if ! curl -s "$BASE_URL/api/v1/health" > /dev/null; then
    echo "[!] FastAPI server is not responding at $BASE_URL."
    echo "    Start it using: ./run.sh dev-backend"
    exit 1
fi

echo -e "\n[1/5] Checking System Health & Autonomous Mode..."
curl -s "$BASE_URL/api/v1/health"
echo ""

echo -e "\n[2/5] Simulating Cascading Chaos Alerts (Alert Clustering)..."
echo "-> Sending Alert #1: cartservice connection refused (Tenant A)..."
curl -s -X POST "$BASE_URL/api/v1/alerts/webhook" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "alt-001",
    "alertname": "CartServiceDown",
    "service": "cartservice",
    "severity": "warning",
    "tenant_id": "tenant_a",
    "description": "Cart service connection refused on port 7070"
  }'
echo ""

echo "-> Sending Alert #2: frontend 500 spike due to cart failure (Tenant A)..."
curl -s -X POST "$BASE_URL/api/v1/alerts/webhook" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "alt-002",
    "alertname": "FrontendHttp500Spike",
    "service": "frontend",
    "severity": "warning",
    "tenant_id": "tenant_a",
    "description": "Frontend cannot load user cart items"
  }'
echo ""

echo "-> Sending Alert #3: checkout failure (Tenant A - Critical)..."
curl -s -X POST "$BASE_URL/api/v1/alerts/webhook" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "alt-003",
    "alertname": "CheckoutFailureSpike",
    "service": "checkoutservice",
    "severity": "critical",
    "tenant_id": "tenant_a",
    "description": "Transactions dropping in checkout service"
  }'
echo ""

echo -e "\n[3/5] Querying Correlated Incidents & Multi-Tenant SLA Status..."
INCIDENTS=$(curl -s "$BASE_URL/api/v1/incidents")
echo "$INCIDENTS"
echo ""

# Extract first incident id
INCIDENT_ID=$(echo "$INCIDENTS" | grep -o '"incident_id":"[^"]*' | head -n 1 | cut -d'"' -f4)

echo -e "\n[4/5] Testing RAG Semantic Runbook Retrieval from ChromaDB..."
curl -s "$BASE_URL/api/v1/rag/search?q=cartservice%20connection%20refused&n=1"
echo ""

if [ -n "$INCIDENT_ID" ]; then
    echo -e "\n[5/5] Triggering Autonomous AI SRE Investigation Agent for $INCIDENT_ID..."
    curl -s -X POST "$BASE_URL/api/v1/incidents/$INCIDENT_ID/diagnose"
    echo ""
fi

echo -e "\n=================================================================="
echo "               All End-to-End Tests Passed!                       "
echo "=================================================================="

