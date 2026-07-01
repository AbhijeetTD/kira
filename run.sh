#!/usr/bin/env bash
# run.sh — KIRA (Kubernetes Intelligent Response Agent) startup script
# Checks and installs all prerequisites with user confirmation before acting.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${CYAN}ℹ️  $*${NC}"; }
success() { echo -e "${GREEN}✅  $*${NC}"; }
warn()    { echo -e "${YELLOW}⚠️  $*${NC}"; }
error()   { echo -e "${RED}❌  $*${NC}"; }
banner()  { echo -e "\n${BOLD}$*${NC}"; }

# ── Helper: ask user yes/no ───────────────────────────────────────────────────
confirm() {
  local prompt="$1"
  echo -e "${YELLOW}${prompt} [y/N]: ${NC}\c"
  read -r answer
  [[ "$answer" =~ ^[Yy]$ ]]
}

# ── Banner ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${CYAN}║   KIRA — Kubernetes Intelligent Response Agent  ║${NC}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════╝${NC}"
echo ""

# ═════════════════════════════════════════════════════════════════════════════
# STEP 1 — Check Homebrew (macOS package manager, needed to install tools)
# ═════════════════════════════════════════════════════════════════════════════
banner "Step 1/6 — Homebrew"
if ! command -v brew &>/dev/null; then
  warn "Homebrew is not installed."
  if confirm "Install Homebrew? (required to install Ollama and other tools)"; then
    info "Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Add brew to PATH for Apple Silicon
    eval "$(/opt/homebrew/bin/brew shellenv)" 2>/dev/null || true
    success "Homebrew installed."
  else
    error "Homebrew is required. Please install it manually from https://brew.sh and re-run."
    exit 1
  fi
else
  success "Homebrew is installed."
fi

# ═════════════════════════════════════════════════════════════════════════════
# STEP 2 — Check Ollama
# ═════════════════════════════════════════════════════════════════════════════
banner "Step 2/6 — Ollama (local LLM runtime)"
if ! command -v ollama &>/dev/null; then
  warn "Ollama is not installed."
  if confirm "Install Ollama via Homebrew?"; then
    info "Installing Ollama..."
    brew install ollama
    success "Ollama installed."
  else
    error "Ollama is required to run KIRA. Install it from https://ollama.com and re-run."
    exit 1
  fi
else
  success "Ollama is installed."
fi

# Start Ollama in the background if not already running
if ! curl -sf http://localhost:11434/api/tags &>/dev/null; then
  info "Starting Ollama in the background..."
  ollama serve &>/tmp/ollama.log &
  OLLAMA_PID=$!
  # Wait up to 10s for it to become ready
  for i in $(seq 1 10); do
    sleep 1
    if curl -sf http://localhost:11434/api/tags &>/dev/null; then
      break
    fi
    if [[ $i -eq 10 ]]; then
      error "Ollama failed to start. Check /tmp/ollama.log for details."
      exit 1
    fi
  done
  success "Ollama started (PID: $OLLAMA_PID)."
else
  success "Ollama is running."
fi

# Pull the configured model if not present
OLLAMA_MODEL="${OLLAMA_MODEL:-llama3.2}"
# Load from .env if it exists
if [[ -f .env ]]; then
  ENV_MODEL=$(grep -E '^OLLAMA_MODEL=' .env | cut -d'=' -f2 | tr -d '[:space:]' || true)
  [[ -n "$ENV_MODEL" ]] && OLLAMA_MODEL="$ENV_MODEL"
fi

if curl -sf http://localhost:11434/api/tags | grep -q "\"${OLLAMA_MODEL}\"" 2>/dev/null; then
  success "Model '${OLLAMA_MODEL}' is ready."
else
  warn "Model '${OLLAMA_MODEL}' is not downloaded yet (~2–5 GB depending on model)."
  if confirm "Download model '${OLLAMA_MODEL}' now?"; then
    info "Pulling model '${OLLAMA_MODEL}'... (this may take a few minutes)"
    ollama pull "${OLLAMA_MODEL}"
    success "Model '${OLLAMA_MODEL}' downloaded."
  else
    error "Model '${OLLAMA_MODEL}' is required. Pull it with:  ollama pull ${OLLAMA_MODEL}"
    exit 1
  fi
