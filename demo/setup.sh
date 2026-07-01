#!/usr/bin/env bash
# demo/setup.sh — deploy the full Turbo platform + healthy cart-web
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CTX="${KUBE_CONTEXT:-rancher-desktop}"
NS="ls-pricing-cloudops-test"

echo "==> Using kubectl context: ${CTX}"
kubectl config use-context "${CTX}"

echo "==> Deploying Turbo platform (namespaces + 17 services)…"
kubectl --context "${CTX}" apply -f "${SCRIPT_DIR}/manifests/turbo-platform.yaml"

echo "==> Deploying healthy cart-web (v1) into ${NS}…"
kubectl --context "${CTX}" apply -f "${SCRIPT_DIR}/manifests/cart-web-baseline.yaml"

echo "==> Waiting for cart-web to become ready…"
kubectl --context "${CTX}" rollout status statefulset/cart-web -n "${NS}" --timeout=90s

echo ""
echo "==> Platform status:"
echo "--- ls-pricing-cloudops-test ---"
kubectl --context "${CTX}" get deploy,statefulset -n ls-pricing-cloudops-test --no-headers 2>/dev/null | while read -r line; do echo "    $line"; done
echo "--- ls-data-cloudops-test ---"
kubectl --context "${CTX}" get deploy -n ls-data-cloudops-test --no-headers 2>/dev/null | while read -r line; do echo "    $line"; done

echo ""
echo "✅ Turbo demo environment ready ($(kubectl --context "${CTX}" get pods -n ls-pricing-cloudops-test --no-headers 2>/dev/null | wc -l | tr -d ' ') + $(kubectl --context "${CTX}" get pods -n ls-data-cloudops-test --no-headers 2>/dev/null | wc -l | tr -d ' ') pods)."
echo "   Run ./demo/trigger_incident.sh to simulate the incident."
