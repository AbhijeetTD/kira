#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# KIRA E2E Test Suite — 25 Scenarios
# ═══════════════════════════════════════════════════════════════════════════════
#
# Usage:
#   ./run_full_e2e.sh                  # Run all tests
#   ./run_full_e2e.sh 3                # Run only test 3
#   ./run_full_e2e.sh 12 14            # Run tests 12 and 14
#   ./run_full_e2e.sh --from 15        # Run tests 15 onwards
#
# Prerequisites:
#   - Docker container running (docker compose up)
#   - kubectl configured with rancher-desktop context
#   - Baselines applied (script does this automatically)
# ═══════════════════════════════════════════════════════════════════════════════
set -uo pipefail  # no -e: we handle errors manually

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFESTS="${SCRIPT_DIR}/manifests"
API="${KIRA_URL:-http://localhost:8000}"
CTX="${KUBE_CONTEXT:-rancher-desktop}"
NS="ls-pricing-cloudops-test"
RESULTS_DIR="${SCRIPT_DIR}/test-results"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RESULTS_FILE="${RESULTS_DIR}/run_${TIMESTAMP}.log"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'
BOLD='\033[1m'

mkdir -p "${RESULTS_DIR}"

# Counters
PASS=0
FAIL=0
SKIP=0
TOTAL=0

# ── Utility functions ─────────────────────────────────────────────────────────

log()  { echo -e "${CYAN}[$(date +%H:%M:%S)]${NC} $*" | tee -a "${RESULTS_FILE}"; }
pass() { echo -e "${GREEN}  ✅ PASS${NC}: $*" | tee -a "${RESULTS_FILE}"; PASS=$((PASS + 1)); }
fail() { echo -e "${RED}  ❌ FAIL${NC}: $*" | tee -a "${RESULTS_FILE}"; FAIL=$((FAIL + 1)); }
warn() { echo -e "${YELLOW}  ⚠️  WARN${NC}: $*" | tee -a "${RESULTS_FILE}"; }

reset_baseline() {
  local workload=$1
  log "  Resetting ${workload} to baseline..."
  if [[ "${workload}" == "cart-web" ]]; then
    kubectl --context "${CTX}" apply -f "${MANIFESTS}/cart-web-baseline.yaml" 2>/dev/null || true
    sleep 5
    kubectl --context "${CTX}" rollout status statefulset/cart-web -n "${NS}" --timeout=120s 2>/dev/null || true
  elif [[ "${workload}" == "test-api" ]]; then
    kubectl --context "${CTX}" apply -f "${MANIFESTS}/test-deploy-baseline.yaml" 2>/dev/null || true
    sleep 5
    kubectl --context "${CTX}" rollout status deployment/test-api -n "${NS}" --timeout=120s 2>/dev/null || true
  fi
}

clear_incidents() {
  log "  Clearing incidents (restarting container)..."
  docker restart kira-backend >/dev/null 2>&1 || true
  sleep 3
  local elapsed=0
  while (( elapsed < 30 )); do
    if curl -sf "${API}/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
    ((elapsed+=2))
  done
  warn "Container may not be ready"
}