fi

# ═════════════════════════════════════════════════════════════════════════════
# STEP 3 — Check kubectl
# ═════════════════════════════════════════════════════════════════════════════
banner "Step 3/6 — kubectl"
if ! command -v kubectl &>/dev/null; then
  warn "kubectl is not installed."
  if confirm "Install kubectl via Homebrew?"; then
    brew install kubectl
    success "kubectl installed."
  else
    error "kubectl is required. Install it from https://kubernetes.io/docs/tasks/tools/ and re-run."
    exit 1
  fi
else
  success "kubectl is installed."
fi

# Check the Kubernetes cluster is reachable
if ! kubectl cluster-info &>/dev/null; then
  error "Cannot reach a Kubernetes cluster."
  echo ""
  echo "  Current contexts:"
  kubectl config get-contexts 2>/dev/null || echo "  (none)"
  echo ""
  echo "  To create a local kind cluster:"
  echo "    brew install kind"
  echo "    kind create cluster --name dev-cluster"
  echo ""
  echo "  Then set KUBE_CONTEXT in your .env to match the context name."
  exit 1
else
  CURRENT_CTX=$(kubectl config current-context 2>/dev/null || echo "unknown")
  success "Kubernetes cluster reachable (context: ${CURRENT_CTX})."
fi

# ═════════════════════════════════════════════════════════════════════════════
# STEP 4 — .env configuration
# ═════════════════════════════════════════════════════════════════════════════
banner "Step 4/6 — Environment configuration"
if [[ ! -f .env ]]; then
  cp .env.example .env
  warn ".env created from .env.example."
  echo ""
  echo "  Please review and set these values in .env:"
  echo -e "  ${YELLOW}KUBE_CONTEXT${NC}      — run: kubectl config get-contexts"
  echo -e "  ${YELLOW}DEFAULT_NAMESPACE${NC} — namespace KIRA will monitor"
  echo -e "  ${YELLOW}OLLAMA_MODEL${NC}      — model you pulled (default: llama3.2)"
  echo ""
  if confirm "Open .env in your default editor now?"; then
    "${EDITOR:-nano}" .env
  fi
else
  success ".env found."
fi

# ═════════════════════════════════════════════════════════════════════════════
# STEP 5 — Python virtual environment + dependencies
# ═════════════════════════════════════════════════════════════════════════════
banner "Step 5/6 — Python dependencies"

# Check Python 3.11+
if ! command -v python3 &>/dev/null; then
  warn "Python 3 is not installed."
  if confirm "Install Python 3.11 via Homebrew?"; then
    brew install python@3.11
    success "Python 3.11 installed."
  else
    error "Python 3.11+ is required. Install from https://python.org and re-run."
    exit 1
  fi
fi

PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
info "Python version: ${PY_VERSION}"

if [[ ! -d .venv ]]; then
  info "Creating Python virtual environment..."
  python3 -m venv .venv
fi

# shellcheck source=/dev/null
source .venv/bin/activate

info "Installing / updating Python dependencies..."
pip install -q --upgrade pip
pip install -q -r requirements.txt
success "Dependencies ready."

# ═════════════════════════════════════════════════════════════════════════════
# STEP 6 — Start KIRA
# ═════════════════════════════════════════════════════════════════════════════
banner "Step 6/6 — Starting KIRA"

# Free port 8000 if busy
if lsof -ti :8000 &>/dev/null; then
  warn "Port 8000 is in use. Killing existing process..."
  lsof -ti :8000 | xargs kill -9 2>/dev/null || true
  sleep 1
fi

echo ""
echo -e "${BOLD}${GREEN}🚀 KIRA is starting!${NC}"
echo ""
echo -e "  ${BOLD}Dashboard  →${NC} http://localhost:8000"
echo -e "  ${BOLD}API docs   →${NC} http://localhost:8000/api/docs"
echo -e "  ${BOLD}Health     →${NC} http://localhost:8000/health"
echo ""
echo -e "  ${CYAN}Trigger a scan:${NC}  curl -X POST http://localhost:8000/scan"
echo ""

uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
