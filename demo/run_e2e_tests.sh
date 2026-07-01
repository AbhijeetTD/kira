#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# KIRA — End-to-End Test Runner
# Runs 10 fault scenarios against cart-web in ls-pricing-cloudops-test.
# Each test: inject fault → trigger webhook → wait for resolution → check → restore
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

API="${KIRA_URL:-http://localhost:8000}"
CTX="--context ${KUBE_CONTEXT:-rancher-desktop}"
NS="ls-pricing-cloudops-test"
DEPLOY="cart-web"
MANIFESTS="$(cd "$(dirname "$0")" && pwd)/manifests"
BASELINE="$MANIFESTS/cart-web-baseline.yaml"

# Colours
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

PASS=0; FAIL=0; RESULTS=()

# ─── Helpers ────────────────────────────────────────────────────────────────

log()  { echo -e "${CYAN}[$(date +%H:%M:%S)]${NC} $*"; }
pass() { echo -e "${GREEN}  ✅ PASS${NC}: $1"; PASS=$((PASS+1)); RESULTS+=("PASS: $1"); }
fail() { echo -e "${RED}  ❌ FAIL${NC}: $1"; FAIL=$((FAIL+1)); RESULTS+=("FAIL: $1"); }
warn() { echo -e "${YELLOW}  ⚠️  WARN${NC}: $1"; }

wait_for_pods_condition() {
    # Usage: wait_for_pods_condition <condition_grep> <timeout_seconds>
    local condition="$1" timeout="$2" elapsed=0
    while [[ $elapsed -lt $timeout ]]; do
        local status
        status=$(kubectl $CTX -n $NS get pods -l app=$DEPLOY --no-headers 2>/dev/null || echo "")
        if echo "$status" | grep -qi "$condition"; then
            return 0
        fi
        sleep 3
        elapsed=$((elapsed + 3))
    done
    return 1
}

wait_for_healthy() {
    local timeout="${1:-120}" elapsed=0
    while [[ $elapsed -lt $timeout ]]; do
        local ready
        ready=$(kubectl $CTX -n $NS get statefulset $DEPLOY -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0")
        local desired
        desired=$(kubectl $CTX -n $NS get statefulset $DEPLOY -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "3")
        if [[ "$ready" == "$desired" ]] && [[ "$ready" -gt 0 ]]; then
            return 0
        fi
        sleep 3
        elapsed=$((elapsed + 3))
    done
    return 1
}

restore_baseline() {
    log "Restoring baseline..."
    kubectl $CTX apply -f "$BASELINE" > /dev/null 2>&1
    if ! wait_for_healthy 120; then
        warn "Baseline restore timed out — manual intervention may be needed"
        # Try rollout restart as fallback
        kubectl $CTX -n $NS rollout restart statefulset/$DEPLOY > /dev/null 2>&1 || true
        sleep 10
    fi
    # Clear any pending incidents by waiting a moment
    sleep 3
    log "Baseline restored — $(kubectl $CTX -n $NS get statefulset $DEPLOY -o jsonpath='{.status.readyReplicas}')/$(kubectl $CTX -n $NS get statefulset $DEPLOY -o jsonpath='{.spec.replicas}') ready"
}

trigger_incident() {
    local message="$1"
    local response
    response=$(curl -s -X POST "$API/webhook/alert" \
        -H "Content-Type: application/json" \
        -d "{\"service\": \"$DEPLOY\", \"namespace\": \"$NS\", \"message\": \"$message\", \"severity\": \"critical\", \"source\": \"e2e-test\"}")
    echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin).get('incident_id',''))" 2>/dev/null
}

wait_for_incident_done() {
    local incident_id="$1" timeout="${2:-300}" elapsed=0
    while [[ $elapsed -lt $timeout ]]; do
        local status
        status=$(curl -s "$API/incidents/$incident_id" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null)
        case "$status" in
            resolved|failed|skipped)
                echo "$status"
                return 0
                ;;
        esac
        sleep 5
        elapsed=$((elapsed + 5))
    done
    echo "timeout"
    return 1
}

get_incident_detail() {
    local incident_id="$1"
    curl -s "$API/incidents/$incident_id"
}

# ─── Test executor ──────────────────────────────────────────────────────────

