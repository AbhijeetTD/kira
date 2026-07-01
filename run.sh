#!/usr/bin/env bash
# run.sh — start KIRA directly (no Docker)
# Requirements: Python 3.11+, Ollama running, kind cluster running
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# ── .env ──────────────────────────────────────────────────────────────────────
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "⚠  Created .env from .env.example — edit KUBE_CONTEXT if needed."
fi

# ── Check Ollama ──────────────────────────────────────────────────────────────
if ! curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; then
  echo "❌  Ollama is not running. Start it with:"
  echo "       ollama serve"
  echo "    Then pull a model:"
  echo "       ollama pull llama3.2"
  exit 1
fi
echo "✅  Ollama is running"

# Pull model if not present
OLLAMA_MODEL="${OLLAMA_MODEL:-llama3.2}"
if ! curl -sf http://localhost:11434/api/tags | grep -q "\"${OLLAMA_MODEL}\"" 2>/dev/null; then
  echo "==> Pulling model '${OLLAMA_MODEL}'..."
  ollama pull "${OLLAMA_MODEL}"
fi
echo "✅  Model '${OLLAMA_MODEL}' is ready"

# ── Check kind cluster ────────────────────────────────────────────────────────
if ! kubectl cluster-info > /dev/null 2>&1; then
  echo "❌  Cannot reach Kubernetes cluster. Is kind running?"
  echo "    Check: kubectl config get-contexts"
  echo "    Create a cluster: kind create cluster --name dev-cluster"
  exit 1
fi
echo "✅  Kubernetes cluster reachable"

# ── Python venv ───────────────────────────────────────────────────────────────
if [[ ! -d .venv ]]; then
  echo "==> Creating Python virtual environment..."
  python3 -m venv .venv
fi
source .venv/bin/activate

echo "==> Installing / updating dependencies..."
pip install -q --upgrade pip
pip install -q -r requirements.txt

# ── Free port 8000 ────────────────────────────────────────────────────────────
if lsof -ti :8000 &>/dev/null; then
  echo "==> Port 8000 in use — killing existing process..."
  lsof -ti :8000 | xargs kill -9 2>/dev/null || true
  sleep 1
fi

echo ""
echo "🔍 KIRA starting..."
echo "   Dashboard → http://localhost:8000"
echo "   API docs  → http://localhost:8000/api/docs"
echo "   Health    → http://localhost:8000/health"
echo ""

uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
