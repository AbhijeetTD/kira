#!/usr/bin/env bash
# demo/advanced/trigger-advanced.sh
#
# Simulates a realistic production incident that would take a human ~2 hours:
#
#   WHAT HAPPENS
#   ─────────────────────────────────────────────────────────────────────────
#   An engineer ships cart-service v2.1.0 as a "cost optimisation":
#     • CPU limit silently reduced from 500m → 50m  (10× drop)
#     • DB_POOL_SIZE silently reduced from 20  → 2   (10× drop)
#
#   Immediate effects:
#     1. cart-service pods hit connection pool exhaustion → crash loop
#     2. payment-service (depends on cart) detects upstream timeouts,
#        opens circuit breaker, fails its own health check → also crashes
#
#   Why a human takes ~2 hours:
#     • Two services failing simultaneously → engineer suspects infra/network
#     • cart-service logs show "DB connection timeout" → DB team pulled in
#     • DB team confirms DB is fine → 30-min dead-end
#     • Only after correlating deployment timestamp do they find the cause
#
#   KIRA does it in ~8 minutes by correlating all signals at once.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
API="${KIRA_URL:-http://localhost:8000}"
CTX="${KUBE_CONTEXT:-rancher-desktop}"

echo ""
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║  KIRA — Advanced Demo: Multi-Service Cascade Incident   ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""
echo "==> Context: ${CTX}"
echo "==> Step 1: Engineering ships cart-service v2.1.0 ('cost optimisation')…"
kubectl --context "${CTX}" apply -f "${SCRIPT_DIR}/manifests/cart-service-v2-broken.yaml"

echo ""
echo "==> Step 2: Deploying degraded payment-service (simulates downstream cascade)…"
kubectl --context "${CTX}" apply -f "${SCRIPT_DIR}/manifests/payment-service.yaml"

echo ""
echo "==> Waiting 8s for crash loops to manifest…"
sleep 8

echo ""
echo "==> Cluster state BEFORE KIRA:"
kubectl --context "${CTX}" get pods -n advanced-demo
echo ""

echo "==> Step 3: Alert fires — KIRA begins autonomous investigation…"
RESPONSE=$(curl -s -X POST "${API}/webhook/alert" \
  -H "Content-Type: application/json" \
  -d '{
    "service":   "cart-service",
    "namespace": "advanced-demo",
    "message":   "CRITICAL: cart-service health checks failing — CrashLoopBackOff detected. payment-service also degraded. Possible cascade failure.",
    "severity":  "critical",
    "source":    "pagerduty"
  }')

echo "${RESPONSE}" | python3 -m json.tool 2>/dev/null || echo "${RESPONSE}"
echo ""
echo "✅ Incident triggered — open http://localhost:8000 to watch the investigation."

