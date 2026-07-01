#!/usr/bin/env bash
# demo/memory/trigger-memory.sh
#
# Deploys a memory-leaking service, simulates "refresh" calls to grow its heap,
# and fires a KIRA alert when memory crosses the 80 % threshold.
# KIRA then autonomously patches the memory limit (doubles it).
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
API="${KIRA_URL:-http://localhost:8000}"
CTX="${KUBE_CONTEXT:-rancher-desktop}"
SERVICE_URL="http://localhost:30880"

MEMORY_LIMIT_MI=128
THRESHOLD_PCT=80
THRESHOLD_MI=$(( MEMORY_LIMIT_MI * THRESHOLD_PCT / 100 ))   # 102 Mi

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  KIRA — Memory Threshold Auto-Remediation Demo      ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  What happens:"
echo "  1. Deploy memory-hog service (limit: ${MEMORY_LIMIT_MI}Mi)"
echo "  2. Simulate HTTP refresh calls — each adds 10 MB to the heap"
echo "  3. When usage > ${THRESHOLD_PCT}% (${THRESHOLD_MI}Mi), alert fires"
echo "  4. KIRA patches memory limit → doubles it to 256Mi"
echo ""

# ── Deploy ──────────────────────────────────────────────────────────────────
echo "==> Deploying memory-hog service..."
kubectl --context "${CTX}" apply -f "${SCRIPT_DIR}/manifests/memory-hog.yaml"

echo "==> Waiting for pod to be Ready (up to 90s)..."
kubectl --context "${CTX}" wait deployment/memory-hog -n memory-demo \
  --for=condition=Available --timeout=90s
echo ""

echo "==> Pod is ready. NodePort service exposed on localhost:30880"
echo "==> Starting refresh simulation..."
echo ""

# ── Simulate refreshes and monitor memory ──────────────────────────────────
ITERATION=0
ALERTED=false

while true; do
    ITERATION=$(( ITERATION + 1 ))

    # Hit the service to allocate another 10 MB
    RESP=$(curl -sf --max-time 5 "${SERVICE_URL}/refresh" 2>/dev/null || echo "(service unreachable)")
    echo "  Refresh #${ITERATION}: ${RESP}"

    sleep 5

    # Read memory from kubectl top (may lag 60 s on first startup)
    MEM_RAW=$(kubectl --context "${CTX}" top pods -n memory-demo \
      --no-headers 2>/dev/null | grep memory-hog | awk '{print $3}' | head -1)

    if [[ -z "$MEM_RAW" ]]; then
        echo "  (kubectl top: metrics warming up — will retry)"
        continue
    fi

    # Strip unit (Mi or m) and normalise to Mi
    MEM_USED_MI="${MEM_RAW//Mi/}"
    MEM_USED_MI="${MEM_USED_MI//m/}"
    PCT=$(( MEM_USED_MI * 100 / MEMORY_LIMIT_MI ))

    BAR=$(printf '█%.0s' $(seq 1 $(( PCT / 5 ))))
    echo "  kubectl top → ${MEM_USED_MI}Mi / ${MEMORY_LIMIT_MI}Mi  [${BAR}] ${PCT}%"

    if [[ "$MEM_USED_MI" -ge "$THRESHOLD_MI" ]] && [[ "$ALERTED" == "false" ]]; then
        ALERTED=true
        echo ""
        echo "  ⚠️  THRESHOLD BREACHED: ${MEM_USED_MI}Mi ≥ ${THRESHOLD_MI}Mi (${PCT}%)"
        echo ""
        echo "==> Firing alert to KIRA..."
        RESPONSE=$(curl -s -X POST "${API}/webhook/alert" \
          -H "Content-Type: application/json" \
          -d "{
            \"service\":   \"memory-hog\",
            \"namespace\": \"memory-demo\",
            \"message\":   \"CRITICAL: memory-hog heap at ${MEM_USED_MI}Mi (${PCT}%) — limit ${MEMORY_LIMIT_MI}Mi. OOM kill imminent. Patch memory limit.\",
            \"severity\":  \"critical\",
            \"source\":    \"memory-monitor\"
          }")

        echo "${RESPONSE}" | python3 -m json.tool 2>/dev/null || echo "${RESPONSE}"
        echo ""
        echo "==> Incident created. Watch the KIRA dashboard:"
        echo "    http://localhost:8000"
        echo ""
        echo "    KIRA will:"
        echo "    1. Investigate pod memory usage"
        echo "    2. Run RCA (Azure OpenAI)"
        echo "    3. Patch memory limit: ${MEMORY_LIMIT_MI}Mi → $((MEMORY_LIMIT_MI * 2))Mi"
        echo ""
        echo "    (Tip: if approval is required, set AUTO_APPROVE_THRESHOLD=80 in .env)"
        echo ""
        break
    fi
done

echo "==> Done. Script exiting — KIRA is handling the rest autonomously."
