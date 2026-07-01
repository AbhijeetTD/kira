#!/usr/bin/env bash
# demo/advanced/setup-advanced.sh
# Provisions the advanced-demo namespace with healthy cart-service v2.0.0
# + payment-service v3.4.1 — both running fine before the incident.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CTX="${KUBE_CONTEXT:-rancher-desktop}"

echo "==> Context: ${CTX}"
kubectl --context "${CTX}" create namespace advanced-demo --dry-run=client -o yaml \
  | kubectl --context "${CTX}" apply -f -

echo "==> Deploying healthy cart-service v2.0.0…"
kubectl --context "${CTX}" apply -f "${SCRIPT_DIR}/manifests/cart-service-v1-healthy.yaml"

echo "==> Deploying healthy payment-service v3.4.1…"
# payment-service yaml in healthy state = same file, it will be Running because
# its exit-1 command only runs when it starts — we patch it to a no-op loop here
kubectl --context "${CTX}" apply -f - <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: payment-service
  namespace: advanced-demo
  labels:
    app: payment-service
    version: v3.4.1
  annotations:
    kubernetes.io/change-cause: "v3.4.1 — stable (no recent changes)"
spec:
  replicas: 2
  selector:
    matchLabels:
      app: payment-service
  template:
    metadata:
      labels:
        app: payment-service
    spec:
      containers:
        - name: payment-service
          image: busybox:1.36
          command:
            - /bin/sh
            - -c
            - |
              echo "[INFO] payment-service v3.4.1 running — all dependencies healthy"
              while true; do
                echo "[INFO] GET /health -> 200 OK  (cart-service latency: 45ms)"
                sleep 30
              done
          resources:
            requests:
              cpu: "50m"
              memory: "64Mi"
            limits:
              cpu: "200m"
              memory: "128Mi"
EOF

echo "==> Waiting for deployments to be ready…"
kubectl --context "${CTX}" rollout status deployment/cart-service   -n advanced-demo --timeout=90s
kubectl --context "${CTX}" rollout status deployment/payment-service -n advanced-demo --timeout=90s

echo ""
echo "✅ Advanced demo environment ready — both services healthy."
echo "   Run ./demo/advanced/trigger-advanced.sh to simulate the incident."
