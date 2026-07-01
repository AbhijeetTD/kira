#!/usr/bin/env bash
# demo/trigger_incident.sh — apply the broken deployment and fire the alert
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
API="${KIRA_URL:-http://localhost:8000}"
CTX="${KUBE_CONTEXT:-rancher-desktop}"
NS="ls-pricing-cloudops-test"

echo "==> Using kubectl context: ${CTX}"
echo "==> Applying broken cart-web deployment (v2 — CrashLoopBackOff) in ${NS}…"
kubectl --context "${CTX}" apply -f "${SCRIPT_DIR}/manifests/fault-02-crashloop.yaml"

echo "==> Waiting 5s for pods to start crashing…"
sleep 5
kubectl --context "${CTX}" get pods -n "${NS}" -l app=cart-web

echo ""
echo "==> Firing alert to KIRA at ${API}…"
curl -s -X POST "${API}/webhook/alert" \
  -H "Content-Type: application/json" \
  -d "{
    \"service\":   \"cart-web\",
    \"namespace\": \"${NS}\",
    \"message\":   \"cart-web health checks failing — pods in CrashLoopBackOff, elevated restart count. Possible bad deployment.\",
    \"severity\":  \"critical\",
    \"source\":    \"grafana\"
  }" | python3 -m json.tool

echo ""
echo "✅ Incident triggered — open http://localhost:8000 to watch KIRA investigate."