run_scan_and_wait() {
  local max_wait=${1:-300}
  local target_service=${2:-}

  log "  Triggering scan..."
  local scan_result
  scan_result=$(curl -sf -X POST "${API}/scan" 2>&1 || echo '{"error":"curl_failed"}')
  log "  Scan response: $(echo "${scan_result}" | head -c 300)"

  local unhealthy_count
  unhealthy_count=$(echo "${scan_result}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('unhealthy',0))" 2>/dev/null || echo "0")

  if [[ "${unhealthy_count}" == "0" ]]; then
    echo "NO_UNHEALTHY"
    return 0
  fi

  local incident_id
  incident_id=$(echo "${scan_result}" | python3 -c '
import sys, json
d = json.load(sys.stdin)
incs = d.get("incidents_created", [])
target = "'"${target_service}"'"
for inc in incs:
    if target and inc.get("service") == target:
        print(inc["id"])
        sys.exit(0)
if incs:
    print(incs[0]["id"])
else:
    print("")
' 2>/dev/null || echo "")

  if [[ -z "${incident_id}" ]]; then
    echo "NO_INCIDENT"
    return 0
  fi

  log "  Incident created: ${incident_id}"

  local elapsed=0
  while (( elapsed < max_wait )); do
    local inc_data status
    inc_data=$(curl -sf "${API}/incidents/${incident_id}" 2>/dev/null || echo '{}')
    status=$(echo "${inc_data}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','unknown'))" 2>/dev/null || echo "unknown")

    if [[ "${status}" == "resolved" || "${status}" == "failed" || "${status}" == "skipped" ]]; then
      log "  Incident ${incident_id} → ${status}"
      echo "${incident_id}"
      return 0
    fi

    # Handle awaiting_approval — auto-approve for testing
    if [[ "${status}" == "awaiting_approval" ]]; then
      log "  Auto-approving for test..."
      curl -sf -X POST "${API}/incidents/${incident_id}/action" \
        -H "Content-Type: application/json" \
        -d '{"action":"approve"}' >/dev/null 2>&1 || true
    fi

    sleep 5
    elapsed=$((elapsed + 5))
  done

  warn "  Timeout waiting for incident ${incident_id}"
  echo "${incident_id}"
  return 0
}

get_incident_result() {
  local incident_id=$1
  local raw
  raw=$(curl -sf "${API}/incidents/${incident_id}" 2>/dev/null || echo '{}')
  echo "${raw}" | python3 -c '
import sys, json
d = json.load(sys.stdin)
status = d.get("status", "unknown")
rca = d.get("rca") or {}
rem = d.get("remediation") or {}
print("status=" + str(status))
print("confidence=" + str(rca.get("confidence", "N/A")))
print("root_cause=" + str(rca.get("root_cause", "N/A"))[:120])
print("remediation_type=" + str(rca.get("remediation_type", "N/A")))
print("remediation_action=" + str(rem.get("action", "N/A")))
print("remediation_command=" + str(rem.get("command", "N/A"))[:150])
print("total_time=" + str(d.get("total_time_seconds", "N/A")))
' 2>/dev/null || echo "status=error"
}

validate_result() {
  local incident_id=$1
  local expected_status=$2
  local expected_remediation=$3
  local test_name=$4

  local result
  result=$(get_incident_result "${incident_id}")

  local actual_status actual_remediation confidence total_time
  actual_status=$(echo "${result}" | grep '^status=' | cut -d= -f2)
  actual_remediation=$(echo "${result}" | grep '^remediation_type=' | cut -d= -f2)
  confidence=$(echo "${result}" | grep '^confidence=' | cut -d= -f2)
  total_time=$(echo "${result}" | grep '^total_time=' | cut -d= -f2)

  echo "${result}" >> "${RESULTS_FILE}"

  local passed=true

  if [[ "${actual_status}" != "${expected_status}" ]]; then
    fail "${test_name}: expected status=${expected_status}, got ${actual_status}"
    passed=false
  fi

  local rem_match=false
  IFS=',' read -ra EXPECTED_REMS <<< "${expected_remediation}"
  for er in "${EXPECTED_REMS[@]}"; do
    if [[ "${actual_remediation}" == "${er}" ]]; then
      rem_match=true
      break
    fi
  done

  if ! ${rem_match}; then
    fail "${test_name}: expected remediation=${expected_remediation}, got ${actual_remediation}"
    passed=false
  fi

  if ${passed}; then
    pass "${test_name} (confidence=${confidence}, time=${total_time}s)"
  fi
}

# ── Core test runner ──────────────────────────────────────────────────────────

run_test() {
  local test_num=$1
  local fault_manifest=$2
  local workload=$3
  local expected_status=$4
  local expected_remediation=$5
  local description=$6
  local fault_wait=${7:-30}
  local scan_wait=${8:-300}

  TOTAL=$((TOTAL + 1))

  echo "" | tee -a "${RESULTS_FILE}"
  echo -e "${BOLD}═══ Test ${test_num}: ${description} ═══${NC}" | tee -a "${RESULTS_FILE}"

  # 1. Reset baseline
  reset_baseline "${workload}"

  # 2. Clear old incidents
  clear_incidents

  # 3. Apply fault
  if [[ "${fault_manifest}" == "SCALE_DOWN" ]]; then
    log "  Scaling ${workload} to 1 replica..."
    if [[ "${workload}" == "cart-web" ]]; then
      kubectl --context "${CTX}" scale statefulset/cart-web -n "${NS}" --replicas=1 2>/dev/null
    else
      kubectl --context "${CTX}" scale deployment/test-api -n "${NS}" --replicas=1 2>/dev/null
    fi
  elif [[ "${fault_manifest}" == "NONE" ]]; then
    log "  No fault applied (healthy test)"
  else
    log "  Applying fault: ${fault_manifest}"
    kubectl --context "${CTX}" apply -f "${MANIFESTS}/${fault_manifest}" 2>/dev/null
  fi

  # 4. Wait for fault to manifest
  if [[ "${fault_manifest}" != "NONE" ]]; then
    log "  Waiting ${fault_wait}s for fault to manifest..."
    sleep "${fault_wait}"
    log "  Pod status:"
    kubectl --context "${CTX}" get pods -n "${NS}" -l "app=${workload}" --no-headers 2>/dev/null | tee -a "${RESULTS_FILE}" || true
  fi

  # 5. Trigger scan and wait
  local incident_id
  incident_id=$(run_scan_and_wait "${scan_wait}" "${workload}")

  if [[ "${incident_id}" == "NO_UNHEALTHY" ]]; then
    if [[ "${expected_remediation}" == "none" ]]; then
      pass "Test ${test_num}: ${description} — correctly identified healthy"
    else
      fail "Test ${test_num}: ${description} — scan found no unhealthy but expected ${expected_remediation}"
    fi
    return
  fi

  if [[ "${incident_id}" == "NO_INCIDENT" || -z "${incident_id}" ]]; then
    fail "Test ${test_num}: ${description} — no incident created"
    return
  fi

  # 6. Validate
  validate_result "${incident_id}" "${expected_status}" "${expected_remediation}" "Test ${test_num}: ${description}"

  # 7. Post-remediation health check
  if [[ "${expected_status}" == "resolved" && "${expected_remediation}" != "none" ]]; then
    sleep 10
    local pod_status
    pod_status=$(kubectl --context "${CTX}" get pods -n "${NS}" -l "app=${workload}" --no-headers 2>/dev/null || true)
    log "  Post-remediation pods:"
    echo "${pod_status}" | tee -a "${RESULTS_FILE}"
  fi
}

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

echo -e "${BOLD}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║        KIRA E2E Test Suite — 25 Scenarios           ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
log "Results: ${RESULTS_FILE}"
log "API: ${API} | Context: ${CTX} | NS: ${NS}"

# Parse args
TESTS_TO_RUN=()
FROM_TEST=0
if [[ $# -gt 0 ]]; then
  if [[ "$1" == "--from" ]]; then
    FROM_TEST=${2:-1}
  else
    TESTS_TO_RUN=("$@")
  fi
fi

should_run() {
  local num=$1
  if (( FROM_TEST > 0 )); then
    (( num >= FROM_TEST )); return $?
  fi
  if [[ ${#TESTS_TO_RUN[@]} -eq 0 ]]; then return 0; fi
  for t in "${TESTS_TO_RUN[@]}"; do [[ "${t}" == "${num}" ]] && return 0; done
  return 1
}

# ── Apply baselines ───────────────────────────────────────────────────────────
log "Applying baselines..."
kubectl --context "${CTX}" apply -f "${MANIFESTS}/cart-web-baseline.yaml" 2>/dev/null || true
kubectl --context "${CTX}" apply -f "${MANIFESTS}/test-deploy-baseline.yaml" 2>/dev/null || true
log "Waiting for baselines..."
kubectl --context "${CTX}" rollout status statefulset/cart-web -n "${NS}" --timeout=120s 2>/dev/null || true
kubectl --context "${CTX}" rollout status deployment/test-api -n "${NS}" --timeout=120s 2>/dev/null || true

# ═══════════════════════════════════════════════════════════════════════════════
# TESTS — StatefulSet (cart-web)
# ═══════════════════════════════════════════════════════════════════════════════

# --- Rollback: Image/crash scenarios ---
should_run 1  && run_test 1  "fault-01-bad-image-tag.yaml"    "cart-web" "resolved" "rollback"     "STS — Bad image tag → ImagePullBackOff"             30
should_run 2  && run_test 2  "fault-02-crashloop.yaml"        "cart-web" "resolved" "rollback"     "STS — CrashLoopBackOff (bad entrypoint)"            20
should_run 6  && run_test 6  "fault-06-bad-registry.yaml"     "cart-web" "resolved" "rollback"     "STS — Invalid registry → ImagePullBackOff"          30
should_run 8  && run_test 8  "fault-08-bad-command.yaml"      "cart-web" "resolved" "rollback"     "STS — Wrong container command → crash"              20
should_run 23 && run_test 23 "fault-23-no-pull-secret.yaml"   "cart-web" "resolved" "rollback"     "STS — Private registry, no imagePullSecret"         30

# --- Rollback: Probe scenarios ---
should_run 5  && run_test 5  "fault-05-bad-probe-port.yaml"   "cart-web" "resolved" "rollback"     "STS — Readiness probe wrong port"                   30
should_run 9  && run_test 9  "fault-09-bad-liveness.yaml"     "cart-web" "resolved" "rollback"     "STS — Liveness probe bad path (404)"                30
should_run 19 && run_test 19 "fault-19-slow-startup.yaml"     "cart-web" "resolved" "rollback"     "STS — Aggressive probes kill slow startup"          30
should_run 20 && run_test 20 "fault-20-wrong-port.yaml"       "cart-web" "resolved" "rollback"     "STS — Wrong containerPort (probe mismatch)"         30

# --- Patch: Resource constraints ---
should_run 3  && run_test 3  "fault-03-oom.yaml"              "cart-web" "resolved" "patch"        "STS — OOMKilled (4Mi memory limit)"                 20
should_run 4  && run_test 4  "fault-04-low-cpu.yaml"          "cart-web" "resolved" "patch"        "STS — Extreme CPU throttle (1m limit)"              20
should_run 7  && run_test 7  "fault-07-undersized-both.yaml"  "cart-web" "resolved" "patch"        "STS — Both CPU + memory undersized"                 20

# --- Scale ---
should_run 10 && run_test 10 "SCALE_DOWN"                     "cart-web" "resolved" "scale,restart,patch" "STS — Scaled down to 1 (need 3)"            10
should_run 11 && run_test 11 "fault-11-scheduling-pressure.yaml" "cart-web" "resolved" "scale,none,rollback" "STS — 20 replicas scheduling pressure"   30 600

# --- Multi-container ---
should_run 15 && run_test 15 "fault-15-sidecar-crash.yaml"    "cart-web" "resolved" "rollback"     "STS — Sidecar container crash"                      20
should_run 16 && run_test 16 "fault-16-multi-container-oom.yaml" "cart-web" "resolved" "patch"     "STS — Multi-container OOM"                          20

# --- Config/security edge cases ---
should_run 17 && run_test 17 "fault-17-config-error.yaml"     "cart-web" "resolved" "rollback,none" "STS — Missing ConfigMap reference"                 20
should_run 18 && run_test 18 "fault-18-missing-secret.yaml"   "cart-web" "resolved" "rollback,none" "STS — Missing Secret volume"                      20
should_run 22 && run_test 22 "fault-22-privileged-crash.yaml" "cart-web" "resolved" "rollback"     "STS — Privileged container crash"                   20
should_run 24 && run_test 24 "fault-24-readonly-fs.yaml"      "cart-web" "resolved" "rollback"     "STS — ReadOnly filesystem blocks nginx"             20

# ═══════════════════════════════════════════════════════════════════════════════
# TESTS — Deployment (test-api)
# ═══════════════════════════════════════════════════════════════════════════════

should_run 12 && run_test 12 "fault-12-deploy-crashloop.yaml" "test-api" "resolved" "rollback"     "Deploy — CrashLoopBackOff"                         20
should_run 13 && run_test 13 "fault-13-deploy-oom.yaml"       "test-api" "resolved" "patch"        "Deploy — OOMKilled (4Mi memory)"                   20
should_run 14 && run_test 14 "fault-14-deploy-bad-image.yaml" "test-api" "resolved" "rollback"     "Deploy — Bad image tag → ImagePullBackOff"         30
should_run 21 && run_test 21 "fault-21-deploy-severe-oom.yaml" "test-api" "resolved" "patch"       "Deploy — Severe OOM (2Mi memory)"                  20
should_run 25 && run_test 25 "fault-25-deploy-bad-probe.yaml" "test-api" "resolved" "rollback"     "Deploy — Bad readiness probe path"                 20

# ═══════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════════════════════════════════════════

echo "" | tee -a "${RESULTS_FILE}"
echo -e "${BOLD}╔══════════════════════════════════════════════════════════════╗${NC}" | tee -a "${RESULTS_FILE}"
echo -e "${BOLD}║                      TEST RESULTS                          ║${NC}" | tee -a "${RESULTS_FILE}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════════════╝${NC}" | tee -a "${RESULTS_FILE}"
echo "" | tee -a "${RESULTS_FILE}"
echo -e "  Total:   ${TOTAL}" | tee -a "${RESULTS_FILE}"
echo -e "  ${GREEN}Passed:  ${PASS}${NC}" | tee -a "${RESULTS_FILE}"
echo -e "  ${RED}Failed:  ${FAIL}${NC}" | tee -a "${RESULTS_FILE}"
echo "" | tee -a "${RESULTS_FILE}"

if (( TOTAL > 0 )); then
  PCT=$(( (PASS * 100) / TOTAL ))
  echo -e "  ${BOLD}Accuracy: ${PCT}% (${PASS}/${TOTAL})${NC}" | tee -a "${RESULTS_FILE}"
  echo "" | tee -a "${RESULTS_FILE}"
fi

log "Full results: ${RESULTS_FILE}"

# Reset baselines
log "Resetting all workloads to baselines..."
kubectl --context "${CTX}" apply -f "${MANIFESTS}/cart-web-baseline.yaml" 2>/dev/null || true
kubectl --context "${CTX}" apply -f "${MANIFESTS}/test-deploy-baseline.yaml" 2>/dev/null || true

if (( FAIL > 0 )); then exit 1; fi
exit 0