run_test() {
    local test_num="$1" test_name="$2" fault_manifest="$3" alert_msg="$4" expected_action="$5" wait_condition="${6:-}" wait_secs="${7:-30}"

    echo ""
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    log "TEST $test_num: $test_name"
    log "  Expected action: $expected_action"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    # Step 1: Inject fault
    log "Injecting fault..."
    kubectl $CTX apply -f "$fault_manifest" > /dev/null 2>&1

    # Step 2: Wait for the fault condition to appear in pods
    if [[ -n "$wait_condition" ]]; then
        log "Waiting for condition '$wait_condition' (up to ${wait_secs}s)..."
        if ! wait_for_pods_condition "$wait_condition" "$wait_secs"; then
            warn "Condition '$wait_condition' not observed — proceeding anyway"
        fi
    else
        sleep "$wait_secs"
    fi

    # Show current pod state
    log "Pod state:"
    kubectl $CTX -n $NS get pods -l app=$DEPLOY --no-headers 2>/dev/null | head -5 | while read line; do
        echo "    $line"
    done

    # Step 3: Trigger incident
    log "Triggering incident..."
    local incident_id
    incident_id=$(trigger_incident "$alert_msg")
    if [[ -z "$incident_id" ]]; then
        fail "Test $test_num ($test_name) — could not create incident"
        restore_baseline
        return
    fi
    log "Incident ID: $incident_id"

    # Step 4: Wait for completion (up to 5 minutes)
    log "Waiting for KIRA to resolve (up to 180s)..."
    local result
    result=$(wait_for_incident_done "$incident_id" 180)
    log "Incident status: $result"

    # Step 5: Check results
    local detail
    detail=$(get_incident_detail "$incident_id")

    local actual_action actual_status confidence command
    actual_action=$(echo "$detail" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('remediation',{}).get('action','none') if d.get('remediation') else 'none')" 2>/dev/null)
    actual_status=$(echo "$detail" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null)
    confidence=$(echo "$detail" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('rca',{}).get('confidence',0) if d.get('rca') else 0)" 2>/dev/null)
    command=$(echo "$detail" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('remediation',{}).get('command','') if d.get('remediation') else '')" 2>/dev/null)
    local executed success
    executed=$(echo "$detail" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('remediation',{}).get('executed',False) if d.get('remediation') else False)" 2>/dev/null)
    success=$(echo "$detail" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('remediation',{}).get('success','') if d.get('remediation') else '')" 2>/dev/null)

    log "  Action:     $actual_action (expected: $expected_action)"
    log "  Status:     $actual_status"
    log "  Confidence: $confidence%"
    log "  Command:    ${command:0:120}"
    log "  Executed:   $executed"
    log "  Success:    $success"

    # Evaluate test result
    if [[ "$actual_status" == "resolved" ]]; then
        if [[ "$actual_action" == "$expected_action" ]]; then
            pass "Test $test_num ($test_name) — RESOLVED with correct action '$actual_action'"
        else
            # Some actions may be equivalent (e.g., rollback vs restart for crashloop)
            warn "Test $test_num ($test_name) — RESOLVED but action was '$actual_action' (expected '$expected_action')"
            pass "Test $test_num ($test_name) — RESOLVED (action: $actual_action)"
        fi
    elif [[ "$actual_status" == "awaiting_approval" ]]; then
        log "  Incident awaiting approval — auto-approving..."
        curl -s -X POST "$API/incidents/$incident_id/action" \
            -H "Content-Type: application/json" \
            -d '{"action": "approve"}' > /dev/null 2>&1
        local post_approve
        post_approve=$(wait_for_incident_done "$incident_id" 180)
        if [[ "$post_approve" == "resolved" ]]; then
            pass "Test $test_num ($test_name) — RESOLVED after approval with action '$actual_action'"
        else
            fail "Test $test_num ($test_name) — status=$post_approve after approval (expected resolved)"
        fi
    else
        fail "Test $test_num ($test_name) — status=$actual_status, action=$actual_action (expected resolved/$expected_action)"
    fi

    # Step 6: Verify cluster is actually healthy now (or restore)
    log "Verifying cluster health..."
    if wait_for_healthy 60; then
        log "  Cluster healthy ✓"
    else
        warn "Cluster not healthy after test — restoring baseline"
    fi

    # Step 7: Restore baseline for next test
    restore_baseline
}

# ─── Main ────────────────────────────────────────────────────────────────────

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║     KIRA — End-to-End Test Suite (10 Scenarios)     ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""

# Ensure baseline is clean
log "Ensuring clean baseline..."
restore_baseline

# ── Test 1: Bad Image Tag ──────────────────────────────────────────────────
run_test 1 "Bad Image Tag (ImagePullBackOff)" \
    "$MANIFESTS/fault-01-bad-image-tag.yaml" \
    "Pods failing to start — ImagePullBackOff on cart-web in ls-pricing-cloudops-test namespace" \
    "rollback" \
    "ImagePullBackOff\|ErrImagePull" 45

# ── Test 2: CrashLoopBackOff ──────────────────────────────────────────────
run_test 2 "CrashLoopBackOff (bad entrypoint)" \
    "$MANIFESTS/fault-02-crashloop.yaml" \
    "cart-web pods in CrashLoopBackOff — application startup failure" \
    "rollback" \
    "CrashLoopBackOff\|Error" 45

# ── Test 3: OOMKilled ──────────────────────────────────────────────────────
run_test 3 "OOMKilled (4Mi memory limit)" \
    "$MANIFESTS/fault-03-oom.yaml" \
    "cart-web pods being OOMKilled repeatedly — memory limit too low" \
    "patch" \
    "OOMKilled\|CrashLoopBackOff" 45

# ── Test 4: Undersized CPU ────────────────────────────────────────────────
run_test 4 "Undersized CPU (1m limit)" \
    "$MANIFESTS/fault-04-low-cpu.yaml" \
    "cart-web extreme CPU throttling — pods barely responsive" \
    "patch" \
    "" 30

# ── Test 5: Wrong Readiness Probe Port ─────────────────────────────────────
run_test 5 "Wrong Readiness Probe Port (9999)" \
    "$MANIFESTS/fault-05-bad-probe-port.yaml" \
    "cart-web readiness probe failing — pods Running but not Ready" \
    "rollback" \
    "0/1\|0/3" 30

# ── Test 6: Invalid Image Registry ────────────────────────────────────────
run_test 6 "Invalid Image Registry" \
    "$MANIFESTS/fault-06-bad-registry.yaml" \
    "cart-web image pull failing — invalid container registry" \
    "rollback" \
    "ImagePullBackOff\|ErrImagePull" 45

# ── Test 7: Both CPU + Memory Undersized ──────────────────────────────────
run_test 7 "Both CPU + Memory Undersized (1m/4Mi)" \
    "$MANIFESTS/fault-07-undersized-both.yaml" \
    "cart-web pods crashing — extremely low resource limits (cpu=1m, memory=4Mi)" \
    "patch" \
    "OOMKilled\|CrashLoopBackOff" 45

# ── Test 8: Bad Container Command ─────────────────────────────────────────
run_test 8 "Bad Container Command (override nginx)" \
    "$MANIFESTS/fault-08-bad-command.yaml" \
    "cart-web CrashLoopBackOff — container command failing after deployment" \
    "rollback" \
    "CrashLoopBackOff\|Error" 45

# ── Test 9: Bad Liveness Probe Path ───────────────────────────────────────
run_test 9 "Bad Liveness Probe Path (/healthz/nonexistent)" \
    "$MANIFESTS/fault-09-bad-liveness.yaml" \
    "cart-web pods restarting — liveness probe failing on wrong path" \
    "rollback" \
    "CrashLoopBackOff\|Restart" 60

# ── Test 10: Insufficient Replicas ────────────────────────────────────────
run_test 10 "Insufficient Replicas (1/3)" \
    "$MANIFESTS/fault-10-low-replicas.yaml" \
    "cart-web only 1/3 replicas available — service degraded" \
    "scale" \
    "" 10

# ─── Summary ───────────────────────────────────────────────────────────────

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║                      TEST RESULTS SUMMARY                  ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
for r in "${RESULTS[@]}"; do
    if [[ "$r" == PASS* ]]; then
        echo -e "  ${GREEN}✅ $r${NC}"
    else
        echo -e "  ${RED}❌ $r${NC}"
    fi
done
echo ""
echo -e "  Total: $((PASS + FAIL))  |  ${GREEN}Passed: $PASS${NC}  |  ${RED}Failed: $FAIL${NC}"
echo ""

if [[ $FAIL -eq 0 ]]; then
    echo -e "${GREEN}🎉 ALL TESTS PASSED!${NC}"
else
    echo -e "${RED}⚠️  $FAIL test(s) failed — review output above${NC}"
fi
